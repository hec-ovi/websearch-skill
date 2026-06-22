# Architecture

The capability is built as a set of isolated layers connected only by versioned JSON
contracts, packaged as one tool (and, later, one Agent-Skills `SKILL.md`). The default
deployment runs the layers in-process for speed. Layers are coupled only through their
versioned contracts, so a layer is swappable as long as it keeps emitting and accepting
its contract, regardless of language or process.

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

Status: Layers 1 (search), 2A (fetch + extract), 2B (format + store), and 3 (agent I/O,
including the untrusted-content fence, the FastMCP server, and `SKILL.md`) are implemented,
plus two standalone keyless tools (`arxiv`, `github`). Harness packaging and the
multi-manifest distribution are designed but not built yet.

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

Default backbone is keyless and works with zero setup: the `ddgs` metasearch library,
which is itself multi-engine (Google, Brave, DuckDuckGo, Yandex, Yahoo, Startpage,
Mojeek, Wikipedia, with Bing and others selectable by name). `ddgs_backend` (the
`search` command's `--ddgs-backends` flag) forces a subset. A self-hosted
SearXNG plugs in as a second, broader engine when `WEBSEARCH_SEARXNG_URL` is set, and the
router fuses both. Public SearXNG instances are not a default: most disable the JSON API
and rate-limit automated clients, so they fail on a fresh install. Keyed, decorrelated
engines plug in behind the same port.

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
body unmodified by design (so the contract stays clean and piping works); fencing it for
indirect-prompt-injection defense happens at the Layer 3 agent boundary
(`web_fetch`/`web_open`), described below.

## Layer 2B: format + store

Two decoupled sub-ports behind their own contracts (`format`, `store`), mirroring the
fetch/extract split.

**Format** takes vendor-neutral results (the union of what Layer 1 and Layer 2A supply,
mapped onto one `ResultInput`) and renders a single layout-stable Markdown document plus
a parallel JSON sidecar that carries the same data. The Markdown body is the model's
native register (fewer tokens than JSON for prose); the sidecar is the program-side
record for citing, dedup, and pagination. Results are ordered by descending relevance and
paginated to fight lost-in-the-middle. Near-duplicate dedup runs before rendering:
byte-exact (normalized SHA-256) first, then a pure-Python MinHash over word 4-gram
shingles (128 permutations, Jaccard 0.9) clustered with union-find, keeping the
best-scored canonical and recording the rest as `dropped_duplicates`. Progressive
disclosure picks the render mode: `auto` inlines full bodies when the page fits a token
budget, otherwise an index (a preview plus a stable id the resolver expands on demand).
The optional `anthropic_search_result_blocks` view down-renders 1:1 into Anthropic
search_result content blocks; it is a derived, vendor-specific projection off the
vendor-neutral results, off by default, and Layer 3 owns the citations-versus-structured
-output toggle (the two are mutually exclusive in the Anthropic API).

There is no output-length cap. The sidecar carries the full body verbatim in both index
and full modes, and the store keeps the full Markdown; `body_char_budget` only offloads
the rendered Markdown view to the resolver (with a resolve hint), and `--no-truncate`
disables even that. Nothing is ever summarized or discarded.

**Store** is an ephemeral fetched-page index behind a `PageIndex` port
(`add`/`search`/`get`/`resolve_index`). No database is used for the per-query result set
(it is tens of rows, handled with plain Python); the store is used only as the
progressive-disclosure index over fetched pages. The default adapter is SQLite FTS5 over
an in-memory connection: it ships in the Python stdlib with BM25 and needs zero
third-party packages. FTS5 is not guaranteed on every build, so availability is probed at
runtime and the adapter falls back to a pure-Python BM25 index that returns the identical
shapes. Adds are idempotent on url plus content hash, an arbitrary query is escaped so
FTS5 operators never raise a syntax error (tokens are quoted and OR-joined for recall),
and persistence is just the presence of a file path (WAL enabled). A vector or Rust
(`sqlite-vec`, `tantivy`) backend plugs in behind the same port, opt-in, never required.

## Layer 3: agent I/O

The consolidated agent-facing surface over Layers 1, 2A, and 2B, behind its own contract
(`agent-io`). Three capabilities, all over the same `Envelope`:

- `web_search` reshapes Layer 1 results into ranked hits, each with a human-readable `handle`.
- `web_fetch` runs Layer 2A (fetch + extract), fences the body as untrusted, paginates it by
  token budget, and indexes the full page into the Layer 2B store.
- `web_open` pages through an already-fetched document from that store by `handle`, without
  re-fetching the network.

`handle` (`site~shorthash`) is the only cross-layer key and is human-readable, never an opaque
id. The store doubles as the handle registry: `web_open` recomputes the handle over the indexed
URLs, so a persisted store resolves handles across processes, and it fails closed on a same-site
collision rather than serving the wrong page. A page reached by a redirect is keyed by the
requested URL (and aliased under the final URL), so a handle from `web_search` stays openable.
Pagination is lossless progressive disclosure: the pages concatenate back to the exact body and
every page is reachable, so the no-output-cap rule holds end to end.

**Untrusted-content fence.** A web-search tool funnels attacker-controllable text into the model,
so each fetched page is wrapped in a fence built from the 2026 primary-source guidance: a
per-instance 128-bit random nonce in the open and close markers (so injected text cannot forge the
close), a data-only directive, and neutralization of any in-body copy of the marker, with optional
datamarking. This is an input-layer mitigation: it prevents the boundary breakout, not persuasion,
and does not eliminate indirect prompt injection. The real guarantees are channel separation (the
MCP face delivers content through the tool-result channel, which models are trained to distrust),
least privilege, and cutting exfiltration paths.

**Faces.** One core, three faces over identical payloads: the `websearch web-search` / `web-fetch`
/ `web-open` CLI; an optional FastMCP stdio server (`websearch mcp`, the `mcp` extra) whose tools
return the same Envelope JSON the CLI emits; and a portable `SKILL.md` (Agent Skills standard, name
plus description) documenting the command grammar so a non-MCP agent can drive the CLI by shell and
read stdout. The lower-level `search` / `fetch` / `open` commands remain as the per-layer surfaces
for debugging and composition.

## Extra tools: arxiv and github

Two standalone keyless tools sit beside the pipeline, not inside it, behind their own
contracts (`arxiv`, `github`). They are not search engines that feed RRF fusion (their
results are domain-specific: papers, repositories), so they keep their own shapes and
emit the same `Envelope` (meta.layer `arxiv` / `github`) consumed uniformly by the CLI
and MCP faces. `arxiv` queries the official export.arxiv.org Atom API (GET, with 429
backoff); `github` queries the unauthenticated GitHub REST search API (repository search
only, with clean rate-limit handling). Both inject their HTTP boundary, so tests feed
canned responses. They exist because a self-hosted SearXNG would also cover these via its
engines; shipping them as keyless built-ins means you get that coverage without running a
SearXNG. Reddit and X have no keyless, terms-clean search path in 2026, so there is no
tool for them; use the general web search with a `--site` filter.

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

The scorecard decomposes "outperform" into seven measurable axes:

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
