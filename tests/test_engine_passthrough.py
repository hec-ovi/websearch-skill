"""The ddgs backend selection (--ddgs-backends) threads from CLI to the ddgs client.

ddgs is itself a keyless metasearch over many engines; this lets a caller force a
specific subset (e.g. google,brave) instead of the default auto.
"""

from __future__ import annotations

from websearch import cli as cli_mod
from websearch.cli import main
from websearch.layer1_search import SearchRequest, build_router

ROWS = [{"title": "t", "href": "https://example.com/a", "body": "b"}]


class RecordingDDGS:
    """A ddgs.DDGS stand-in that records the kwargs (so we can see ``backend``)."""

    last_kwargs: dict | None = None

    def __init__(self, rows):
        self._rows = rows

    def text(self, query, **kwargs):
        RecordingDDGS.last_kwargs = kwargs
        return list(self._rows)


def _factory():
    return RecordingDDGS(ROWS)


def test_router_passes_ddgs_backend_to_client():
    RecordingDDGS.last_kwargs = None
    router = build_router(ddgs_factory=_factory, ddgs_backend="google,brave")
    env = router.search(SearchRequest(query="x"))
    assert env.ok
    assert RecordingDDGS.last_kwargs["backend"] == "google,brave"


def test_router_defaults_to_auto_backend():
    RecordingDDGS.last_kwargs = None
    build_router(ddgs_factory=_factory).search(SearchRequest(query="x"))
    assert RecordingDDGS.last_kwargs["backend"] == "auto"


def test_cli_search_threads_ddgs_backends(monkeypatch):
    captured: dict = {}

    def spy(**kwargs):
        captured.update(kwargs)
        return build_router(ddgs_factory=_factory)

    monkeypatch.setattr(cli_mod, "build_router", spy)
    rc = main(["search", "x", "--ddgs-backends", "google,brave,mojeek", "--json"])
    assert rc == 0
    assert captured["ddgs_backend"] == "google,brave,mojeek"


def test_cli_search_default_backend_is_auto(monkeypatch):
    captured: dict = {}

    def spy(**kwargs):
        captured.update(kwargs)
        return build_router(ddgs_factory=_factory)

    monkeypatch.setattr(cli_mod, "build_router", spy)
    rc = main(["search", "x", "--json"])
    assert rc == 0
    assert captured["ddgs_backend"] == "auto"


def test_cli_websearch_threads_ddgs_backends(monkeypatch):
    from websearch.layer3_agentio import build_agent_io as real_build_agent_io

    captured: dict = {}

    def spy(**kwargs):
        captured.update(kwargs)
        router = build_router(ddgs_factory=_factory)
        return real_build_agent_io(router=router)

    monkeypatch.setattr(cli_mod, "build_agent_io", spy)
    rc = main(["web-search", "x", "--ddgs-backends", "brave", "--json"])
    assert rc == 0
    assert captured["ddgs_backend"] == "brave"


class _RecordingDDGSClass:
    """A no-arg DDGS() stand-in, so we can monkeypatch ddgs.DDGS and prove the value
    reaches the real client through the production adapter."""

    last_kwargs: dict | None = None
    last_query: str | None = None

    def __init__(self, *a, **k):
        pass

    def text(self, query, **kwargs):
        _RecordingDDGSClass.last_kwargs = kwargs
        _RecordingDDGSClass.last_query = query
        return list(ROWS)


def test_cli_search_backend_reaches_client_end_to_end(monkeypatch):
    # No spy on build_router: the real adapter constructs DDGS(), which we patch.
    _RecordingDDGSClass.last_kwargs = None
    monkeypatch.setattr("ddgs.DDGS", _RecordingDDGSClass)
    rc = main(["search", "x", "--ddgs-backends", "brave", "--json"])
    assert rc == 0
    assert _RecordingDDGSClass.last_kwargs["backend"] == "brave"


def test_web_search_site_injects_site_operator_into_query(monkeypatch):
    from websearch.layer3_agentio import AgentSearchRequest, build_agent_io

    _RecordingDDGSClass.last_query = None
    monkeypatch.setattr("ddgs.DDGS", _RecordingDDGSClass)
    aio = build_agent_io()  # real router, real ddgs adapter, faked DDGS client
    aio.web_search(AgentSearchRequest(query="RAG", site="reddit.com", max_results=3))
    # the engine itself restricts via site:, instead of only post-filtering
    assert "site:reddit.com" in _RecordingDDGSClass.last_query
