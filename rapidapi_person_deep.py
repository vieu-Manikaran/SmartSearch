"""RapidAPI linkedin-data-scraper person_deep client (URN → vanity URL resolution)."""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.parse import unquote

import requests

from config import settings

logger = logging.getLogger(__name__)

RAPIDAPI_HOST = "linkedin-data-scraper.p.rapidapi.com"
MAX_RETRIES = 6
RETRY_BACKOFF_SEC = 3.0
PROVIDER_BUSY_ATTEMPTS = 2
PROVIDER_BUSY_BACKOFF_SEC = 1.5
LINKEDIN_IN_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.I)
MEMBER_URN_SLUG_RE = re.compile(r"^AC[ow]A[A-Za-z0-9_-]+$", re.I)
_AUTH_ERROR_MARKERS = (
    "invalid api key",
    "not subscribed",
    "you are not subscribed",
    "unauthorized",
    "forbidden",
)
_PROVIDER_BUSY_MARKERS = ("no free account spotted",)
_KEY_DEAD_ERRORS = {"unauthorized", "quota_exceeded"}
_TRANSPORT_FAIL_ERRORS = {"network_error", "max_retries_exceeded"}
_CIRCUIT_TRANSPORT_THRESHOLD = 2

_key_locks_guard = threading.Lock()
_key_locks: dict[str, threading.Lock] = {}
_key_health_lock = threading.Lock()
_disabled_keys: set[str] = set()
_key_fail_counts: dict[str, int] = {}


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


def reset_rapidapi_key_health() -> None:
    """Test helper: re-enable every RapidAPI key in this process."""
    with _key_health_lock:
        _disabled_keys.clear()
        _key_fail_counts.clear()


def is_rapidapi_key_disabled(api_key: str) -> bool:
    key = (api_key or "").strip()
    if not key:
        return False
    with _key_health_lock:
        return key in _disabled_keys


def disable_rapidapi_key(api_key: str, reason: str) -> None:
    key = (api_key or "").strip()
    if not key:
        return
    with _key_health_lock:
        if key in _disabled_keys:
            return
        _disabled_keys.add(key)
    logger.warning("Disabling RapidAPI key after %s; remaining keys will be used alone", reason)


def healthy_rapidapi_keys() -> list[str]:
    keys = collect_rapidapi_keys()
    with _key_health_lock:
        live = [key for key in keys if key not in _disabled_keys]
    return live


def _record_key_outcome(api_key: str, error: str | None) -> None:
    key = (api_key or "").strip()
    if not key:
        return
    if not error:
        with _key_health_lock:
            _key_fail_counts[key] = 0
        return
    if error in _KEY_DEAD_ERRORS:
        disable_rapidapi_key(key, error)
        return
    if error not in _TRANSPORT_FAIL_ERRORS:
        return
    with _key_health_lock:
        _key_fail_counts[key] = _key_fail_counts.get(key, 0) + 1
        count = _key_fail_counts[key]
    if count >= _CIRCUIT_TRANSPORT_THRESHOLD:
        disable_rapidapi_key(key, f"{error} x{count}")


def _keys_to_try(preferred: str | None = None) -> list[str]:
    keys = healthy_rapidapi_keys()
    if not keys:
        keys = collect_rapidapi_keys()
    preferred_key = (preferred or "").strip()
    if preferred_key and preferred_key in keys:
        rest = [key for key in keys if key != preferred_key]
        return [preferred_key, *rest]
    return keys


def _lock_for_key(api_key: str) -> threading.Lock:
    with _key_locks_guard:
        lock = _key_locks.get(api_key)
        if lock is None:
            lock = threading.Lock()
            _key_locks[api_key] = lock
        return lock


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


def _is_member_urn_slug(slug: str) -> bool:
    token = (slug or "").strip().strip("/")
    return bool(MEMBER_URN_SLUG_RE.fullmatch(token))


def _slug_from_profile_value(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    match = LINKEDIN_IN_RE.search(text)
    if match:
        return unquote(match.group(1)).strip().strip("/")
    if "/" in text or " " in text:
        return ""
    return unquote(text).strip().strip("/")


def _response_message(resp: requests.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return (resp.text or "").lower()
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or "").lower()
    return str(payload).lower()


def _message_has_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _is_rapidapi_auth_error(resp: requests.Response) -> bool:
    return _message_has_marker(_response_message(resp), _AUTH_ERROR_MARKERS)


def _is_provider_busy(resp: requests.Response) -> bool:
    return _message_has_marker(_response_message(resp), _PROVIDER_BUSY_MARKERS)


def _unwrap_person_data(raw: Any) -> dict[str, Any] | None:
    """Normalize person_deep payloads (dict, nested data, or a list of profiles)."""
    if isinstance(raw, list):
        dicts = [item for item in raw if isinstance(item, dict)]
        if not dicts:
            return None
        unwrapped = [(_unwrap_person_data(item) or item) for item in dicts]
        unwrapped = [item for item in unwrapped if isinstance(item, dict)]
        for item in unwrapped:
            slug = vanity_identifier_from_person_data(item)
            if slug and not _is_member_urn_slug(slug):
                return item
        return unwrapped[0] if unwrapped else None
    if not isinstance(raw, dict):
        return None
    inner = raw.get("data")
    if isinstance(inner, (dict, list)):
        nested = _unwrap_person_data(inner)
        if nested and (
            nested.get("publicIdentifier")
            or nested.get("vanityName")
            or nested.get("firstName")
            or nested.get("experiences")
        ):
            return nested
    return raw


def vanity_identifier_from_person_data(data: dict[str, Any]) -> str:
    """Prefer a real vanity slug over a member URN (ACo... / ACw...)."""
    slugs: list[str] = []
    for key in (
        "publicIdentifier",
        "public_identifier",
        "vanityName",
        "vanity_name",
        "vanityNameId",
    ):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            slug = _slug_from_profile_value(val)
            if slug:
                slugs.append(slug)
    for key in (
        "linkedinUrl",
        "linkedin_url",
        "url",
        "profileUrl",
        "profile_url",
        "profileURL",
        "inputUrl",
    ):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            slug = _slug_from_profile_value(val)
            if slug:
                slugs.append(slug)
    for slug in slugs:
        if not _is_member_urn_slug(slug):
            return slug
    return slugs[0] if slugs else ""


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
            with _lock_for_key(api_key):
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

        if resp.status_code == 404:
            return {"success": False, "error": "profile_not_found"}

        if resp.status_code in {401, 403}:
            if resp.status_code == 401 or _is_rapidapi_auth_error(resp):
                return {"success": False, "error": "unauthorized"}
            # Scraper sessions often 403 inaccessible profiles; caller should try the other key.
            return {"success": False, "error": "http_403"}

        if resp.status_code == 500 and _is_provider_busy(resp):
            last_error = "provider_busy"
            logger.warning("person_deep provider busy (attempt %s)", attempt)
            if attempt < PROVIDER_BUSY_ATTEMPTS:
                time.sleep(PROVIDER_BUSY_BACKOFF_SEC * attempt)
                continue
            return {"success": False, "error": "provider_busy"}

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

        data = _unwrap_person_data(payload.get("data"))
        if not isinstance(data, dict):
            return {"success": False, "error": "missing_data"}

        return {"success": True, "data": data}

    return {"success": False, "error": last_error}


def fetch_person_deep_with_fallback(
    link: str,
    preferred_key: str | None = None,
) -> dict[str, Any]:
    """Call person_deep, trying the other RapidAPI key if the preferred key fails."""
    last: dict[str, Any] = {"success": False, "error": "missing_rapidapi_key"}
    for key in _keys_to_try(preferred_key):
        last = fetch_person_deep(link, key)
        error = None if last.get("success") else str(last.get("error") or "unknown")
        _record_key_outcome(key, error)
        if last.get("success"):
            return last
        logger.info(
            "person_deep failed with one RapidAPI key (%s); trying next key if available",
            last.get("error"),
        )
    return last


def public_identifier_to_url(public_identifier: str) -> str:
    slug = (public_identifier or "").strip().strip("/")
    if not slug:
        return ""
    return f"https://www.linkedin.com/in/{slug}/"


def _empty_resolve(normalized: str, status: str, public_id: str = "") -> dict[str, str]:
    return {
        "linkedin_url_input": normalized,
        "linkedin_url_resolved": "",
        "public_identifier": public_id,
        "status": status,
    }


def resolve_vanity_url(link: str, api_key: str | None = None) -> dict[str, str]:
    """
    Resolve one LinkedIn profile link to a vanity URL.

    Tries every healthy RapidAPI key. A dead/unsubscribed key is disabled after
    the first auth failure so later rows are not slowed down by retries.

    Returns keys: linkedin_url_input, linkedin_url_resolved, public_identifier, status.
    """
    normalized = normalize_linkedin_profile_url(link)
    keys = _keys_to_try(api_key)
    if not keys:
        return _empty_resolve(normalized, "missing_rapidapi_key")

    last_status = "resolve_failed"
    last_public = ""
    for key in keys:
        result = fetch_person_deep(normalized, key)
        if not result.get("success"):
            last_status = str(result.get("error") or "resolve_failed")
            _record_key_outcome(key, last_status)
            continue
        _record_key_outcome(key, None)

        data = result["data"] if isinstance(result.get("data"), dict) else {}
        public_id = vanity_identifier_from_person_data(data)
        if not public_id:
            last_status = "missing_public_identifier"
            continue
        if _is_member_urn_slug(public_id):
            last_status = "still_urn"
            last_public = public_id
            continue

        return {
            "linkedin_url_input": normalized,
            "linkedin_url_resolved": public_identifier_to_url(public_id),
            "public_identifier": public_id,
            "status": "resolved",
        }

    if last_status in {"http_403", "http_404"}:
        last_status = "profile_not_found"
    return _empty_resolve(normalized, last_status, last_public)


def resolve_profiles_batch(
    rows: list[dict[str, Any]],
    *,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    """Resolve many profile rows concurrently (one RapidAPI key per worker, with fallback)."""
    keys = healthy_rapidapi_keys() or collect_rapidapi_keys()
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
        live = healthy_rapidapi_keys() or keys
        key = live[idx % len(live)]
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
