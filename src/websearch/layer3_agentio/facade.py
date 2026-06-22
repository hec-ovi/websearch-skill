"""The Layer 3 facade: web_search / web_fetch / web_open over Layers 1, 2A, and 2B.

This is the consolidating agent surface. It owns no search, fetch, or extraction logic;
it reshapes the lower layers' Envelopes into the agent-io shape, fences fetched page
content as untrusted, paginates by token budget (losslessly), and indexes fetched pages
into the Layer 2B store so ``web_open`` can page through a document without re-fetching.

``handle`` (``site~shorthash``) is the only cross-layer key and is human-readable. The
store IS the handle registry: ``web_open`` resolves a handle by recomputing it over the
indexed URLs, so a persisted store makes handles resolvable across processes too. The
facade keeps the store for its lifetime, so one long-lived instance (the MCP session)
accumulates the pages it fetched and can open any of them.
"""

from __future__ import annotations

import hashlib

from pydantic import ValidationError

from .. import errors
from ..envelope import Envelope, error_envelope, ok_envelope
from ..layer1_search import SearchRequest, build_router
from ..layer2_extract import FetchRequest, build_pipeline
from ..layer2_format import PageInput, StoreConfig, build_page_index
from ..layer2_format.ids import site_of
from ..layer2_format.tokens import estimate_tokens
from .fence import fence_untrusted
from .models import (
    AGENTIO_CONTRACT_VERSION,
    DEFAULT_CHARS_PER_TOKEN,
    DEFAULT_PAGE_SIZE_TOKENS,
    AgentFetchPayload,
    AgentFetchRequest,
    AgentOpenRequest,
    AgentPage,
    AgentSearchHit,
    AgentSearchPayload,
    AgentSearchRequest,
)
from .pagination import paginate


def make_handle(url: str) -> str:
    """A human-readable, stable cross-layer key: ``site~shorthash`` (e.g. ``a.org~1f2e3d4c5e6f``).

    The short hash is 12 hex chars (48 bits): readable, and collision within a site's
    handful of fetched pages is negligible. ``web_open`` additionally fails closed on the
    astronomically-unlikely same-site collision rather than returning a wrong page.
    """
    site = site_of(url) or "web"
    short = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"{site}~{short}"


class AgentIO:
    """Holds a search router, a fetch/extract pipeline, and a page-index store."""

    def __init__(
        self,
        router,
        pipeline,
        *,
        store_config: StoreConfig | None = None,
        chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    ):
        self._router = router
        self._pipeline = pipeline
        self._store_config = store_config or StoreConfig()
        self._chars_per_token = chars_per_token
        self._store = None  # lazily built so a search-only run never touches the store

    @property
    def store(self):
        if self._store is None:
            self._store = build_page_index(self._store_config)
        return self._store

    # --- web_search ----------------------------------------------------------------

    def web_search(self, req: AgentSearchRequest) -> Envelope:
        # When a site is requested, push a `site:` operator into the query so the engine
        # itself restricts (a post-filter alone only keeps whatever the engine happened to
        # return, which is unreliable). Keep include_sites as a belt-and-suspenders filter.
        query = req.query
        site = req.site.strip().lstrip(".") if req.site else None
        if site and f"site:{site}".lower() not in query.lower():
            query = f"{query} site:{site}"
        try:
            search_req = SearchRequest(
                query=query,
                count=max(req.max_results, 10),
                offset=req.offset,
                country=req.country,
                language=req.language,
                safesearch=req.safesearch,
                freshness=req.freshness,
                max_total_results=req.max_results,
                include_sites=[site] if site else [],
                engines=req.engines,
            )
        except ValidationError:
            return error_envelope(
                AGENTIO_CONTRACT_VERSION,
                code=errors.INVALID_REQUEST,
                message="invalid search request.",
                retriable=False,
                layer="agentio",
                backend="search",
            )

        env = self._router.search(search_req)
        if not env.ok:
            return self._propagate_error(env, backend="search")

        data = env.data or {}
        detailed = req.detail == "detailed"
        hits: list[AgentSearchHit] = []
        for rank, r in enumerate(data.get("results", []), start=1):
            url = r["url"]
            engines = [s["engine"] for s in r.get("sources", [])]
            hits.append(
                AgentSearchHit(
                    rank=rank,
                    url=url,
                    handle=make_handle(url),
                    title=r.get("title"),
                    snippet=r.get("snippet"),
                    engines=engines if detailed else [],
                    score=r.get("fused_score") if detailed else None,
                    published=r.get("published_date"),
                )
            )

        # next_offset is left null: the default keyless backends do not support reliable
        # result paging (ddgs ignores offset; SearXNG pages only at offset >= count), so
        # advertising a cursor would lure an agent into re-fetching overlapping results. The
        # offset field stays plumbed for a backend that honors it; until Layer 1 applies
        # offset uniformly, the honest answer is "refine the query" (documented in SKILL.md).
        payload = AgentSearchPayload(
            query=req.query,
            results=hits,
            total_returned=len(hits),
            next_offset=None,
            warnings=list(data.get("warnings", [])),
        )
        return ok_envelope(
            AGENTIO_CONTRACT_VERSION,
            payload.model_dump(mode="json"),
            layer="agentio",
            backend="search",
        )

    # --- web_fetch -----------------------------------------------------------------

    def web_fetch(self, req: AgentFetchRequest) -> Envelope:
        page, req_warnings = self._fetch_one(
            req.url,
            page=req.page,
            page_size_tokens=req.page_size_tokens,
            tier=req.tier,
            timeout_ms=req.timeout_ms,
            allow_private_hosts=req.allow_private_hosts,
            datamark=req.datamark,
            chars_per_token=req.chars_per_token,
        )
        if page is None:
            return error_envelope(
                AGENTIO_CONTRACT_VERSION,
                code=errors.FETCH_FAILED,
                message=req_warnings[0] if req_warnings else f"{req.url}: fetch failed.",
                retriable=True,
                layer="agentio",
                backend="fetch",
            )
        payload = AgentFetchPayload(pages=[page], warnings=req_warnings)
        return ok_envelope(
            AGENTIO_CONTRACT_VERSION,
            payload.model_dump(mode="json"),
            layer="agentio",
            backend="fetch",
        )

    def web_fetch_many(
        self,
        urls: list[str],
        *,
        page: int = 1,
        page_size_tokens: int = DEFAULT_PAGE_SIZE_TOKENS,
        tier: str = "auto",
        timeout_ms: int = 20000,
        allow_private_hosts: bool = False,
        datamark: bool = False,
        chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
        query: str | None = None,
    ) -> Envelope:
        pages: list[AgentPage] = []
        warnings: list[str] = []
        for url in urls:
            page_obj, req_warnings = self._fetch_one(
                url,
                page=page,
                page_size_tokens=page_size_tokens,
                tier=tier,
                timeout_ms=timeout_ms,
                allow_private_hosts=allow_private_hosts,
                datamark=datamark,
                chars_per_token=chars_per_token,
            )
            warnings.extend(req_warnings)
            if page_obj is not None:
                pages.append(page_obj)
        if not pages:
            # Carry the per-URL reasons into the message so the cause is not lost (a single
            # failed URL then reads as "<url>: <code>: <detail>", which points at the fix,
            # e.g. a private host needing --allow-private-hosts).
            detail = "; ".join(warnings) if warnings else f"all {len(urls)} url(s) failed to fetch"
            return error_envelope(
                AGENTIO_CONTRACT_VERSION,
                code=errors.FETCH_FAILED,
                message=f"{detail}; nothing to return.",
                retriable=True,
                layer="agentio",
                backend="fetch",
            )
        payload = AgentFetchPayload(pages=pages, query=query, warnings=warnings)
        return ok_envelope(
            AGENTIO_CONTRACT_VERSION,
            payload.model_dump(mode="json"),
            layer="agentio",
            backend="fetch",
        )

    def _fetch_one(
        self,
        url: str,
        *,
        page: int,
        page_size_tokens: int,
        tier: str,
        timeout_ms: int,
        allow_private_hosts: bool,
        datamark: bool,
        chars_per_token: float,
    ) -> tuple[AgentPage | None, list[str]]:
        try:
            freq = FetchRequest(
                url=url,
                tier_hint=tier,
                timeout_ms=timeout_ms,
                allow_private_hosts=allow_private_hosts,
            )
        except ValidationError:
            return None, [f"{url}: invalid fetch request; skipped."]

        env = self._pipeline.run(freq)
        if not env.ok:
            err = env.error
            msg = f"{url}: {err.code}: {err.message}" if err else f"{url}: fetch failed."
            return None, [msg]

        data = env.data or {}
        src = data.get("source") or {}
        res = data.get("result") or {}
        final_url = src.get("final_url") or src.get("url") or url
        title = res.get("title")
        markdown = res.get("content_markdown") or ""

        page_warnings: list[str] = list(res.get("warnings") or []) + list(
            data.get("warnings") or []
        )
        # Key the page by the REQUESTED url, not the post-redirect final_url, so the handle
        # the agent gets here equals make_handle(url) -- the same handle web_search returned
        # for this URL -- and web_open resolves it. Index under both URLs (the store dedups on
        # content) so a later direct fetch of the redirect target also resolves; note the
        # redirect so the agent still sees where it landed.
        index_urls = [url]
        if final_url and final_url != url:
            index_urls.append(final_url)
            page_warnings.append(f"redirected to {final_url}")
        # Index the FULL body so web_open can paginate it later without re-fetching. A
        # store failure must not lose the fetched content, so it degrades to a warning.
        try:
            self.store.add(
                [
                    PageInput(url=u, markdown=markdown, title=title, fetched_at=None)
                    for u in index_urls
                ]
            )
        except Exception as exc:
            page_warnings.append(f"page index failed: {type(exc).__name__}: {exc}")

        page_obj = self._build_page(
            url=url,
            title=title,
            markdown=markdown,
            page=page,
            page_size_tokens=page_size_tokens,
            datamark=datamark,
            chars_per_token=chars_per_token,
            source="live",
            blocked=bool(src.get("blocked")),
            block_reason=src.get("block_reason"),
            fetched_at=None,
            warnings=page_warnings,
        )
        return page_obj, []

    # --- web_open ------------------------------------------------------------------

    def web_open(self, req: AgentOpenRequest) -> Envelope:
        doc = self._resolve(req.handle)
        if doc is None:
            return error_envelope(
                AGENTIO_CONTRACT_VERSION,
                code=errors.NOT_OPENED,
                message=(
                    f"handle {req.handle!r} is not in the page store; call web_fetch on the"
                    " URL first (web_open paginates an already-fetched page, it never fetches"
                    " the network)."
                ),
                retriable=False,
                layer="agentio",
                backend="store",
            )
        page_obj = self._build_page(
            url=doc.url,
            title=doc.title,
            markdown=doc.markdown,
            page=req.page,
            page_size_tokens=req.page_size_tokens,
            datamark=req.datamark,
            chars_per_token=req.chars_per_token,
            source="cache",
            fetched_at=doc.fetched_at,
            warnings=[],
        )
        payload = AgentFetchPayload(pages=[page_obj])
        return ok_envelope(
            AGENTIO_CONTRACT_VERSION,
            payload.model_dump(mode="json"),
            layer="agentio",
            backend="store",
        )

    def _resolve(self, handle: str):
        """Resolve a handle (or a raw URL) to a stored PageDocument, or None.

        Fails closed on the (astronomically unlikely) same-site handle collision: if two
        distinct stored URLs recompute to the requested handle, return None so web_open
        reports not_opened rather than silently serving the wrong page (the agent can then
        pass the exact URL).
        """
        if handle.startswith(("http://", "https://")):
            return self.store.get(handle)
        matches = {
            entry.url
            for entry in self.store.resolve_index().docs
            if make_handle(entry.url) == handle
        }
        if len(matches) != 1:
            return None
        return self.store.get(next(iter(matches)))

    # --- shared --------------------------------------------------------------------

    def _build_page(
        self,
        *,
        url: str,
        title: str | None,
        markdown: str,
        page: int,
        page_size_tokens: int,
        datamark: bool,
        chars_per_token: float,
        source: str,
        blocked: bool = False,
        block_reason: str | None = None,
        fetched_at: str | None = None,
        warnings: list[str] | None = None,
    ) -> AgentPage:
        warnings = list(warnings or [])
        pages = paginate(
            markdown, page_size_tokens=page_size_tokens, chars_per_token=chars_per_token
        )
        total_pages = len(pages)
        effective_page = page
        if page > total_pages:
            warnings.append(
                f"page {page} requested; document has {total_pages} page(s); showing the last."
            )
            effective_page = total_pages
        page_markdown = pages[effective_page - 1]
        fenced, info = fence_untrusted(page_markdown, source_url=url, datamark=datamark)
        return AgentPage(
            handle=make_handle(url),
            url=url,
            title=title,
            content=fenced,
            page=effective_page,
            total_pages=total_pages,
            page_tokens=estimate_tokens(page_markdown, chars_per_token=chars_per_token),
            total_tokens=estimate_tokens(markdown, chars_per_token=chars_per_token),
            has_more=effective_page < total_pages,
            fence=info,
            blocked=blocked,
            block_reason=block_reason,
            source=source,  # type: ignore[arg-type]
            fetched_at=fetched_at,
            warnings=warnings,
        )

    def _propagate_error(self, env: Envelope, *, backend: str) -> Envelope:
        err = env.error
        return error_envelope(
            AGENTIO_CONTRACT_VERSION,
            code=err.code if err else "unknown_error",
            message=err.message if err else "upstream error",
            retriable=err.retriable if err else False,
            layer="agentio",
            backend=backend,
        )


def build_agent_io(
    *,
    searxng_url: str | None = None,
    enable_ddgs: bool = True,
    ddgs_factory=None,
    ddgs_backend: str = "auto",
    enable_curl_cffi: bool = True,
    curl_cffi_getter=None,
    store_config: StoreConfig | None = None,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    router=None,
    pipeline=None,
) -> AgentIO:
    """Wire an AgentIO with the default Layer-1 router and Layer-2A pipeline.

    Tests inject ``router``/``pipeline`` (or the ``*_factory``/``*_getter`` boundary
    fakes) to stand in for the network; production passes deployment config (searxng_url).
    ``ddgs_backend`` selects which keyless engines ddgs queries (e.g. "google,brave").
    """
    router = router or build_router(
        searxng_url=searxng_url,
        enable_ddgs=enable_ddgs,
        ddgs_factory=ddgs_factory,
        ddgs_backend=ddgs_backend,
    )
    pipeline = pipeline or build_pipeline(
        enable_curl_cffi=enable_curl_cffi, curl_cffi_getter=curl_cffi_getter
    )
    return AgentIO(router, pipeline, store_config=store_config, chars_per_token=chars_per_token)
