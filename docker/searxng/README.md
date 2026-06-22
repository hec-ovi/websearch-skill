# Self-hosted SearXNG (optional)

The tool works with zero setup using the built-in keyless engines (the `ddgs`
metasearch library: Google, Brave, DuckDuckGo, Yandex, Yahoo, Startpage, Mojeek,
Wikipedia). You do not need this.

Run your own SearXNG when you want the reliable, broad path: hundreds of engines
across categories (including specialized sources), your own server so there is no
public rate-limit roulette, and no API keys. Public SearXNG instances are not a
good fit for automated use (most disable the JSON API and actively throttle bots),
which is exactly why this config self-hosts a private one.

## Start it

Requires Docker with the Compose plugin. From the repo root:

```bash
docker compose -f docker/searxng/docker-compose.yml up -d
```

That starts one container bound to `127.0.0.1:8080`, with the JSON API enabled and
the bot limiter off (this instance is private and only your tool queries it).

Point the tool at it:

```bash
export WEBSEARCH_SEARXNG_URL=http://localhost:8080
uv run websearch web-search "your query"        # now fuses SearXNG + ddgs
```

Stop it with `docker compose -f docker/searxng/docker-compose.yml down`.

## What is in here

- `docker-compose.yml`: one `searxng` service, no Valkey/Redis (the limiter is off,
  so the cache backend it needs is not required).
- `core-config/settings.yml`: minimal overrides on top of SearXNG's defaults:
  `search.formats: [html, json]` (the JSON API the tool reads, off by default
  upstream), `server.limiter: false`, and `server.public_instance: false`.
- `.env`: version pin, bind address, and port.

## Before exposing it

This config is meant for local use only. The `server.secret_key` in
`core-config/settings.yml` is a fixed placeholder committed to this repo, so it is the
same for everyone who clones it. That is fine on a `127.0.0.1`-only box, but before you
bind to anything else (`SEARXNG_HOST=0.0.0.0`):

1. Replace the secret with your own:
   ```bash
   sed -i "s/change-me-local-only-placeholder-secret-key/$(openssl rand -hex 32)/" \
     docker/searxng/core-config/settings.yml
   ```
2. Re-enable the limiter and add a Valkey service back if others can reach it. See the
   upstream docs: <https://docs.searxng.org/admin/installation-docker.html>

## How it plugs in

SearXNG is just one engine behind the same Layer 1 search port as `ddgs`. With
`WEBSEARCH_SEARXNG_URL` set, the router queries both and fuses them with
de-correlated RRF (so the engines they share, like Google and Bing, are not double
counted). Unset it and the tool falls back to `ddgs` alone. Nothing else changes.
