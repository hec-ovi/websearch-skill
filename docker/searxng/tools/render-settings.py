#!/usr/bin/env python3
"""Render the settings file the container actually mounts.

``core-config/settings.yml`` is tracked in git, so it cannot hold an egress proxy URL:
those carry credentials. This copies it to ``.runtime/settings.yml`` (gitignored, mode
0600) and swaps the proxy marker line for a real ``outgoing.proxies`` entry when a
proxy is configured.

Routing SearXNG's own egress matters because the searches leave from the container, not
from the client. Proxying the client's hop to a loopback SearXNG is impossible anyway
(see websearch.proxy.bypasses_proxy); this is the hop that actually reaches Google and
Bing, so it is the one worth anonymizing.

Stdlib only.

    render-settings.py                          # no proxy: a plain copy
    render-settings.py --proxy socks5h://...    # route SearXNG's engine traffic
"""

from __future__ import annotations

import argparse
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
SOURCE = HERE.parent / "core-config" / "settings.yml"
RUNTIME_DIR = HERE.parent / ".runtime"
TARGET = RUNTIME_DIR / "settings.yml"

MARKER = ">>> PROXY MARKER <<<"

# SearXNG maps a URL pattern to the proxy; "all://" covers every engine request.
# socks5h is understood natively (searx.network.client rewrites it for httpx_socks),
# so the engine hostnames resolve at the exit node rather than locally.
PROXY_BLOCK = '  proxies:\n    all://: "{url}"'


def render(source_text: str, proxy: str | None) -> str:
    """Swap the one marker line for a proxies entry, or drop the line."""
    out = []
    for line in source_text.splitlines(keepends=True):
        if MARKER not in line:
            out.append(line)
            continue
        if proxy:
            newline = "\n" if line.endswith("\n") else ""
            out.append(PROXY_BLOCK.format(url=proxy) + newline)
    return "".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", default="", help="egress proxy URL for SearXNG itself")
    parser.add_argument("--print-target", action="store_true", help="print the output path")
    args = parser.parse_args()

    if args.print_target:
        print(TARGET)
        return 0

    if MARKER not in SOURCE.read_text():
        print(f"proxy marker missing from {SOURCE}", file=sys.stderr)
        return 2

    RUNTIME_DIR.mkdir(exist_ok=True)
    # World-readable on purpose. The container drops every capability, so its root has
    # no CAP_DAC_OVERRIDE and cannot read a file owned by the host user under 0600: the
    # entrypoint fails with "is not a valid file" and SearXNG never starts. Keeping the
    # capability drop is worth more than hiding a URL from other local accounts, and the
    # same credentials already sit in the environment of any shell that set them.
    RUNTIME_DIR.chmod(0o755)
    rendered = render(SOURCE.read_text(), args.proxy or None)
    TARGET.write_text(rendered)
    TARGET.chmod(0o644)
    print(f"rendered {TARGET} ({'proxied egress' if args.proxy else 'direct egress'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
