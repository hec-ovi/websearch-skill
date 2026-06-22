# Install and harness setup

Every route below is keyless and needs internet. The only hard requirement is
[uv](https://docs.astral.sh/uv/) (it provides `uvx` and downloads a compatible Python on
first run). The tool is the same Python package everywhere; harnesses differ only in where
the skill file goes and how the MCP server is registered.

Two facts that recur:

- **Distribution name** is `websearch-skill`; the **command** is `websearch`. The package
  also installs a second console script named `websearch-skill`, so `uvx websearch-skill
  <cmd>` resolves with no `--from`.
- **Before the PyPI release**, swap any `uvx websearch-skill ...` for the git form
  `uvx --from git+https://github.com/hec-ovi/websearch-skill websearch ...`. It clones and
  builds on each run (slower), but needs no PyPI.

The MCP server's logical name is `web-search` and it exposes five tools: `web_search`,
`web_fetch`, `web_open`, `arxiv_search`, `github_search`. fastmcp ships in the base install,
so `uvx websearch-skill mcp` starts the stdio server with no extra.

## Route summary

| You want | Do this |
|---|---|
| Run it once, no install | `uvx websearch-skill web-search "..."` |
| A skill in your agent | `npx skills add hec-ovi/websearch-skill` |
| A Claude Code plugin (skill + MCP in one) | `/plugin marketplace add hec-ovi/websearch-skill` then `/plugin install web-search@websearch-skill` |
| An MCP server in any client | register `uvx websearch-skill mcp` (snippets below) |
| Develop on it | `git clone ...` then `uv sync` |

## CLI, no install (uvx)

```bash
# once on PyPI:
uvx websearch-skill web-search "open source vector database 2026"
uvx websearch-skill web-fetch "https://example.com"
uvx websearch-skill arxiv "diffusion models" --max-results 5
uvx websearch-skill github "fastmcp" --language Python --sort stars

# straight from git, today (no PyPI):
uvx --from git+https://github.com/hec-ovi/websearch-skill websearch web-search "..."
```

`uvx` caches the build, so the second run is fast. Pin a ref with `@<tag>` or `@<sha>` on the
git URL for reproducibility.

## As an agent skill (npx skills add)

The [`skills`](https://www.npmjs.com/package/skills) CLI installs the `skills/web-search/`
directory into every agent it detects (Claude Code, Codex, OpenCode, Cursor, Gemini, and
others), so the same SKILL.md works across all of them.

```bash
npx skills add hec-ovi/websearch-skill                 # all detected agents, project scope
npx skills add hec-ovi/websearch-skill -g              # global (your user dir)
npx skills add hec-ovi/websearch-skill -a claude-code -a codex -s web-search
npx skills add hec-ovi/websearch-skill --list          # show what the repo offers, install nothing
npx skills add hec-ovi/websearch-skill --copy -y       # copy instead of symlink (e.g. Windows)
```

By default it symlinks a single canonical copy into each agent's skills folder
(`.claude/skills/web-search/`, `.codex/skills/web-search/`,
`.config/opencode/skills/web-search/`, and so on). The skill tells the agent to run the
`websearch` CLI; if `websearch` is not on PATH it uses the `uvx` forms above.

## Claude Code

### Plugin (skill plus MCP server, one install)

```text
/plugin marketplace add hec-ovi/websearch-skill
/plugin install web-search@websearch-skill
/reload-plugins
```

The plugin's `.claude-plugin/marketplace.json` points at this repo (`source: "./"`), so the
root `skills/web-search/SKILL.md` and the root `.mcp.json` are auto-discovered: you get the
`/web-search:web-search` skill and the `web-search` MCP server in one step.

### Manual (skill drop plus MCP config)

Drop the skill and add the server yourself. Skill: `npx skills add` above, or copy
`skills/web-search/` into `~/.claude/skills/`. MCP: add to `.mcp.json` (project) or your user
config:

```json
{
  "mcpServers": {
    "web-search": {
      "command": "uvx",
      "args": ["websearch-skill", "mcp"]
    }
  }
}
```

Git fallback (pre-PyPI): set `"args": ["--from", "git+https://github.com/hec-ovi/websearch-skill", "websearch", "mcp"]`.

## Codex CLI

Skill: `npx skills add hec-ovi/websearch-skill -a codex` (lands in `.codex/skills/` or
`~/.agents/skills/`). MCP server, either the CLI:

```bash
codex mcp add web-search -- uvx websearch-skill mcp
```

or `~/.codex/config.toml` directly (the TOML table key must be a bare identifier, so
`web_search` with an underscore; the server name shown to you is still `web-search`):

```toml
[mcp_servers.web_search]
command = "uvx"
args = ["websearch-skill", "mcp"]
startup_timeout_sec = 60   # first uvx run installs the package; the default 10s can be tight
```

### Codex sandboxes network by default (required step)

Codex's default `sandbox_mode` is `workspace-write`, which **asks before any internet
access**, so this search server is blocked or prompted until you grant network. On Linux,
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
opencode`. MCP goes in `opencode.json` (note: `command` is an array, the env key is
`environment`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "web-search": {
      "type": "local",
      "command": ["uvx", "websearch-skill", "mcp"],
      "environment": {},
      "enabled": true
    }
  }
}
```

## Cursor and Claude Desktop

Both read the standard `mcpServers` block. Use the same JSON as the Claude Code manual
config above, in the client's MCP settings.

## Hermes and OpenClaw

Both are MCP clients on the Agent Skills standard.

- **Hermes:** add the server under `mcp_servers` in `~/.hermes/config.yaml` (stdio, command
  `uvx`, args `["websearch-skill", "mcp"]`); install the skill with `hermes skills install`.
  Drop Hermes's native web/search toolset, or the model keeps calling the built-in one.
- **OpenClaw:** `openclaw mcp add web-search --command uvx --arg websearch-skill --arg mcp`,
  and `openclaw skills install git:hec-ovi/websearch-skill@main`. Pin a ref rather than
  pulling latest, and keep any future keys in env.

## Publishing to PyPI (maintainer, one time)

Releases use PyPI Trusted Publishing (OIDC). No API token is created, pasted, or stored. The
workflow is `.github/workflows/release.yml`.

1. On PyPI, open `https://pypi.org/manage/account/publishing/` and add a **pending
   publisher** (the project does not exist yet): Provider GitHub, PyPI Project Name
   `websearch-skill`, Owner `hec-ovi`, Repository `websearch-skill`, Workflow `release.yml`,
   Environment `pypi`.
2. On GitHub, create an environment named `pypi` (Settings, Environments). Optionally add
   required reviewers to gate releases.
3. Set the version in `pyproject.toml`, commit, then tag and push:
   `git tag v0.1.0 && git push origin v0.1.0`. The tag must start with `v`.
4. The workflow builds with `uv build` and publishes via OIDC. The first publish claims the
   `websearch-skill` name. Verify at `https://pypi.org/project/websearch-skill/`, then
   `uvx websearch-skill --help`.

Subsequent releases: bump the version, push a new `v*` tag. No PyPI reconfiguration needed.

## Listing in the MCP Registry (maintainer, optional)

`server.json` is ready for the official MCP Registry. It requires the package to be on PyPI
first, because the registry proves PyPI ownership through a marker in the published README.

1. The README carries `mcp-name: io.github.hec-ovi/web-search` (in an HTML comment near the
   top). That string must match `server.json`'s `name` and ship in the PyPI long
   description, which it does (`readme = "README.md"`).
2. Install the publisher CLI (`mcp-publisher`, from the modelcontextprotocol/registry
   releases).
3. `mcp-publisher login github` (device/OAuth flow; grants the `io.github.hec-ovi/*`
   namespace), then `mcp-publisher publish`.
4. Confirm with
   `curl 'https://registry.modelcontextprotocol.io/v0/servers?search=io.github.hec-ovi/web-search'`.
