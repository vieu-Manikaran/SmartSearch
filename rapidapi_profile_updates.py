"""RapidAPI linkedin-data-scraper GET /profile_updates (paginated posts)."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from config import settings
from rapidapi_person_deep import RAPIDAPI_HOST, _lock_for_key, _retryable_status

logger = logging.getLogger(__name__)

MAX_RETRIES = 6
RETRY_BACKOFF_SEC = 3.0


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": RAPIDAPI_HOST,
    }


def _unwrap_json(data: Any) -> Any:
    if isinstance(data, dict) and data.get("data") is not None:
        return data["data"]
    if isinstance(data, dict) and "result" in data:
        return data["result"]
    return data


def fetch_profile_updates(
    profile_url: str,
    api_key: str,
    *,
    page: int = 1,
    reposts: int = 1,
    comments: int = 0,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Fetch one page of profile posts. Returns {success, data?} or {success: False, error}."""
    url = (settings.rapidapi_profile_updates_url or "").strip().rstrip("/")
    if not url.endswith("/profile_updates"):
        url = "https://linkedin-data-scraper.p.rapidapi.com/profile_updates"
    params = {
        "profile_url": profile_url,
        "page": page,
        "reposts": reposts,
        "comments": comments,
    }
    last_error = "unknown_error"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with _lock_for_key(api_key):
                resp = requests.get(
                    url,
                    headers=_headers(api_key),
                    params=params,
                    timeout=timeout,
                )
        except requests.RequestException as exc:
            last_error = str(exc)
            logger.warning("profile_updates request failed (attempt %s): %s", attempt, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
                continue
            return {"success": False, "error": "network_error"}

        if resp.status_code in {401, 403, 404}:
            return {
                "success": False,
                "status_code": resp.status_code,
                "error": "not_found_or_forbidden" if resp.status_code != 401 else "unauthorized",
            }

        if _retryable_status(resp.status_code):
            last_error = f"http_{resp.status_code}"
            logger.warning("profile_updates HTTP %s (attempt %s)", resp.status_code, attempt)
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

        return {"success": True, "data": _unwrap_json(payload)}

    return {"success": False, "error": last_error}
