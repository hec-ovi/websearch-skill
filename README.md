# websearch-skill

Open-source multi-engine web search and content extraction for AI agents, built as isolated layers connected only by versioned JSON Schema contracts.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](pyproject.toml)
[![tests](https://img.shields.io/badge/tests-261%20passing-brightgreen.svg)](tests/)
[![built with uv](https://img.shields.io/badge/built%20with-uv-de5fe9.svg)](https://docs.astral.sh/uv/)

## What it is

A web-search and page-extraction toolkit you can self-host, with no API keys required to get started. It fans a query across multiple search engines, fuses and dedups the results, then fetches and extracts target pages into clean Markdown plus metadata. Each layer is isolated behind a versioned JSON Schema contract, runs in-process by default, and is independently swappable.

The scope is an honest Pareto win, not a clean sweep. Among 2026 agentic-search APIs the top tier is statistically tied on result quality, so cost and latency are the real differentiators (see the scorecard below). This project wins on cost (about zero at the software layer), privacy and self-hosting, multi-engine recall plus dedup, clean extraction, and the unprotected majority of the web. Hard anti-bot at scale stays a swappable paid egress adapter, because residential proxies and captcha solving have no reliable free equivalent.

## Layer status

| Layer | What it does | Status |
|---|---|---|
| Layer 1: Search | Multi-engine router (SearXNG + ddgs), canonicalize, dedup, de-correlated RRF fusion | Built |
| Layer 2A: Fetch + Extract | Tiered fetch (httpx, curl_cffi impersonation), Trafilatura extraction to Markdown + metadata | Built |
| Layer 2B: Format + Store | Paginated Markdown + JSON sidecar, progressive-disclosure index/resolver, MinHash dedup, ephemeral SQLite-FTS5 store | Built |
| Layer 3: Agent I/O | Consolidated `web_search`/`web_fetch`/`web_open`, optional MCP stdio server, `SKILL.md` | Planned |

Contracts are frozen as JSON Schema 2020-12: `envelope@1.0.0`, `search@1.0.0`, `fetch@1.1.0`, `extract@1.0.0`, `format@1.0.0`, `store@1.0.0`. Every response is wrapped in one `Envelope { contract_version, ok, data, error, meta }`.

## Layer 1: Search

A thin router fans a normalized request out to per-engine adapters behind an `EngineAdapter` port, canonicalizes URLs, dedups with provenance merge, and fuses results with provenance-aware weighted Reciprocal Rank Fusion (RRF, k=60). The default backbone is keyless: SearXNG (point `WEBSEARCH_SEARXNG_URL` at any instance) plus ddgs as a zero-config fallback. Keyed engines (Brave, Exa, Tavily, and others) are a planned drop-in behind the same port.

The load-bearing decision is **de-correlation**. SearXNG and ddgs both lean on the same upstream crawlers (Google, Bing), so a naive union lets a consensus bonus amplify the same crawler agreeing with itself, and the fused ranking can end up worse than a single well-tuned engine. The fix: a result's sources are grouped by `correlation_group`, each group contributes one RRF term at its best rank, and the consensus bonus scales only with the number of distinct groups. SearXNG and ddgs agreeing counts as one independent vote; SearXNG and a neural index agreeing counts as two. The router records the de-correlation in a warning.

Every result carries full per-engine provenance (which engine returned it, at what rank), so the ranking is auditable.

## Layer 2A: Fetch + Extract

Two decoupled sub-ports, each independently swappable.

**Fetch** escalates by tier and escalates only when it detects an anti-bot block. Tier 0 is plain httpx; on a detected challenge it escalates to curl_cffi (browser TLS/JA3 impersonation). It does not escalate on a 404 or on a terminal block (rate limit, auth required, legal/geo block), since a stealthier client from the same egress will not help. Block detection reads header markers first, then gated body markers for Cloudflare, DataDome, PerimeterX, Akamai, and Imperva (Imperva can return a block with HTTP 200, so its body markers are scanned on any status). Browser and stealth tiers (Crawl4AI, nodriver) are named in the contract enum but stay opt-in, not in the base install.

**Extract** defaults to **Trafilatura**, a heuristic extractor. Per the May 2026 WCXB benchmark, heuristic extractors beat neural extractors on both quality and cost (neural runs tens to hundreds of times more expensive), and Trafilatura lands at about 0.79 F1 on CPU in roughly 100ms; article pages saturate around 0.93 F1. The adapter parses the raw HTML once with lxml to recover the raw schema.org JSON-LD blocks and `og:type` (Trafilatura folds JSON-LD into metadata and never exposes the raw blocks), runs Trafilatura for the Markdown body and plain text plus metadata, then computes:

- a heuristic **`quality_score`** (0..1) from runtime signals (text density, word-count saturation, paragraph count, inverse link density, JSON-LD presence, clean title) with hard vetoes for soft-404s and shells; below about 0.80 a page is a fallback candidate.
- a cheap **`page_type`** resolved from JSON-LD `@type`, then `og:type`, then URL shape.

Neural extract engines (`crawl4ai`, `jina_readerlm`, and others) are named in the contract enum but stay opt-in. The default dependency closure is permissive: Apache-2.0 (trafilatura) plus MIT/BSD/MPL deps.

There is **no output-length cap anywhere**. `content_markdown` is never truncated; `--max-bytes` is a transport guard only, not a content cap.

## Layer 2B: Format + Store

Two decoupled sub-ports that turn results into something an agent can actually read, and keep the full pages around for follow-up.

**Format** takes vendor-neutral results (the union of what Layer 1 and Layer 2A produce) and renders one layout-stable Markdown document plus a parallel JSON sidecar holding the same data. Results are ordered by descending relevance and paginated, because lost-in-the-middle is still real in 2026 even on long-context models. Near-duplicate dedup runs first: byte-exact (normalized SHA-256), then a pure-Python MinHash over word 4-gram shingles (128 permutations, Jaccard 0.9) clustered with union-find. The best-scored page in a cluster is kept and the rest are recorded as `dropped_duplicates`, so the fold is auditable. The 0.9 threshold is deliberately conservative: it folds genuinely near-identical pages (mirrors, syndication), not merely topically similar ones, so two pages that just read alike will not collapse at the default. Lower `jaccard_threshold` for boilerplate-heavy corpora. Dedup is pure Python by design (no `datasketch`), so the default install stays dependency-light. Progressive disclosure chooses how much to inline:

- **`auto`** (default) inlines full bodies when the page fits a token budget, otherwise switches to an index (a preview plus a stable `id`).
- **`index`** always shows a preview and a resolve hint; **`full`** always inlines.

The optional `anthropic_search_result_blocks` view maps 1:1 onto Anthropic search_result content blocks (`source` as a bare string, at least one non-empty text block, citations all-or-nothing). It is off by default and is a derived view, not the canonical shape, so installing into a non-Anthropic harness costs nothing.

There is **no output-length cap here either**. The sidecar carries the full body verbatim in every mode, and the store keeps the full Markdown. `body_char_budget` only offloads the rendered Markdown view to the resolver (with a hint), and `--no-truncate` turns even that off. Nothing is summarized or discarded.

**Store** is an ephemeral page index behind a `PageIndex` port (`add` / `search` / `get` / `resolve_index`). There is no database for the per-query result set, which is tens of rows of plain Python; the store earns its keep only as the progressive-disclosure index over fetched pages. The default adapter is SQLite FTS5 over an in-memory connection: it ships in the Python stdlib with BM25 and needs no third-party package. FTS5 is not compiled into every SQLite build, so the adapter probes for it at runtime and falls back to a pure-Python BM25 index that returns the identical shapes. Adds are idempotent on url plus content hash, an arbitrary query is escaped so FTS5 operators never raise a syntax error, and persistence is just passing a file path. A vector or Rust backend (`sqlite-vec`, `tantivy`) plugs in behind the same port, opt-in.

## Install

The project is uv-native. With [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/hec-ovi/websearch-skill
cd websearch-skill
uv sync
```

## Quickstart

Search works with no setup (ddgs is the zero-config default). Add SearXNG for a second, keyless engine and let the router fuse and dedup across both:

```bash
# Layer 1: search
uv run websearch search "rust ownership" --json

# point at a SearXNG instance for a second engine
export WEBSEARCH_SEARXNG_URL=http://localhost:8080
uv run websearch search "rust ownership" --engines searxng,ddgs

# Layer 2A: fetch + extract one page to clean Markdown
uv run websearch fetch "https://en.wikipedia.org/wiki/Rust_(programming_language)" --json

# Layer 2B: open several pages into one paginated, deduped, LLM-ready document,
# and full-text search the passages across them
uv run websearch open \
  "https://en.wikipedia.org/wiki/Rust_(programming_language)" \
  "https://doc.rust-lang.org/book/ch04-00-understanding-ownership.html" \
  --search "ownership borrow checker"
```

Every command prints a compact human view by default, or the raw JSON `Envelope` with `--json` (exit 0 on success, 1 on a request-level error). For the fetch command, `--output-format {markdown,text,json}` selects the body representation the human view prints (`text` emits the plain-text rendering), and `--quiet` prints only the extracted body, for piping. For the open command, `--mode {auto,index,full}` controls progressive disclosure, `--no-truncate` inlines every full body, `--search QUERY` runs a BM25 passage search over the opened pages, and `--anthropic-blocks` adds the Anthropic search_result view to the sidecar. Useful search flags include `--engines`, `--searxng-url` (or `WEBSEARCH_SEARXNG_URL`), and `--no-ddgs`. See `uv run websearch <command> --help` for the full flag list.

A `fetch --json` response looks like:

```json
{
  "contract_version": "1.0.0",
  "ok": true,
  "data": {
    "source": { "final_url": "https://...", "status": 200, "fetched_via": "http", "blocked": false },
    "result": {
      "title": "Rust (programming language)",
      "page_type": "article",
      "quality_score": 0.91,
      "word_count": 8123,
      "content_markdown": "# Rust ...",
      "metadata": { "og_type": "article" }
    }
  },
  "error": null,
  "meta": { "layer": "extract", "backend": "http", "elapsed_ms": 412 }
}
```

As a library:

```python
from websearch.layer1_search import build_router, SearchRequest

router = build_router(searxng_url="http://localhost:8080", enable_ddgs=True)
envelope = router.search(SearchRequest(query="rust ownership", count=10))
for r in envelope.data["results"]:
    print(r["fused_score"], r["url"], [s["engine"] for s in r["sources"]])
```

Layer 2B (format + store) the same way. `run` returns an `Envelope`; its `data` is the JSON `FormatPayload`, so `markdown` and the lossless `sidecar` are plain dict access:

```python
from websearch.layer2_format import (
    FormatRequest, ResultInput, build_format_pipeline,
    build_page_index, StoreConfig, PageInput, SearchPageRequest,
)

env = build_format_pipeline().run(
    FormatRequest(
        query="rust ownership",
        results=[ResultInput(url="https://x.test/a", title="A", score=0.9, body_markdown="# A ...")],
    )
)
document = env.data["markdown"]       # the layout-stable document
sidecar = env.data["sidecar"]         # identical data; full bodies verbatim, never capped

# an ephemeral page index for passage search and resolve-by-id
index = build_page_index(StoreConfig())          # in-memory SQLite FTS5 (BM25)
index.add([PageInput(url="https://x.test/a", markdown="# A ...")])
hits = index.search(SearchPageRequest(query="borrow checker"))
```

## Security

A fetch tool an agent can point anywhere is an SSRF and prompt-injection surface, so:

- **SSRF guard (built):** fetch enforces an http(s) scheme allowlist and resolves every host, refusing private, loopback, link-local (the `169.254.169.254` cloud-metadata endpoint), reserved, and multicast addresses. Redirects are followed manually with the same check on each hop, so a public URL cannot redirect into the internal network. Override per request with `--allow-private-hosts` for deliberate internal fetches.
- **Untrusted content (Layer 3):** fetched page text is untrusted input and must never be treated as instructions. Fencing it in explicit untrusted-content markers at the agent boundary is a Layer 3 (agent I/O) responsibility; Layer 2A returns the clean body unmodified by design, so the contract stays clean and piping works. Until Layer 3 lands, treat `content_markdown` as untrusted in your own prompt assembly.

## Architecture

Each layer is a folder with a port (a capability-named interface) and one or more adapters behind it, connected only by versioned JSON Schema 2020-12 contracts. Port fields are capability-named (`snippet`, `fused_score`, `sources`); a backend's native shape is mapped onto the port inside that backend's adapter. The default deployment runs in-process for speed, and a layer can later move to a subprocess or a local service without its neighbors changing, because the isolation is the contract, not the process boundary. Additive contract changes are MINOR (consumers ignore unknown fields); a removal, rename, or type change is MAJOR, and consumer-driven contract tests fail any producer change that breaks a recorded fixture. Full design in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## The 7-axis scorecard

"Outperform" is decomposed so it is measurable, not rhetorical:

| Axis | This tool | Notes |
|---|---|---|
| Retrieval quality | At top-tier parity on the common case | 2026 leaders are statistically tied on quality |
| Freshness | On-demand recency filter | per-engine, best-effort |
| Extraction recall / noise | Competitive (Trafilatura ~0.79 F1, articles ~0.93) | heuristic beats neural on quality and cost |
| Anti-bot success | About 70 to 90% without paid proxies | near-cloud with a plugged-in residential adapter |
| End-to-end latency | The unprotected common case (where leaders actually differ) | multi-engine + fuse adds some cost |
| Cost | About zero at the software layer | infra documented separately |
| Citation accuracy | Source-anchored, deduped results | no fabricated URLs |

Honest scope: top agentic-search APIs are tied on quality, and hard anti-bot at scale is irreducibly paid (even Firecrawl scores about 34% on independently tested protected sites). This project concedes the protected long tail to a swappable paid egress adapter and does not claim to beat everything. The egress adapter, when added, is scoped to search geo-targeting and rate-limit rotation, not anti-bot for page fetches (commercial VPNs use datacenter IPs that anti-bot systems flag).

## Roadmap

Planned, not built yet:

- **Layer 3 (Agent I/O):** consolidate to `web_search` / `web_fetch` / `web_open`, an optional FastMCP stdio server, and one `SKILL.md` to the Agent Skills standard. `web_open` builds on the Layer 2B format and store ports and adds the untrusted-content fencing.
- **Opt-in egress:** gluetun / wg-netns proxy or VPN scoped to search geo and rate limits (not anti-bot), plus a paid residential-proxy adapter for the protected long tail.
- **Local rerank:** a cross-encoder pass to turn multi-engine recall into precision.
- **More engines:** keyed adapters (Brave, Exa, Tavily) behind the existing `EngineAdapter` port; an optional neural index.
- **Distribution:** harness packaging (`npx skills add`, plugin marketplaces, PyPI/uvx), with a dual-directory skill drop for Claude, Codex, and OpenCode.

## Development

```bash
uv sync          # install deps (including the dev group)
uv run pytest    # 188 tests
uv run ruff check .
```

CI runs ruff and pytest on Python 3.11, 3.12, and 3.13 via uv. The contract tests validate real output against the frozen JSON Schemas, so a change that breaks a contract shape fails CI. Build one isolated layer at a time, against its versioned contract; adding or swapping an engine or an extractor touches only its adapter module.

## License

MIT. See [`LICENSE`](LICENSE). Optional anti-bot tiers that depend on AGPL components (for example nodriver) stay as out-of-band adapters you install separately, not bundled into the MIT core.
