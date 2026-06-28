"""Serper (Google search) API – single search and return organic results."""

import logging
import re
from dataclasses import dataclass
from typing import Any, List
from urllib.parse import unquote

import requests

logger = logging.getLogger(__name__)

SERPER_BASE = "https://google.serper.dev/search"
SERPER_NEWS_BASE = "https://google.serper.dev/news"
REQUEST_TIMEOUT = 10.0


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


LINKEDIN_COMPANY_PATH = "linkedin.com/company/"


def find_linkedin_company_url(
    company_name: str,
    api_key: str,
    num: int = 10,
    date_restrict: str | None = None,
) -> str | None:
    """
    Search ``{company} site:linkedin.com`` and return the first organic result URL whose
    link contains ``linkedin.com/company/`` (checked case-insensitively), scanning up to
    ``num`` results in order.
    """
    name = company_name.strip()
    if not name:
        return None
    query = f"{name} site:linkedin.com"
    items = search_serper(query, api_key, num=num, date_restrict=date_restrict, gl=None, page=1)
    needle = LINKEDIN_COMPANY_PATH.casefold()
    for item in items:
        link = item.get("link") if isinstance(item.get("link"), str) else None
        if not link:
            continue
        if needle in link.casefold():
            return link
    return None


LINKEDIN_PERSON_PATH = "linkedin.com/in/"
MIN_SCORE_FOUND = 6
MIN_SCORE_LOW = 4

_INVALID_PERSON_RE = re.compile(
    r"^\[?\s*not\s+provided\s*\]?$|^\s*invoices\s*$|^\s*fraud\s+unknown\s*$",
    re.I,
)
_COMPANY_SUFFIX_RE = re.compile(
    r"\b(inc\.?|llc\.?|l\.?l\.?c\.?|corp\.?|corporation|ltd\.?|n\.?a\.?|co\.?|company|"
    r"dba|fka|formerly|formally|group|holdings|international)\b",
    re.I,
)
_URN_SLUG_RE = re.compile(r"^ACwA[A-Za-z0-9_-]+$")

_FIRST_NAME_NICKNAMES: dict[str, set[str]] = {
    "michael": {"mike"},
    "mike": {"michael"},
    "robert": {"bob", "rob"},
    "bob": {"robert"},
    "rob": {"robert"},
    "william": {"bill", "will"},
    "bill": {"william"},
    "will": {"william"},
    "richard": {"rick", "dick"},
    "rick": {"richard"},
    "james": {"jim", "jimmy"},
    "jim": {"james"},
    "jimmy": {"james"},
    "joseph": {"joe"},
    "joe": {"joseph"},
    "jonathan": {"jon"},
    "jon": {"jonathan"},
    "jonathon": {"jon"},
    "elizabeth": {"liz", "beth"},
    "liz": {"elizabeth"},
    "beth": {"elizabeth"},
    "katherine": {"kate", "kathy"},
    "kate": {"katherine"},
    "kathy": {"katherine"},
    "catherine": {"kate", "kathy"},
    "thomas": {"tom"},
    "tom": {"thomas"},
    "daniel": {"dan"},
    "dan": {"daniel"},
    "christopher": {"chris"},
    "chris": {"christopher"},
    "matthew": {"matt"},
    "matt": {"matthew"},
    "andrew": {"andy"},
    "andy": {"andrew"},
    "jennifer": {"jen", "jenny"},
    "jen": {"jennifer"},
    "jenny": {"jennifer"},
    "rebecca": {"becca"},
    "becca": {"rebecca"},
    "stephanie": {"steph"},
    "steph": {"stephanie"},
    "patricia": {"pat", "trish"},
    "pat": {"patricia"},
    "trish": {"patricia"},
    "tricia": {"patricia"},
    "jeannette": {"jeanette"},
    "jeanette": {"jeannette"},
    "aaron": {"aarran"},
    "aarran": {"aaron"},
}


@dataclass
class PersonLinkedInMatch:
    url: str | None
    status: str
    score: int
    search_query: str


def _is_invalid_person_input(person: str) -> bool:
    if not person or not person.strip():
        return True
    if _INVALID_PERSON_RE.match(person.strip()):
        return True
    tokens = re.findall(r"[a-z]+", person.lower())
    return len(tokens) < 2


def _person_name_parts(name: str) -> tuple[str, str, set[str]]:
    alternates: set[str] = set()
    for match in re.finditer(r"\(([^)]+)\)", name):
        for token in re.findall(r"[a-z]+", match.group(1).lower()):
            if len(token) >= 2:
                alternates.add(token)
    cleaned = re.sub(r"\([^)]*\)", " ", name)
    tokens = [t for t in re.findall(r"[a-z]+", cleaned.lower()) if len(t) >= 2]
    if not tokens:
        return "", "", alternates
    first = tokens[0]
    last = tokens[-1] if len(tokens) > 1 else ""
    return first, last, alternates


def _company_search_term(company: str) -> str:
    cleaned = re.sub(r"\([^)]*\)", " ", company)
    cleaned = _COMPANY_SUFFIX_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[,/&]+", " ", cleaned)
    tokens = [t for t in re.findall(r"[a-z0-9]+", cleaned.lower()) if len(t) >= 2]
    if not tokens:
        return company.strip()
    if len(tokens) == 1:
        return tokens[0]
    if len(tokens[0]) <= 4 and len(tokens) >= 2:
        return f"{tokens[0]} {tokens[1]}"
    return tokens[0]


def _slug_from_url(url: str) -> str:
    match = re.search(r"linkedin\.com/in/([^/?#]+)", url, re.I)
    if not match:
        return ""
    slug = unquote(match.group(1))
    slug = re.sub(r"[^a-z0-9]+", "-", slug.lower())
    return re.sub(r"-+", "-", slug).strip("-")


def _slug_tokens(slug: str) -> list[str]:
    return [t for t in slug.split("-") if len(t) >= 2 and not t.isdigit()]


def _first_name_variants(first: str, alternates: set[str]) -> set[str]:
    variants = {first} | alternates
    for name in list(variants):
        variants |= _FIRST_NAME_NICKNAMES.get(name, set())
    return {v for v in variants if v}


def _token_in_text(token: str, text: str) -> bool:
    if not token or not text:
        return False
    text_cf = text.casefold()
    token_cf = token.casefold()
    if re.search(rf"\b{re.escape(token_cf)}\b", text_cf):
        return True
    compact = re.sub(r"[^a-z0-9]+", "", text_cf)
    return len(token_cf) >= 3 and token_cf in compact


def _first_name_matches(first: str, alternates: set[str], slug_tokens: list[str], text: str) -> bool:
    for variant in _first_name_variants(first, alternates):
        if _token_in_text(variant, text):
            return True
        if any(variant in tok or tok in variant for tok in slug_tokens if len(variant) >= 3):
            return True
    initial = first[:1]
    if initial and slug_tokens:
        for tok in slug_tokens:
            if tok.startswith(initial) and len(tok) >= 2:
                return True
    return False


def _last_name_matches(last: str, slug_tokens: list[str], text: str) -> bool:
    if not last:
        return False
    if _token_in_text(last, text):
        return True
    return any(last in tok or tok in last for tok in slug_tokens if len(last) >= 3)


def _company_matches(company_term: str, title: str, snippet: str) -> bool:
    text = f"{title} {snippet}".casefold()
    for token in re.findall(r"[a-z0-9]+", company_term.casefold()):
        if len(token) >= 3 and token in text:
            return True
    return False


def _score_person_result(
    first: str,
    last: str,
    alternates: set[str],
    company_term: str,
    link: str,
    title: str,
    snippet: str,
) -> int:
    slug = _slug_from_url(link)
    slug_tokens = _slug_tokens(slug)
    combined_text = f"{title} {snippet} {slug.replace('-', ' ')}"

    last_in_slug = _last_name_matches(last, slug_tokens, slug.replace("-", " "))
    last_in_title = _last_name_matches(last, slug_tokens, f"{title} {snippet}")
    first_in_slug = _first_name_matches(first, alternates, slug_tokens, slug.replace("-", " "))
    first_in_title = _first_name_matches(first, alternates, slug_tokens, f"{title} {snippet}")

    if not last_in_slug:
        return 0
    if not first_in_slug and not first_in_title:
        return 0

    score = 0
    if last_in_slug:
        score += 4
    if first_in_slug:
        score += 3
    if last_in_title:
        score += 2
    if first_in_title:
        score += 2
    if _company_matches(company_term, title, snippet):
        score += 1
    if link.casefold().startswith("https://www.linkedin.com/"):
        score += 1
    if _URN_SLUG_RE.match(slug.split("-")[0] if slug else ""):
        score -= 3
    return score


def find_linkedin_person_match(
    person_name: str,
    company_name: str,
    api_key: str,
    num: int = 10,
    date_restrict: str | None = None,
) -> PersonLinkedInMatch:
    person = person_name.strip()
    company = company_name.strip()
    if not person or not company:
        return PersonLinkedInMatch(None, "no_profile_in_top_10", 0, "")
    if _is_invalid_person_input(person):
        return PersonLinkedInMatch(None, "invalid_input", 0, "")

    company_term = _company_search_term(company)
    query = f'"{person}" {company_term} site:linkedin.com/in'
    first, last, alternates = _person_name_parts(person)
    items = search_serper(query, api_key, num=num, date_restrict=date_restrict, gl="us", page=1)

    candidates: list[tuple[int, int, str]] = []
    needle = LINKEDIN_PERSON_PATH.casefold()
    for rank, item in enumerate(items):
        link = item.get("link") if isinstance(item.get("link"), str) else None
        if not link or needle not in link.casefold():
            continue
        title = item.get("title") if isinstance(item.get("title"), str) else ""
        snippet = item.get("snippet") if isinstance(item.get("snippet"), str) else ""
        score = _score_person_result(first, last, alternates, company_term, link, title, snippet)
        if score > 0:
            candidates.append((score, rank, link))

    if not candidates:
        return PersonLinkedInMatch(None, "no_profile_in_top_10", 0, query)

    best_score, _, best_url = max(candidates, key=lambda row: (row[0], -row[1]))
    if best_score >= MIN_SCORE_FOUND:
        status = "found"
    elif best_score >= MIN_SCORE_LOW:
        status = "low_confidence"
    else:
        return PersonLinkedInMatch(None, "no_profile_in_top_10", best_score, query)

    return PersonLinkedInMatch(best_url, status, best_score, query)


def find_linkedin_person_url(
    person_name: str,
    company_name: str,
    api_key: str,
    num: int = 10,
    date_restrict: str | None = None,
) -> str | None:
    """Return the best-scoring LinkedIn profile URL, or None if no match clears the threshold."""
    match = find_linkedin_person_match(person_name, company_name, api_key, num, date_restrict)
    if match.status == "found":
        return match.url
    return None


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
