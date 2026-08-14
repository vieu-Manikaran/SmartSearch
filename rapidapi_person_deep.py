"""RapidAPI linkedin-data-scraper person_deep client (URN → vanity URL resolution)."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import requests

from config import settings

logger = logging.getLogger(__name__)

RAPIDAPI_HOST = "linkedin-data-scraper.p.rapidapi.com"
MAX_RETRIES = 6
RETRY_BACKOFF_SEC = 3.0
LINKEDIN_IN_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.I)


class PersonDeepError(Exception):
    """Raised when person_deep cannot be called (missing config, exhausted retries)."""


def collect_rapidapi_keys() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in (settings.rapidapi_key, settings.rapidapi_key2):
        key = (raw or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def normalize_linkedin_profile_url(raw: str) -> str:
    """Accept full URLs, partial paths, or bare URN/slug identifiers."""
    value = (raw or "").strip()
    if not value:
        return ""

    if value.startswith("http://") or value.startswith("https://"):
        url = value.split("?")[0].split("#")[0].rstrip("/")
        return f"{url}/"

    lower = value.lower()
    if "linkedin.com/in/" in lower:
        if not lower.startswith("http"):
            value = "https://" + value.lstrip("/")
        url = value.split("?")[0].split("#")[0].rstrip("/")
        return f"{url}/"

    slug = value.strip("/")
    return f"https://www.linkedin.com/in/{slug}/"


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": RAPIDAPI_HOST,
        "Content-Type": "application/json",
    }


def _retryable_status(code: int) -> bool:
    return code in {408, 425, 429, 500, 502, 503, 504}


def fetch_person_deep(link: str, api_key: str) -> dict[str, Any]:
    """
    Call POST /person_deep for one profile URL.

    Returns {"success": True, "data": {...}} or {"success": False, "error": "<code>"}.
    """
    url = settings.rapidapi_person_deep_url
    normalized = normalize_linkedin_profile_url(link)
    if not normalized:
        return {"success": False, "error": "invalid_url"}

    last_error = "unknown_error"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                url,
                headers=_headers(api_key),
                json={"link": normalized},
                timeout=60,
            )
        except requests.RequestException as exc:
            last_error = str(exc)
            logger.warning("person_deep request failed (attempt %s): %s", attempt, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
                continue
            return {"success": False, "error": "network_error"}

        if resp.status_code in {403, 404}:
            return {"success": False, "error": "profile_not_found"}

        if _retryable_status(resp.status_code):
            last_error = f"http_{resp.status_code}"
            logger.warning("person_deep HTTP %s (attempt %s)", resp.status_code, attempt)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
                continue
            return {"success": False, "error": "max_retries_exceeded"}

        if resp.status_code != 200:
            return {"success": False, "error": f"http_{resp.status_code}"}

        try:
            payload = resp.json()
        except ValueError:
            last_error = "invalid_json"
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
                continue
            return {"success": False, "error": "invalid_json"}

        if not payload.get("success"):
            return {"success": False, "error": "api_error"}

        data = payload.get("data")
        if not isinstance(data, dict):
            return {"success": False, "error": "missing_data"}

        return {"success": True, "data": data}

    return {"success": False, "error": last_error}


def public_identifier_to_url(public_identifier: str) -> str:
    slug = (public_identifier or "").strip().strip("/")
    if not slug:
        return ""
    return f"https://www.linkedin.com/in/{slug}/"


def resolve_vanity_url(link: str, api_key: str | None = None) -> dict[str, str]:
    """
    Resolve one LinkedIn profile link to a vanity URL.

    Returns keys: linkedin_url_input, linkedin_url_resolved, public_identifier, status.
    """
    normalized = normalize_linkedin_profile_url(link)
    keys = collect_rapidapi_keys()
    key = api_key or (keys[0] if keys else "")
    if not key:
        return {
            "linkedin_url_input": normalized,
            "linkedin_url_resolved": "",
            "public_identifier": "",
            "status": "missing_rapidapi_key",
        }

    result = fetch_person_deep(normalized, key)
    if not result.get("success"):
        return {
            "linkedin_url_input": normalized,
            "linkedin_url_resolved": "",
            "public_identifier": "",
            "status": str(result.get("error") or "resolve_failed"),
        }

    data = result["data"]
    public_id = str(data.get("publicIdentifier") or "").strip()
    resolved = public_identifier_to_url(public_id)
    if not resolved:
        return {
            "linkedin_url_input": normalized,
            "linkedin_url_resolved": "",
            "public_identifier": "",
            "status": "missing_public_identifier",
        }

    return {
        "linkedin_url_input": normalized,
        "linkedin_url_resolved": resolved,
        "public_identifier": public_id,
        "status": "resolved",
    }


def resolve_profiles_batch(
    rows: list[dict[str, Any]],
    *,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    """Resolve many profile rows concurrently (one RapidAPI key per worker)."""
    keys = collect_rapidapi_keys()
    if not keys:
        out: list[dict[str, Any]] = []
        for row in rows:
            merged = dict(row)
            merged.update(
                {
                    "linkedin_url_resolved": "",
                    "public_identifier": "",
                    "status": "missing_rapidapi_key",
                }
            )
            out.append(merged)
        return out

    total = len(rows)
    workers = min(len(keys), total) if total else 1
    completed = 0
    results: list[dict[str, Any] | None] = [None] * total

    def _resolve_index(idx: int) -> tuple[int, dict[str, Any]]:
        row = rows[idx]
        link = str(row.get("linkedin_url") or "")
        key = keys[idx % len(keys)]
        resolved = resolve_vanity_url(link, api_key=key)
        merged = dict(row)
        merged.update(
            {
                "linkedin_url": resolved["linkedin_url_input"] or link,
                "linkedin_url_resolved": resolved["linkedin_url_resolved"],
                "public_identifier": resolved["public_identifier"],
                "status": resolved["status"],
            }
        )
        return idx, merged

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_resolve_index, i): i for i in range(total)}
        for future in as_completed(futures):
            idx, merged = future.result()
            results[idx] = merged
            completed += 1
            if progress:
                label = str(merged.get("linkedin_url") or "")
                progress(completed, total, label)

    return [r for r in results if r is not None]
