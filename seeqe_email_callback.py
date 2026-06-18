"""POST FullEnrich email results to Seeqe person/email integration callback."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

from config import settings

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/api/v1/person/email/integration/granite/callback"
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SEC = 2
REQUEST_TIMEOUT_SEC = 30


def _callback_url() -> str:
    base = (settings.jobs_api_base_url or "").rstrip("/")
    return f"{base}{CALLBACK_PATH}"


def _headers() -> dict[str, str] | None:
    api_key = (settings.vieu_api_key or "").strip()
    if not api_key:
        return None
    return {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "x-requester-id": (settings.seeqe_requester_id or "clay.caravan-tech").strip(),
    }


def _is_transient_http(status_code: int) -> bool:
    return status_code in {408, 425, 429, 500, 502, 503, 504}


def _build_payload(row: dict[str, Any]) -> dict[str, str] | None:
    email = (row.get("work_email") or "").strip()
    if not email:
        return None

    linkedin_url = (row.get("linkedin_url") or "").strip()
    if not linkedin_url:
        logger.warning("Seeqe callback skipped: work email found but linkedin_url missing")
        return None

    return {
        "linkedInUrl": linkedin_url,
        "email": email,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "confidence_status": (row.get("email_status") or "").strip(),
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
        logger.warning("Seeqe callback skipped: missing VIEU_API_KEY")
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

        logger.error("Seeqe callback failed for %s: %s", linkedin_url, last_error)
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
