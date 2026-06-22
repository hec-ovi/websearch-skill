# Contracts

Every inter-layer message, CLI `--json` output, and (later) MCP `structuredContent`
is a JSON Schema 2020-12 document. The schema is the load-bearing isolation: a layer
is swappable as long as it keeps emitting and accepting the same contract, regardless
of language or process boundary.

## Files

| File | Port | Version | Status |
|---|---|---|---|
| `envelope.schema.json` | Cross-cutting wrapper (`contract_version`, `ok`, `data`, `error`, `meta`) | 1.0.0 | frozen |
| `search.schema.json` | Layer 1 search (`SearchRequest`, `SearchPayload`, `ResultItem`, `SourceProvenance`) | 1.0.0 | frozen |
| `fetch.schema.json` | Layer 2A fetch sub-port (`FetchRequest`, `FetchResult`) | 1.1.0 | frozen |
| `extract.schema.json` | Layer 2A extract sub-port + agent-facing response (`ExtractRequest`, `ExtractResult`, `ExtractSource`, `ExtractPayload`) | 1.0.0 | frozen |
| `format.schema.json` | Layer 2B format sub-port (`ResultInput`, `FormatRequest`, `FormatPayload`, `FormatSidecar`, `AnthropicSearchResultBlock`) | 1.0.0 | frozen |
| `store.schema.json` | Layer 2B store/page-index sub-port (`PageInput`, `Passage`, `SearchPageRequest`, `SearchPageResult`, `PageDocument`, `ResolveIndex`, `StoreConfig`) | 1.0.0 | frozen |

Layer 2A is two decoupled sub-ports: `fetch` (URL in, raw HTML out) and `extract`
(HTML in, clean Markdown + metadata out). Layer 2B is likewise two decoupled
sub-ports: `format` (vendor-neutral results in, one layout-stable Markdown document
plus a parallel JSON sidecar out, relevance-ordered and paginated with near-duplicate
dedup and progressive disclosure) and `store` (full pages in, ranked passages and a
resolver out, default adapter SQLite FTS5 in-memory). All sub-ports are independently
swappable, so each gets its own contract file and version. The agent-io (Layer 3)
contract is added when that layer is built (progressive disclosure), as its own file
with its own `x-contract-version`.

Two cross-cutting guarantees the 2B contracts make explicit: there is **no
output-length cap** (full bodies are stored and echoed in the sidecar verbatim;
`body_char_budget` only offloads the *rendered* Markdown view to the resolver), and
`anthropic_search_result_blocks` is an **optional, derived, vendor-specific view** off
the vendor-neutral `ResultInput`, never the canonical shape.

## Versioning rule

Each file carries `x-contract-version` (semver).

- **MINOR** - additive only. New optional fields. Consumers ignore unknown fields, so a
  MINOR bump in one layer never forces a change in another.
- **MAJOR** - a removal, rename, type change, or meaning change of an existing field
  (a field that keeps its name and type but changes meaning is still MAJOR).

Compatibility is enforced by consumer-driven contract tests: each consumer checks in
golden fixtures of the fields it actually reads, and CI fails any producer change that
breaks a recorded fixture. The `search.schema.json` `SearchResponse` definition pulls
the envelope in by cross-file `$ref` (resolved through a `referencing` registry in the
tests), so the two files cannot drift apart silently.

## The port-vs-adapter boundary

The port uses **capability-named** fields (`snippet`, `fused_score`, `sources`). A
backend's native shape (SearXNG's `title`/`url`/`content`/`engine`, a keyed API's
fields) is mapped onto the port inside that backend's adapter. The port never inherits
a vendor's quirks.
