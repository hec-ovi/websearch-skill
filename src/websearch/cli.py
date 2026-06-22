"""Layer-3 CLI: the primary agent-facing entry point.

It exposes ``websearch search`` (Layer 1) and ``websearch fetch`` (Layer 2A). The
optional MCP adapter and the open/resolve subcommand arrive with their layers, over
the same Envelope payloads. ``--json`` emits the raw Envelope (the contract surface);
the default is a compact human view. Exit code is 0 on success, 1 on an error Envelope.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from pydantic import ValidationError

from . import errors
from .envelope import error_envelope
from .layer1_search import SEARCH_CONTRACT_VERSION, SearchRequest, build_router
from .layer2_extract import EXTRACT_CONTRACT_VERSION, FetchRequest, build_pipeline


def _add_search_command(sub: Any) -> None:
    sp = sub.add_parser("search", help="Search the web across engines (Layer 1).")
    sp.add_argument("query", help="The search query.")
    sp.add_argument("--count", type=int, default=10, help="Results requested per engine.")
    sp.add_argument(
        "--engines",
        help="Comma-separated engine names to query (default: all configured). "
        "Built-in engines: ddgs, searxng.",
    )
    sp.add_argument("--language", help="ISO 639-1 language, e.g. en.")
    sp.add_argument("--country", help="ISO 3166-1 alpha-2 country, e.g. us.")
    sp.add_argument("--safesearch", choices=["off", "moderate", "strict"], default="moderate")
    sp.add_argument(
        "--freshness",
        choices=["any", "day", "week", "month", "year"],
        default="any",
        help="Recency filter (best-effort; each engine honors it differently).",
    )
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
    try:
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
    except ValidationError as exc:
        env = error_envelope(
            SEARCH_CONTRACT_VERSION,
            code=errors.INVALID_REQUEST,
            message=f"Invalid search request ({exc.error_count()} validation error(s)).",
            retriable=False,
            layer="search",
            backend=None,
        )
        if args.json:
            print(json.dumps(env.model_dump(mode="json"), indent=2, ensure_ascii=False))
        else:
            print(f"error: {errors.INVALID_REQUEST}: invalid search request", file=sys.stderr)
        return 1

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


def _add_fetch_command(sub: Any) -> None:
    fp = sub.add_parser(
        "fetch",
        help="Fetch a URL and extract clean Markdown + metadata (Layer 2A).",
        epilog=(
            "exit codes: 0 when a response was fetched and processed (inspect "
            "source.blocked and source.status in the output for content-level problems "
            "such as an anti-bot block or an HTTP 404); 1 on a request-level error "
            "(invalid URL, no response from any tier, or a missing dependency)."
        ),
    )
    fp.add_argument("url", help="The http(s) URL to fetch.")
    fp.add_argument(
        "--tier",
        choices=["auto", "http", "browser", "stealth"],
        default="auto",
        help="Fetch tier. auto escalates http -> impersonation on a block. "
        "browser/stealth are opt-in adapters (not in the base install).",
    )
    fp.add_argument("--timeout-ms", type=int, default=20000)
    fp.add_argument("--user-agent", help="Override the request User-Agent.")
    fp.add_argument("--proxy", help="Egress proxy URL (e.g. socks5h://127.0.0.1:1080).")
    fp.add_argument("--max-bytes", type=int, help="Transport guard only (not a content cap).")
    fp.add_argument(
        "--respect-robots", action="store_true", help="Honor robots.txt (off by default)."
    )
    fp.add_argument("--per-host-delay-ms", type=int, default=0)
    fp.add_argument(
        "--engine",
        choices=[
            "trafilatura",
            "resiliparse",
            "rs_trafilatura",
            "crawl4ai",
            "jina_readerlm",
            "auto",
        ],
        default="trafilatura",
        help="Extract engine. Only trafilatura ships in the base install; others are opt-in.",
    )
    fp.add_argument("--favor", choices=["precision", "recall", "balanced"], default="balanced")
    fp.add_argument("--output-format", choices=["markdown", "text", "json"], default="markdown")
    fp.add_argument("--no-tables", dest="tables", action="store_false", help="Drop tables.")
    fp.add_argument("--no-links", dest="links", action="store_false", help="Drop links.")
    fp.add_argument("--images", action="store_true", help="Keep images.")
    fp.add_argument("--comments", action="store_true", help="Keep comment sections.")
    fp.add_argument("--query", help="Relevance hint (best-effort; engine-dependent).")
    fp.add_argument(
        "--no-neural-fallback",
        dest="neural_fallback",
        action="store_false",
        help="Do not route low-quality pages to a neural/structured fallback.",
    )
    fp.add_argument(
        "--quiet",
        action="store_true",
        help="Print only the extracted body (no header/warnings), for piping.",
    )
    fp.add_argument("--json", action="store_true", help="Emit the raw JSON Envelope.")


def _cmd_fetch(args: argparse.Namespace) -> int:
    if not args.url.startswith(("http://", "https://")):
        return _emit_error(
            EXTRACT_CONTRACT_VERSION,
            code=errors.INVALID_REQUEST,
            message="url must be an absolute http(s) URL.",
            layer="extract",
            as_json=args.json,
        )

    proxy = None
    if args.proxy:
        ptype = "socks5" if args.proxy.lower().startswith("socks") else "http"
        proxy = {"url": args.proxy, "type": ptype}

    try:
        request = FetchRequest(
            url=args.url,
            tier_hint=args.tier,
            timeout_ms=args.timeout_ms,
            user_agent=args.user_agent,
            proxy=proxy,
            max_bytes=args.max_bytes,
            politeness={
                "per_host_delay_ms": args.per_host_delay_ms,
                "respect_robots": args.respect_robots,
            },
        )
    except ValidationError:
        return _emit_error(
            EXTRACT_CONTRACT_VERSION,
            code=errors.INVALID_REQUEST,
            message="invalid fetch request.",
            layer="extract",
            as_json=args.json,
        )

    overrides = {
        "engine": args.engine,
        "favor": args.favor,
        "output_format": args.output_format,
        "include_tables": args.tables,
        "include_links": args.links,
        "include_images": args.images,
        "include_comments": args.comments,
        "query": args.query,
        "neural_fallback": args.neural_fallback,
    }
    envelope = build_pipeline().run(request, extract_overrides=overrides)
    payload = envelope.model_dump(mode="json")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_fetch_human(payload, output_format=args.output_format, quiet=args.quiet)
    return 0 if envelope.ok else 1


def _select_body(res: dict, output_format: str) -> str:
    """The body the human view prints, honoring --output-format."""
    if output_format == "text":
        return res.get("content_text") or res.get("content_markdown") or ""
    if output_format == "json":
        return json.dumps(res, indent=2, ensure_ascii=False)
    return res.get("content_markdown") or ""


def _print_fetch_human(env: dict, output_format: str = "markdown", quiet: bool = False) -> None:
    if not env.get("ok"):
        err = env.get("error") or {}
        print(f"error: {err.get('code')}: {err.get('message')}", file=sys.stderr)
        return
    data = env.get("data") or {}
    src = data.get("source") or {}
    res = data.get("result") or {}
    body = _select_body(res, output_format)
    if not quiet:
        print(f"# {res.get('title') or '(untitled)'}")
        print(f"url:        {src.get('final_url') or src.get('url')}")
        print(
            f"fetched:    status={src.get('status')} via={src.get('fetched_via')} "
            f"type={res.get('page_type')} quality={res.get('quality_score'):.2f} "
            f"words={res.get('word_count')}"
        )
        if res.get("date") or res.get("byline"):
            print(f"meta:       {res.get('byline') or ''} {res.get('date') or ''}".rstrip())
        if src.get("blocked"):
            print(f"[blocked]   {src.get('block_reason')}", file=sys.stderr)
        for w in (data.get("warnings") or []) + (res.get("warnings") or []):
            print(f"[warning]   {w}", file=sys.stderr)
        print()
    print(body or "(no content extracted)")


def _emit_error(
    contract_version: str, *, code: str, message: str, layer: str, as_json: bool
) -> int:
    env = error_envelope(
        contract_version, code=code, message=message, retriable=False, layer=layer, backend=None
    )
    if as_json:
        print(json.dumps(env.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        print(f"error: {code}: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="websearch",
        description="Open-source multi-engine web search for AI agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _add_search_command(sub)
    _add_fetch_command(sub)
    args = parser.parse_args(argv)
    if args.command == "search":
        return _cmd_search(args)
    if args.command == "fetch":
        return _cmd_fetch(args)
    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; argparse.error exits
