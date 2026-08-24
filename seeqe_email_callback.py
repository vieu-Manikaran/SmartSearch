"""POST FullEnrich email results to Seeqe person/email integration callback."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from config import settings

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/api/v1/person/email/integration/granite/callback"
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SEC = 2
REQUEST_TIMEOUT_SEC = 30

# Clay historically sent FullEnrich row status, not Molster letter grades.
_CONFIDENCE_BY_STATUS = {
    "a": "Success",
    "b": "Success",
    "success": "Success",
    "deliverable": "Success",
    "valid": "Success",
    "valid & safe to send email": "Success",
    "c": "Partial success",
    "partial success": "Partial success",
    "probably valid email": "Partial success",
    "catch-all": "Partial success",
    "catch all": "Partial success",
    "risky": "Partial success",
    "d": "Partial success",
    "f": "Partial success",
}


def _callback_url() -> str:
    base = (settings.jobs_api_base_url or "").rstrip("/")
    return f"{base}{CALLBACK_PATH}"


def _granite_api_key() -> str:
    return (settings.seeqe_granite_api_key or "").strip()


def _headers() -> dict[str, str] | None:
    # Granite requires the Clay token, not VIEU_API_KEY (jobs 0f4k_ key).
    api_key = _granite_api_key()
    if not api_key:
        return None
    requester = (settings.seeqe_requester_id or "clay.caravan-tech").strip()
    return {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "x-requester-id": requester or "clay.caravan-tech",
    }


def _is_transient_http(status_code: int) -> bool:
    return status_code in {408, 425, 429, 500, 502, 503, 504}


def _normalize_linkedin_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw and "linkedin.com" in raw.lower():
        raw = "https://" + raw.lstrip("/")
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if host == "linkedin.com":
        host = "www.linkedin.com"
    path = re.sub(r"/+", "/", parsed.path or "").rstrip("/")
    if "linkedin.com" not in host or not path:
        return raw.split("?")[0].split("#")[0].rstrip("/")
    scheme = "https"
    return f"{scheme}://{host}{path}"


def _iso_created_at(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = text.replace(" UTC", "").replace("Z", "+00:00")
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
        text = text.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _confidence_status(raw: str) -> str:
    key = (raw or "").strip().lower()
    if not key:
        return "Success"
    return _CONFIDENCE_BY_STATUS.get(key, "Success")


def _build_payload(row: dict[str, Any]) -> dict[str, str] | None:
    email = (row.get("work_email") or "").strip()
    if not email:
        return None

    linkedin_url = _normalize_linkedin_url(row.get("linkedin_url") or "")
    if not linkedin_url:
        logger.warning("Seeqe callback skipped: work email found but linkedin_url missing")
        return None

    return {
        "linkedInUrl": linkedin_url,
        "email": email,
        "createdAt": _iso_created_at(row.get("created_at") or ""),
        "confidence_status": _confidence_status(row.get("email_status") or ""),
        "email_type": "professional",
    }


def post_email_to_seeqe(row: dict[str, Any]) -> bool:
    """
    POST one enriched contact to Seeqe when work_email is present.
    Retries transient failures; logs and returns False on permanent failure.
    """
    payload = _build_payload(row)
    if not payload:
        return False

    headers = _headers()
    if not headers:
        logger.warning("Seeqe callback skipped: missing SEEQE_GRANITE_API_KEY / x-api-key")
        return False

    url = _callback_url()
    linkedin_url = payload["linkedInUrl"]
    last_error = "Seeqe callback request failed."

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SEC)
        except requests.RequestException as exc:
            last_error = f"Seeqe callback network error: {exc}"
            logger.warning("%s (attempt %s/%s)", last_error, attempt, MAX_ATTEMPTS)
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
            continue

        if resp.ok:
            logger.info("Seeqe callback posted for %s", linkedin_url)
            return True

        detail = (resp.text or "")[:240].strip()
        last_error = detail or f"Seeqe callback returned HTTP {resp.status_code}"
        if _is_transient_http(resp.status_code) and attempt < MAX_ATTEMPTS:
            logger.warning(
                "Seeqe callback transient HTTP %s for %s (attempt %s/%s)",
                resp.status_code,
                linkedin_url,
                attempt,
                MAX_ATTEMPTS,
            )
            time.sleep(RETRY_BACKOFF_SEC * attempt)
            continue

        email = payload.get("email") or ""
        domain = email.split("@")[-1] if "@" in email else ""
        logger.error(
            "Seeqe callback failed for %s: HTTP %s %s createdAt=%s confidence_status=%s email_type=%s email_domain=%s",
            linkedin_url,
            resp.status_code,
            last_error,
            payload.get("createdAt"),
            payload.get("confidence_status"),
            payload.get("email_type"),
            domain,
        )
        return False

    logger.error("Seeqe callback failed for %s after %s attempts: %s", linkedin_url, MAX_ATTEMPTS, last_error)
    return False


def sync_rows_to_seeqe(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """POST each row with a work email to Seeqe. Returns (success_count, failure_count)."""
    ok = 0
    failed = 0
    for row in rows:
        if not (row.get("work_email") or "").strip():
            continue
        if post_email_to_seeqe(row):
            ok += 1
        else:
            failed += 1
    return ok, failed
