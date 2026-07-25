# Self-hosted SearXNG (optional)

The tool works with zero setup using the built-in keyless engines (the `ddgs`
metasearch library: Google, Brave, DuckDuckGo, Yandex, Yahoo, Startpage, Mojeek,
Wikipedia). You do not need this.

Run your own SearXNG when you want the broad path: every engine SearXNG can reach, no
public rate limits, and no API keys. Public SearXNG instances are not a good fit for
automated use (most disable the JSON API and actively throttle bots), which is why this
config self-hosts a private one.

Everything lives in this folder or in Docker. Nothing is installed on the host, and
nothing is written into the repo working tree.

## Start it

Requires Docker with the Compose plugin. From anywhere in the repo:

```bash
./docker/searxng/searxng.sh up
export WEBSEARCH_SEARXNG_URL=http://127.0.0.1:8888
uv run websearch web-search "your query"        # now fuses SearXNG + ddgs
```

`up` generates a `.env` with a secret key unique to your machine, starts one container
on `127.0.0.1:8888`, and waits for `/healthz`. Port 8888 rather than 8080, which
collides with roughly everything.

| Command | What it does |
|---|---|
| `searxng.sh up` | generate the secret if missing, start, wait for health |
| `searxng.sh status` | container state, engine counts, and a live query |
| `searxng.sh verify` | run a real search through the `websearch` CLI |
| `searxng.sh engines` | re-probe every engine and regenerate `settings.yml` |
| `searxng.sh restart` | apply a `settings.yml` change |
| `searxng.sh logs` | follow container logs |
| `searxng.sh down` | stop and remove (`--purge` also drops the cache volume) |

## All the engines, not the default handful

SearXNG knows about roughly 280 engines but ships most of them off, so a plain query
reaches about six general ones, and on a normal home connection Brave and Startpage
answer with a CAPTCHA. That is the gap this config closes.

`searxng.sh engines` asks the running instance what it has, sends one single-engine
query per engine (three different queries before giving up), and rewrites the block at
the bottom of `core-config/settings.yml` to enable every engine that actually answered.
Each engine it leaves off is listed there with the reason (`CAPTCHA`, `timeout`,
`access denied`), so the config records what was measured rather than what was hoped
for. On the machine this was built on it enabled 180 engines, 45 of them in the
`general` category, taking a plain query from 26 results across 2 engines to 155-240
across 21-29 in 3 to 4 seconds.

Two things it deliberately does not do:

- It never touches an engine that SearXNG enables upstream. Failures there are often
  transient, and SearXNG already suspends a failing engine on its own.
- It holds back torrent trackers and shadow libraries (`nyaa`, `annas archive`,
  `piratebay`, and friends). They stay loaded and you can still query them by name;
  they just do not join the default fanout. `searxng.sh engines --include-restricted`
  turns that off.

Re-run it whenever you bump the image: engines come and go upstream, and one that
worked last year may be blocking your IP range today. `--reuse` re-renders the block
from the last probe without querying anything, and `--dry-run` reports without writing.

Reaching one specific engine never needed any of this. The `engines` parameter
activates an engine whether or not it is in the default fanout:

```bash
curl 'http://127.0.0.1:8888/search?q=rust&format=json&engines=bing,mojeek,yandex'
```

## What is in here

- `docker-compose.yml`: one `searxng` service, no Valkey/Redis (the limiter is off, so
  the cache backend it needs is not required).
- `core-config/settings.yml`: overrides on top of SearXNG's defaults. `search.formats:
  [html, json]` (the JSON API the tool reads, off by default upstream),
  `server.limiter: false`, `server.public_instance: false`, a connection pool sized for
  the wide fanout, and the generated engine block.
- `tools/probe-engines.py`: the probe behind `searxng.sh engines`. Stdlib only.
- `.env`: generated, gitignored, machine-local. Holds `SEARXNG_SECRET` and optionally
  `SEARXNG_PORT`, `SEARXNG_HOST`, `SEARXNG_VERSION`. See `.env.example`.

## How it stays in its box

- The config directory is mounted read-only and `FORCE_OWNERSHIP=false` stops the
  entrypoint chowning it. The previous version of this config let the container rewrite
  the ownership of files in the repo.
- Runtime state (favicon cache, engine state) goes to the `searxng-cache` Docker
  volume, never to a path in the working tree.
- The container drops all capabilities and runs with `no-new-privileges`.
- The port binds to `127.0.0.1` unless you change `SEARXNG_HOST`.
- The secret key is generated per machine into a gitignored `.env`. Compose has no
  fallback value, so an unset secret aborts the start rather than quietly sharing a
  placeholder with everyone who cloned the repo.

## Before exposing it

This config is meant for local use. Before binding to anything other than loopback
(`SEARXNG_HOST=0.0.0.0`), re-enable the limiter and add a Valkey service, since the
limiter needs it as a cache backend. See the upstream docs:
<https://docs.searxng.org/admin/installation-docker.html>

## How it plugs in

SearXNG is one engine behind the same Layer 1 search port as `ddgs`. With
`WEBSEARCH_SEARXNG_URL` set, the router queries both and fuses them with de-correlated
RRF (so the engines they share, like Google and Bing, are not double counted). Unset it
and the tool falls back to `ddgs` alone. Nothing else changes.
