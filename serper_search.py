"""Serper (Google search) API – single search and return organic results."""

import logging
from typing import Any, List

import requests

logger = logging.getLogger(__name__)

SERPER_BASE = "https://google.serper.dev/search"
SERPER_NEWS_BASE = "https://google.serper.dev/news"
REQUEST_TIMEOUT = 30.0


def search_serper(
    query: str,
    api_key: str,
    num: int = 10,
    date_restrict: str | None = "qdr:y",
    gl: str | None = None,
    page: int = 1,
) -> List[dict[str, Any]]:
    """
    Run one Serper search and return organic results (list of {link, title, snippet, ...}).

    Args:
        query: Search query string.
        api_key: Serper API key (X-API-KEY header).
        num: Max number of results to request (1–100).
        date_restrict: Optional Google date filter: "qdr:d" (day), "qdr:w" (week),
            "qdr:m" (month), "qdr:m3" (3 months), "qdr:y" (year). None = no filter.
        gl: Optional country code for result locale (e.g. "us", "uk", "in"). None = API default (typically US).
        page: Page number for pagination (1-based). Use with num=10 to get more results.

    Returns:
        List of organic result dicts. Empty list on error or empty response.
    """
    results: List[dict[str, Any]] = []
    num_safe = min(max(1, num), 100)
    page_safe = max(1, min(page, 100))
    payload: dict = {"q": query, "num": num_safe, "page": page_safe}
    if date_restrict:
        payload["dateRestrict"] = date_restrict
    if gl:
        payload["gl"] = gl.lower()[:2]
    try:
        resp = requests.post(
            SERPER_BASE,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 400:
            logger.warning(
                "Serper 400 Bad Request. Payload: %s | Response: %s",
                payload,
                resp.text[:500] if resp.text else resp.reason,
            )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("organic", [])[:num_safe]:
            if isinstance(item, dict):
                results.append(item)
    except requests.RequestException as e:
        logger.warning("Serper request failed for %s: %s", query[:50], e)
    except (KeyError, TypeError) as e:
        logger.warning("Unexpected Serper response for %s: %s", query[:50], e)
    return results


def search_serper_urls(
    query: str,
    api_key: str,
    num: int = 10,
    date_restrict: str | None = "qdr:y",
    gl: str | None = None,
    page: int = 1,
) -> List[str]:
    """
    Run one Serper search and return only URLs from organic results.

    Convenience wrapper around search_serper for callers that only need links.
    """
    items = search_serper(query, api_key, num=num, date_restrict=date_restrict, gl=gl, page=page)
    urls: List[str] = []
    for item in items:
        link = item.get("link") if isinstance(item.get("link"), str) else None
        if link and link.startswith("http"):
            urls.append(link)
    return urls


def search_serper_news(
    query: str,
    api_key: str,
    num: int = 10,
    date_restrict: str | None = "qdr:m3",
    gl: str | None = None,
    page: int = 1,
) -> List[dict[str, Any]]:
    """
    Run a Serper news search (recent articles). Returns list of result dicts with url, title, content, date, etc.

    Args:
        date_restrict: Optional Google date filter for news: "qdr:d", "qdr:w", "qdr:m", "qdr:m3" (3 months), "qdr:y". Default "qdr:m3" = last 3 months.
        gl: Optional country code for result locale (e.g. "us", "uk", "in"). None = API default (typically US).
        page: Page number for pagination (1-based).
    """
    results: List[dict[str, Any]] = []
    num_safe = min(max(1, num), 100)
    page_safe = max(1, min(page, 100))
    payload: dict = {"q": query, "num": num_safe, "page": page_safe}
    if date_restrict:
        payload["dateRestrict"] = date_restrict
    if gl:
        payload["gl"] = gl.lower()[:2]
    try:
        resp = requests.post(
            SERPER_NEWS_BASE,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 400:
            logger.warning(
                "Serper news 400 Bad Request. Payload: %s | Response: %s",
                payload,
                resp.text[:500] if resp.text else resp.reason,
            )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("news", [])[:num_safe]:
            if isinstance(item, dict):
                if "url" in item and "link" not in item:
                    item = {**item, "link": item["url"]}
                results.append(item)
    except requests.RequestException as e:
        logger.warning("Serper news request failed for %s: %s", query[:50], e)
    except (KeyError, TypeError) as e:
        logger.warning("Unexpected Serper response for %s: %s", query[:50], e)
    return results
