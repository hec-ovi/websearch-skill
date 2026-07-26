"""Distribution-layer guards.

These validate the packaging and harness manifests so the install routes documented in
``docs/INSTALL.md`` cannot silently rot: every manifest parses, the versions stay in
lockstep, the launch strings reference real CLI subcommands, no MCP surface creeps back
in, the release workflow uses tokenless Trusted Publishing, and no doc carries a Unicode
em/en dash.
"""

from __future__ import annotations

import json
import pathlib
import tomllib

import pytest

from websearch import cli

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _json(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _version() -> str:
    return _pyproject()["project"]["version"]


# --- packaging -------------------------------------------------------------------------


def test_no_mcp_dependency_or_extra_remains():
    # 0.3.0 dropped the MCP face: the CLI is the only surface. fastmcp must not come back
    # as a dependency or an extra, and nothing may declare an [mcp] install route.
    project = _pyproject()["project"]
    deps = project["dependencies"] + [
        d for group in project.get("optional-dependencies", {}).values() for d in group
    ]
    assert not [d for d in deps if "mcp" in d.replace("_", "-").lower()], deps
    assert "mcp" not in project.get("optional-dependencies", {})


def test_both_console_scripts_point_at_the_cli():
    scripts = _pyproject()["project"]["scripts"]
    # `websearch` is canonical; `websearch-skill` matches the dist name so `uvx
    # websearch-skill <cmd>` resolves with no --from.
    assert scripts.get("websearch") == "websearch.cli:main"
    assert scripts.get("websearch-skill") == "websearch.cli:main"


def test_sdist_uses_an_allowlist_that_excludes_local_state():
    # The source distribution must ship only intended sources; a globally-gitignored
    # .claude/settings.local.json once leaked in. Guard the explicit allowlist.
    include = _pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert "src/websearch" in include
    assert not any(p.strip("/").startswith(".claude") for p in include)


# --- launch strings reference real subcommands -----------------------------------------


@pytest.mark.parametrize(
    "subcommand", ["init", "doctor", "searxng", "web-search", "web-fetch", "web-open"]
)
def test_manifest_launch_subcommands_exist(subcommand):
    # argparse prints help and exits 0 for a real subcommand, before any dispatch/server.
    with pytest.raises(SystemExit) as exc:
        cli.main([subcommand, "--help"])
    assert exc.value.code == 0


def test_no_mcp_surface_ships():
    # The stdio server, the client config that launched it, and the registry manifest all
    # went with 0.3.0. A file coming back means half a face returned with it.
    for gone in (".mcp.json", "server.json", "src/websearch/layer3_agentio/mcp_server.py"):
        assert not (ROOT / gone).exists(), gone
    with pytest.raises(SystemExit) as exc:
        cli.main(["mcp", "--help"])
    assert exc.value.code != 0  # argparse: invalid choice


# --- Claude plugin + marketplace -------------------------------------------------------


def test_claude_plugin_manifest():
    plugin = _json(".claude-plugin/plugin.json")
    assert plugin["name"] == "web-search"  # required; stable skill/command name
    assert plugin["license"] == "MIT"
    assert plugin["version"] == _version()


def test_claude_marketplace_manifest():
    mkt = _json(".claude-plugin/marketplace.json")
    assert mkt["name"] == "websearch-skill"
    assert mkt["owner"]["name"]
    (entry,) = mkt["plugins"]
    assert entry["name"] == "web-search"
    # source "./" means the repo root is the plugin; that dir must exist and hold plugin.json.
    assert entry["source"] == "./"
    assert (ROOT / ".claude-plugin" / "plugin.json").is_file()


# --- version lockstep ------------------------------------------------------------------


def test_all_manifest_versions_match_pyproject():
    import websearch

    v = _version()
    plugin = _json(".claude-plugin/plugin.json")
    mkt = _json(".claude-plugin/marketplace.json")
    assert plugin["version"] == v
    assert mkt["metadata"]["version"] == v
    assert mkt["plugins"][0]["version"] == v
    # __version__ is derived from the installed distribution metadata; if it reports a
    # different version than pyproject, the environment is stale (re-sync) or the
    # derivation broke. It must never be a hardcoded string again.
    assert websearch.__version__ == v


# --- release workflow uses tokenless Trusted Publishing --------------------------------


def test_release_workflow_is_tokenless_trusted_publishing():
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "id-token: write" in wf  # OIDC
    assert "pypa/gh-action-pypi-publish" in wf
    assert "name: pypi" in wf  # the environment registered on PyPI
    assert "tags:" in wf and "v*" in wf  # publishes on a v* tag
    # never a stored token
    assert "password:" not in wf
    assert "PYPI_API_TOKEN" not in wf


# --- skill discoverability -------------------------------------------------------------


def test_skill_is_in_the_flat_layout_with_a_stable_name():
    skill = ROOT / "skills" / "web-search" / "SKILL.md"
    assert skill.is_file()  # npx skills add walks skills/<name>/SKILL.md
    head = skill.read_text(encoding="utf-8")[:600]
    assert "name: web-search" in head  # stable name; the plugin relies on it


# --- no Unicode dashes in any doc or manifest ------------------------------------------


def test_no_em_or_en_dashes_in_docs_and_manifests():
    targets: list[pathlib.Path] = []
    for pattern in ("*.md", "docs/*.md", "contracts/*.md", "skills/**/SKILL.md", "docker/**/*.md"):
        targets.extend(ROOT.glob(pattern))
    targets += [
        ROOT / ".claude-plugin" / "plugin.json",
        ROOT / ".claude-plugin" / "marketplace.json",
        ROOT / ".github" / "workflows" / "release.yml",
    ]
    offenders = []
    for path in sorted(set(targets)):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "—" in line or "–" in line:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, f"em/en dashes found: {offenders}"
