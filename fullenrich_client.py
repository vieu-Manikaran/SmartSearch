"""FullEnrich API client for verified work-email enrichment."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable

import requests

from config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://app.fullenrich.com/api/v2"
POLL_INTERVAL_SEC = 8
MIN_MAX_WAIT_SEC = 900  # 15 minutes (matches fE.md)
SECONDS_PER_CONTACT = 90  # FullEnrich docs: ~30–90s per contact in a batch
BATCH_SIZE = 50  # smaller batches finish sooner and are less likely to time out


class FullEnrichError(Exception):
    """Raised when the FullEnrich API returns an error."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def split_person_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def is_valid_linkedin_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return "linkedin.com/in/" in u or "linkedin.com/sales/" in u


def _headers() -> dict[str, str]:
    api_key = settings.fullenrich_api_key or ""
    if not api_key:
        raise FullEnrichError("Missing FULLENRICH_API_KEY in environment.")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def build_contact_payload(
    person: str,
    company: str,
    linkedin_url: str,
    row_index: int,
) -> dict[str, Any]:
    first_name, last_name = split_person_name(person)
    payload: dict[str, Any] = {
        "first_name": first_name,
        "last_name": last_name,
        "linkedin_url": linkedin_url.strip(),
        "enrich_fields": ["contact.work_emails"],
        "custom": {"row_index": str(row_index)},
    }
    if company.strip():
        payload["company_name"] = company.strip()
    return payload


def _is_transient_http(status_code: int) -> bool:
    return status_code in {408, 425, 429, 500, 502, 503, 504}


def _looks_like_html_response(resp: requests.Response) -> bool:
    content_type = (resp.headers.get("content-type") or "").lower()
    if "html" in content_type:
        return True
    snippet = (resp.text or "")[:300].lstrip().lower()
    return snippet.startswith("<!doctype") or snippet.startswith("<html")


def sanitize_error_message(raw: str, *, max_len: int = 240) -> str:
    text = (raw or "").strip()
    if not text:
        return "Unknown error."
    lower = text.lower()
    if lower.startswith("<!doctype") or lower.startswith("<html"):
        return "FullEnrich API temporarily unavailable (gateway error). Will retry automatically."
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def start_bulk_enrichment(contacts: list[dict[str, Any]], batch_name: str) -> str:
    if not contacts:
        raise FullEnrichError("No contacts to enrich.")
    url = f"{BASE_URL}/contact/enrich/bulk?silentFail=true"
    body = {
        "name": batch_name,
        "data": contacts,
    }
    last_error = "FullEnrich request failed."
    for attempt in range(1, 6):
        try:
            resp = requests.post(url, json=body, headers=_headers(), timeout=60)
        except requests.RequestException as exc:
            last_error = f"FullEnrich request failed: {exc}"
            logger.warning("FullEnrich POST attempt %s failed: %s", attempt, exc)
            time.sleep(min(30, 5 * attempt))
            continue

        if resp.status_code == 401:
            raise FullEnrichError("Invalid FULLENRICH_API_KEY.")
        if resp.status_code == 429 or _is_transient_http(resp.status_code) or _looks_like_html_response(resp):
            last_error = f"FullEnrich returned HTTP {resp.status_code}"
            logger.warning("FullEnrich POST attempt %s transient: HTTP %s", attempt, resp.status_code)
            time.sleep(min(60, 10 * attempt))
            continue
        if not resp.ok:
            detail = sanitize_error_message(_error_message(resp))
            raise FullEnrichError(detail or f"FullEnrich returned HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError:
            last_error = "FullEnrich returned a non-JSON response."
            time.sleep(min(30, 5 * attempt))
            continue

        enrichment_id = data.get("enrichment_id")
        if not enrichment_id:
            raise FullEnrichError("FullEnrich did not return an enrichment_id.")
        logger.info("FullEnrich enrichment started: %s (%s contacts)", enrichment_id, len(contacts))
        return enrichment_id

    raise FullEnrichError(sanitize_error_message(last_error), transient=True)


def _max_wait_seconds(num_contacts: int) -> int:
    """Scale wait time with batch size; FullEnrich batches can run for many minutes."""
    return max(MIN_MAX_WAIT_SEC, num_contacts * SECONDS_PER_CONTACT)


def poll_enrichment_until_done(
    enrichment_id: str,
    on_progress: Callable[[int, int, str], None] | None = None,
    expected_total: int = 1,
    batch_size: int = 1,
) -> dict[str, Any]:
    url = f"{BASE_URL}/contact/enrich/bulk/{enrichment_id}"
    max_wait = _max_wait_seconds(batch_size)
    deadline = time.time() + max_wait
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        try:
            resp = requests.get(url, headers=_headers(), timeout=45)
        except requests.RequestException as exc:
            logger.warning("FullEnrich poll %s network error (attempt %s): %s", enrichment_id, attempt, exc)
            time.sleep(POLL_INTERVAL_SEC)
            continue

        if resp.status_code == 400:
            err = _parse_error(resp)
            if err.get("code") == "error.enrichment.in_progress":
                if on_progress:
                    on_progress(min(attempt, expected_total), expected_total, "Waiting for FullEnrich…")
                logger.info("FullEnrich %s in progress (attempt %s)", enrichment_id, attempt)
                time.sleep(POLL_INTERVAL_SEC)
                continue
            raise FullEnrichError(err.get("message") or "FullEnrich enrichment failed.")

        if resp.status_code == 404:
            raise FullEnrichError("FullEnrich enrichment not found.")
        if resp.status_code == 401:
            raise FullEnrichError("Invalid FULLENRICH_API_KEY.")
        if resp.status_code == 429:
            logger.warning("FullEnrich rate limit on poll %s; backing off", enrichment_id)
            time.sleep(60)
            continue
        if not resp.ok:
            if _is_transient_http(resp.status_code) or _looks_like_html_response(resp):
                logger.warning(
                    "FullEnrich poll %s transient HTTP %s (attempt %s)",
                    enrichment_id,
                    resp.status_code,
                    attempt,
                )
                time.sleep(POLL_INTERVAL_SEC)
                continue
            detail = sanitize_error_message(_error_message(resp))
            raise FullEnrichError(detail or f"FullEnrich poll returned HTTP {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError:
            logger.warning("FullEnrich poll %s returned non-JSON (attempt %s)", enrichment_id, attempt)
            time.sleep(POLL_INTERVAL_SEC)
            continue
        status = (payload.get("status") or "").upper()
        if status in {"CREATED", "IN_PROGRESS", "RATE_LIMIT"}:
            if on_progress:
                on_progress(min(attempt, expected_total), expected_total, "Waiting for FullEnrich…")
            logger.info("FullEnrich %s status=%s (attempt %s)", enrichment_id, status, attempt)
            time.sleep(POLL_INTERVAL_SEC)
            continue
        if status in {"FAILED", "CANCELED", "CANCELLED"}:
            raise FullEnrichError(f"FullEnrich enrichment ended with status {status}.")
        if status in {"CREDITS_INSUFFICIENT"}:
            raise FullEnrichError("FullEnrich credits insufficient. Add credits and retry.")
        if status == "UNKNOWN":
            raise FullEnrichError("FullEnrich enrichment ended with unknown status.")
        if status != "FINISHED":
            logger.warning("FullEnrich %s unexpected status %s; continuing to poll", enrichment_id, status)
            time.sleep(POLL_INTERVAL_SEC)
            continue
        if on_progress:
            on_progress(expected_total, expected_total, "Enrichment complete")
        return payload

    wait_min = max_wait // 60
    raise FullEnrichError(
        f"FullEnrich enrichment timed out after {wait_min} minutes for {batch_size} contact(s). "
        "Will keep polling on retry.",
        transient=True,
    )


def enrich_batch(
    batch: list[dict[str, str]],
    *,
    batch_label: str,
    on_progress: Callable[[int, int, str], None] | None = None,
    expected_total: int | None = None,
    existing_enrichment_id: str | None = None,
    on_enrichment_started: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """Enrich one batch (up to BATCH_SIZE contacts) via FullEnrich."""
    if not batch:
        return []

    total = expected_total or len(batch)
    if existing_enrichment_id:
        enrichment_id = existing_enrichment_id
        logger.info("Resuming FullEnrich enrichment %s (%s contacts)", enrichment_id, len(batch))
    else:
        contacts = [
            build_contact_payload(
                row["person"],
                row.get("company") or "",
                row["linkedin_url"],
                int(row["row_index"]),
            )
            for row in batch
        ]
        enrichment_id = start_bulk_enrichment(contacts, batch_label)
        if on_enrichment_started:
            on_enrichment_started(enrichment_id)

    try:
        payload = poll_enrichment_until_done(
            enrichment_id,
            on_progress=on_progress,
            expected_total=total,
            batch_size=len(batch),
        )
    except FullEnrichError as exc:
        if exc.transient:
            raise FullEnrichError(str(exc), transient=True) from exc
        raise

    results_by_index: dict[int, dict[str, str]] = {}
    for item in payload.get("data") or []:
        mapped = _map_result_item(item)
        if mapped is not None:
            results_by_index[int(mapped["row_index"])] = mapped

    output: list[dict[str, str]] = []
    for row in batch:
        idx = int(row["row_index"])
        base = _result_base_row(row)
        if idx in results_by_index:
            mapped = results_by_index[idx]
            output.append({**base, **_enrichment_fields(mapped)})
        else:
            output.append({**base, **_empty_enrichment_fields("not_enriched")})
    return output


def enrich_contacts(
    rows: list[dict[str, str]],
    on_progress: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, str]]:
    """Enrich all contacts in sequential batches (non-resumable)."""
    if not rows:
        return []

    total = len(rows)
    output: list[dict[str, str]] = []
    processed = 0

    for batch_start in range(0, total, BATCH_SIZE):
        batch = rows[batch_start : batch_start + BATCH_SIZE]
        batch_label = f"Dashboard email enrichment {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        enriched = enrich_batch(
            batch,
            batch_label=batch_label,
            on_progress=on_progress,
            expected_total=total,
        )
        output.extend(enriched)
        processed += len(batch)
        if on_progress:
            on_progress(processed, total, f"Batch complete ({processed}/{total})")
    return output


def _result_base_row(row: dict[str, str]) -> dict[str, str]:
    return {
        "person": row["person"],
        "company": row.get("company") or "",
        "linkedin_url": row["linkedin_url"],
        "original": row.get("original") or {},
        "_fieldnames": row.get("_fieldnames") or [],
    }


def _enrichment_fields(mapped: dict[str, str]) -> dict[str, str]:
    return {
        "work_email": mapped.get("work_email") or "",
        "email_status": mapped.get("email_status") or "",
        "all_work_emails": mapped.get("all_work_emails") or "",
        "job_title": mapped.get("job_title") or "",
        "status": mapped.get("status") or "no_email_found",
    }


def _empty_enrichment_fields(status: str = "not_enriched") -> dict[str, str]:
    return {
        "work_email": "",
        "email_status": "",
        "all_work_emails": "",
        "job_title": "",
        "status": status,
    }


def _map_result_item(item: dict[str, Any]) -> dict[str, str] | None:
    custom = item.get("custom") or {}
    row_index = custom.get("row_index")
    if row_index is None:
        return None

    contact_info = item.get("contact_info") or {}
    most_probable = contact_info.get("most_probable_work_email") or {}
    work_email = (most_probable.get("email") or "").strip()
    email_status = (most_probable.get("status") or "").strip()

    all_emails = _format_all_work_emails(contact_info.get("work_emails") or [])

    profile = item.get("profile") or {}
    employment = profile.get("employment") or {}
    current = employment.get("current") or {}
    job_title = (current.get("title") or "").strip()

    status = "found" if work_email else "no_email_found"
    return {
        "row_index": str(row_index),
        "work_email": work_email,
        "email_status": email_status,
        "all_work_emails": all_emails,
        "job_title": job_title,
        "status": status,
    }


def _format_all_work_emails(emails: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for entry in emails:
        email = (entry.get("email") or "").strip()
        if not email:
            continue
        status = (entry.get("status") or "").strip()
        parts.append(f"{email} ({status})" if status else email)
    return "; ".join(parts)


def _parse_error(resp: requests.Response) -> dict[str, str]:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return {
                "code": str(data.get("code") or ""),
                "message": str(data.get("message") or ""),
            }
    except ValueError:
        pass
    return {"code": "", "message": resp.text[:300]}


def _error_message(resp: requests.Response) -> str:
    err = _parse_error(resp)
    return err.get("message") or ""
