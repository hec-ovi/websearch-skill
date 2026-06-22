# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project will follow
semantic versioning once it reaches a tagged release.

## [Unreleased]

### Added

- Frozen contracts as JSON Schema 2020-12: `envelope@1.0.0` (the cross-cutting wrapper
  for every inter-layer message and CLI `--json` output) and `search@1.0.0` (the
  Layer-1 search port). The semver rule (additive is MINOR, removal/rename/retype is
  MAJOR) and the consumer-driven fixture check are documented in `contracts/README.md`.
- Layer 1 (search): a multi-engine router with isolated per-engine adapters behind an
  `EngineAdapter` port. Ships a SearXNG adapter (keyless backbone, over httpx) and a
  ddgs adapter (zero-config fallback).
- Provenance-aware weighted Reciprocal Rank Fusion (k=60) with mandatory
  de-correlation: engines that share a correlation group count as one independent vote,
  so a consensus bonus cannot amplify the same crawler agreeing with itself. The router
  records the de-correlation in a warning.
- URL canonicalization and dedup with provenance merge, site include/exclude filtering,
  concurrent fan-out, and per-engine fault tolerance (an error Envelope is returned only
  when every selected engine fails).
- CLI entry point `websearch search` (`--json` emits the raw Envelope; exit code 1 on an
  error Envelope).
- Test suite: an end-to-end test through the real CLI entry point with both external
  boundaries stubbed (SearXNG via pytest-httpx, ddgs via a fake), plus focused
  canonicalization, dedup, fusion, adapter, router fault-tolerance, and
  contract-conformance tests that validate real output against the frozen schemas. CI
  runs ruff and pytest on Python 3.11 to 3.13 via uv.
- Layer 2A contracts: `fetch@1.1.0` (`FetchRequest`, `FetchResult`) and `extract@1.0.0`
  (`ExtractRequest`, `ExtractResult`, `ExtractSource`, `ExtractPayload`), the two
  decoupled sub-ports of fetch and extract.
- Layer 2A (fetch + extract): a tiered fetch that starts on plain httpx and escalates to
  curl_cffi browser TLS/JA3 impersonation only when it detects an anti-bot block (header
  markers first, then gated body markers for Cloudflare, DataDome, PerimeterX, Akamai,
  and Imperva), never on a 404 or a terminal block (rate limit, auth, legal). Extraction
  defaults to Trafilatura, emitting clean Markdown plus plain text and metadata,
  recovering raw schema.org JSON-LD and `og:type` with lxml, and computing a heuristic
  `quality_score` and a cheap `page_type`. Browser/stealth fetch tiers and neural extract
  engines are named in the contract enums but stay opt-in.
- CLI `websearch fetch` (`--json` emits the Envelope; `--output-format`, `--quiet`,
  `--tier`, `--proxy`, `--allow-private-hosts`, and the extract options). No
  output-length cap: `content_markdown` is never truncated; `max_bytes` is a transport
  guard only (default 10 MB).
- SSRF egress guard: an http(s) scheme allowlist plus DNS resolution that refuses
  private, loopback, link-local (the `169.254.169.254` metadata endpoint), reserved, and
  multicast targets, applied before the first request and on every redirect hop.
- Test suite is now 188 tests (Layer 1 plus Layer 2A: block detection, quality scoring,
  fetch tiers and escalation, the egress guard, extraction, and CLI end-to-end).
- Layer 2B contracts: `format@1.0.0` (`ResultInput`, `FormatRequest`, `FormatPayload`,
  `FormatSidecar`, and the derived `AnthropicSearchResultBlock`) and `store@1.0.0`
  (`PageInput`, `Passage`, `SearchPageRequest`, `SearchPageResult`, `PageDocument`,
  `ResolveIndex`, `StoreConfig`), the two decoupled sub-ports of format and store.
- Layer 2B format: turns vendor-neutral results into one layout-stable Markdown
  document plus a parallel JSON sidecar carrying identical data, ordered by descending
  relevance and paginated. Near-duplicate dedup (byte-exact SHA-256 first, then a
  pure-Python MinHash over word 4-gram shingles) folds duplicates into the best-scored
  canonical and records `dropped_duplicates`. Progressive disclosure picks the render
  mode: `auto` inlines full bodies when the page fits a token budget, otherwise an
  index (a preview plus a stable id to resolve). The optional
  `anthropic_search_result_blocks` view maps 1:1 onto Anthropic search_result content
  blocks (source as a bare string, at least one non-empty text block, citations
  all-or-nothing); it is off by default and Layer 3 owns the citations toggle.
- Layer 2B store: an ephemeral page index behind a `PageIndex` port with
  `add`/`search`/`get`/`resolve_index`. The default adapter is SQLite FTS5 over an
  in-memory connection (Python stdlib, BM25 ranking), with a runtime FTS5 probe that
  falls back to a pure-Python BM25 index when FTS5 is not compiled into the local
  SQLite. Adds are idempotent on url plus content hash; an arbitrary query is escaped so
  FTS5 operators never raise a syntax error; persistence is the presence of a file path.
- CLI `websearch open <url> ...`: composes Layer 2A and 2B (fetch, extract, format,
  index) into one paginated, deduped document, with `--mode`, `--body`,
  `--body-char-budget`/`--no-truncate`, `--anthropic-blocks`, `--search` (BM25 passage
  search over the opened pages), and `--persist-path`. Per-URL fetch failures surface as
  warnings rather than failing the whole request.
- No output-length cap in Layer 2B either: full bodies are stored and echoed in the JSON
  sidecar verbatim in both index and full modes; `body_char_budget` only offloads the
  rendered Markdown view to the resolver, and `--no-truncate` disables even that.
- Test suite is now 272 tests (adds dedup, chunk-offset, renderer layout-stability,
  both store adapters, format/store contract conformance, and the `open` end-to-end).
- Layer 3 contract: `agent-io@1.0.0` (`AgentSearchRequest`/`AgentSearchPayload`,
  `AgentFetchRequest`/`AgentOpenRequest`/`AgentPage`/`AgentFetchPayload`, `FenceInfo`),
  the consolidated agent-facing surface over Layers 1/2A/2B.
- Layer 3 (agent I/O): `web_search` (Layer 1), `web_fetch` (Layer 2A, fenced and
  paginated), and `web_open` (paginate an already-fetched page from the Layer 2B store
  by handle, no re-fetch), all over the same `Envelope`. The only cross-layer key is a
  human-readable `handle` (`site~shorthash`), not an opaque id. Pagination is lossless
  progressive disclosure, never a content cap.
- Untrusted-content fence: each fetched page's content is wrapped in delimiters carrying
  a per-instance 128-bit random nonce (so injected text cannot forge the closing
  marker), a data-only directive, and neutralization of any in-body copy of the marker,
  with optional datamarking (`--datamark`). Documented as reducing, not eliminating,
  indirect prompt injection (it prevents the boundary breakout, not persuasion).
- Optional FastMCP stdio server (`websearch mcp`, the `mcp` extra) exposing
  `web_search`/`web_fetch`/`web_open`; the tool returns the same Envelope JSON the CLI
  emits. New CLI commands `web-search`/`web-fetch`/`web-open`/`mcp`; the lower-level
  `search`/`fetch`/`open` commands stay as the per-layer surfaces.
- A portable `SKILL.md` (`skills/web-search/`) to the Agent Skills standard (name plus
  description), documenting the command grammar, the search/fetch/open decision table,
  and the untrusted-content rule.
- Test suite is now 332 tests (adds the fence, token-budget pagination losslessness,
  the agent-io facade and contract, and the FastMCP server and `web-*` CLI end-to-end).

### Fixed

After an adversarial multi-agent review (eleven confirmed findings) and a fresh-agent
dogfooding pass of Layer 3:

- A page reached by a redirect is now keyed by the requested URL (and aliased under the
  final URL), so a `handle` from `web_search` stays resolvable by `web_open` after a
  redirect instead of diverging to the post-redirect URL.
- `web_search` no longer advertises a `next_offset` cursor, because the keyless backends
  do not page results reliably (feeding it back re-showed earlier results); to get
  different results, refine the query. The `offset` field stays plumbed for a backend
  that honors it.
- The fence neutralizes any copy of its marker case-insensitively (a lowercase or
  mixed-case copy previously survived verbatim in the body).
- `web_open` fails closed on the astronomically-unlikely same-site handle collision
  (returns `not_opened`) rather than serving the wrong cached page; the short hash was
  widened to 48 bits.
- A single-URL `web-fetch` failure preserves the specific cause in the error message
  (it previously collapsed to a generic "all 1 url(s) failed").
- Doc and contract accuracy: `SKILL.md` surfaces the previously-omitted flags
  (`--tier`, `--quiet`, `--datamark` on `web-open`, and others) and the full output
  field list; `AgentPage.fence` is now required in the schema (it was always emitted);
  and the envelope `meta.layer` description lists `agentio`, matching the code.

### Fixed

After an adversarial multi-agent review (nine confirmed findings) and a fresh-agent
dogfooding pass of Layer 2B:

- Distinct pages with an empty or whitespace-only body are no longer folded as exact
  duplicates (they all hashed to the empty-string digest); each body-less result, such
  as a snippet-only or failed-extraction page, now survives as its own entry.
- The page-index query escaper strips control characters, so a NUL byte in a query no
  longer raises a SQLite "unterminated string" error, and an arbitrary query stays safe.
- The pure-Python BM25 fallback now folds diacritics and tokenizes Unicode letters
  (NFKD plus a Unicode word pattern), so accented and non-Latin queries match the same
  pages as the SQLite FTS5 adapter instead of silently returning nothing; its IDF and
  `resolve_index` ordering after a content change were also aligned with FTS5.
- The rendered Markdown status line never shows an impossible position (for example
  "page 6 of 3") on a page past the last one, including when dedup shrinks the set.
- `websearch open --search` degrades a page-index failure to a warning instead of
  leaking a traceback, so a successful fetch and format is never lost to a search error.

### Fixed

After an adversarial multi-agent review and a fresh-agent dogfooding pass of Layer 1:

- URL canonicalization no longer crashes on a malformed port (e.g. a non-numeric or
  out-of-range port) or an IPv6 literal. Previously that ValueError propagated through
  dedup and aborted the entire search, defeating per-engine fault tolerance; now the
  whole canonicalization body is guarded and IPv6 hosts keep their brackets.
- SearXNG and ddgs adapters tolerate malformed responses (non-object JSON, non-dict
  entries), coerce upstream fields (score to float, publishedDate to str), and use a
  valid ddgs region for BCP-47 language tags.
- The router bounds hung engines (a slow engine can no longer block the request past
  its timeout) and reports unknown engine names instead of silently dropping them.
- The CLI returns a clean error Envelope on invalid input instead of an uncaught
  traceback, and `--help` names the built-in engines.

After an adversarial multi-agent review (22 confirmed findings) and a fresh-agent
dogfooding pass of Layer 2A:

- Stopped treating the Imperva `x-iinfo` / `x-cdn` headers as a block (they are on every
  Imperva-proxied response, not just challenges), scanned high-precision DataDome and
  PerimeterX markers on any status/size, and wired the previously dead Akamai body
  markers. Made the markdown-link regexes non-backtracking to remove a ReDoS path.
- Tuned the quality score so content-typed-but-thin pages (products, forums, listings)
  no longer clear the 0.80 gate the way real articles do, widened the link-ratio band,
  and made paragraph counting robust to single-newline markdown.
- Decode bodies with declared-charset then detection instead of a blind UTF-8 fallback,
  default a 10 MB streaming transport guard, and skip the HTML extractor on non-HTML
  responses with a surfaced warning.
- `--output-format text` now emits plain text (it was a silent no-op), added `--quiet`
  for piping the body, and `request_id` is present in `meta` on every response path.

### Notes

- `fusion.method: score_convex` is accepted but currently falls back to `weighted_rrf`
  (a warning is emitted).
- Layer 2A still returns clean page content unmodified (so piping and composition stay
  clean); the untrusted-content fence is applied at the Layer 3 agent boundary
  (`web_fetch`/`web_open`), not in Layer 2A.
- The FastMCP server depends on the optional `fastmcp` package; install it with the
  `mcp` extra. The harness packaging and multi-manifest distribution (npx skills add,
  plugin marketplaces, PyPI/uvx) are not built yet.
