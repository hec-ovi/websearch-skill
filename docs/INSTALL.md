# Install and harness setup

Every route below is keyless and needs internet. The only hard requirement is
[uv](https://docs.astral.sh/uv/) (it provides `uvx` and downloads a compatible Python on
first run). The tool is the same Python package everywhere; harnesses differ only in where
the skill file goes.

Two facts that recur:

- **Distribution name** is `websearch-skill`; the **command** is `websearch`. The package
  also installs a second console script named `websearch-skill`, so `uvx websearch-skill
  <cmd>` resolves with no `--from`.
- The package is on [PyPI](https://pypi.org/project/websearch-skill/), so `uvx
  websearch-skill ...` works as-is. To run a specific commit instead, use the git form
  `uvx --from git+https://github.com/hec-ovi/websearch-skill@<ref> websearch ...`.

There is no MCP server: the agent drives the `websearch` CLI through its own shell, and the
skill file tells it how. The reason is in the README under "No MCP".
Whatever your harness, the first call in a session should be `websearch init`.

## Route summary

| You want | Do this |
|---|---|
| Run it once, no install | `uvx websearch-skill web-search "..."` |
| A skill in your agent | `npx skills add hec-ovi/websearch-skill` |
| A Claude Code plugin | `/plugin marketplace add hec-ovi/websearch-skill` then `/plugin install web-search@websearch-skill` |
| Develop on it | `git clone ...` then `uv sync` |

## CLI, no install (uvx)

```bash
uvx websearch-skill init                     # bring it online, report what works
uvx websearch-skill web-search "open source vector database 2026"
uvx websearch-skill web-fetch "https://example.com"
uvx websearch-skill arxiv "diffusion models" --max-results 5
uvx websearch-skill github "agent framework" --language Python --sort stars

# or straight from git, for an unreleased commit:
uvx --from git+https://github.com/hec-ovi/websearch-skill websearch web-search "..."
```

`uvx` caches the build, so the second run is fast. Pin a ref with `@<tag>` or `@<sha>` on the
git URL for reproducibility.

## As an agent skill (npx skills add)

The [`skills`](https://www.npmjs.com/package/skills) CLI installs the `skills/` directories
into every agent it detects (Claude Code, Codex, OpenCode, Cursor, Gemini, and others), so
the same SKILL.md works across all of them. There are two: `web-search` (the CLI) and
`web-search-tor` (the same CLI with the Tor layer on, for `.onion` and onion search).

```bash
npx skills add hec-ovi/websearch-skill                 # all detected agents, project scope
npx skills add hec-ovi/websearch-skill -g              # global (your user dir)
npx skills add hec-ovi/websearch-skill -a claude-code -a codex -s web-search
npx skills add hec-ovi/websearch-skill -s web-search-tor   # just the Tor one
npx skills add hec-ovi/websearch-skill --list          # show what the repo offers, install nothing
npx skills add hec-ovi/websearch-skill --copy -y       # copy instead of symlink (e.g. Windows)
```

By default it symlinks a single canonical copy into each agent's skills folder
(`.claude/skills/web-search/`, `.codex/skills/web-search/`,
`.config/opencode/skills/web-search/`, and so on). The skill tells the agent to run the
`websearch` CLI; if `websearch` is not on PATH it uses the `uvx` forms above.

## Claude Code

### Plugin

```text
/plugin marketplace add hec-ovi/websearch-skill
/plugin install web-search@websearch-skill
/reload-plugins
```

The plugin's `.claude-plugin/marketplace.json` points at this repo (`source: "./"`), so the
root `skills/web-search/SKILL.md` is auto-discovered and you get the
`/web-search:web-search` skill.

### Manual

`npx skills add` as above, or copy `skills/web-search/` into `~/.claude/skills/`. The skill
runs the CLI, so nothing else is registered; make sure `websearch` is on PATH or that `uvx`
is available.

## Codex CLI

Skill: `npx skills add hec-ovi/websearch-skill -a codex` (lands in `.codex/skills/` or
`~/.agents/skills/`). Nothing else to configure, but see the sandbox note below.

### Codex sandboxes network by default (required step)

Codex's default `sandbox_mode` is `workspace-write`, which **asks before any internet
access**, so every search is blocked or prompted until you grant network. On Linux,
add to `~/.codex/config.toml`:

```toml
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true
```

Per-run alternative: `codex --config sandbox_workspace_write.network_access=true`. On macOS
(Seatbelt) the config flag is currently ignored (openai/codex issue #10390), so use the
`--config` form or `--sandbox danger-full-access` there.

## OpenCode

OpenCode already reads `.claude/skills` and `.agents/skills`, so a Claude or Codex skill
install is picked up automatically; otherwise `npx skills add hec-ovi/websearch-skill -a
opencode`.

## Cursor and Claude Desktop

Both run shell commands for the agent, so the skill route is the install: `npx skills add
hec-ovi/websearch-skill`, and make sure `websearch` (or `uvx`) is on the PATH the agent
inherits.

## Hermes and OpenClaw

Both are on the Agent Skills standard.

- **Hermes:** `hermes skills install`. Drop Hermes's native web/search toolset, or the
  model keeps calling the built-in one.
- **OpenClaw:** `openclaw skills install git:hec-ovi/websearch-skill@main`. Pin a ref
  rather than pulling latest, and keep any future keys in env.

## Publishing to PyPI (maintainer)

Releases use PyPI Trusted Publishing (OIDC); no API token is created, pasted, or stored. The
workflow is `.github/workflows/release.yml`, bound to the GitHub environment `pypi` and the
trusted publisher configured on the PyPI project.

To release: set the version in `pyproject.toml` and the two `.claude-plugin` manifests
(the test suite enforces the lockstep), commit, then tag and push a `v*` tag (`git tag
v0.3.0 && git push origin v0.3.0`). The workflow builds with `uv build` and publishes via
OIDC. Verify at `https://pypi.org/project/websearch-skill/`.
