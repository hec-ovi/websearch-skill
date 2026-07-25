#!/usr/bin/env python3
"""Probe every engine a running SearXNG knows about, then regenerate settings.yml.

SearXNG ships ~280 engines but leaves most of them off in the default fanout, so a
plain query reaches roughly six general engines. This script asks the instance what
it has (`/config`), sends one single-engine query per engine, and rewrites the
generated block of `core-config/settings.yml` so every engine that actually answered
is enabled. Engines that time out, CAPTCHA, or get blocked are left off, with the
reason recorded next to them.

Stdlib only, so it runs against any Python 3.11+ without the project venv.

    ./searxng.sh engines            # probe and rewrite settings.yml
    ./searxng.sh engines --dry-run  # probe and report, change nothing
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = pathlib.Path(__file__).resolve().parent
SETTINGS = HERE.parent / "core-config" / "settings.yml"
# Raw probe output, machine-local and gitignored: lets --reuse re-render the block
# under a different policy without spending another pass over ~280 engines.
CACHE = HERE.parent / ".probe-cache.json"

BEGIN = "# >>> BEGIN generated engine list"
END = "# <<< END generated engine list"

# Three unrelated probes: an engine that answers none of them is either broken or so
# narrow that adding it to the fanout only costs a request.
PROBES = ("python", "linux", "music")

# Torrent trackers and shadow libraries. SearXNG ships them off, and a general web
# search tool has no business switching them on for everyone who clones this repo.
# They stay loadable: query them by name (`--searxng-engines nyaa`) or pass
# --include-restricted to put them in the default fanout.
RESTRICTED = frozenset(
    {
        "1337x",
        "annas archive",
        "bt4g",
        "btdigg",
        "kickass",
        "library genesis",
        "nyaa",
        "piratebay",
        "solidtorrents",
        "tokyotoshokan",
    }
)


def _get(base: str, path: str, params: dict | None = None, timeout: float = 45.0):
    url = f"{base.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def probe_one(base: str, engine: str) -> tuple[str, str, str]:
    """Return (engine, status, detail) where status is OK, EMPTY, or ERROR."""
    detail = "no response"
    for query in PROBES:
        try:
            payload = _get(base, "/search", {"q": query, "format": "json", "engines": engine})
        except Exception as exc:  # noqa: BLE001 - any transport failure is just a fail
            detail = f"{type(exc).__name__}: {exc}"
            continue
        unresponsive = payload.get("unresponsive_engines") or []
        if unresponsive:
            detail = "; ".join(
                " ".join(str(part) for part in item) if isinstance(item, list) else str(item)
                for item in unresponsive
            )
            continue
        hits = len(payload.get("results") or [])
        extras = len(payload.get("answers") or []) + len(payload.get("infoboxes") or [])
        if hits or extras:
            return engine, "OK", f"{hits} results, {extras} answers ({query})"
        detail = "0 results"
    return engine, ("EMPTY" if detail == "0 results" else "ERROR"), detail


def render_block(rows: list[tuple[str, str, str]], *, include_restricted: bool = False) -> str:
    """Render the YAML the generated section of settings.yml owns.

    The list is absolute, not a diff against the shipped defaults: every engine that
    answered gets ``disabled: false``, including the ones SearXNG already enables.
    Writing it as a diff looks tidier and is a trap, because the second run reads its
    own overrides back out of ``/config``, sees those engines as "already enabled", and
    drops them from the list. Restating them is a no-op and makes the generator
    idempotent. Engines that failed the probe get no entry at all, so an upstream
    engine having a bad day keeps whatever the defaults say.
    """
    restricted = frozenset() if include_restricted else RESTRICTED
    enable = sorted(e for e, status, _ in rows if status == "OK" and e not in restricted)
    skipped = sorted(
        (e, status, detail) for e, status, detail in rows if status != "OK" and e not in restricted
    )
    held_back = sorted(e for e, _, _ in rows if e in restricted)

    lines = [
        BEGIN,
        "# Regenerate with: ./docker/searxng/searxng.sh engines",
        "# Every engine below answered a live probe from this host, so it joins the",
        f"# default fanout. {len(enable)} engines.",
        "engines:",
    ]
    for name in enable:
        lines.append(f"  - name: {name}")
        lines.append("    disabled: false")
    lines.append("#")
    if held_back:
        lines.append(
            f"# Held back ({len(held_back)}): torrent trackers and shadow libraries, still"
        )
        lines.append("# queryable by name. Rerun with --include-restricted to enable them.")
        lines.append("#   " + ", ".join(held_back))
        lines.append("#")
    lines.append(
        f"# Left off ({len(skipped)}): probed and did not answer. An engine SearXNG enables"
    )
    lines.append("# upstream stays on regardless; SearXNG suspends it on its own when it fails.")
    for name, status, detail in skipped:
        lines.append(f"#   {name}: {status} - {detail[:90]}")
    lines.append(END)
    return "\n".join(lines)


def splice(text: str, block: str) -> str:
    start = text.find(BEGIN)
    end = text.find(END)
    if start == -1 or end == -1:
        raise SystemExit(f"markers {BEGIN!r} / {END!r} not found in {SETTINGS}")
    return text[:start] + block + text[end + len(END) :]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8888", help="SearXNG base URL")
    parser.add_argument("--dry-run", action="store_true", help="report only, do not write")
    parser.add_argument("--workers", type=int, default=6, help="concurrent probes")
    parser.add_argument(
        "--reuse",
        action="store_true",
        help=f"re-render from the last probe in {CACHE.name} instead of querying again",
    )
    parser.add_argument(
        "--include-restricted",
        action="store_true",
        help="also enable torrent trackers and shadow libraries",
    )
    args = parser.parse_args()

    if args.reuse:
        if not CACHE.exists():
            print(f"no cached probe at {CACHE}; run without --reuse first", file=sys.stderr)
            return 2
        cached = json.loads(CACHE.read_text())
        defaults = cached["defaults"]
        rows = [tuple(row) for row in cached["rows"]]
        print(f"reusing {len(rows)} probe results from {CACHE.name}")
    else:
        try:
            config = _get(args.url, "/config", timeout=15)
        except Exception as exc:  # noqa: BLE001
            print(f"cannot reach SearXNG at {args.url}: {exc}", file=sys.stderr)
            print("start it first:  ./docker/searxng/searxng.sh up", file=sys.stderr)
            return 2

        defaults = {e["name"]: bool(e.get("enabled")) for e in config["engines"]}
        names = sorted(defaults)
        print(f"SearXNG {config.get('version')} - probing {len(names)} engines, 3 queries each")

        rows = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(probe_one, args.url, name) for name in names]
            for i, future in enumerate(futures, 1):
                engine, status, detail = future.result()
                rows.append((engine, status, detail))
                print(f"[{i:3d}/{len(names)}] {status:5s} {engine:34s} {detail[:60]}", flush=True)
        CACHE.write_text(json.dumps({"defaults": defaults, "rows": rows}, indent=1))

    tally: dict[str, int] = {}
    for _, status, _ in rows:
        tally[status] = tally.get(status, 0) + 1
    already = sum(1 for name, on in defaults.items() if on)
    newly = sum(1 for e, s, _ in rows if s == "OK" and not defaults.get(e))
    print(f"\n{tally}")
    print(f"enabled upstream: {already} | answered and off upstream: {newly}")

    block = render_block(rows, include_restricted=args.include_restricted)
    if args.dry_run:
        print("\n--dry-run: settings.yml untouched")
        return 0

    SETTINGS.write_text(splice(SETTINGS.read_text(), block))
    print(f"\nwrote {SETTINGS}")
    print("restart to pick it up:  ./docker/searxng/searxng.sh restart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
