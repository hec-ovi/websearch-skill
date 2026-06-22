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
from .layer2_format import (
    FORMAT_CONTRACT_VERSION,
    FormatRequest,
    PageInput,
    ResultInput,
    SearchPageRequest,
    StoreConfig,
    build_format_pipeline,
    build_page_index,
)


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
    fp.add_argument(
        "--max-bytes",
        type=int,
        help="Transport guard only, not a content cap (default 10 MB).",
    )
    fp.add_argument(
        "--allow-private-hosts",
        action="store_true",
        help="Permit fetching private/loopback/metadata addresses (SSRF guard off).",
    )
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

    fetch_kwargs: dict[str, Any] = dict(
        url=args.url,
        tier_hint=args.tier,
        timeout_ms=args.timeout_ms,
        user_agent=args.user_agent,
        proxy=proxy,
        allow_private_hosts=args.allow_private_hosts,
        politeness={
            "per_host_delay_ms": args.per_host_delay_ms,
            "respect_robots": args.respect_robots,
        },
    )
    if args.max_bytes is not None:  # absent flag keeps the model's default transport guard
        fetch_kwargs["max_bytes"] = args.max_bytes
    try:
        request = FetchRequest(**fetch_kwargs)
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


def _add_open_command(sub: Any) -> None:
    op = sub.add_parser(
        "open",
        help="Fetch+extract one or more URLs and format them into one paginated, "
        "deduped, LLM-ready Markdown document (Layer 2A + 2B).",
        epilog=(
            "exit codes: 0 when at least one URL was fetched and formatted (per-URL "
            "fetch failures are surfaced as warnings); 1 when every URL failed or the "
            "request was invalid."
        ),
    )
    op.add_argument("urls", nargs="+", help="One or more http(s) URLs to open.")
    op.add_argument("--query", help="Optional label for the document header.")
    op.add_argument("--page", type=int, default=0, help="Zero-based page index.")
    op.add_argument("--page-size", type=int, default=5)
    op.add_argument(
        "--mode",
        choices=["auto", "index", "full"],
        default="auto",
        help="auto inlines full bodies when the page fits the token budget, else an "
        "index (preview + resolve id). index/full force the choice.",
    )
    op.add_argument("--body", choices=["highlights", "summary", "text"], default="highlights")
    op.add_argument(
        "--body-char-budget",
        type=int,
        default=4000,
        help="Soft budget for a rendered body in full mode (offload trigger, not a "
        "content cap; the full body stays in the sidecar and store).",
    )
    op.add_argument(
        "--no-truncate",
        action="store_true",
        help="Inline every full body with no resolver offload (body_char_budget off).",
    )
    op.add_argument(
        "--inline-token-budget",
        type=int,
        default=6000,
        help="auto mode renders full when the page's estimated tokens are at or below this.",
    )
    op.add_argument("--no-dedup", action="store_true", help="Disable near-duplicate folding.")
    op.add_argument(
        "--jaccard", type=float, default=0.9, help="MinHash near-dup threshold (0..1)."
    )
    op.add_argument(
        "--anthropic-blocks",
        action="store_true",
        help="Include the derived anthropic_search_result_blocks view in the sidecar.",
    )
    op.add_argument(
        "--search",
        metavar="QUERY",
        help="After formatting, BM25-search passages across the opened pages and show hits.",
    )
    op.add_argument("--top-k", type=int, default=10, help="Max passages for --search.")
    op.add_argument(
        "--persist-path", help="Persist the page index to this file (default: in-memory)."
    )
    op.add_argument(
        "--tier",
        choices=["auto", "http", "browser", "stealth"],
        default="auto",
        help="Fetch tier for each URL.",
    )
    op.add_argument("--timeout-ms", type=int, default=20000)
    op.add_argument(
        "--allow-private-hosts",
        action="store_true",
        help="Permit private/loopback/metadata addresses (SSRF guard off).",
    )
    op.add_argument("--quiet", action="store_true", help="Print only the Markdown document.")
    op.add_argument("--json", action="store_true", help="Emit the raw JSON Envelope.")


def _extract_to_result_input(payload: dict) -> ResultInput:
    """Map a Layer 2A ExtractPayload onto a vendor-neutral Layer 2B ResultInput."""
    src = payload.get("source") or {}
    res = payload.get("result") or {}
    return ResultInput(
        url=src.get("final_url") or src.get("url"),
        title=res.get("title"),
        published_date=res.get("date"),
        author=res.get("byline"),
        lang=res.get("language"),
        page_type=res.get("page_type"),
        quality_score=res.get("quality_score"),
        body_markdown=res.get("content_markdown") or "",
        # No relevance score for a direct open: preserve the user's URL order.
        score=None,
    )


def _cmd_open(args: argparse.Namespace) -> int:
    for u in args.urls:
        if not u.startswith(("http://", "https://")):
            return _emit_error(
                FORMAT_CONTRACT_VERSION,
                code=errors.INVALID_REQUEST,
                message=f"url must be an absolute http(s) URL: {u}",
                layer="format",
                as_json=args.json,
            )

    pipeline = build_pipeline()
    results: list[ResultInput] = []
    pages: list[PageInput] = []
    warnings: list[str] = []
    for u in args.urls:
        try:
            request = FetchRequest(
                url=u,
                tier_hint=args.tier,
                timeout_ms=args.timeout_ms,
                allow_private_hosts=args.allow_private_hosts,
            )
        except ValidationError:
            warnings.append(f"{u}: invalid fetch request; skipped.")
            continue
        env = pipeline.run(request)
        if not env.ok:
            err = env.error
            warnings.append(f"{u}: {err.code}: {err.message}" if err else f"{u}: fetch failed.")
            continue
        payload = env.data
        ri = _extract_to_result_input(payload)
        results.append(ri)
        pages.append(
            PageInput(url=ri.url, markdown=ri.body_markdown or "", title=ri.title)
        )

    if not results:
        return _emit_error(
            FORMAT_CONTRACT_VERSION,
            code=errors.FETCH_FAILED,
            message=f"all {len(args.urls)} url(s) failed to fetch; nothing to format.",
            layer="format",
            as_json=args.json,
        )

    format_request = FormatRequest(
        query=args.query,
        results=results,
        page=args.page,
        page_size=args.page_size,
        mode=args.mode,
        body=args.body,
        body_char_budget=None if args.no_truncate else args.body_char_budget,
        inline_token_budget=args.inline_token_budget,
        include_anthropic_blocks=args.anthropic_blocks,
        dedup={
            "enabled": not args.no_dedup,
            "method": "both",
            "jaccard_threshold": args.jaccard,
            "num_perm": 128,
            "shingle_size": 4,
        },
    )
    envelope = build_format_pipeline().run(format_request)
    payload = envelope.model_dump(mode="json")
    payload["data"]["warnings"] = (payload["data"].get("warnings") or []) + warnings

    # Index the opened pages so resolve-by-id and --search work over this corpus. The
    # format document is already built, so any store/search failure degrades to a
    # warning rather than discarding the work or leaking a traceback.
    search_result = None
    try:
        store = build_page_index(StoreConfig(persist_path=args.persist_path))
        store.add(pages)
        if args.search:
            search_result = store.search(
                SearchPageRequest(query=args.search, top_k=args.top_k)
            ).model_dump(mode="json")
            payload["meta"]["page_search"] = search_result
    except Exception as exc:  # never lose the formatted document to an index error
        payload["data"]["warnings"].append(
            f"page index/search failed: {type(exc).__name__}: {exc}"
        )

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_open_human(payload, search_result, quiet=args.quiet)
    return 0 if envelope.ok else 1


def _print_open_human(env: dict, search: dict | None, quiet: bool = False) -> None:
    data = env.get("data") or {}
    print(data.get("markdown") or "(no document)")
    if quiet:
        return
    for w in data.get("warnings") or []:
        print(f"\n[warning]   {w}", file=sys.stderr)
    if search is not None:
        passages = search.get("passages") or []
        print(
            f"\n# Passage matches ({len(passages)} of {search.get('total')}, "
            f"backend {search.get('backend')})",
            file=sys.stderr,
        )
        for p in passages:
            head = (p.get("text") or "").strip().replace("\n", " ")[:160]
            print(f"- [{p.get('score'):.4f}] {p.get('url')} #{p.get('ordinal')}: {head}",
                  file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="websearch",
        description="Open-source multi-engine web search for AI agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _add_search_command(sub)
    _add_fetch_command(sub)
    _add_open_command(sub)
    args = parser.parse_args(argv)
    if args.command == "search":
        return _cmd_search(args)
    if args.command == "fetch":
        return _cmd_fetch(args)
    if args.command == "open":
        return _cmd_open(args)
    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; argparse.error exits
