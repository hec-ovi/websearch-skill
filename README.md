# websearch-skill

An open-source web search SKILL + TOOL for AI agents, aimed at June-2026 state of the art. The goal is to beat Firecrawl, raw Google, and existing agent web-search tools on the axes a self-hosted open-source tool can actually own, while staying installable into any skill-compatible environment (Claude Code, Codex CLI, OpenCode, Hermes, OpenClaw, or a bare agent).

Status: research complete, no code yet.

## Where the design lives

The full research and the architecture plan are in `.research/` (gitignored by default; un-ignore it if you want the plan committed). Start with:

- `.research/architecture-plan/FINDINGS.md` (the design of record)
- `.research/INDEX.md` (dispatch table to 10 per-layer findings, each carrying its in/out contract)

## The shape

Isolated, contract-driven layers. Each is its own folder with a README and a versioned JSON-Schema contract, swappable without touching the others.

- Layer 1 SEARCH: an engine router (SearXNG + ddgs + optional keyed adapters) with rank fusion, plus an optional, isolated proxy/VPN egress sub-module.
- Layer 2 READ: tiered fetch + Trafilatura extraction, then a hybrid paginated-Markdown plus JSON-sidecar format with progressive disclosure; an in-memory store by default (SQLite FTS5 only for the page index).
- Layer 3 AGENT I/O: a CLI-first core that is also an optional MCP server, shipped as one SKILL.md plus a bundled Python tool (PEP 723 + uv).

## Honest scope

A Pareto win, not a clean sweep. It matches the cloud leaders on the unprotected majority of the web and on extraction cleanliness, wins on cost and privacy, and treats hard anti-bot (paid residential proxies, captcha solving) as a swappable adapter rather than a free feature. The architecture plan includes a 7-axis scorecard so "outperform" is measurable rather than rhetorical.
