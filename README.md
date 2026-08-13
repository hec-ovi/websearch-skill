# websearch-skill

Open-source multi-engine web search and content extraction for AI agents. Internal layers communicate through versioned JSON Schema contracts.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](pyproject.toml)
[![tests](https://img.shields.io/badge/tests-748%20passing-brightgreen.svg)](tests/)
[![built with uv](https://img.shields.io/badge/built%20with-uv-de5fe9.svg)](https://docs.astral.sh/uv/)

> **Status: early.** First public release 2026-06-22; current version in [`CHANGELOG.md`](CHANGELOG.md). Available now: keyless search, Markdown extraction, five agent commands, self-hosted SearXNG, and an opt-in egress proxy. These are covered by the test suite. **There is no MCP server**; use the CLI directly or install the skill (see No MCP below). Hard anti-bot tiers and local reranking remain on the roadmap. Pin a version and test it in a sandbox before using it with sensitive data.

## What it is

A self-hosted web search and page reader for AI agents, with no API keys or paid search service. It queries multiple engines, combines and deduplicates the results, then fetches pages as clean Markdown. The CLI runs locally and sends queries directly to the configured engines.

Core commands:

- **`init`** reads the settings files, starts local SearXNG (and Tor, when that layer is on), and reports available capabilities.
- **`web_search`** finds pages: ranked, deduplicated results across engines, each with a reusable `handle`.
- **`web_fetch`** reads a page: clean Markdown, fenced as untrusted, paginated so a long page never overflows context.
- **`web_open`** pages back through a document you already fetched, from cache, without hitting the network again.

Additional keyless commands:

- **`arxiv`** searches arXiv papers and returns structured metadata (authors, abstract, categories, abstract and PDF links).
- **`github`** searches GitHub repositories and returns typed fields you can sort on (stars, language, topics).
- **`tor`** starts a local Tor and routes everything through it, which is what makes `.onion` reachable and `--onion` search the Tor indexes. Off by default; see Tor below.

### Engines, out of the box, no keys

The default engine is the keyless [`ddgs`](https://github.com/deedy5/ddgs) metasearch library, which spans **Google, Brave, DuckDuckGo, Yandex, Yahoo, Startpage, Mojeek, Wikipedia, and Grokipedia**. Each query uses the providers that respond. The agent-facing `web-search` command requires no account, service, or engine flags. The lower-level `search` command can restrict providers with `--ddgs-backends google,brave,mojeek`. Provider availability varies by network; `websearch doctor` reports it per provider.

For broader and more reliable search you can run your own SearXNG (~280 engines, your own server, still no keys), and the router fuses it with `ddgs` and de-correlates the engines they share. `websearch searxng up` sets one up and starts it on any machine with git and Python; on a Docker host, [`docker/searxng/`](docker/searxng/) is the curated stack, including a probe that enables every engine that answers on your connection. See Self-hosting SearXNG below. Public SearXNG instances are deliberately not a default: most disable the JSON API and rate-limit automated clients, so depending on them would break on a fresh install.

The project is MIT-licensed. Optional integrations include self-hosted SearXNG, keyed engines (Brave, Exa, Tavily), and a paid egress adapter.

The scorecard below compares result quality, latency, cost, privacy, and retrieval limits. The software layer is free and self-hosted, with multi-engine search, deduplication, and Markdown extraction. Protected sites may require the planned paid egress adapter; residential proxies and CAPTCHA solving have no reliable free equivalent.

## Layer status

| Layer | What it does | Status |
|---|---|---|
| Layer 1: Search | Multi-engine router (keyless `ddgs` across many engines, optional self-hosted SearXNG), canonicalize, dedup, de-correlated RRF fusion | Built |
| Layer 2A: Fetch + Extract | Tiered fetch (httpx, curl_cffi impersonation), Trafilatura extraction to Markdown + metadata | Built |
| Layer 2B: Format + Store | Paginated Markdown + JSON sidecar, progressive-disclosure index/resolver, MinHash dedup, ephemeral SQLite-FTS5 store | Built |
| Layer 3: Agent I/O | Consolidated `web_search`/`web_fetch`/`web_open`, untrusted-content fence, `SKILL.md` | Built |
| Extra tools | Keyless `arxiv` (paper search) and `github` (repo search), standalone over the same Envelope | Built |
| Doctor | `websearch doctor`: per-capability self-test of the optional layers, every engine, the tools, and both fetch tiers | Built |
| Init | `websearch init`: reads the settings files, starts SearXNG and Tor, runs diagnostics, and reports capability state | Built |
| Tor | `websearch tor`: a local Tor with no Docker, onion search (Ahmia, SearXNG's onions category) and `.onion` fetching, chained behind an existing proxy rather than replacing it | Built |

Contracts are frozen as JSON Schema 2020-12: `envelope@1.0.0`, `search@1.2.0`, `fetch@1.3.0`, `extract@1.0.0`, `format@1.0.0`, `store@1.0.0`, `agent-io@1.2.0`, `arxiv@1.1.0`, `github@1.1.0`, `doctor@2.1.0`, `searxng@1.2.0`, `init@1.1.0`, `tor@1.0.0`, `proxy@1.0.0`. Every response is wrapped in one `Envelope { contract_version, ok, data, error, meta }`.

## Layer 1: Search

Search normalizes a request, sends it to each configured `EngineAdapter`, canonicalizes and deduplicates URLs, and combines the rankings with weighted Reciprocal Rank Fusion (RRF, k=60). The keyless default is `ddgs`, a metasearch library for Google, Brave, DuckDuckGo, Yandex, Yahoo, Startpage, Mojeek, Wikipedia, and Grokipedia. On the lower-level `search` command, `--ddgs-backends` selects a provider subset and `--engines` selects the ddgs and SearXNG adapters. The agent-facing `web-search` command uses the defaults. Set `WEBSEARCH_SEARXNG_URL` to add a self-hosted SearXNG instance. Keyed engines such as Brave, Exa, and Tavily are planned.

`ddgs` is a keyless Python library that runs in-process. SearXNG is a separate self-hosted server with a larger engine catalog. Search can use either adapter or combine both. SearXNG provides search results only; page fetching and extraction remain in Layer 2A.

De-correlation prevents duplicate upstream data from distorting the ranking. SearXNG and ddgs both use crawlers such as Google and Bing, so a naive union can count the same source twice. Results are grouped by `correlation_group`; each group contributes one RRF term at its best rank, and the consensus bonus uses only the number of distinct groups. SearXNG and ddgs agreeing counts as one independent vote; SearXNG and a neural index agreeing counts as two. The response includes a warning when de-correlation is applied.

Each result records which engines returned it and its rank within each engine.

## Layer 2A: Fetch + Extract

Fetching and extraction use separate interfaces and can be replaced independently.

**Fetch** starts with httpx and retries with curl_cffi browser impersonation only when it detects an anti-bot challenge. It does not retry 404s, rate limits, authentication failures, or legal and geographic blocks because the same network address is unlikely to change the result. Detection checks response headers, then known body markers for Cloudflare, DataDome, PerimeterX, Akamai, and Imperva. Imperva body markers are checked on every status because its blocks can return HTTP 200. Browser and stealth tiers (Crawl4AI, nodriver) are present in the contract but are not part of the base install.

**Extract** defaults to **Trafilatura**, a heuristic extractor. Per the May 2026 WCXB benchmark, heuristic extractors beat neural extractors on both quality and cost (neural runs tens to hundreds of times more expensive), and Trafilatura lands at about 0.79 F1 on CPU in roughly 100ms; article pages saturate around 0.93 F1. The adapter parses the raw HTML once with lxml to recover the raw schema.org JSON-LD blocks and `og:type` (Trafilatura folds JSON-LD into metadata and never exposes the raw blocks), runs Trafilatura for the Markdown body and plain text plus metadata, then computes:

- a heuristic **`quality_score`** (0..1) from runtime signals (text density, word-count saturation, paragraph count, inverse link density, JSON-LD presence, clean title) with hard vetoes for soft-404s and shells; below about 0.80 a page is a fallback candidate.
- a cheap **`page_type`** resolved from JSON-LD `@type`, then `og:type`, then URL shape.

Neural extract engines (`crawl4ai`, `jina_readerlm`, and others) are named in the contract enum but stay opt-in. The default dependency closure is permissive: Apache-2.0 (trafilatura) plus MIT/BSD/MPL deps.

Extraction does not truncate `content_markdown`. `--max-bytes` limits the downloaded response size, and `--max-bytes 0` disables that transport limit. A value of `0` also disables `--max-results`, `--page-size-tokens`, and `--per-page`, subject to provider limits such as GitHub's 100 results per page and arXiv's 2,000 results per request.

## Layer 2B: Format + Store

This layer formats results for agents and stores full pages for later access.

**Format** takes vendor-neutral results from Layers 1 and 2A and renders a layout-stable Markdown document plus a JSON sidecar containing the same data. Results are ordered by relevance and paginated according to the configured token budget. Near-duplicate deduplication runs first: byte-exact (normalized SHA-256), then pure-Python MinHash over word 4-gram shingles (128 permutations, Jaccard 0.9) clustered with union-find. The highest-scored page in each cluster is kept and the rest are listed in `dropped_duplicates`. The default threshold merges near-identical pages such as mirrors and syndicated copies, while keeping pages that are only topically similar. Lower `jaccard_threshold` for boilerplate-heavy corpora. The implementation does not depend on `datasketch`. Progressive disclosure controls how much to inline:

- **`auto`** (default) inlines full bodies when the page fits a token budget, otherwise switches to an index (a preview plus a stable `id`).
- **`index`** always shows a preview and a resolve hint; **`full`** always inlines.

The optional `anthropic_search_result_blocks` view maps 1:1 onto Anthropic search_result content blocks (`source` as a bare string, at least one non-empty text block, citations all-or-nothing). It is off by default and separate from the canonical response shape.

The sidecar and store retain the full body in every mode. `body_char_budget` moves content from the rendered Markdown view into the resolver and adds a lookup hint; it does not modify the stored content. `--no-truncate` keeps the full body inline.

**Store** indexes fetched pages behind a `PageIndex` interface (`add` / `search` / `get` / `resolve_index`). Per-query result sets remain as plain Python objects; only fetched pages enter the index. The default adapter uses SQLite FTS5 with BM25 and requires no third-party package. If the local SQLite build lacks FTS5, it falls back to a pure-Python BM25 index with the same response shape. Adds are idempotent by URL and content hash, queries are escaped before reaching FTS5, and a file path enables persistence. Optional vector or Rust backends can implement the same interface.

## Layer 3: Agent I/O

Layer 3 exposes `web_search` (find), `web_fetch` (read a URL), and `web_open` (page through an already-fetched document). Each returns the same `Envelope`. The CLI and portable `SKILL.md` use the same implementation. The skill follows the Agent Skills format and can be installed in Claude Code, Codex, OpenCode, and other compatible agents.

Commands refer to pages with a human-readable **handle** (`site~shorthash`, for example `en.wikipedia.org~3a1f9c2b5e6f`). `web_fetch` indexes the full page in the Layer 2B store and returns one token-budget page; `web_open` reads later pages from that store without fetching the URL again. Because each command runs in a new process, the store uses a file by default beside the env file or in the XDG cache. `--persist-path off` keeps it in memory. Pagination does not discard content; the full body remains available page by page. The lower-level `search` / `fetch` / `open` commands remain available for debugging and composition.

Fetched page text is untrusted, so `web_fetch` and `web_open` wrap each page in a fence (see Security). The fenced content is returned as tool output, separate from user instructions.

## Extra keyless tools

Two standalone tools cover sources that general web search handles poorly, both keyless and over the same Envelope:

- **`arxiv`** searches arXiv via the official Atom API and returns structured papers (title, authors, abstract, categories, abstract and PDF links). It supports field-targeted search (`--field title|author|abstract`) and sorting by date or relevance, uses GET so it benefits from arXiv's cache, and backs off on the 2026 rate limiting.
- **`github`** searches GitHub repositories via the unauthenticated REST API and returns typed fields (full name, stars, forks, language, topics, updated date). Unauthenticated search is about 10 requests per minute; rate-limited requests return a `rate_limited` error. Code search needs a token and is not included in the keyless path.

```bash
uv run websearch arxiv "diffusion models for protein design" --max-results 5 --sort-by submittedDate
uv run websearch github "vector database" --language Rust --sort stars --per-page 10
```

There are no dedicated Reddit or X tools because neither has a keyless search API compatible with its terms in 2026 (Reddit's anonymous JSON endpoints return 403 as of May 2026; X requires a paid API or a logged-in account). Use an open-web site filter:

```bash
uv run websearch web-search "rust async" --site reddit.com
uv run websearch web-search "frontier model release" --site x.com
```

## No MCP (since 0.3.0)

Version 0.3.0 removed the MCP server. The server was a long-lived process that read configuration at startup and cached its engine set. Changes to the proxy, self-hosted SearXNG, engine selection, or env file required a client restart. CLI commands start a new process and read that configuration on every call. `websearch init` handles setup and reports the resulting capabilities.

For an existing `websearch mcp` installation, install the skill with `npx skills add hec-ovi/websearch-skill`; it exposes the commands through the agent's shell.

## Install

Pick the route that matches how you use it. Everything is keyless and needs internet; the only hard requirement is [uv](https://docs.astral.sh/uv/). Full per-harness instructions (Claude Code, Codex, OpenCode, Cursor, Hermes, OpenClaw, and PyPI publishing) are in [`docs/INSTALL.md`](docs/INSTALL.md).

**As a CLI tool, no install** (`uvx` builds an ephemeral env and runs it):

```bash
uvx websearch-skill web-search "your query"
# or straight from git, for an unreleased commit:
uvx --from git+https://github.com/hec-ovi/websearch-skill websearch web-search "your query"
```

**As an agent skill** across 40+ agents via the [`skills`](https://www.npmjs.com/package/skills) CLI (it installs the `skills/web-search/` directory into each detected agent):

```bash
npx skills add hec-ovi/websearch-skill            # all detected agents
npx skills add hec-ovi/websearch-skill -a claude-code -a codex -s web-search
```

**As a Claude Code plugin**:

```text
/plugin marketplace add hec-ovi/websearch-skill
/plugin install web-search@websearch-skill
```

**From source** (development, uv-native):

```bash
git clone https://github.com/hec-ovi/websearch-skill
cd websearch-skill
uv sync
uv run websearch web-search "your query"
```

## Quickstart

Search works with no setup: the keyless `ddgs` metasearch is the default. The agent-facing `web-search` (Layer 3) needs no engine flags. The lower-level `search` (Layer 1) is for debugging and power use, and is the only command that takes `--engines`, `--ddgs-backends`, and `--no-ddgs`:

```bash
# Layer 1: search (keyless, multi-engine via ddgs)
uv run websearch search "rust ownership" --json

# force specific keyless engines (search command only, not web-search)
uv run websearch search "rust ownership" --ddgs-backends google,brave,mojeek

# add a self-hosted SearXNG (see docker/searxng/) as a second, broader engine
export WEBSEARCH_SEARXNG_URL=http://127.0.0.1:8888
uv run websearch search "rust ownership" --engines searxng,ddgs

# Layer 2A: fetch + extract one page to clean Markdown
uv run websearch fetch "https://en.wikipedia.org/wiki/Rust_(programming_language)" --json

# Layer 2B: open several pages into one paginated, deduplicated Markdown document,
# and full-text search the passages across them
uv run websearch open \
  "https://en.wikipedia.org/wiki/Rust_(programming_language)" \
  "https://doc.rust-lang.org/book/ch04-00-understanding-ownership.html" \
  --search "ownership borrow checker"

# Layer 3: the fenced, handle-keyed agent interface
uv run websearch web-search "rust ownership" --json
uv run websearch web-fetch "https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html" \
  --page-size-tokens 4000
# page through the rest of a fetched doc by its handle, from cache (no refetch, no flags:
# the page index is on disk by default, so a later command resolves the same handle)
uv run websearch web-open "doc.rust-lang.org~<hash>" --page 2

# extra keyless tools: arXiv papers and GitHub repos
uv run websearch arxiv "mixture of experts scaling laws" --max-results 5
uv run websearch github "llm agent framework" --language Python --sort stars

# Tor (off by default): start it, then search the onion indexes and read .onion pages
uv run websearch tor up
uv run websearch web-search "hidden wiki" --onion
uv run websearch web-fetch "http://2gzyxa5ihm7nsggfxnu52rck2vv4rvmdlkiu3zzui5du4xyclen53wid.onion/"

# initialize local services and report available capabilities
uv run websearch init

# check configured capabilities and providers
uv run websearch doctor
```

Every command prints a compact human view by default, or the raw JSON `Envelope` with `--json` (exit 0 on success, 1 on a request-level error). For the fetch command, `--output-format {markdown,text,json}` selects the body representation the human view prints (`text` emits the plain-text rendering), and `--quiet` prints only the extracted body, for piping. For the open command, `--mode {auto,index,full}` controls progressive disclosure, `--no-truncate` inlines every full body, `--search QUERY` runs a BM25 passage search over the opened pages, and `--anthropic-blocks` adds the Anthropic search_result view to the sidecar. The agent-facing `web-search` takes `--max-results`, `--detail`, `--freshness`, `--site`, `--language`, `--country`, `--safesearch`, `--offset`, and `--searxng-url`; the engine-selection flags (`--engines`, `--ddgs-backends`, `--no-ddgs`) live only on the lower-level `search` command, alongside `--searxng-url` (or `WEBSEARCH_SEARXNG_URL`). See `uv run websearch <command> --help` for the full flag list.

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

router = build_router(searxng_url="http://127.0.0.1:8888", enable_ddgs=True)
envelope = router.search(SearchRequest(query="rust ownership", count=10))
for r in envelope.data["results"]:
    print(r["fused_score"], r["url"], [s["engine"] for s in r["sources"]])
```

Use Layer 2B (format + store) the same way. `run` returns an `Envelope`; its `data` is the JSON `FormatPayload`, so `markdown` and the full sidecar use normal dictionary access:

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
sidecar = env.data["sidecar"]         # identical data with full page bodies

# an ephemeral page index for passage search and resolve-by-id
index = build_page_index(StoreConfig())          # in-memory SQLite FTS5 (BM25)
index.add([PageInput(url="https://x.test/a", markdown="# A ...")])
hits = index.search(SearchPageRequest(query="borrow checker"))
```

## Optional layers (all off by default)

The base install searches keyless engines over a direct connection. Four optional settings add VPN verification, proxy routing, Tor, or self-hosted SearXNG. `websearch doctor` verifies each configured setting:

| Layer | Switch | What it adds |
|---|---|---|
| VPN | `WEBSEARCH_VPN=nordvpn` or `any` | declares that egress should be tunneled, so the doctor verifies it instead of assuming it. It routes nothing on its own: the tunnel is your VPN app's job |
| Egress proxy | `WEBSEARCH_PROXY=<url>` or `nordvpn` | one proxy for every path that leaves this machine, in both processes: the CLI's own requests, and the local SearXNG's requests to the engines it queries. `websearch proxy setup` walks through the credentials, `websearch proxy use <city>` picks the exit, `websearch proxy lock` refuses anything that would go direct |
| Tor | `WEBSEARCH_TOR=on` | every path goes through a local Tor, `.onion` becomes reachable, and `--onion` searches the onion indexes. `websearch tor up` starts one and sets this for you |
| SearXNG | `WEBSEARCH_SEARXNG_URL=<url>` | a self-hosted metasearch instance joins the Layer-1 fanout. `websearch searxng up` starts one and sets this for you, with or without Docker |

### Where settings live

An exported variable always wins, and `--vpn` / `--proxy` / `--tor` / `--searxng-url` beat everything for a single run. Otherwise the settings come from the first file that defines them:

1. `WEBSEARCH_ENV_FILE`, when you point it at one (a container that mounts a config directory does this).
2. `.env` in the working directory, for a project that keeps its own (copy `.env.example`).
3. `$XDG_CONFIG_HOME/websearch/.env`, usually `~/.config/websearch/.env`. This is the one that applies wherever you run the command, and it is where `searxng up` and `tor up` record what they started.

Nothing already set is ever overwritten, so a file can only fill in a gap, and both files are outside the repo or gitignored, which keeps NordVPN service credentials out of your shell history. `websearch init` prints the files it read and the ones it looked for.

### Self-hosting SearXNG

SearXNG is optional; the keyless `ddgs` engines work without it. A self-hosted SearXNG adds more engines without relying on a public instance or API keys. `websearch doctor` can compare its results with `ddgs` to distinguish a parser failure from a provider blocking your IP.

Choose the setup based on Docker availability.

**No Docker (works anywhere, including an agent sandbox):**

```bash
websearch searxng up                          # clone, install, start detached, wire it in
websearch web-search "your query"             # now fuses SearXNG + ddgs
websearch searxng status                      # where it lives, whether it answers
websearch searxng down                        # stop it
```

The first `up` clones upstream SearXNG and builds a virtualenv beside it, about 15 to 30 seconds and a few hundred MB; later ones only start it. Everything lands in one state directory (`WEBSEARCH_SEARXNG_HOME`, defaulting beside `WEBSEARCH_ENV_FILE` or in the XDG cache), and `WEBSEARCH_SEARXNG_URL` is written into your env file for you, so the next search picks it up with nothing else to do. The server is started in its own session rather than as a child of the command, which is what lets it survive an agent CLI killing the process group of every shell command it runs. `WEBSEARCH_SEARXNG_PORT` moves it off 8888. `websearch init` does this step for you as part of bringing everything up.

**With Docker:**

```bash
./docker/searxng/searxng.sh up                # generates a per-machine secret, waits for health
export WEBSEARCH_SEARXNG_URL=http://127.0.0.1:8888
uv run websearch web-search "your query"
```

One container, nothing installed on the host, and it can route SearXNG's own engine requests through your egress proxy. SearXNG ships ~280 engines but leaves most of them off, so a stock instance answers a general query from about six of them (and on a home connection Brave and Startpage return a CAPTCHA). `searxng.sh engines` probes every engine from your own connection and enables the ones that answer, recording the reason next to each one it skips. Measured here: a general query went from 26 results across 2 engines to 155-240 across 21-29, in 3 to 4 seconds.

The container mounts its configuration read-only, stores runtime state in a Docker volume, drops all capabilities, and binds to loopback by default. No secret is committed. Torrent trackers and shadow libraries are excluded from the default engine set but remain queryable by name. See [`docker/searxng/`](docker/searxng/) before exposing it beyond localhost.

Both bind to loopback, turn the JSON API on, and route the instance's own engine requests through your egress proxy. The difference is the engine list: the no-Docker path runs upstream defaults (83 engines enabled of 278 here), while the Docker stack's probe enables everything that answers from your own connection (213 of 279 on the same machine, same day). Use it when you have Docker; use `websearch searxng up` when you do not.

### Egress proxy

One switch, `WEBSEARCH_PROXY`, routes every network path (search engines, fetch tiers, arxiv, github) through a proxy:

```bash
export WEBSEARCH_PROXY=socks5h://user:pass@host:1080   # any proxy URL (http:// works too)
export WEBSEARCH_PROXY=nordvpn                         # NordVPN shorthand, see below
export WEBSEARCH_PROXY=off                             # or unset it: direct connection
```

The `nordvpn` shorthand builds the SOCKS5 URL for you from `NORDVPN_USER` and `NORDVPN_PASS`. These are the service credentials shown in the Nord Account dashboard under "Set up NordVPN manually", not your account login. `websearch proxy` sets all of it up without you having to find the file:

```bash
websearch proxy setup          # creates the settings file and prints its path, with both keys empty
websearch proxy locations      # the exits NordVPN runs: Netherlands, Sweden, nine US cities
websearch proxy use dallas     # writes NORDVPN_HOST, turns the proxy on, restarts a running SearXNG
websearch proxy status         # what is set, what is missing, and where the traffic actually comes out
```

Fill the two credential lines yourself in the file `setup` names; nothing reads them back or prints them. `status` asks NordVPN through the proxy where the connection surfaces, so a selection that did not take effect shows up as a city that does not match. A single command can also take `--location dallas` to use one exit for that call alone.

### The lock

```bash
websearch proxy lock     # WEBSEARCH_EGRESS_LOCK=on
websearch proxy unlock
```

Locked, nothing leaves this machine outside the proxy. A path that has no proxy is refused with `error.code: "egress_locked"` rather than falling back to a direct connection: both fetch tiers, the search engines, the local SearXNG's engine requests, the SearXNG and Tor installs, and `doctor --baseline`. `websearch proxy off` is refused until you unlock. Loopback and LAN targets are unaffected, because they never leave the machine.

The fallback is the reason this exists: it happens on the day the proxy is unavailable, not on the day you are watching.

Every network command also takes `--proxy <url|nordvpn|off>`, which overrides the variable for that run, so `--proxy off` gets you a direct connection without unsetting anything. Prefer `socks5h://` over `socks5://`: it resolves DNS through the proxy, so hostnames never hit your local resolver. A per-request `fetch --proxy` still wins over the process-wide default.

With `WEBSEARCH_PROXY` set, every path that leaves this machine uses it: both fetch tiers, the `ddgs` engines, Ahmia, arXiv, GitHub, the doctor's probes, the SearXNG and Tor installs, and the local SearXNG instance's own engine requests. Tests cover two important behaviors:
- **DNS resolution.** With a proxy configured, the SSRF guard does not resolve hostnames locally. `socks5h://` resolves them at the exit node. Literal IPs are still refused without a lookup, including `http://127.0.0.1` and the `169.254.169.254` metadata endpoint.
- **Failure behavior.** If the proxy is unavailable, every component returns an error instead of retrying over a direct connection. This includes `ddgs` (Rust) and `curl_cffi` (libcurl), which open sockets outside Python.

The opt-in `websearch doctor --baseline` check makes one direct request to compare the direct and proxied exit IPs. It is disabled by default because it sends the direct IP to an echo service.

A self-hosted SearXNG request has two hops, and they are proxied differently on purpose. The client connects to local SearXNG at `127.0.0.1` directly, because that hop never leaves the machine and a remote exit asked to reach its own localhost fails every time. SearXNG then queries Google, Brave, Mojeek and the rest itself, and that hop is the one that leaves: both stacks write your proxy into its `outgoing.proxies` (with `extra_proxy_timeout`, since a SOCKS exit costs a round trip), so those searches come out where the rest of the tool does. `websearch searxng status` prints that address as `egress`, `websearch proxy use <city>` restarts a running instance onto the new exit, and `searxng.sh egress` prints both IPs for the Docker stack. Set `SEARXNG_OUTGOING_PROXY=off` for direct instance egress. A remote SearXNG address is reached through the client proxy like any other host.

### Tor

Off until you start it. `websearch tor up` is the whole setup:

```bash
websearch tor up                              # start Tor, verify the exit, turn the layer on
websearch web-search "query" --onion          # search the onion indexes
websearch web-fetch "http://<addr>.onion/"    # read an onion page, same output as any other
websearch tor status                          # listening, and is the traffic really Tor
websearch tor down                            # stop it and turn the layer off
```

`up` uses a Tor that is already listening, else `tor` on PATH, else it downloads the official Tor Expert Bundle (about 30 MB) into the state directory and checks it against the sha256 published beside it. The binary is not GPG-verified: what this trusts is TLS to torproject.org plus that digest. Tor is started in its own session so it survives the command, the same way SearXNG is, and `up` waits for the bootstrap and then asks `check.torproject.org` whether the traffic really leaves through Tor. A port that answers is not the same thing as a port that is Tor, which is why `is_tor` is a separate field from `reachable` and why the doctor fails on `false` rather than treating "something is listening" as ready.

With the layer on, every path goes through Tor: search, fetch, arXiv, GitHub. `.onion` addresses work, and with it off they are refused before anything resolves, because looking up an onion name locally leaks it to your resolver on its way to failing anyway.

`--onion` swaps the clearnet engines for the onion ones (Ahmia, plus SearXNG's onions category when you run a local instance, which adds Torch). It replaces rather than extends: no clearnet engine indexes onion services and no onion index crawls the clearnet, so a mixed fanout would fuse two half-empty lists over disjoint corpora. Onion searches take ten to thirty seconds and get a longer default timeout to match.

**Tor does not replace your proxy.** If `WEBSEARCH_PROXY` is set, it is written into Tor's own torrc as its upstream (`Socks5Proxy` / `HTTPSProxy`), so the path becomes you, then the proxy, then Tor: the VPN hop still hides Tor use from your ISP, and turning on the layer that was supposed to add a hop never silently removes one. A proxy scheme Tor cannot dial through (WireGuard, say) is a loud error rather than a dropped hop. The one thing that does change is the vpn check, which is skipped while Tor is on and says why: behind Tor the outside world only sees the exit node, so nothing external can confirm a VPN in front of it.

Put your own Tor directives in `torrc.local` beside the generated `torrc`; it is included and survives regeneration. `WEBSEARCH_TOR_SOCKS` points at a Tor you already run (`socks5h://127.0.0.1:9150` is Tor Browser's), `WEBSEARCH_TOR_PORT` moves the port, `WEBSEARCH_TOR_BINARY` skips the download, and `WEBSEARCH_TOR_VERSION` pins the bundle release.

Onion pages are indexed in their own file (`pages-onion.json` beside the ordinary index), so nothing read over Tor mixes into the clearnet store and that history can be deleted on its own, and the untrusted-content fence gains a line naming the source as an onion service: unattributable by design, so nobody behind one can be reported or blocked, which makes it the likeliest place to meet a payload written for an agent rather than a person.

This is Tor, not Tor Browser. It hides where your requests come from; it does not isolate circuits per site, and it does not make what you send anonymous. There is no browser fingerprint surface here (no JS, no canvas, no cookie jar between fetches), but every request does carry the same client signature from every exit, which is linkable across them.

### VPN

`WEBSEARCH_VPN` records the expected tunnel state but does not configure routing. If the tunnel drops, the doctor reports a failure. `nordvpn` is verified against NordVPN's keyless connection endpoint, which reports whether the caller is inside its network. `any` checks only for an active tunnel interface because there is no provider-specific endpoint. The doctor checks through the egress proxy when one is set and directly otherwise.

`nordvpn` works on every platform, since it is an HTTP check. `any` reads interface names, which are meaningful on Linux (`tun`, `wg`, `nordlynx`) and macOS (`utun`) but not on Windows, where they look like `ethernet_32770`; there it reports the tunnel as unconfirmed rather than guessing.

## websearch init

`websearch init` reads the settings files, starts Tor when that layer is on, starts local SearXNG unless `--skip-searxng` is set, and runs the doctor checks:

```bash
uv run websearch init                # bring everything up, then report
uv run websearch init --json         # the same run as an Envelope
uv run websearch init --quick        # skip the slow sweep; unprobed capabilities read "unknown"
uv run websearch init --skip-tor     # leave Tor alone even with the layer on
```

```
  ok    env       read /config/websearch.env
  skip  tor       off (WEBSEARCH_TOR is not set); egress is not going through Tor
  ok    searxng   SearXNG 2026.7.25 answering at http://127.0.0.1:8888, 83 of 278 engines
  warn  doctor    18 ok, 4 warn, 0 fail, 3 skipped

capabilities
  web_search  ok
  web_fetch   ok
  arxiv       ok
  github      ok
  searxng     ok
  proxy       ok
  vpn         off
  tor         off

ready: search online (5 of 9 keyless providers + SearXNG, 83 engines); fetch, arxiv, github ok; egress through socks5h://***:***@nl.socks.nordhold.net:1080
```

Wait for `data.ready`. `data.state` is `ready`, `degraded` (search works but a requested capability is missing), or `broken` (search does not work). `data.capabilities` lists available commands, `data.next_actions` lists remediation steps, and `data.doctor` contains the full diagnostic payload. The exit code is 0 when search works, including a `degraded` result, and 1 when it does not.

## websearch doctor

`websearch doctor` checks current capability and provider status:

```bash
uv run websearch doctor                          # everything
uv run websearch doctor --quick                  # skip the engine fanout, tools, and fetch tiers
uv run websearch doctor --check engines --check proxy
uv run websearch doctor --check tor              # just the Tor layer
uv run websearch doctor --json                   # the same run as an Envelope
```

It prints the four optional layers' state, then checks Python and the dependency closure, direct internet and the exit IP, the egress proxy and whether the exit IP actually moved, Tor (listening, and confirmed as Tor by `check.torproject.org`, plus a real onion fetch), the declared VPN, a self-hosted SearXNG (health, active engine count, live JSON query), each `ddgs` provider on its own, arXiv and GitHub, and both fetch tiers. Exit code is 1 only when something failed: an optional layer that is off is skipped, and one rate-limited provider is a warning. Proxy credentials never reach the output, including inside HTTP client error text.

The SearXNG cross-check helps classify `ddgs` failures. `ddgs` reports "No results found" for CAPTCHAs, rate limits, and successful responses whose markup its parser cannot read. With SearXNG running, the doctor queries the same provider through SearXNG's separately maintained scraper:

```
warn  engine:ddgs:google     No results found. (SearXNG reached it: 260 results)
                             -> ddgs's parser, not a block on your IP. Query it through SearXNG.
warn  engine:ddgs:startpage  No results found. (SearXNG got nothing from it either)
                             -> startpage is refusing this IP. Only a different egress exit changes that.
```

Two runs on one home connection in July 2026, minutes apart, produced different results across the nine `ddgs` providers. Brave, Yahoo, and Yandex answered both times. Google and Mojeek were silent through `ddgs` but returned 181 and 10 results through SearXNG, indicating `ddgs` parser failures. Startpage and Wikipedia were empty through both paths, indicating a block. DuckDuckGo and Grokipedia changed between runs, and NordVPN's shared SOCKS exit lost one additional provider. Run the doctor for current results.

## Security

Fetch accepts arbitrary URLs and page content, so it includes SSRF and prompt-injection controls:

- **SSRF guard (built):** fetch enforces an http(s) scheme allowlist and resolves every host, refusing private, loopback, link-local (the `169.254.169.254` cloud-metadata endpoint), reserved, and multicast addresses. Redirects are followed manually with the same check on each hop, so a public URL cannot redirect into the internal network. Override per request with `--allow-private-hosts` for deliberate internal fetches.
- **Untrusted content (built, Layer 3):** fetched page text is never presented as instructions. `web_fetch` and `web_open` wrap each page in a fence based on 2026 primary-source guidance: a per-instance 128-bit random nonce in the opening and closing markers, a data-only directive, and neutralization of marker copies inside the body. Optional datamarking (`--datamark`) adds resistance. The fence prevents boundary breakout but cannot prevent persuasion. Tool-output separation, least privilege, and blocked exfiltration paths are still required. The lower-level `fetch` command returns the extracted body without the fence for piping and composition.

## Architecture

Each layer is a folder with a port (a capability-named interface) and one or more adapters behind it, connected only by versioned JSON Schema 2020-12 contracts. Port fields are capability-named (`snippet`, `fused_score`, `sources`); a backend's native shape is mapped onto the port inside that backend's adapter. The default deployment runs in-process for speed, and because layers are coupled only through their contracts, a layer can later move to a subprocess or a local service without its neighbors changing. Additive contract changes are MINOR (consumers ignore unknown fields); a removal, rename, or type change is MAJOR, and consumer-driven contract tests fail any producer change that breaks a recorded fixture. Full design in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## The 7-axis scorecard

The scorecard separates retrieval quality, cost, latency, and operational limits:

| Axis | This tool | Notes |
|---|---|---|
| Retrieval quality | Comparable to leading APIs for common queries | 2026 leaders are statistically tied on quality |
| Freshness | On-demand recency filter | per-engine, best-effort |
| Extraction recall / noise | Competitive (Trafilatura ~0.79 F1, articles ~0.93) | heuristic beats neural on quality and cost |
| Anti-bot success | About 70 to 90% without paid proxies | protected sites require residential egress |
| End-to-end latency | Varies with responsive engines | multi-engine fanout and fusion add latency |
| Cost | About zero at the software layer | infra documented separately |
| Citation accuracy | Source-anchored, deduped results | no fabricated URLs |

Protected sites usually require paid residential egress (even Firecrawl scores about 34% on independently tested protected sites). The built-in proxy supports search geo-targeting and rate-limit rotation. It does not bypass page-level anti-bot systems because commercial VPNs use datacenter IPs that those systems commonly flag. A residential egress adapter is planned.

## Benchmark

[`docs/BENCHMARK.md`](docs/BENCHMARK.md) compares the same queries at the same time against the hosted web search in Claude Code and includes commands for reproduction. Both found comparable relevant and recent pages. This project adds local extraction, multi-engine fusion, and the `arxiv` and `github` commands; the hosted search returns a summary in one call.

## Roadmap

Current distribution options include `npx skills add`, a Claude Code plugin and marketplace, and PyPI/uvx. See [`docs/INSTALL.md`](docs/INSTALL.md) for each supported agent.

The opt-in egress proxy (`WEBSEARCH_PROXY`, `--proxy`, NordVPN shorthand) covers every path that leaves the machine, including the self-hosted SearXNG's own engine requests, and `websearch proxy lock` refuses the ones that cannot use it instead of falling back to a direct connection.

Planned, not built yet:

- **Paid egress adapter:** a residential-proxy adapter for the protected long tail, plus optional gluetun / wg-netns network-namespace isolation.
- **Local rerank:** a cross-encoder pass to turn multi-engine recall into precision.
- **More engines:** keyed adapters (Brave, Exa, Tavily) behind the existing `EngineAdapter` port; an optional neural index.

## Development

```bash
uv sync          # install deps (including the dev group)
uv run pytest    # 748 tests, no network
uv run ruff check .
```

CI runs ruff and pytest on Python 3.11, 3.12, and 3.13 via uv, on Linux only. The package itself is pure Python with no OS-specific imports, and its two compiled dependencies (`curl_cffi`, and `primp` under `ddgs`) publish wheels for Linux, macOS, and Windows on both x86-64 and arm64, so macOS and Windows should work; they are not covered by CI, so run `uv run pytest` and `uv run websearch doctor` there before trusting it. Two platform notes: `websearch doctor` reads network interface names for `WEBSEARCH_VPN=any`, which Windows does not expose usefully (it reports the tunnel as unconfirmed there, while `WEBSEARCH_VPN=nordvpn` works everywhere because it is an HTTP check), and the Docker SearXNG stack is driven by a bash script that needs WSL or Git Bash on Windows (`websearch searxng up` is Python and has no such requirement).

The contract tests validate real output against the frozen JSON Schemas, so a change that breaks a contract shape fails CI. Build one isolated layer at a time, against its versioned contract; adding or swapping an engine or an extractor touches only its adapter module.

## License

MIT. See [`LICENSE`](LICENSE). Optional anti-bot tiers that depend on AGPL components (for example nodriver) stay as out-of-band adapters you install separately, not bundled into the MIT core.
