"""Find a company LinkedIn URL on the company's own website, then Serper."""

from __future__ import annotations

import html as html_lib
import logging
import re
import time
from collections import deque
from typing import Callable
from urllib.parse import urljoin, urlparse, urlunparse

import requests

from serper_search import find_linkedin_company_url
from vendor_file.urls import canonicalize_company_url
from vendor_file.website import canonicalize_website, registrable_domain

logger = logging.getLogger(__name__)

MAX_PAGES = 150
MAX_SECONDS = 90.0
REQUEST_TIMEOUT = 10.0
SITEMAP_LIMIT = 5

_SKIP_EXT = {
    ".7z",
    ".avi",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".svg",
    ".tar",
    ".tgz",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}

_PRIORITY_PATH = re.compile(
    r"/(about|contact|company|connect|who-we-are|who-weare|team|social|footer|"
    r"legal|impressum|imprint)(?:/|$)",
    re.I,
)

# linkedin.com/company/{slug or numeric id} — stop before query/hash/path junk.
COMPANY_LINKEDIN_RE = re.compile(
    r"(?:https?:)?//(?:(?:www|nl|de|fr|uk|in)\.)?linkedin\.com/company/"
    r"([A-Za-z0-9][A-Za-z0-9_%+-]*)",
    re.I,
)

HREF_RE = re.compile(
    r"""(?:href|src)\s*=\s*(?P<q>["'])(?P<url>.*?)(?P=q)""",
    re.I | re.DOTALL,
)
SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)

FetchFn = Callable[[str], tuple[int, str, str]]


def seed_website_url(raw: str) -> str:
    """Turn a domain or website cell into an https homepage URL."""
    text = (raw or "").strip()
    if not text:
        return ""
    cleaned = canonicalize_website(text)
    if cleaned:
        return cleaned
    if text.startswith("//"):
        text = "https:" + text
    elif not re.match(r"^https?://", text, re.I):
        text = "https://" + text.lstrip("/")
    parsed = urlparse(text)
    if not parsed.netloc:
        return ""
    return f"https://{parsed.netloc.split('@')[-1].split(':')[0].lower()}"


def extract_company_linkedin_from_html(html: str) -> str | None:
    """Return the first canonical linkedin.com/company/{slug-or-id} URL in HTML."""
    if not html:
        return None
    text = html_lib.unescape(html).replace("\\/", "/")
    for match in COMPANY_LINKEDIN_RE.finditer(text):
        slug = match.group(1)
        candidate = f"https://www.linkedin.com/company/{slug}"
        canon = canonicalize_company_url(candidate)
        if canon.ok:
            return canon.url
    return None


def extract_hrefs(html: str, base_url: str) -> list[str]:
    if not html:
        return []
    text = html_lib.unescape(html)
    urls: list[str] = []
    seen: set[str] = set()
    for match in HREF_RE.finditer(text):
        raw = (match.group("url") or "").strip()
        if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        absolute = urljoin(base_url, raw)
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
    return urls


def _normalize_page_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = "https" if parsed.scheme in {"http", "https"} else parsed.scheme
    host = (parsed.netloc or "").lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, host, path, "", "", ""))


def _registrable_for_url(url: str) -> str:
    host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return registrable_domain(host)


def _is_same_site(url: str, seed_registrable: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return _registrable_for_url(url) == seed_registrable


def _looks_like_html_page(url: str) -> bool:
    path = urlparse(url).path.lower()
    if not path or path.endswith("/"):
        return True
    dot = path.rfind(".")
    if dot < 0:
        return True
    ext = path[dot:]
    if ext in {".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".cfm"}:
        return True
    if ext in _SKIP_EXT:
        return False
    return len(ext) > 6


def _link_priority(url: str) -> int:
    path = urlparse(url).path or "/"
    if path in {"", "/"}:
        return 0
    if _PRIORITY_PATH.search(path):
        return 1
    return 2


def _default_fetch(url: str) -> tuple[int, str, str]:
    resp = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; CompanyLinkedInFinder/1.0; +https://linkedin.com)"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        allow_redirects=True,
    )
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "html" not in content_type and "xml" not in content_type and "text" not in content_type:
        return resp.status_code, resp.url, ""
    return resp.status_code, resp.url, resp.text or ""


def _sitemap_urls(seed: str, fetch: FetchFn, seed_registrable: str) -> list[str]:
    parsed = urlparse(seed)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    queue = [urljoin(origin, "/sitemap.xml"), urljoin(origin, "/sitemap_index.xml")]
    found: list[str] = []
    seen: set[str] = set()
    sitemaps_fetched = 0
    while queue and sitemaps_fetched < SITEMAP_LIMIT:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            status, final_url, body = fetch(url)
        except (requests.RequestException, OSError, ValueError) as exc:
            logger.debug("sitemap fetch failed %s: %s", url, exc)
            continue
        sitemaps_fetched += 1
        if status >= 400 or not body:
            continue
        if not _is_same_site(final_url, seed_registrable):
            continue
        locs = [html_lib.unescape(m.strip()) for m in SITEMAP_LOC_RE.findall(body)]
        for loc in locs:
            if "sitemap" in loc.lower() and loc.lower().endswith(".xml"):
                if loc not in seen:
                    queue.append(loc)
                continue
            if _is_same_site(loc, seed_registrable) and _looks_like_html_page(loc):
                found.append(loc)
    return found


def crawl_website_for_company_linkedin(
    website: str,
    *,
    max_pages: int = MAX_PAGES,
    max_seconds: float = MAX_SECONDS,
    fetch: FetchFn | None = None,
) -> tuple[str | None, str]:
    """BFS same-site pages until a company LinkedIn URL is found.

    Returns (linkedin_url or None, page_url where it was found or the seed).
    """
    seed = seed_website_url(website)
    if not seed:
        return None, ""
    fetch_fn = fetch or _default_fetch
    seed_registrable = _registrable_for_url(seed)
    if not seed_registrable:
        return None, seed

    started = time.monotonic()
    queue: deque[str] = deque([seed])
    try:
        for loc in _sitemap_urls(seed, fetch_fn, seed_registrable):
            queue.append(loc)
    except Exception as exc:
        logger.debug("sitemap discovery failed for %s: %s", seed, exc)

    seen: set[str] = set()
    pages = 0
    while queue and pages < max_pages and (time.monotonic() - started) < max_seconds:
        url = queue.popleft()
        key = _normalize_page_url(url)
        if key in seen:
            continue
        seen.add(key)
        if not _is_same_site(url, seed_registrable) or not _looks_like_html_page(url):
            continue
        try:
            status, final_url, body = fetch_fn(url)
        except (requests.RequestException, OSError, ValueError) as exc:
            logger.debug("page fetch failed %s: %s", url, exc)
            continue
        if not _is_same_site(final_url, seed_registrable):
            continue
        pages += 1
        found = extract_company_linkedin_from_html(body)
        if found:
            logger.info("Found company LinkedIn on %s -> %s", final_url, found)
            return found, final_url
        next_links = extract_hrefs(body, final_url)
        next_links.sort(key=_link_priority)
        for link in next_links:
            if _normalize_page_url(link) in seen:
                continue
            if _is_same_site(link, seed_registrable) and _looks_like_html_page(link):
                if _link_priority(link) <= 1:
                    queue.appendleft(link)
                else:
                    queue.append(link)
        logger.debug("Website crawl %s page %s/%s %s", seed, pages, max_pages, final_url)

    logger.info("No company LinkedIn on website %s after %s pages", seed, pages)
    return None, seed


def find_company_linkedin(
    company_name: str,
    website: str = "",
    *,
    serper_api_key: str = "",
    fetch: FetchFn | None = None,
) -> dict[str, str]:
    """Website crawl first when a domain/website is present; otherwise Serper."""
    name = (company_name or "").strip()
    site = (website or "").strip()
    serper_query = f"{name} site:linkedin.com" if name else ""

    if site:
        found, found_on = crawl_website_for_company_linkedin(site, fetch=fetch)
        if found:
            return {
                "company": name,
                "website": seed_website_url(site) or site,
                "search_query": found_on,
                "linkedin_url": found,
                "status": "found_on_website",
                "source": "website",
            }
        if not name or not serper_api_key:
            return {
                "company": name,
                "website": seed_website_url(site) or site,
                "search_query": serper_query,
                "linkedin_url": "",
                "status": "no_company_page_in_top_10",
                "source": "",
            }
        serper_url = find_linkedin_company_url(name, serper_api_key, num=10, date_restrict=None)
        return {
            "company": name,
            "website": seed_website_url(site) or site,
            "search_query": serper_query,
            "linkedin_url": serper_url or "",
            "status": "found" if serper_url else "no_company_page_in_top_10",
            "source": "serper" if serper_url else "",
        }

    if not name:
        return {
            "company": "",
            "website": "",
            "search_query": "",
            "linkedin_url": "",
            "status": "no_company_page_in_top_10",
            "source": "",
        }
    serper_url = (
        find_linkedin_company_url(name, serper_api_key, num=10, date_restrict=None)
        if serper_api_key
        else None
    )
    return {
        "company": name,
        "website": "",
        "search_query": serper_query,
        "linkedin_url": serper_url or "",
        "status": "found" if serper_url else "no_company_page_in_top_10",
        "source": "serper" if serper_url else "",
    }
