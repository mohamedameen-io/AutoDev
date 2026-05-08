"""v0.17.0 S2: web-search adapter for the stuck-recovery escalation ladder.

A thin httpx-backed search wrapper used by the escalation ladder when a
task pivots repeatedly. The orchestrator calls :func:`web_search` to
fetch the top-3 results and splices them as a ``WEB_CONTEXT:`` block
into the next ``critic_sounding_board`` prompt.

The default provider is a DuckDuckGo HTML scraper (no API key required,
no tracking). Pluggable via the ``provider`` argument:

* ``"duckduckgo"`` (default) — HTML scrape against ``html.duckduckgo.com``.
* ``"serpapi"`` — SerpAPI JSON endpoint (requires ``AUTODEV_SERPAPI_KEY``
  env var). Future v0.17.x can register additional providers without
  touching call sites.

All errors (network, parse failure) are swallowed and return ``[]``. The
escalation ladder treats the empty case as "no useful context" and
proceeds to the next ladder rung. Web search must NEVER block forward
progress.

Cooldown / rate-limiting policy lives in the orchestrator
(``cfg.web_search_enabled``, search_count counter on
:class:`StuckState`); this module is a pure adapter.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal

import httpx

from autologging import get_logger


logger = get_logger(__name__)


# Conservative timeout. Web search is advisory; we'd rather fall through
# to the next ladder rung than burn 30s on a hung HTTP request.
_HTTP_TIMEOUT_S: float = 8.0


# DuckDuckGo HTML scrape pattern. Matches the ``<a class="result__a">``
# link + the adjacent ``<a class="result__snippet">``. Tolerant of
# whitespace / attribute reordering. Compiled once at import time.
_DDG_RESULT_PATTERN = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
    r'.*?<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)


_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Remove HTML tags + collapse whitespace. Defensive against malformed
    snippets."""
    no_tags = _HTML_TAG_PATTERN.sub("", text)
    return " ".join(no_tags.split())


@dataclass(frozen=True)
class WebSearchResult:
    """One search result. ``url`` is the canonical destination, ``snippet``
    is the search engine's summary line (HTML-stripped, whitespace-collapsed).
    """

    title: str
    url: str
    snippet: str


async def _duckduckgo_search(
    query: str, max_results: int
) -> list[WebSearchResult]:
    """DuckDuckGo HTML endpoint. No API key, no auth.

    Posts to ``https://html.duckduckgo.com/html/`` with the query as a
    form parameter; parses the response with the
    :data:`_DDG_RESULT_PATTERN` regex. Returns up to ``max_results``
    results, sliced from the top of the parser output.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; AutoDevWebSearch/0.17.0)"
                    )
                },
                timeout=_HTTP_TIMEOUT_S,
            )
            resp.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        logger.warning(
            "web_search.duckduckgo_failed",
            error=str(exc)[:200],
            query=query[:80],
        )
        return []

    html_text = getattr(resp, "text", "") or ""
    out: list[WebSearchResult] = []
    for match in _DDG_RESULT_PATTERN.finditer(html_text):
        url = match.group(1).strip()
        title = _strip_html(match.group(2))
        snippet = _strip_html(match.group(3))
        if not url or not title:
            continue
        out.append(WebSearchResult(title=title, url=url, snippet=snippet))
        if len(out) >= max_results:
            break
    return out


async def _serpapi_search(
    query: str, max_results: int
) -> list[WebSearchResult]:
    """SerpAPI JSON endpoint. Requires ``AUTODEV_SERPAPI_KEY`` env var.

    Returns ``[]`` when the key is unset (graceful degradation).
    """
    api_key = os.environ.get("AUTODEV_SERPAPI_KEY", "").strip()
    if not api_key:
        logger.warning("web_search.serpapi_no_key")
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://serpapi.com/search.json",
                params={
                    "q": query,
                    "api_key": api_key,
                    "engine": "google",
                    "num": max_results,
                },
                timeout=_HTTP_TIMEOUT_S,
            )
            resp.raise_for_status()
            data = resp.json()
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        logger.warning(
            "web_search.serpapi_failed",
            error=str(exc)[:200],
        )
        return []
    organic = data.get("organic_results", []) if isinstance(data, dict) else []
    out: list[WebSearchResult] = []
    for entry in organic[:max_results]:
        if not isinstance(entry, dict):
            continue
        out.append(
            WebSearchResult(
                title=str(entry.get("title", "")),
                url=str(entry.get("link", "")),
                snippet=str(entry.get("snippet", "")),
            )
        )
    return out


async def web_search(
    query: str,
    *,
    max_results: int = 3,
    provider: Literal["duckduckgo", "serpapi"] = "duckduckgo",
) -> list[WebSearchResult]:
    """Run a web search and return up to ``max_results`` :class:`WebSearchResult`.

    Args:
        query: Free-text search query (e.g. the failing critic's
            hypothesis). Truncation is the caller's responsibility —
            we forward as-is.
        max_results: Maximum number of results to return. The escalation
            ladder typically passes 3 (top-3 splice into the critic
            prompt). Default 3.
        provider: One of ``"duckduckgo"`` (default) or ``"serpapi"``.
            Future providers can be registered by adding a dispatch
            branch here.

    Returns:
        A list of :class:`WebSearchResult` (possibly empty on error).
        NEVER raises — web search is advisory.
    """
    if not query or not query.strip():
        return []
    if provider == "serpapi":
        return await _serpapi_search(query, max_results)
    return await _duckduckgo_search(query, max_results)


__all__ = ["WebSearchResult", "web_search"]
