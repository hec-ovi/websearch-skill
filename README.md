# websearch-skill

An open-source web search and extraction tool for AI agents, built as isolated,
contract-driven layers. It aims to win where a self-hosted open-source tool actually
can (cost, privacy, multi-engine recall, clean extraction, freshness) and to be honest
about the rest (hard anti-bot is a swappable paid adapter, not a free feature).

Status: Layer 1 (multi-engine search) is implemented and tested. Layer 2 (fetch and
extraction, markdown formatting) and the Layer 3 MCP adapter are designed and on the
way. See `docs/ARCHITECTURE.md` for the full design.

## Install

The project is uv-native. With [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/hec-ovi/websearch-skill
cd websearch-skill
uv sync
```

## Quick start

ddgs is the zero-config default, so search works with no setup:

```bash
uv run websearch search "rust web frameworks 2026"
```

Add SearXNG (any instance) for a second, keyless engine and let the router fuse and
dedup across both:

```bash
export WEBSEARCH_SEARXNG_URL=http://localhost:8080
uv run websearch search "rust web frameworks 2026" --json
```

Useful flags: `--engines searxng,ddgs`, `--count`, `--freshness day|week|month|year`,
`--language en`, `--country us`, `--include-site docs.rs`, `--exclude-site pinterest.com`,
`--no-ddgs`. `--json` prints the raw Envelope (the contract surface); the default is a
compact human view. Exit code is 1 on an error Envelope.

As a library:

```python
from websearch.layer1_search import build_router, SearchRequest

router = build_router(searxng_url="http://localhost:8080", enable_ddgs=True)
envelope = router.search(SearchRequest(query="rust web frameworks 2026", count=10))
for r in envelope.data["results"]:
    print(r["fused_score"], r["url"], [s["engine"] for s in r["sources"]])
```

## How it works

Each layer is a folder with a port (a capability-named interface) and adapters behind
it, connected only by versioned JSON-Schema contracts. The default runs in-process; a
layer can later move to a subprocess or a local service without its neighbors noticing,
because the contract is the isolation, not the process boundary.

Layer 1 fans a query out to isolated per-engine adapters (SearXNG, ddgs, and optional
keyed engines), canonicalizes and dedups the results, then fuses them with
provenance-aware weighted Reciprocal Rank Fusion. The fusion is de-correlated on
purpose: SearXNG and ddgs both lean on Google and Bing, so they count as one
independent vote rather than several, and the consensus bonus only rewards agreement
across genuinely independent indexes. Without that, a naive union of many engines can
rank worse than a single well-tuned one.

Every result carries full per-engine provenance (which engine returned it, at what
rank), and every response is wrapped in one `Envelope`
(`contract_version`, `ok`, `data`, `error`, `meta`).

## Honest scope

A Pareto win, not a clean sweep. Among 2026 agentic-search APIs the top tier is roughly
tied on result quality, so cost and latency are the real differentiators. Hard anti-bot
on protected sites is bought with paid residential proxies and captcha solving, which
have no reliable free or local equivalent, so this tool treats egress as a swappable
adapter (free direct egress by default, a paid pool when you need the protected long
tail). The architecture defines a 7-axis scorecard (in `docs/ARCHITECTURE.md`) so
"outperform" is measurable rather than rhetorical.

## Contributing

One isolated layer at a time, against its versioned contract. Adding or swapping an
engine touches only its adapter module plus the capability map. Tests run in CI (ruff
plus pytest on Python 3.11 to 3.13); the contract tests validate real output against the
frozen JSON Schemas, so a change that breaks the shape fails CI.

## License

MIT. See `LICENSE`. Optional anti-bot tiers that depend on AGPL components (for example
nodriver) are kept as out-of-band adapters you install separately, not bundled into the
MIT core.
