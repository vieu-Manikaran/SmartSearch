"""RapidAPI linkedin-data-scraper search_people_with_filters client."""

from __future__ import annotations

import logging
import threading
import time
from itertools import cycle
from typing import Any

import requests

from config import settings
from rapidapi_person_deep import RAPIDAPI_HOST, collect_rapidapi_keys, normalize_linkedin_profile_url

logger = logging.getLogger(__name__)

SEARCH_PEOPLE_URL = "https://linkedin-data-scraper.p.rapidapi.com/search_people_with_filters"
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 1.0
REQUEST_TIMEOUT = 15.0
_key_cycle = None
_key_lock = threading.Lock()


def _next_rapidapi_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip()
    global _key_cycle
    keys = collect_rapidapi_keys()
    if not keys:
        return (settings.rapidapi_key or "").strip()
    with _key_lock:
        if _key_cycle is None or len(keys) == 1:
            _key_cycle = cycle(keys)
        return next(_key_cycle)


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": RAPIDAPI_HOST,
        "Content-Type": "application/json",
    }


def _title_word(word: str) -> str:
    if not word:
        return ""
    if word.isupper() and len(word) <= 3:
        return word
    return word[:1].upper() + word[1:].lower()


def search_people_with_filters(
    first_name: str,
    last_name: str,
    company: str,
    *,
    api_key: str | None = None,
    keyword: str | None = None,
    page: int = 1,
) -> list[dict[str, Any]]:
    """
    Search LinkedIn people by first name, last name, and company free text.
    Returns a list of people dicts from the API (may be empty).
    """
    key = _next_rapidapi_key(api_key)
    if not key:
        logger.warning("search_people_with_filters: missing RapidAPI key")
        return []

    payload = {
        "keyword": keyword or _title_word(first_name) or first_name,
        "page": max(1, min(page, 100)),
        "first_name": _title_word(first_name),
        "last_name": _title_word(last_name),
        "location_list": "",
        "language_list": "",
        "industry_list": "",
        "school_list": "",
        "current_company_list": "",
        "past_company_list": "",
        "service_catagory_list": "",
        "school_free_text": "",
        "title_free_text": "",
        "company_free_text": company.strip(),
    }

    last_error = "unknown_error"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                SEARCH_PEOPLE_URL,
                headers=_headers(key),
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_error = str(exc)
            logger.warning("search_people_with_filters network error (attempt %s): %s", attempt, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
                continue
            return []

        if resp.status_code == 429:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
                continue
            return []

        if resp.status_code >= 400:
            logger.warning(
                "search_people_with_filters HTTP %s: %s",
                resp.status_code,
                resp.text[:300],
            )
            return []

        try:
            body = resp.json()
        except ValueError:
            return []

        if not body.get("success"):
            return []

        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        people = data.get("people") if isinstance(data.get("people"), list) else []
        return [p for p in people if isinstance(p, dict)]

    logger.warning("search_people_with_filters failed: %s", last_error)
    return []


def person_navigation_url(person: dict[str, Any]) -> str:
    raw = person.get("navigationUrl") if isinstance(person.get("navigationUrl"), str) else ""
    if not raw:
        return ""
    return normalize_linkedin_profile_url(raw.split("?")[0]).rstrip("/")
