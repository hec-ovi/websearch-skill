"""The websearch CLI: every layer's commands behind one entry point.

Per-layer commands (``search``, ``fetch``, ``open``), the consolidated Layer-3 agent
face (``web-search``, ``web-fetch``, ``web-open``), the keyless ``arxiv``/``github``
tools, ``doctor`` (the installation self-test), and ``mcp`` (the FastMCP stdio server).
``--json`` emits the raw Envelope (the contract surface); the default is a compact human
view. Exit code is 0 on success, 1 on an error Envelope. Each command imports its own
layer inside its handler, so ``arxiv`` or ``github`` never pays the search/fetch stack's
import cost.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from pydantic import ValidationError

from . import errors
from .envelope import ENVELOPE_CONTRACT_VERSION, error_envelope
from .proxy import ProxyConfigError, as_fetch_proxy, resolve_proxy

# Mirrors layer3_agentio.DEFAULT_PAGE_SIZE_TOKENS without importing the layer at module
# import time (tests pin the two in lockstep).
_DEFAULT_PAGE_SIZE_TOKENS = 4000

# The per-layer builders are lazy module attributes (PEP 562): each command imports only
# its own layer at dispatch time, while tests keep monkeypatching e.g. ``cli.build_router``
# to inject a fake.
_LAZY_BUILDERS = {
    "build_router": ".layer1_search",
    "build_agent_io": ".layer3_agentio",
    "build_arxiv_tool": ".tool_arxiv",
    "build_github_tool": ".tool_github",
    "build_doctor": ".doctor",
}


def __getattr__(name: str):
    layer = _LAZY_BUILDERS.get(name)
    if layer is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(layer, __package__), name)


def _builder(name: str):
    """The active builder: a monkeypatched module attribute wins over the lazy import."""
    return globals().get(name) or __getattr__(name)


def _add_proxy_arg(p: Any) -> None:
    p.add_argument(
        "--proxy",
        help="Egress proxy: a URL (socks5h://user:pass@host:1080, http://...), "
        "'nordvpn' (SOCKS5 from NORDVPN_USER/NORDVPN_PASS service credentials), or "
        "'off'. Default: the WEBSEARCH_PROXY environment variable.",
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
    sp.add_argument(
        "--max-results", type=int, default=20, help="Result cap after fusion; 0 = no cap."
    )
    sp.add_argument("--include-site", action="append", default=[], metavar="DOMAIN")
    sp.add_argument("--exclude-site", action="append", default=[], metavar="DOMAIN")
    sp.add_argument(
        "--searxng-url",
        default=os.environ.get("WEBSEARCH_SEARXNG_URL"),
        help="SearXNG base URL (or set WEBSEARCH_SEARXNG_URL).",
    )
    sp.add_argument("--no-ddgs", action="store_true", help="Disable the ddgs fallback engine.")
    _add_proxy_arg(sp)
    sp.add_argument(
        "--ddgs-backends",
        help="Which keyless engines ddgs queries, comma-separated (default auto = all). "
        "Engines: google, brave, duckduckgo, yandex, yahoo, startpage, mojeek, "
        "wikipedia, grokipedia. `websearch doctor --check engines` reports which of "
        "them answer from here. Example: --ddgs-backends google,brave,mojeek",
    )
    sp.add_argument("--json", action="store_true", help="Emit the raw JSON Envelope.")


def _cmd_search(args: argparse.Namespace) -> int:
    from .layer1_search import SEARCH_CONTRACT_VERSION, SearchRequest

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

    router = _builder("build_router")(
        searxng_url=args.searxng_url,
        enable_ddgs=not args.no_ddgs,
        ddgs_backend=args.ddgs_backends or "auto",
        proxy=resolve_proxy(args.proxy),
    )
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
    _add_proxy_arg(fp)
    fp.add_argument(
        "--max-bytes",
        type=int,
        help="Transport guard only, not a content cap (default 10 MB); 0 = no guard.",
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
    from .layer2_extract import EXTRACT_CONTRACT_VERSION, FetchRequest, build_pipeline

    if not args.url.startswith(("http://", "https://")):
        return _emit_error(
            EXTRACT_CONTRACT_VERSION,
            code=errors.INVALID_REQUEST,
            message="url must be an absolute http(s) URL.",
            layer="extract",
            as_json=args.json,
        )

    proxy = as_fetch_proxy(resolve_proxy(args.proxy))

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
    op.add_argument("--jaccard", type=float, default=0.9, help="MinHash near-dup threshold (0..1).")
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
    _add_proxy_arg(op)
    op.add_argument("--quiet", action="store_true", help="Print only the Markdown document.")
    op.add_argument("--json", action="store_true", help="Emit the raw JSON Envelope.")


def _extract_to_result_input(payload: dict):
    """Map a Layer 2A ExtractPayload onto a vendor-neutral Layer 2B ResultInput."""
    from .layer2_format import ResultInput

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
    from .layer2_extract import FetchRequest, build_pipeline
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

    for u in args.urls:
        if not u.startswith(("http://", "https://")):
            return _emit_error(
                FORMAT_CONTRACT_VERSION,
                code=errors.INVALID_REQUEST,
                message=f"url must be an absolute http(s) URL: {u}",
                layer="format",
                as_json=args.json,
            )

    pipeline = build_pipeline(proxy=resolve_proxy(args.proxy))
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
        pages.append(PageInput(url=ri.url, markdown=ri.body_markdown or "", title=ri.title))

    if not results:
        return _emit_error(
            FORMAT_CONTRACT_VERSION,
            code=errors.FETCH_FAILED,
            message=f"all {len(args.urls)} url(s) failed to fetch; nothing to format.",
            layer="format",
            as_json=args.json,
        )

    try:
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
    except ValidationError as exc:
        return _emit_error(
            FORMAT_CONTRACT_VERSION,
            code=errors.INVALID_REQUEST,
            message=f"invalid format request: {exc}",
            layer="format",
            as_json=args.json,
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
        payload["data"]["warnings"].append(f"page index/search failed: {type(exc).__name__}: {exc}")

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
            print(
                f"- [{p.get('score'):.4f}] {p.get('url')} #{p.get('ordinal')}: {head}",
                file=sys.stderr,
            )


# --- Layer 3: the consolidated agent face (web-search / web-fetch / web-open / mcp) --
#
# These emit the agentio Envelope: fenced, paginated, handle-keyed. They are what a
# SKILL.md or the MCP tools drive. The bare search/fetch/open commands above stay as the
# lower-level per-layer surfaces (debugging, composition, raw contracts).


def _add_websearch_command(sub: Any) -> None:
    wp = sub.add_parser(
        "web-search",
        help="Agent-facing web search (Layer 3): ranked results with handles, over the "
        "agentio Envelope. `search` is the lower-level Layer-1 surface.",
    )
    wp.add_argument("query")
    wp.add_argument("--max-results", type=int, default=8, help="0 = no cap.")
    wp.add_argument("--detail", choices=["concise", "detailed"], default="concise")
    wp.add_argument("--language", help="ISO 639-1, e.g. en.")
    wp.add_argument("--country", help="ISO 3166-1 alpha-2, e.g. us.")
    wp.add_argument("--freshness", choices=["any", "day", "week", "month", "year"], default="any")
    wp.add_argument("--safesearch", choices=["off", "moderate", "strict"], default="moderate")
    wp.add_argument("--site", help="Restrict to one host.")
    wp.add_argument("--offset", type=int, default=0)
    # Self-host opt-in only; the keyless ddgs metasearch (many engines at once) is the
    # zero-config default and needs no engine flags. Engine/backend selection lives on the
    # lower-level `search` command for debugging, not on this plug-and-play agent surface.
    wp.add_argument("--searxng-url", default=os.environ.get("WEBSEARCH_SEARXNG_URL"))
    _add_proxy_arg(wp)
    wp.add_argument("--json", action="store_true", help="Emit the raw agentio Envelope.")


def _cmd_websearch(args: argparse.Namespace) -> int:
    from .layer3_agentio import AGENTIO_CONTRACT_VERSION, AgentSearchRequest

    try:
        req = AgentSearchRequest(
            query=args.query,
            max_results=args.max_results,
            detail=args.detail,
            country=args.country,
            language=args.language,
            freshness=args.freshness,
            safesearch=args.safesearch,
            site=args.site,
            offset=args.offset,
        )
    except ValidationError as exc:
        return _emit_error(
            AGENTIO_CONTRACT_VERSION,
            code=errors.INVALID_REQUEST,
            message=f"invalid web-search request: {exc}",
            layer="agentio",
            as_json=args.json,
        )
    aio = _builder("build_agent_io")(searxng_url=args.searxng_url, proxy=resolve_proxy(args.proxy))
    env = aio.web_search(req)
    payload = env.model_dump(mode="json")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_agent_search_human(payload)
    return 0 if env.ok else 1


def _print_agent_search_human(env: dict) -> None:
    if not env.get("ok"):
        err = env.get("error") or {}
        print(f"error: {err.get('code')}: {err.get('message')}", file=sys.stderr)
        return
    data = env.get("data") or {}
    results = data.get("results", [])
    print(f"{len(results)} result(s) for: {data.get('query')}")
    for r in results:
        print(f"\n{r.get('rank')}. {r.get('title')}")
        print(f"   {r.get('url')}")
        meta = []
        if r.get("score") is not None:
            meta.append(f"score={r['score']:.4f}")
        if r.get("engines"):
            meta.append("engines=[" + ",".join(r["engines"]) + "]")
        if meta:
            print("   " + "  ".join(meta))
        print(f"   handle: {r.get('handle')}")
        snippet = (r.get("snippet") or "").strip()
        if snippet:
            print(f"   {snippet[:200]}")
    for w in data.get("warnings", []):
        print(f"\n[warning] {w}", file=sys.stderr)


def _add_webfetch_command(sub: Any) -> None:
    fp = sub.add_parser(
        "web-fetch",
        help="Agent-facing fetch + read (Layer 3): clean Markdown fenced as untrusted and "
        "paginated by token budget, over the agentio Envelope.",
    )
    fp.add_argument("urls", nargs="+", help="One or more http(s) URLs.")
    fp.add_argument("--page", type=int, default=1, help="1-based page over the token pagination.")
    fp.add_argument(
        "--page-size-tokens",
        type=int,
        default=_DEFAULT_PAGE_SIZE_TOKENS,
        help="Per-page token budget; 0 = the whole document as one page.",
    )
    fp.add_argument("--tier", choices=["auto", "http", "browser", "stealth"], default="auto")
    fp.add_argument(
        "--datamark", action="store_true", help="Interleave a marker between words in the fence."
    )
    fp.add_argument("--timeout-ms", type=int, default=20000)
    fp.add_argument("--allow-private-hosts", action="store_true")
    fp.add_argument(
        "--persist-path", help="Persist the page index so web-open resolves handles across runs."
    )
    _add_proxy_arg(fp)
    fp.add_argument("--quiet", action="store_true", help="Print only the fenced content.")
    fp.add_argument("--json", action="store_true", help="Emit the raw agentio Envelope.")


def _cmd_webfetch(args: argparse.Namespace) -> int:
    from .layer2_format import StoreConfig
    from .layer3_agentio import AGENTIO_CONTRACT_VERSION, AgentFetchRequest

    for u in args.urls:
        if not u.startswith(("http://", "https://")):
            return _emit_error(
                AGENTIO_CONTRACT_VERSION,
                code=errors.INVALID_REQUEST,
                message=f"url must be an absolute http(s) URL: {u}",
                layer="agentio",
                as_json=args.json,
            )
    # web-fetch is multi-URL so it calls web_fetch_many with raw kwargs; validate the shared
    # paging params through the request model here (page>=1, page_size_tokens>=0, etc.) so a
    # bad --page yields a clean invalid_request rather than crashing deep in the facade.
    try:
        AgentFetchRequest(
            url=args.urls[0],
            page=args.page,
            page_size_tokens=args.page_size_tokens,
            tier=args.tier,
            timeout_ms=args.timeout_ms,
            allow_private_hosts=args.allow_private_hosts,
            datamark=args.datamark,
        )
    except ValidationError as exc:
        return _emit_error(
            AGENTIO_CONTRACT_VERSION,
            code=errors.INVALID_REQUEST,
            message=f"invalid web-fetch request: {exc}",
            layer="agentio",
            as_json=args.json,
        )
    aio = _builder("build_agent_io")(
        enable_ddgs=False,
        store_config=StoreConfig(persist_path=args.persist_path),
        proxy=resolve_proxy(args.proxy),
    )
    env = aio.web_fetch_many(
        args.urls,
        page=args.page,
        page_size_tokens=args.page_size_tokens,
        tier=args.tier,
        timeout_ms=args.timeout_ms,
        allow_private_hosts=args.allow_private_hosts,
        datamark=args.datamark,
    )
    payload = env.model_dump(mode="json")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_agent_pages_human(
            payload,
            quiet=args.quiet,
            persist_path=args.persist_path,
            page_size_tokens=args.page_size_tokens,
        )
    return 0 if env.ok else 1


def _add_webopen_command(sub: Any) -> None:
    op = sub.add_parser(
        "web-open",
        help="Paginate an already-fetched page from the store by handle (Layer 3); never "
        "re-fetches. Needs --persist-path matching the web-fetch run (or the same process).",
    )
    op.add_argument("handle", help="A handle (site~shorthash) or the page URL.")
    op.add_argument("--page", type=int, default=1)
    op.add_argument(
        "--page-size-tokens",
        type=int,
        default=_DEFAULT_PAGE_SIZE_TOKENS,
        help="Per-page token budget; 0 = the whole document as one page.",
    )
    op.add_argument("--datamark", action="store_true")
    op.add_argument("--persist-path", help="The page-index file written by web-fetch.")
    op.add_argument("--quiet", action="store_true", help="Print only the fenced content.")
    op.add_argument("--json", action="store_true", help="Emit the raw agentio Envelope.")


def _cmd_webopen(args: argparse.Namespace) -> int:
    from .layer2_format import StoreConfig
    from .layer3_agentio import AGENTIO_CONTRACT_VERSION, AgentOpenRequest

    try:
        req = AgentOpenRequest(
            handle=args.handle,
            page=args.page,
            page_size_tokens=args.page_size_tokens,
            datamark=args.datamark,
        )
    except ValidationError:
        return _emit_error(
            AGENTIO_CONTRACT_VERSION,
            code=errors.INVALID_REQUEST,
            message="invalid web-open request.",
            layer="agentio",
            as_json=args.json,
        )
    aio = _builder("build_agent_io")(
        enable_ddgs=False, store_config=StoreConfig(persist_path=args.persist_path)
    )
    env = aio.web_open(req)
    payload = env.model_dump(mode="json")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_agent_pages_human(
            payload,
            quiet=args.quiet,
            persist_path=args.persist_path,
            page_size_tokens=args.page_size_tokens,
        )
    return 0 if env.ok else 1


def _more_hint(
    p: dict, persist_path: str | None, page_size_tokens: int = _DEFAULT_PAGE_SIZE_TOKENS
) -> str:
    """The copy-paste-correct next-page command. web-open resolves a handle only against a
    shared store, so it is suggested only with the --persist-path the user already passed;
    otherwise suggest re-running web-fetch on the URL (always works, re-fetches). A
    non-default page size is carried over, else the suggested command would re-paginate
    with different geometry and silently skip content."""
    nxt = (p.get("page") or 1) + 1
    size = ""
    if page_size_tokens != _DEFAULT_PAGE_SIZE_TOKENS:
        size = f" --page-size-tokens {page_size_tokens}"
    if persist_path:
        return (
            f"   (more: web-open {p.get('handle')} --page {nxt}{size}"
            f" --persist-path {persist_path})"
        )
    return f'   (more: web-fetch "{p.get("url")}" --page {nxt}{size})'


def _print_agent_pages_human(
    env: dict,
    quiet: bool = False,
    persist_path: str | None = None,
    page_size_tokens: int = _DEFAULT_PAGE_SIZE_TOKENS,
) -> None:
    if not env.get("ok"):
        err = env.get("error") or {}
        print(f"error: {err.get('code')}: {err.get('message')}", file=sys.stderr)
        return
    data = env.get("data") or {}
    pages = data.get("pages", [])
    for i, p in enumerate(pages):
        if not quiet:
            if i:
                print()
            print(f"# {p.get('title') or '(untitled)'}")
            print(f"url:    {p.get('url')}")
            location = f"page {p.get('page')} of {p.get('total_pages')}"
            more = _more_hint(p, persist_path, page_size_tokens) if p.get("has_more") else ""
            print(f"handle: {p.get('handle')}   {location}   source={p.get('source')}{more}")
            if p.get("blocked"):
                print(f"[blocked] {p.get('block_reason')}", file=sys.stderr)
            for w in p.get("warnings", []):
                print(f"[warning] {w}", file=sys.stderr)
            print()
        print(p.get("content") or "(no content)")
    for w in data.get("warnings", []):
        print(f"\n[warning] {w}", file=sys.stderr)


# --- Extra keyless tools: arxiv (and github), standalone over the same Envelope ------


def _add_arxiv_command(sub: Any) -> None:
    ap = sub.add_parser(
        "arxiv",
        help="Search arXiv papers (keyless, no API key). Structured paper metadata + "
        "abstract/PDF links, which general web search does not give you.",
    )
    ap.add_argument("query")
    ap.add_argument(
        "--field",
        choices=["all", "title", "author", "abstract"],
        default="all",
        help="Which arXiv field to match.",
    )
    ap.add_argument(
        "--max-results",
        type=int,
        default=10,
        help="Up to 2000 (the arXiv API per-request max); 0 = that maximum.",
    )
    ap.add_argument("--start", type=int, default=0, help="0-based offset for paging.")
    ap.add_argument(
        "--sort-by",
        choices=["relevance", "lastUpdatedDate", "submittedDate"],
        default="relevance",
    )
    ap.add_argument("--sort-order", choices=["ascending", "descending"], default="descending")
    _add_proxy_arg(ap)
    ap.add_argument("--json", action="store_true", help="Emit the raw JSON Envelope.")


def _cmd_arxiv(args: argparse.Namespace) -> int:
    from .tool_arxiv import ARXIV_CONTRACT_VERSION, ArxivSearchRequest

    try:
        req = ArxivSearchRequest(
            query=args.query,
            field=args.field,
            max_results=args.max_results,
            start=args.start,
            sort_by=args.sort_by,
            sort_order=args.sort_order,
        )
    except ValidationError:
        return _emit_error(
            ARXIV_CONTRACT_VERSION,
            code=errors.INVALID_REQUEST,
            message="invalid arxiv request (check --max-results is 0..2000).",
            layer="arxiv",
            as_json=args.json,
        )
    env = _builder("build_arxiv_tool")(proxy=resolve_proxy(args.proxy)).search(req)
    payload = env.model_dump(mode="json")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_arxiv_human(payload)
    return 0 if env.ok else 1


def _print_arxiv_human(env: dict) -> None:
    if not env.get("ok"):
        err = env.get("error") or {}
        print(f"error: {err.get('code')}: {err.get('message')}", file=sys.stderr)
        return
    data = env.get("data") or {}
    papers = data.get("papers", [])
    total = data.get("total_results")
    head = f"{len(papers)} paper(s)" + (f" of ~{total}" if total is not None else "")
    print(f"{head} for: {data.get('query')}")
    for i, p in enumerate(papers, 1):
        authors = list(p.get("authors", []))
        shown = ", ".join(authors[:4]) + (", et al." if len(authors) > 4 else "")
        print(f"\n{i}. {p.get('title')}")
        print(f"   {p.get('abs_url')}")
        meta = [x for x in (p.get("primary_category"), (p.get("published") or "")[:10]) if x]
        if meta:
            print("   " + "  ".join(meta))
        if shown:
            print(f"   {shown}")
        summary = " ".join((p.get("summary") or "").split())
        if summary:
            print(f"   {summary[:240]}")
    for w in data.get("warnings", []):
        print(f"\n[warning] {w}", file=sys.stderr)


def _add_github_command(sub: Any) -> None:
    gp = sub.add_parser(
        "github",
        help="Search GitHub repositories (keyless, no token). Typed fields (stars, "
        "language, topics) you can sort on, which general web search cannot. "
        "Unauthenticated search is about 10 requests/min.",
    )
    gp.add_argument("query")
    gp.add_argument("--language", help="Filter to a language (appended as language:X).")
    gp.add_argument(
        "--sort",
        choices=["best-match", "stars", "forks", "updated"],
        default="stars",
        help="best-match uses GitHub's relevance ranking.",
    )
    gp.add_argument("--order", choices=["asc", "desc"], default="desc")
    gp.add_argument(
        "--per-page", type=int, default=10, help="1..100 (GitHub's max); 0 = that maximum."
    )
    _add_proxy_arg(gp)
    gp.add_argument("--json", action="store_true", help="Emit the raw JSON Envelope.")


def _cmd_github(args: argparse.Namespace) -> int:
    from .tool_github import GITHUB_CONTRACT_VERSION, GithubSearchRequest

    try:
        req = GithubSearchRequest(
            query=args.query,
            language=args.language,
            sort=args.sort,
            order=args.order,
            per_page=args.per_page,
        )
    except ValidationError:
        return _emit_error(
            GITHUB_CONTRACT_VERSION,
            code=errors.INVALID_REQUEST,
            message="invalid github request (check --per-page is 0..100).",
            layer="github",
            as_json=args.json,
        )
    env = _builder("build_github_tool")(proxy=resolve_proxy(args.proxy)).search(req)
    payload = env.model_dump(mode="json")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_github_human(payload)
    return 0 if env.ok else 1


def _print_github_human(env: dict) -> None:
    if not env.get("ok"):
        err = env.get("error") or {}
        print(f"error: {err.get('code')}: {err.get('message')}", file=sys.stderr)
        return
    data = env.get("data") or {}
    repos = data.get("repos", [])
    total = data.get("total_count")
    head = f"{len(repos)} repo(s)" + (f" of {total}" if total is not None else "")
    print(f"{head} for: {data.get('query')}")
    for i, r in enumerate(repos, 1):
        print(f"\n{i}. {r.get('full_name')}")
        print(f"   {r.get('html_url')}")
        facts = [f"stars={r.get('stars')}"]
        if r.get("language"):
            facts.append(str(r["language"]))
        if r.get("updated_at"):
            facts.append("updated " + str(r["updated_at"])[:10])
        print("   " + "  ".join(facts))
        desc = (r.get("description") or "").strip()
        if desc:
            print(f"   {desc[:200]}")
    if data.get("incomplete_results"):
        print("\n[warning] GitHub reported incomplete_results (partial)", file=sys.stderr)
    for w in data.get("warnings", []):
        print(f"\n[warning] {w}", file=sys.stderr)


# --- doctor: does this installation actually work, layer by layer -------------------


def _add_doctor_command(sub: Any) -> None:
    dp = sub.add_parser(
        "doctor",
        help="Self-test the installation: the optional layers (VPN, egress proxy, "
        "SearXNG), every search engine, the extra tools, both fetch tiers, and MCP.",
        epilog=(
            "exit codes: 0 when nothing failed (warnings and skipped checks are fine), "
            "1 when at least one check failed. An optional layer that is off is skipped, "
            "never failed."
        ),
    )
    dp.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="NAME",
        help="Run only checks matching this name prefix or group, repeatable. Groups: "
        "runtime, egress, vpn, searxng, engines, tools, fetch, mcp. Examples: "
        "--check proxy, --check engines, --check engine:ddgs:google.",
    )
    dp.add_argument(
        "--quick",
        action="store_true",
        help="Skip the per-engine fanout, the extra tools, and the fetch tiers.",
    )
    dp.add_argument(
        "--baseline",
        action="store_true",
        help="Allow one direct request, outside the egress proxy, to learn this "
        "machine's own exit IP for comparison. Off by default: with a proxy on it is "
        "the only request that leaves the tunnel.",
    )
    # No argparse default: DoctorRequest owns it, so the contract default cannot drift
    # out of lockstep with a number copied into the parser.
    dp.add_argument("--timeout-ms", type=int, help="Per-check network timeout (default 15000).")
    dp.add_argument(
        "--query",
        default=None,
        help="The probe query sent to each engine (default: rust ownership).",
    )
    dp.add_argument("--fetch-url", default=None, help="The URL the fetch tiers are probed against.")
    dp.add_argument(
        "--vpn",
        help="VPN layer for this run: 'nordvpn', 'any', or 'off'. Default: the "
        "WEBSEARCH_VPN environment variable, which defaults to off.",
    )
    _add_proxy_arg(dp)
    dp.add_argument(
        "--searxng-url",
        help="SearXNG base URL for this run. Default: WEBSEARCH_SEARXNG_URL, unset = off.",
    )
    dp.add_argument("--json", action="store_true", help="Emit the raw JSON Envelope.")


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import DOCTOR_CONTRACT_VERSION, DoctorRequest

    fields: dict[str, Any] = {
        "checks": args.check or None,
        "quick": args.quick,
        "baseline": args.baseline,
    }
    for flag, field in (
        ("timeout_ms", "timeout_ms"),
        ("query", "query"),
        ("fetch_url", "fetch_url"),
    ):
        value = getattr(args, flag)
        if value:  # an omitted flag keeps DoctorRequest's contract default
            fields[field] = value
    try:
        request = DoctorRequest(**fields)
    except ValidationError as exc:
        return _emit_error(
            DOCTOR_CONTRACT_VERSION,
            code=errors.INVALID_REQUEST,
            message=f"invalid doctor request: {exc}",
            layer="doctor",
            as_json=args.json,
        )

    # A misconfigured proxy is a finding, not a crash: build_doctor records it as the
    # proxy layer's error and the run continues so you see everything else too.
    doctor = _builder("build_doctor")(vpn=args.vpn, proxy=args.proxy, searxng_url=args.searxng_url)
    env = doctor.run(request)
    payload = env.model_dump(mode="json")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_doctor_human(payload)
    data = payload.get("data") or {}
    summary = data.get("summary") or {}
    return 1 if summary.get("fail") else 0


_DOCTOR_GLYPH = {"ok": "ok  ", "warn": "warn", "fail": "FAIL", "skipped": "skip"}


def _print_doctor_human(env: dict) -> None:
    if not env.get("ok"):
        err = env.get("error") or {}
        print(f"error: {err.get('code')}: {err.get('message')}", file=sys.stderr)
        return
    data = env.get("data") or {}

    print("optional layers (every one off until you turn it on)")
    for name in ("vpn", "proxy", "searxng"):
        layer = (data.get("layers") or {}).get(name) or {}
        state = "on " if layer.get("enabled") else "off"
        value = layer.get("value") or ""
        source = f"  [{layer['source']}]" if layer.get("source") else ""
        print(f"  {name:<9} {state}  {value}{source}")
        if not layer.get("enabled") or layer.get("error"):
            print(f"  {'':<9}      {layer.get('error') or layer.get('detail')}")

    group = None
    for check in data.get("checks") or []:
        if check.get("group") != group:
            group = check.get("group")
            print(f"\n{group}")
        glyph = _DOCTOR_GLYPH.get(check.get("status"), "?   ")
        print(f"  {glyph}  {check.get('name'):<22} {check.get('summary')}")
        if check.get("hint") and check.get("status") in ("warn", "fail"):
            print(f"        {'':<22} -> {check['hint']}")

    summary = data.get("summary") or {}
    print(
        f"\n{summary.get('ok', 0)} ok, {summary.get('warn', 0)} warn, "
        f"{summary.get('fail', 0)} fail, {summary.get('skipped', 0)} skipped"
    )
    for w in data.get("warnings") or []:
        print(f"[note] {w}", file=sys.stderr)


def _add_searxng_command(sub: Any) -> None:
    sp = sub.add_parser(
        "searxng",
        help="Run a local SearXNG without Docker: clone it, install it, start it "
        "detached, and point this tool at it.",
        epilog=(
            "state lives in WEBSEARCH_SEARXNG_HOME (default: a 'searxng' directory beside "
            "WEBSEARCH_ENV_FILE, else the XDG cache). The first 'up' clones upstream and "
            "installs it, which needs git and network; later ones only start it."
        ),
    )
    sp.add_argument(
        "action",
        choices=["up", "status", "down", "url"],
        help="up: install if needed, then start and wire it in. status: where it is and "
        "whether it answers. down: stop it. url: print the base URL.",
    )
    sp.add_argument(
        "--reinstall",
        action="store_true",
        help="Delete the state directory and rebuild it from scratch (up only).",
    )
    sp.add_argument(
        "--ref",
        default=None,
        help="Git branch or tag of upstream SearXNG to clone (default: its default branch).",
    )
    sp.add_argument("--json", action="store_true", help="Emit the state as JSON.")


def _cmd_searxng(args: argparse.Namespace) -> int:
    from . import searxng_local as sx

    try:
        p = sx.port()
    except sx.SearxngError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    paths = sx.Paths(sx.home_dir())
    url = sx.base_url(p)

    if args.action == "url":
        print(url)
        return 0

    was_running = sx.is_healthy(url)
    # The first `up` clones a repository and builds a virtualenv, which is otherwise a
    # minute of silence. Said before the work, not after.
    if args.action == "up" and not was_running and (args.reinstall or not paths.granian.exists()):
        print(f"installing SearXNG into {paths.home} (clone + dependencies)...", flush=True)

    envelope = sx.control(
        sx.SearxngRequest(action=args.action, reinstall=args.reinstall, ref=args.ref)
    )
    if args.json:
        print(json.dumps(envelope.model_dump(mode="json"), indent=2))
        return 0 if envelope.ok else 1
    if not envelope.ok:
        print(f"error: {envelope.error.message}", file=sys.stderr)  # type: ignore[union-attr]
        return 1

    state = envelope.data
    if args.action == "down":
        print(f"stopped {url}" if was_running else f"nothing was running at {url}")
        return 0
    if args.action == "status":
        _print_searxng_status(state)
        return 0 if state["healthy"] else 1
    if was_running:
        print(f"already running at {state['url']}")
        _print_searxng_wiring(state["url"], state["wired"])
        return 0

    version = f"SearXNG {state['version']}, " if state.get("version") else ""
    active = f"{state['engines_active']} engines active, " if state.get("engines_active") else ""
    where = f" (pid {state['pid']}, log {state['log']})" if state.get("pid") else ""
    print(f"{version}{active}ready at {state['url']}{where}")
    _print_searxng_wiring(state["url"], state["wired"])
    return 0


def _print_searxng_status(state: dict[str, Any]) -> None:
    pid = state["pid"]
    process = f"running (pid {pid})" if pid else "not running"
    install = "present" if state["installed"] else "not installed yet"
    health = "answering" if state["healthy"] else "not answering"
    version, active, available = (
        state.get("version"),
        state.get("engines_active"),
        state.get("engines_available"),
    )
    print(f"home    {state['home']}")
    print(f"url     {state['url']}")
    print(f"install {install}")
    print(f"process {process}")
    print(f"health  {health}")
    if version:
        print(f"engines SearXNG {version}, {active} active of {available}")
    if state.get("log"):
        print(f"log     {state['log']}")
    if not state["healthy"]:
        print("\nstart it with: websearch searxng up")


def _print_searxng_wiring(url: str, wired: str | None) -> None:
    from .optional_layers import SEARXNG_ENV

    if wired:
        print(f"wired {SEARXNG_ENV} into {wired}")
    else:
        print(f"point this tool at it: export {SEARXNG_ENV}={url}")


def _add_mcp_command(sub: Any) -> None:
    sub.add_parser(
        "mcp",
        help="Start the FastMCP stdio server (web_search/web_fetch/web_open/arxiv_search/"
        "github_search/searxng_setup). fastmcp ships in the base install.",
    )


def _load_mcp_server():
    """Import the FastMCP server module (a base dependency; imported lazily to keep the
    other subcommands' startup free of the MCP import)."""
    from .layer3_agentio import mcp_server

    return mcp_server


def _cmd_mcp(args: argparse.Namespace) -> int:
    try:
        mcp_server = _load_mcp_server()
    except ImportError as exc:
        # fastmcp is a base dependency, so this only fires if the install was stripped.
        print(
            f"error: {errors.DEPENDENCY_MISSING}: the MCP server needs 'fastmcp', which ships "
            f"with this package. Reinstall it, e.g. pip install 'websearch-skill' "
            f"(or uv sync). [{exc}]",
            file=sys.stderr,
        )
        return 1
    mcp_server.run()  # blocks: stdio server until the client disconnects
    return 0


def _contract_version_for(command: str) -> str:
    """The contract version stamped on a cross-layer 'cli' error envelope: the active
    command's own contract, so a `websearch github` failure carries github@x.y.z rather
    than agent-io's version. Imported lazily to keep the startup cost per-command."""
    if command == "search":
        from .layer1_search import SEARCH_CONTRACT_VERSION

        return SEARCH_CONTRACT_VERSION
    if command == "fetch":
        from .layer2_extract import EXTRACT_CONTRACT_VERSION

        return EXTRACT_CONTRACT_VERSION
    if command == "open":
        from .layer2_format import FORMAT_CONTRACT_VERSION

        return FORMAT_CONTRACT_VERSION
    if command in ("web-search", "web-fetch", "web-open"):
        from .layer3_agentio import AGENTIO_CONTRACT_VERSION

        return AGENTIO_CONTRACT_VERSION
    if command == "arxiv":
        from .tool_arxiv import ARXIV_CONTRACT_VERSION

        return ARXIV_CONTRACT_VERSION
    if command == "github":
        from .tool_github import GITHUB_CONTRACT_VERSION

        return GITHUB_CONTRACT_VERSION
    if command == "doctor":
        from .doctor import DOCTOR_CONTRACT_VERSION

        return DOCTOR_CONTRACT_VERSION
    # mcp (serves several contracts) or an unknown command: the cross-cutting envelope.
    return ENVELOPE_CONTRACT_VERSION


def main(argv: list[str] | None = None) -> int:
    # Fetched/extracted content is frequently non-ASCII; under a C/POSIX locale a bare
    # print() would die with UnicodeEncodeError, so pin the output streams to UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    # Pick up a gitignored .env before anything reads the optional layers' variables.
    # An exported variable still wins, so this only fills in what the shell did not set.
    from .optional_layers import load_env_file

    load_env_file()

    parser = argparse.ArgumentParser(
        prog="websearch",
        description="Open-source multi-engine web search for AI agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _add_search_command(sub)
    _add_fetch_command(sub)
    _add_open_command(sub)
    _add_websearch_command(sub)
    _add_webfetch_command(sub)
    _add_webopen_command(sub)
    _add_arxiv_command(sub)
    _add_github_command(sub)
    _add_doctor_command(sub)
    _add_searxng_command(sub)
    _add_mcp_command(sub)
    args = parser.parse_args(argv)
    dispatch = {
        "search": _cmd_search,
        "fetch": _cmd_fetch,
        "open": _cmd_open,
        "web-search": _cmd_websearch,
        "web-fetch": _cmd_webfetch,
        "web-open": _cmd_webopen,
        "arxiv": _cmd_arxiv,
        "github": _cmd_github,
        "doctor": _cmd_doctor,
        "searxng": _cmd_searxng,
        "mcp": _cmd_mcp,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
        return 2  # unreachable; argparse.error exits
    try:
        return handler(args)
    except ProxyConfigError as exc:
        # A misconfigured proxy (e.g. 'nordvpn' without service credentials) is caller
        # configuration, not an internal failure.
        return _emit_error(
            _contract_version_for(args.command),
            code=errors.INVALID_REQUEST,
            message=str(exc),
            layer="cli",
            as_json=getattr(args, "json", False),
        )
    except Exception as exc:
        # Final backstop: a command must never surface a raw traceback to the user. Any
        # unexpected error becomes a clean internal_error (honoring --json when present).
        # SystemExit / KeyboardInterrupt are BaseExceptions and intentionally propagate.
        return _emit_error(
            _contract_version_for(args.command),
            code=errors.INTERNAL_ERROR,
            message=f"{args.command} failed unexpectedly: {type(exc).__name__}: {exc}",
            layer="cli",
            as_json=getattr(args, "json", False),
        )
