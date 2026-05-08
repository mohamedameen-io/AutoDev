"""v0.17.0 S2: ``web_search`` adapter — DuckDuckGo HTML scrape default.

Returns a list of ``WebSearchResult`` (title, url, snippet) given a
query. Pluggable via ``provider`` argument; default is the
DuckDuckGo HTML scraper.

Tests use respx-mocked httpx clients so no real network calls fire.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_web_search_duckduckgo_parses_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DuckDuckGo HTML scrape extracts title/url/snippet from result cards."""
    from adapters.web_search import web_search

    sample_html = """
    <html><body>
    <div class="result">
      <a class="result__a" href="https://example.com/x">Example title</a>
      <a class="result__snippet" href="https://example.com/x">A short snippet</a>
    </div>
    <div class="result">
      <a class="result__a" href="https://b.example.com/y">Another title</a>
      <a class="result__snippet" href="https://b.example.com/y">Snippet 2</a>
    </div>
    </body></html>
    """

    async def fake_post(self, url, data=None, headers=None, timeout=None):  # noqa: ANN001, ARG001
        class _Resp:
            status_code = 200
            text = sample_html

            def raise_for_status(self):  # noqa: D401
                pass

        return _Resp()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    results = await web_search("python web search", max_results=2)
    assert len(results) == 2
    assert results[0].title == "Example title"
    assert results[0].url.startswith("https://")
    assert "snippet" in results[0].snippet.lower()


@pytest.mark.asyncio
async def test_web_search_max_results_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters.web_search import web_search

    sample_html = "".join(
        f'<div class="result">'
        f'<a class="result__a" href="https://example.com/{i}">title {i}</a>'
        f'<a class="result__snippet" href="x">snip {i}</a>'
        f"</div>"
        for i in range(10)
    )

    async def fake_post(self, url, data=None, headers=None, timeout=None):  # noqa: ANN001, ARG001
        class _Resp:
            status_code = 200
            text = sample_html

            def raise_for_status(self):
                pass

        return _Resp()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    results = await web_search("q", max_results=3)
    assert len(results) == 3


@pytest.mark.asyncio
async def test_web_search_returns_empty_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters.web_search import web_search

    async def fake_post(self, url, data=None, headers=None, timeout=None):  # noqa: ANN001, ARG001
        import httpx

        raise httpx.RequestError("network down", request=None)

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    results = await web_search("q")
    # Errors are swallowed: web search is advisory, must never block.
    assert results == []


@pytest.mark.asyncio
async def test_web_search_result_dataclass() -> None:
    from adapters.web_search import WebSearchResult

    r = WebSearchResult(title="t", url="u", snippet="s")
    assert r.title == "t"
    assert r.url == "u"
    assert r.snippet == "s"
