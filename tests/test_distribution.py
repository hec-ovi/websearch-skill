"""Distribution-layer guards.

These validate the packaging and harness manifests so the install routes documented in
``docs/INSTALL.md`` cannot silently rot: every manifest parses, the versions stay in
lockstep, the launch strings reference real CLI subcommands, the MCP-registry PyPI-ownership
marker matches ``server.json``, the release workflow uses tokenless Trusted Publishing, and
no doc carries a Unicode em/en dash.
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


def test_fastmcp_is_a_base_dependency():
    # The MCP-registry runner and bare `uvx websearch-skill mcp` cannot pass an extra, so
    # fastmcp must be a base dependency, not only the back-compat [mcp] extra.
    deps = _pyproject()["project"]["dependencies"]
    assert any(d.replace("_", "-").lower().startswith("fastmcp") for d in deps), deps


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


@pytest.mark.parametrize("subcommand", ["mcp", "web-search", "web-fetch", "web-open"])
def test_manifest_launch_subcommands_exist(subcommand):
    # argparse prints help and exits 0 for a real subcommand, before any dispatch/server.
    with pytest.raises(SystemExit) as exc:
        cli.main([subcommand, "--help"])
    assert exc.value.code == 0


def test_root_mcp_json_launches_the_stdio_server():
    server = _json(".mcp.json")["mcpServers"]["web-search"]
    assert server["command"] == "uvx"
    assert server["args"] == ["websearch-skill", "mcp"]


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


# --- MCP registry server.json ----------------------------------------------------------


def test_server_json_shape_and_pypi_mapping():
    srv = _json("server.json")
    # reverse-DNS name, exactly one slash, GitHub namespace for the hec-ovi owner.
    assert srv["name"] == "io.github.hec-ovi/web-search"
    assert srv["name"].count("/") == 1
    assert srv["$schema"].startswith("https://static.modelcontextprotocol.io/schemas/")
    (pkg,) = srv["packages"]
    assert pkg["registryType"] == "pypi"
    assert pkg["identifier"] == _pyproject()["project"]["name"]  # websearch-skill
    assert pkg["runtimeHint"] == "uvx"
    assert pkg["transport"]["type"] == "stdio"
    # the runner appends the positional subcommand -> `uvx websearch-skill mcp`
    args = pkg.get("packageArguments", [])
    assert any(a.get("type") == "positional" and a.get("value") == "mcp" for a in args)


def test_server_json_text_fields_within_registry_limits():
    # The 2025-12-11 registry schema caps description and title at maxLength 100; a longer
    # value is rejected at publish time. Guard both.
    srv = _json("server.json")
    assert 1 <= len(srv["description"]) <= 100, len(srv["description"])
    assert 1 <= len(srv.get("title", "x")) <= 100


def test_server_json_keys_are_camelcase_not_snake_case():
    # the registry rejects snake_case; guard against a regression to registry_type etc.
    raw = (ROOT / "server.json").read_text(encoding="utf-8")
    for forbidden in ("registry_type", "runtime_hint", "package_arguments", "website_url"):
        assert forbidden not in raw


# --- version lockstep ------------------------------------------------------------------


def test_all_manifest_versions_match_pyproject():
    v = _version()
    plugin = _json(".claude-plugin/plugin.json")
    mkt = _json(".claude-plugin/marketplace.json")
    srv = _json("server.json")
    assert plugin["version"] == v
    assert mkt["metadata"]["version"] == v
    assert mkt["plugins"][0]["version"] == v
    assert srv["version"] == v
    assert srv["packages"][0]["version"] == v


# --- MCP registry PyPI-ownership marker ------------------------------------------------


def test_readme_carries_the_mcp_name_marker_matching_server_json():
    # The registry proves PyPI ownership by finding `mcp-name: <server name>` in the
    # published README (which is the PyPI long description). It must match server.json.
    name = _json("server.json")["name"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"mcp-name: {name}" in readme


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
        ROOT / ".mcp.json",
        ROOT / "server.json",
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
