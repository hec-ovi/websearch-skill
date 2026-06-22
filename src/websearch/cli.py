"""Layer-3 CLI: the primary agent-facing entry point.

For this slice it exposes ``websearch search``. The optional MCP adapter and the
other subcommands (fetch, open/resolve) arrive with their layers, over the same
Envelope payloads. ``--json`` emits the raw Envelope (the contract surface); the
default is a compact human view. Exit code is 0 on success, 1 on an error Envelope.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .layer1_search import SearchRequest, build_router


def _add_search_command(sub: Any) -> None:
    sp = sub.add_parser("search", help="Search the web across engines (Layer 1).")
    sp.add_argument("query", help="The search query.")
    sp.add_argument("--count", type=int, default=10, help="Results requested per engine.")
    sp.add_argument("--engines", help="Comma-separated engine names (default: all enabled).")
    sp.add_argument("--language", help="ISO 639-1 language, e.g. en.")
    sp.add_argument("--country", help="ISO 3166-1 alpha-2 country, e.g. us.")
    sp.add_argument("--safesearch", choices=["off", "moderate", "strict"], default="moderate")
    sp.add_argument("--freshness", choices=["any", "day", "week", "month", "year"], default="any")
    sp.add_argument("--max-results", type=int, default=20, help="Result cap after fusion.")
    sp.add_argument("--include-site", action="append", default=[], metavar="DOMAIN")
    sp.add_argument("--exclude-site", action="append", default=[], metavar="DOMAIN")
    sp.add_argument(
        "--searxng-url",
        default=os.environ.get("WEBSEARCH_SEARXNG_URL"),
        help="SearXNG base URL (or set WEBSEARCH_SEARXNG_URL).",
    )
    sp.add_argument("--no-ddgs", action="store_true", help="Disable the ddgs fallback engine.")
    sp.add_argument("--json", action="store_true", help="Emit the raw JSON Envelope.")


def _cmd_search(args: argparse.Namespace) -> int:
    engines = [e.strip() for e in args.engines.split(",") if e.strip()] if args.engines else None
    request = SearchRequest(
        query=args.query,
        count=args.count,
        language=args.language,
        country=args.country,
        safesearch=args.safesearch,
        freshness=args.freshness,
        max_total_results=args.max_results,
        include_sites=args.include_site,
        exclude_sites=args.exclude_site,
        engines=engines,
    )
    router = build_router(searxng_url=args.searxng_url, enable_ddgs=not args.no_ddgs)
    envelope = router.search(request)
    payload = envelope.model_dump(mode="json")

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_human(payload)
    return 0 if envelope.ok else 1


def _print_human(env: dict) -> None:
    if not env.get("ok"):
        err = env.get("error") or {}
        print(f"error: {err.get('code')}: {err.get('message')}", file=sys.stderr)
        return
    data = env.get("data") or {}
    results = data.get("results", [])
    print(f"{len(results)} result(s) for: {data.get('query')}")
    for i, r in enumerate(results, 1):
        engines = ",".join(s["engine"] for s in r.get("sources", []))
        print(f"\n{i}. {r.get('title')}")
        print(f"   {r.get('url')}")
        print(f"   score={r.get('fused_score'):.4f}  engines=[{engines}]")
        snippet = (r.get("snippet") or "").strip()
        if snippet:
            print(f"   {snippet[:200]}")
    for w in data.get("warnings", []):
        print(f"\n[warning] {w}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="websearch",
        description="Open-source multi-engine web search for AI agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _add_search_command(sub)
    args = parser.parse_args(argv)
    if args.command == "search":
        return _cmd_search(args)
    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; argparse.error exits
