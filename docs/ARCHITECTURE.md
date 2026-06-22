# Architecture

The capability is built as a set of isolated layers connected only by versioned JSON
contracts, packaged as one tool (and, later, one Agent-Skills `SKILL.md`). The default
deployment runs the layers in-process for speed; the isolation that matters is the
contract, not a forced process boundary. A layer is swappable as long as it keeps
emitting and accepting its contract, regardless of language or process.

## Layers

```
        Layer 3  AGENT I/O   CLI-first core (+ optional MCP stdio adapter)
                   |   web_search / web_fetch / web_open(resolve)
                   |   doc_handle is the only cross-layer key
       +-----------+------------------------+
       |                                    |
  Layer 1  SEARCH                      Layer 2  READ
  thin engine router                   2A fetch + extract
  - SearXNG (keyless backbone)          - tiered fetch (curl_cffi -> browser tiers)
  - ddgs (zero-config fallback)         - Trafilatura extraction
  - optional keyed adapters             - emit markdown + json_ld + quality_score
  - provenance-aware RRF (k=60)        2B format
  - de-correlate engines                - paginated markdown + JSON sidecar
       |                                 - progressive-disclosure index + resolver
  egress sub-module (optional)          store (ephemeral)
  - proxy/VPN for geo + rate limit      - in-memory by default; SQLite FTS5 for the
  - NOT anti-bot for page fetches         page index; opt-in persistence
```

Status: Layer 1 (search) and Layer 2A (fetch + extract) are implemented. Layer 2B
(format/store) and the Layer 3 MCP adapter and `SKILL.md` are designed but not built yet.

## Ports and adapters

Each layer is a folder with a port (a capability-named interface, e.g. "search") and one
or more adapters behind it (SearXNG, ddgs, a future Rust extractor). The router depends
only on the port, never on a concrete backend, so adding or swapping an engine touches
only its adapter module plus the capability map. Port fields are capability-named
(`snippet`, `fused_score`, `sources`); a backend's native shape (SearXNG's
`title`/`url`/`content`/`engine`) is mapped onto the port inside that backend's adapter.

## Contracts and the Envelope

Every inter-layer message and CLI `--json` output is an `Envelope`:

```
Envelope { contract_version, ok, data, error, meta{ layer, backend, elapsed_ms, trace_id } }
```

Contracts live in `contracts/` as JSON Schema 2020-12 files, each carrying an
`x-contract-version` (semver). Additive fields are MINOR (consumers ignore unknown
fields), so a MINOR change in one layer never forces a change in another; a removal,
rename, type change, or meaning change is MAJOR. The rule is enforced by consumer-driven
contract tests: each consumer checks in golden fixtures of the fields it reads, and CI
fails any producer change that breaks a fixture. The `SearchResponse` schema pulls the
`Envelope` in by cross-file `$ref`, so the two cannot drift apart silently.

## Layer 1: search

A thin router fans a normalized `SearchRequest` out to isolated per-engine adapters,
then canonicalizes URLs, dedups with provenance merge, and fuses with provenance-aware
weighted Reciprocal Rank Fusion (k=60).

The load-bearing decision is **de-correlation**. SearXNG and ddgs both lean on the same
underlying crawlers (Google, Bing), so naively fusing them lets a consensus bonus
amplify the same crawler agreeing with itself, and the fused ranking can be worse than a
single well-tuned engine. The fix: a doc's sources are grouped by `correlation_group`,
each group contributes a single RRF term at its best rank, and the consensus bonus
scales only with the number of distinct groups. So SearXNG and ddgs agreeing counts as
one independent vote; SearXNG and a decorrelated index (Exa-style neural, Tavily-style
curated) agreeing counts as two. When correlated engines are queried together, the
router emits a warning recording the de-correlation.

Default backbone is keyless: SearXNG (point `WEBSEARCH_SEARXNG_URL` at any instance) plus
ddgs as a zero-config fallback. Keyed, decorrelated engines plug in behind the same port.

## Layer 2A: fetch + extract

Two decoupled sub-ports behind their own contracts (`fetch`, `extract`), each
independently swappable. A fetcher knows nothing about extraction and vice versa.

**Fetch** escalates by tier and only when it detects an anti-bot block: Tier 0 is plain
httpx, and on a detected challenge it escalates to curl_cffi (browser TLS/JA3
impersonation). It does not escalate on a 404 or a terminal block (rate limit, auth,
legal/geo), since a stealthier client from the same egress will not help. Block detection
reads response headers first, then body markers gated behind a short-body / candidate
-status check (Cloudflare, DataDome, PerimeterX, Akamai, and Imperva, which can block with
HTTP 200). Browser and stealth tiers (Crawl4AI, nodriver) are named in the contract enum
but stay opt-in. Fetch enforces an SSRF egress guard (an http(s) scheme allowlist plus a
resolved private/loopback/link-local/reserved address refusal, including the
`169.254.169.254` metadata endpoint) before each request and on every redirect hop;
`allow_private_hosts` opts out per request.

**Extract** defaults to Trafilatura, which beats neural extractors on quality and cost.
It emits clean Markdown plus plain text and metadata, recovers the raw schema.org JSON-LD
and `og:type` with lxml (Trafilatura folds JSON-LD into metadata and never exposes the raw
blocks), and computes a heuristic `quality_score` (0..1, below ~0.80 is a fallback
candidate) and a cheap `page_type`. There is no output-length cap: `content_markdown` is
never truncated; `max_bytes` is a transport guard only. Neural engines plug in behind the
same port.

**Untrusted content.** Fetched page text is untrusted input. Layer 2A returns the clean
body unmodified by design (so the contract stays clean and piping works); fencing it in
explicit untrusted-content markers for indirect-prompt-injection defense is a Layer 3
(agent I/O) responsibility, added with that layer.

## Honest scope: a Pareto win

This is not a clean sweep over the cloud leaders. Among 2026 agentic-search APIs the top
tier is statistically tied on result quality, so the real differentiators are latency and
cost. Anti-bot success on protected sites is low even for paid leaders, and higher
success is bought with paid residential/mobile proxies plus captcha solving, for which
there is no reliable free or local solver.

So the goal is to win decisively on cost, privacy/self-host, configurability, multi-engine
recall plus dedup, clean extraction, and freshness-on-demand; match the leaders on the
unprotected common case; and make the protected long tail a swappable, configurable
egress decision (direct, then home/residential, then a paid pool). The egress sub-module
is scoped to search geo-targeting and rate-limit rotation only; a commercial VPN uses
datacenter IPs that anti-bot systems flag, so it is not anti-bot for page fetches.

What stays irreducibly paid and out of scope as a default: residential/mobile proxy pools,
reliable hard-captcha solving, and a billion-page neural index. Each plugs in as a
swappable contract adapter when the user supplies a key or backend.

## The 7-axis scorecard

"Outperform" is measured, not asserted:

1. retrieval quality (mean-relevant x authoritativeness)
2. freshness (force-livecrawl option)
3. extraction recall and noise ratio
4. anti-bot success (be honest: good without paid proxies, near-cloud with a plugged-in
   residential adapter)
5. end-to-end latency (the unprotected common case, where leaders actually differ)
6. cost (about zero at the software layer; infra documented)
7. for answer-mode, citation accuracy and zero fabricated URLs

Benchmark against retriever-isolated metrics, not live-web model leaderboards (those
measure the wrapping model, not the search layer).
