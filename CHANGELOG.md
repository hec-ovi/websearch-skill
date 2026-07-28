# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and releases follow
semantic versioning.

## [Unreleased]

## [0.4.0] - 2026-07-28

### Added

- **A Tor layer, off by default.** `websearch tor up|status|down` runs Tor on this machine
  the same way `searxng up` runs SearXNG: it uses a Tor that is already listening, else
  `tor` on PATH, else it downloads the official Tor Expert Bundle into the state directory
  and checks it against the sha256 published beside it, then starts it detached and waits
  for the bootstrap. Nothing goes through Tor until you run it, and `up` writes
  `WEBSEARCH_TOR=on` into your settings file so the next command inherits it. New contract
  `tor@1.0.0`.

  `up` and the doctor both ask `check.torproject.org` whether the traffic really leaves
  through Tor, and that answer is reported separately from reachability: a port that
  accepts connections is not a port that is Tor, and treating the two as the same would
  tell you your requests are anonymous when they are not.

- **A configured proxy is chained rather than replaced.** With both layers on, the proxy is
  written into Tor's torrc as its upstream (`Socks5Proxy` / `HTTPSProxy`), so the path is
  you, then the proxy, then Tor. A VPN hop still hides Tor use from your ISP, and turning
  on the layer that was supposed to add a hop never silently drops one. A proxy scheme Tor
  cannot dial through is an error instead. The vpn check is skipped while Tor is on and
  says why: behind Tor nothing external can see the hop in front of it.

- **Onion search and `.onion` fetching.** `--onion` on `search` and `web-search` swaps the
  clearnet engines for the onion ones: a new keyless Ahmia adapter, plus SearXNG's onions
  category (which adds Torch) when a local instance is configured. The two sets never run
  together, since no clearnet engine indexes onion services and no onion index crawls the
  clearnet. `web-fetch` and `web-open` take `.onion` URLs unchanged. Onion searches get a
  45s default timeout because ten to thirty seconds is normal through three relays.
  `search@1.2.0` (new `onion`), `agent-io@1.2.0` (same), `fetch@1.3.0` (`Proxy.tor`).

- **`.onion` fails closed without the layer.** The egress guard refuses an onion URL before
  anything resolves and names the command that turns Tor on. Resolving an onion name
  locally leaks it to your resolver on the way to failing anyway.

- **A user-level settings file.** Settings now come from the first file that defines them:
  `WEBSEARCH_ENV_FILE`, then `./.env`, then `$XDG_CONFIG_HOME/websearch/.env`. An exported
  variable still beats all of them. Before this the only fallback was a `.env` in the
  working directory, so "set it once" meant a file per project or re-exporting in every
  shell. `searxng up` and `tor up` record what they started in that file, and skip the
  write when a higher-precedence file already sets the key rather than writing a line the
  environment would ignore. `init@1.1.0` reports every candidate it looked at.

- **`web-search-tor` skill**, covering the layer, onion search, and what Tor does not give
  you. The base skill points at it.

- **Onion page bodies are indexed separately** (`pages-onion.json` beside the ordinary
  index), so nothing read over Tor lands in the clearnet store and onion reading history
  can be deleted on its own. `web-open` still pages through an onion document, because the
  handle carries its site and picks the same index going in and coming out.
  `--persist-path off` still means nothing is written for either.

- **The fence names an onion source.** A page from a `.onion` address gets one extra line
  in the untrusted-content directive: it is an onion service, accountable to nobody, and
  should be treated as hostile by default. Fencing is an input-layer mitigation either
  way, but the reader now knows which pages carry the higher risk.

### Fixed

- The persisted page index now creates its own directory. sqlite does not, and the default
  path lives in the XDG cache, which a fresh machine has never made, so every `web-fetch`
  reported "page index failed" and then `web-open` could not resolve the handle it had just
  printed. Only installs with no `WEBSEARCH_ENV_FILE` were affected, which is why a
  container never saw it.

### Changed

- The generated SearXNG `settings.yml` is regenerated on `up` while it carries its managed
  marker, and the instance restarts when the content changes, because the onion engines
  need a proxy block that a write-once file could never gain. Removing the marker (or
  editing the file) hands it to you permanently. A file written by an earlier version is
  adopted only when it is byte-identical to what that version produced. `searxng@1.1.0`.
- `doctor@2.1.0`: a `tor` layer and group, a `tor` check, and an onion fetch check that
  only runs with the layer on.
- The off-word vocabulary (`off`, `none`, `no`, `false`, `0`, `direct`) is now one
  definition shared by every layer switch instead of two that had drifted apart.

## [0.3.0] - 2026-07-25

### Removed

- **The MCP server.** `websearch mcp`, the `fastmcp` dependency, the `[mcp]` extra, the
  root `.mcp.json`, the registry manifest `server.json`, and the per-harness registration
  in `docs/INSTALL.md` are gone. The CLI plus the skill is the whole surface.

  The reason is that a resident server is the wrong shape for the two optional layers this
  tool leans on. An MCP server reads its configuration once at startup and builds its
  engine fanout once, on the first search. `websearch searxng up` runs in a different,
  short-lived process: it starts SearXNG and writes `WEBSEARCH_SEARXNG_URL` into the env
  file, which cannot reach into the memory of a server that is already running. The server
  kept searching on the keyless engines only, returned `all_engines_failed` when those were
  blocked, and had no honest way to say why. `WEBSEARCH_PROXY` had the same problem. A
  process per command reads the env file, the proxy, and the SearXNG URL fresh every time,
  so a setting takes effect on the next command instead of the next restart.

  If you registered `uvx websearch-skill mcp` with a client, install the skill instead
  (`npx skills add hec-ovi/websearch-skill`): the agent gets the same capabilities through
  its own shell.

- The doctor's `mcp` check and its `mcp` group, with it. That is a breaking contract
  change, so `doctor` goes to **2.0.0** (an enum member was removed).

### Added

- **`websearch init`: one call that brings everything online and says what works.** It
  reads the configured env file, starts the local SearXNG (`--skip-searxng` opts out),
  points this process at it, then runs the full doctor sweep. The response answers the
  question directly: `data.ready` (the flag to wait on), `data.state`
  (`ready`/`degraded`/`broken`), `data.capabilities` (per capability: `ok`, `degraded`,
  `down`, `off`, or `unknown`), `data.engines`, `data.next_actions`, and the whole doctor
  payload in `data.doctor` so nothing needs a second call. Exit code is 0 when search
  works and 1 only when it does not, so a degraded run does not read as a failure. Over
  the new `init@1.0.0` contract.

  It exists because the alternative is an agent probing the install by hand: twenty shell
  calls, a wrong conclusion drawn from a half-configured environment, and a lot of tokens
  spent to learn what one command measures. The skill now tells the agent to run it once
  at the start of a session and to read three fields.

### Changed

- **The page index behind `web-open` persists by default.** With no resident process to
  hold it, an in-memory store made `web-open` unusable across commands: the handle
  `web-fetch` had just returned resolved to nothing. It now writes to the tool's state
  directory (beside `WEBSEARCH_ENV_FILE`, else the XDG cache; `WEBSEARCH_STATE_DIR`
  overrides), so `web-fetch` then `web-open` works with no flags on either side. Pass
  `--persist-path off` (or `WEBSEARCH_PERSIST_PATH=off`) to keep a run in memory and leave
  nothing on disk.

## [0.2.6] - 2026-07-25

### Added

- **`websearch searxng up|status|down`: a self-hosted SearXNG with no Docker.** It clones
  upstream SearXNG into a state directory, builds a virtualenv beside it, writes a
  settings file with the JSON API on and the server bound to loopback, starts it, and
  records `WEBSEARCH_SEARXNG_URL` in the configured env file so the next search fuses it
  with the keyless engines. The first `up` takes about 15 to 30 seconds; later ones only
  start it. `WEBSEARCH_SEARXNG_HOME` moves the state directory (it defaults beside
  `WEBSEARCH_ENV_FILE`, else the XDG cache) and `WEBSEARCH_SEARXNG_PORT` moves it off
  8888. Over the new `searxng@1.0.0` contract.
- The same lifecycle is on the MCP face as `searxng_setup`, so an agent that only speaks
  MCP can set SearXNG up itself instead of reporting that it cannot.

### Fixed

- The server is started in its own session, not as a child of the calling shell. Agent
  CLIs run each tool command as a process group and kill that group when the command
  returns, so a SearXNG backgrounded with `&` died with the call that started it. That is
  the difference between an agent being able to bring this layer up and only being able
  to describe it.
- The hints that said to run `./docker/searxng/searxng.sh up` now name a command that
  exists everywhere. That script ships in this repository, not in the installed package,
  so from a `pip` or `uvx` install (which is how an agent sandbox has it) the instruction
  pointed at a path that was not there. The Docker stack is unchanged and is still the
  better option on a Docker host: it curates the engine list and routes SearXNG's own
  egress through the configured proxy.

## [0.2.5] - 2026-07-25

### Fixed

- **DNS leak behind an egress proxy.** The SSRF guard resolved every URL's hostname
  through the local resolver before fetching, so the ISP saw every site visited even
  though the traffic itself went through the proxy, which is the one thing turning the
  proxy on was meant to prevent. Behind a proxy the guard no longer resolves: a
  `socks5h://` proxy resolves at the exit node, so the local lookup never decided the
  route and could not bind it. Literal IPs are still refused with no lookup at all, so
  `http://127.0.0.1` and the `169.254.169.254` metadata endpoint stay blocked, and the
  full resolution check is unchanged on the direct path.
- `websearch doctor` no longer makes a direct request while an egress proxy is
  configured. Connectivity is now measured along the path the tool actually uses, and
  the direct exit IP, previously taken on every run to compare against the proxied one,
  moved behind `--baseline`. It is still taken automatically when no proxy is set, since
  direct is then the only path. `doctor@1.1.0` adds the `baseline` request field.

### Notes

- Verified by instrumenting every socket and DNS call during a real run, and by pointing
  each component at a dead proxy to confirm it fails rather than falling back to a direct
  connection. That covers `ddgs` (Rust) and `curl_cffi` (libcurl), whose native sockets a
  Python-level check cannot observe.

## [0.2.4] - 2026-07-25

### Added

- `websearch doctor`: a per-capability self-test of one installation, over the new
  `doctor@1.0.0` contract. It reports the state of the three optional layers, then
  checks the interpreter and dependency closure, direct internet and the exit IP, the
  egress proxy and whether the exit IP actually moved, the declared VPN, a self-hosted
  SearXNG (health, active engine count, live JSON query), every `ddgs` provider on its
  own, the arXiv and GitHub tools, both fetch tiers, and the MCP tool registration.
  `--check NAME` narrows to a name prefix or a group, `--quick` drops the query-heavy
  groups, `--json` emits the Envelope. Exit code is 1 only when a check failed; an
  optional layer that is off is skipped, and one rate-limited provider is a warning.
  `Envelope.ok` reports that the diagnostic ran, not that the install is healthy, which
  is `data.summary.fail == 0` (mirrored on `meta.healthy`).
- The doctor tells a stale parser apart from a blocked IP. `ddgs` reports "No results
  found" for a CAPTCHA, a rate limit, and an HTTP 200 whose markup its parser no longer
  reads, and those need opposite fixes, so with SearXNG running the doctor asks it about
  exactly the providers that went quiet. Results there mean the provider is up and
  `ddgs` cannot read it; nothing there means the provider is refusing this IP.
- `WEBSEARCH_VPN` (`nordvpn`, `any`, or off): declares that egress should be tunneled so
  the doctor verifies it rather than assuming it. It routes nothing itself. `nordvpn` is
  checked against NordVPN's keyless connection endpoint, along the path the tool
  actually uses, which is the egress proxy when one is configured.
- The doctor's tunnel detection now uses `socket.if_nameindex`, which is stdlib on Linux,
  macOS, and Windows, instead of walking `/sys/class/net`. macOS puts every tunnel on
  `utun` and is matched; Windows names like `ethernet_32770` carry no tunnel signal, so
  `WEBSEARCH_VPN=any` reports unconfirmed there rather than guessing (`nordvpn` is an
  HTTP check and works everywhere). The runtime check also reports the CPU architecture,
  since a missing wheel is almost always an arm64-versus-amd64 problem.
- `websearch.optional_layers`: one module for the three optional layers (VPN, egress
  proxy, SearXNG), each off unless its variable is set, with credential redaction that
  every display path goes through. Proxy userinfo never reaches output, including inside
  HTTP client error text.
- A gitignored `.env` in the working directory is read at CLI startup, so NordVPN
  service credentials and a proxy URL stay out of shell history. An exported variable
  still wins over the file. See `.env.example`.
- `docker/searxng/searxng.sh`: one entry point for the local SearXNG (`up`, `down`,
  `status`, `verify`, `engines`, `restart`, `logs`). `up` generates a per-machine
  secret into a gitignored `.env`, starts the container, and waits for `/healthz`.
- SearXNG's own engine requests now follow `WEBSEARCH_PROXY`. That is the hop that
  reaches Google and Bing, and it leaves from the container, so it was going out
  unproxied while every other path was covered. `searxng.sh up` expands the proxy with
  the same resolver the CLI uses and renders an `outgoing.proxies` entry into the
  settings the container mounts; `SEARXNG_OUTGOING_PROXY` overrides it, `off` disables
  it, and `searxng.sh egress` prints the host and container egress IPs to confirm.
  Because a proxy URL carries credentials, the tracked settings file holds a marker and
  the real URL only ever lands in the gitignored `.runtime/settings.yml`.
- `docker/searxng/tools/probe-engines.py`: probes every engine the running instance
  knows about and regenerates the engine block in `core-config/settings.yml`, enabling
  the ones that answered and recording the reason next to the ones that did not.
  `--reuse` re-renders from the last probe, `--dry-run` reports without writing, and
  `--include-restricted` opts torrent trackers and shadow libraries into the fanout
  (they are held out by default and stay queryable by name).

### Fixed

- A SearXNG instance on loopback or a LAN address is no longer sent through the egress
  proxy. With `WEBSEARCH_PROXY` set, every search against a self-hosted instance failed
  with `all_engines_failed`, because the remote exit node was asked to reach its own
  localhost.
- The SearXNG container no longer writes into the repo: the config directory is mounted
  read-only with `FORCE_OWNERSHIP=false`, so the entrypoint can no longer chown tracked
  files to a container uid. Runtime cache moved to a named Docker volume.

### Changed

- SearXNG now runs the wide fanout. SearXNG enables about six general engines out of
  the ~280 it ships, so the generated config enables every engine that answers a live
  probe: 180 here, 45 of them general. A plain query went from 26 results across 2
  engines to 155-240 across 21-29, in 3 to 4 seconds. `outgoing` pool sizes were raised
  to match, with a 5s per-engine ceiling so no single slow engine outlives the client's
  8s search timeout.
- The default SearXNG port is 8888 rather than 8080, which collides with most things.
- The container drops all capabilities, runs with `no-new-privileges`, and has a
  healthcheck. The committed placeholder secret key is gone: compose reads
  `SEARXNG_SECRET` with no fallback, so an unset secret aborts the start.
- `docker/searxng/searxng.sh verify` now calls `websearch doctor --check searxng` rather
  than repeating the health check and live query, and keeps only the part the doctor
  cannot see from outside the container: where SearXNG's engine requests leave from.
- Package metadata now identifies the author by name and GitHub profile.

## [0.2.3] - 2026-07-23

### Fixed

- Fetch security: the curl_cffi tier now streams with a capped read, so `--max-bytes`
  stops the download instead of truncating after buffering the whole body; redirects to
  a different host drop the `Authorization` header and caller cookies (`Cookie.domain`
  scopes a cookie to matching hosts); the SSRF guard fails closed when every resolved
  address is unparseable.
- `web_fetch`/`web_open` errors carry the underlying layer's error code and
  retriability instead of a blanket retriable `fetch_failed`, and CLI error envelopes
  carry the active command's contract version.
- GitHub tool: a non-dict `owner` or a non-string 403 message no longer escapes
  `search()` as a raw exception.
- ddgs adapter: news requests query the news index, and the request timeout reaches
  the client.
- arXiv tool: a numeric `Retry-After` is clamped like the HTTP-date form, and boolean
  operators match case-sensitively so natural-language queries stay phrase-quoted.
- Router: repeated engine names are queried once; timed-out engines report a named
  reason; requested-but-disabled engines and unsupported freshness date ranges warn.
- The `web-open` hint printed after a fetch keeps a non-default `--page-size-tokens`,
  so the copy-paste command preserves pagination geometry.
- The error-title veto no longer flags short benign titles ("Forbidden City").
- `body_char_budget=0` and `inline_token_budget=0` now mean no limit, matching the
  convention everywhere else.
- `websearch.__version__` derives from the installed package metadata and is covered
  by the manifest-lockstep test.

### Changed

- The release workflow now publishes to the official MCP Registry (GitHub OIDC) right
  after the PyPI publish, so one `v*` tag updates both and the registry listing can no
  longer lag PyPI.
- Connection reuse on every network path (search adapters, fetch tiers, arxiv, github),
  and per-command lazy imports that roughly halve CLI startup for `arxiv`/`github`.
- `politeness`, `wait_for`, `cache_ttl_seconds`, and the search `egress` block are
  marked reserved in the contracts; setting the fetch knobs emits a warning.
- Removed dead code paths (capability map, unused store/extractor `available()`,
  unused setters and parameters) and tightened `SKILL.md`.

## [0.2.2] - 2026-07-22

### Added

- 0 now means "no limit" on every capping knob, at every layer: `--max-results 0`
  (web-search and Layer 1 `max_total_results`) returns everything the engines gave,
  `--page-size-tokens 0` (web-fetch, web-open, and the MCP tools) returns the whole
  document as one page, and `--max-bytes 0` lifts the transport guard like `null`
  does. Where a provider enforces its own ceiling, 0 requests that maximum: 2000 per
  request for arxiv, 100 per page for github. The arxiv `--max-results` upper bound
  is now the API's real 2000 instead of the old convenience cap of 50.

### Fixed

- `FETCH_CONTRACT_VERSION` said 1.1.0 while `contracts/fetch.schema.json` declared
  1.2.0; the constant now matches, and a new lockstep test asserts every schema's
  `x-contract-version` against its Python constant so the pair cannot drift again.

## [0.2.1] - 2026-07-21

### Fixed

- The Claude plugin and MCP registry manifests (`.claude-plugin/`, `server.json`) now
  carry the same version as the package.

## [0.2.0] - 2026-07-21

### Added

- Optional egress proxy for every network path (search engines, both fetch tiers,
  arxiv, github, and the MCP tools). `WEBSEARCH_PROXY` takes a proxy URL
  (`socks5h://`, `socks5://`, `http://`, `https://`), the shorthand `nordvpn`
  (expands to NordVPN SOCKS5 from the `NORDVPN_USER` / `NORDVPN_PASS` service
  credentials, with `NORDVPN_HOST` to pick a server), or `off`. Each network command
  also takes `--proxy <url|nordvpn|off>` to override the variable per run; the
  existing per-request `fetch --proxy` keeps precedence over the process-wide
  default. Off by default; a misconfigured proxy surfaces as a clean
  `invalid_request` envelope. httpx now installs with the `[socks]` extra so SOCKS5
  URLs work out of the box.

## [0.1.0] - 2026-06-22

First tagged release, published to PyPI.

### Changed

- fastmcp is now a base dependency, not the optional `mcp` extra, so `websearch mcp`,
  `uvx websearch-skill mcp`, and the MCP-registry runner start the stdio server with one
  command and no extra. The `[mcp]` extra stays as a no-op alias so older install commands
  keep resolving. The package also installs a second console script, `websearch-skill`
  (matching the distribution name), so `uvx websearch-skill <cmd>` resolves with no `--from`.
- The agent surface is plug-and-play: `web-search` (CLI) and the `web_search` MCP tool no
  longer take engine-selection flags (`--engines`, `--no-ddgs`, `--ddgs-backends`, the MCP
  `engines` parameter, or `WEBSEARCH_DDGS_BACKENDS`). The keyless multi-engine default
  needs no flags. Engine and backend selection stays on the lower-level `search` command
  for debugging. `web-search --offset` now actually pages results (the ddgs adapter maps
  offset to a page), instead of silently returning page one.
- `fetch@1.2.0`: `FetchResult` gains a structured `failure_kind`
  (`egress_refused` / `redirect_loop` / `transport_error` / `timeout` /
  `dependency_missing`) so retriability is set from the cause, not by matching error text.

### Fixed

- No command can dump a raw traceback. `web-fetch --page 0` (or negative), invalid
  `open`/extract parameters, a store-open failure, and any unexpected error now return a
  clean `invalid_request` / `internal_error` Envelope. `web-search` rejects a
  whitespace-only query; an unknown `engines` value falls back to the default with a
  warning instead of failing.
- `meta.elapsed_ms` is populated on every Layer-3 (`web_search` / `web_fetch` / `web_open`)
  and format Envelope (it was always `0.0`); `_propagate_error` keeps the upstream
  `trace_id` and uses a defined error code.
- Retriability precision: a permanent fetch failure (egress refusal, redirect loop, missing
  optional dependency) is non-retriable; a transport error or timeout is retriable.
- SSRF egress guard uses an allowlist (`is_global`), closing CGNAT `100.64.0.0/10`
  (RFC 6598) and any other non-public range the prior denylist missed.
- The page store (SQLite FTS5 and the BM25 fallback) serializes its methods with a lock and
  writes each document in one atomic transaction, so concurrent MCP tool calls cannot race
  the shared connection or leave a half-written document. `search().total` is the true match
  count, not the capped top-k pool. MCP singletons build under a lock.
- curl_cffi response handling is fully guarded (a mid-body decode/reset becomes an
  escalatable result, not a crash); GitHub/arXiv tolerate a malformed upstream shape with a
  clean `upstream_error`; a non-positive chunk size no longer hangs; a NaN score no longer
  corrupts result ordering; the error-title quality veto no longer tanks a long article
  whose title merely contains a word like "forbidden" or a number like "404".
- The CLI pins stdout/stderr to UTF-8 (no `UnicodeEncodeError` under a C/POSIX locale), and
  the `web-fetch` "more pages" hint suggests a command that actually works in context
  (`web-open ... --persist-path` when persisting, else a re-fetch).

### Added

- Distribution layer: a Claude Code plugin and marketplace (`.claude-plugin/`, with no
  SKILL duplication, since `source: "./"` auto-discovers the root `skills/web-search/` and
  root `.mcp.json`), a generic root `.mcp.json`, an MCP-registry `server.json`
  (`io.github.hec-ovi/web-search`, PyPI/uvx), a PyPI Trusted-Publishing release workflow
  (`.github/workflows/release.yml`, OIDC, no token), and `docs/INSTALL.md` covering every
  harness route (`npx skills add`, `uvx`, the Claude plugin, Codex with its network-sandbox
  caveat, OpenCode, Cursor, Hermes, OpenClaw, and the registry). Install with
  `npx skills add hec-ovi/websearch-skill`, `/plugin install web-search@websearch-skill`, or
  `uvx websearch-skill <cmd>`.
- Keyless multi-engine search out of the box: the `ddgs` adapter is treated as the
  metasearch it is (Google, Brave, DuckDuckGo, Yandex, Yahoo, Startpage, Mojeek,
  Wikipedia by default, with Bing and others selectable by name). The lower-level
  `search` command can force a subset with `--ddgs-backends google,brave,mojeek`, and
  `build_router` / `build_agent_io` take a `ddgs_backend` parameter. `ddgs` is the keyless
  default; a self-hosted SearXNG is the optional broader engine. Public SearXNG instances
  are not used as a default (most disable the JSON API and rate-limit automated clients).
- `arxiv@1.0.0` contract and a keyless `websearch arxiv` tool (also the MCP
  `arxiv_search` tool): arXiv paper search over the official Atom API with field-targeted
  search (`--field`), sorting, GET caching, and exponential backoff on HTTP 429. Returns
  structured papers (authors, abstract, categories, abstract and PDF links).
- `github@1.0.0` contract and a keyless `websearch github` tool (also the MCP
  `github_search` tool): GitHub repository search over the unauthenticated REST API with
  typed fields (stars, forks, language, topics) and a clean `rate_limited` error on the
  ~10 req/min limit. Code search needs a token and is left out of the keyless path.
- New cross-cutting error codes `upstream_error` and `rate_limited` for the extra tools.
- `docker/searxng/`: a lean one-container SearXNG self-host config (JSON API enabled, bot
  limiter off, no Valkey needed) with its own README and a before-you-expose-it checklist.
- `docs/BENCHMARK.md`: a reproducible, same-query head-to-head against the web search
  built into Claude Code, with the recorded verdict (comparable on retrieval; this tool
  wins on cost, privacy, control, and extraction).
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
- FastMCP stdio server (`websearch mcp`) exposing
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
- The FastMCP server ships in the base install (fastmcp is a base dependency). Harness
  packaging and distribution (npx skills add, the Claude plugin and marketplace, the MCP
  registry `server.json`, and PyPI/uvx) are built; see `docs/INSTALL.md`.
