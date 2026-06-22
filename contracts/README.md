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

Extract, format, and agent-io contracts are added when those layers are built
(progressive disclosure), each as its own file with its own `x-contract-version`.

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
