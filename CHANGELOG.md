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
- Layer 2A returns clean page content unmodified; fencing fetched (untrusted) content in
  explicit markers for prompt-injection defense is a Layer 3 (agent I/O) responsibility,
  added with that layer.
- Layer 2B (format/store) and the Layer 3 MCP adapter and `SKILL.md` are not built yet.
