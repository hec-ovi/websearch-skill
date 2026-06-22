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
- Test suite (75 tests): an end-to-end test through the real CLI entry point with both
  external boundaries stubbed (SearXNG via pytest-httpx, ddgs via a fake), plus focused
  canonicalization, dedup, fusion, adapter, router fault-tolerance, and
  contract-conformance tests that validate real output against the frozen schemas. CI
  runs ruff and pytest on Python 3.11 to 3.13 via uv.

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

### Notes

- `fusion.method: score_convex` is accepted but currently falls back to `weighted_rrf`
  (a warning is emitted).
- Layer 2 (fetch/extract, format/store) and the Layer 3 MCP adapter are not built yet.
