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
POLL_INTERVAL_SEC = 30
MAX_POLL_ATTEMPTS = 60
BATCH_SIZE = 100


class FullEnrichError(Exception):
    """Raised when the FullEnrich API returns an error."""


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


def start_bulk_enrichment(contacts: list[dict[str, Any]], batch_name: str) -> str:
    if not contacts:
        raise FullEnrichError("No contacts to enrich.")
    url = f"{BASE_URL}/contact/enrich/bulk?silentFail=true"
    body = {
        "name": batch_name,
        "data": contacts,
    }
    try:
        resp = requests.post(url, json=body, headers=_headers(), timeout=90)
    except requests.RequestException as exc:
        raise FullEnrichError(f"FullEnrich request failed: {exc}") from exc

    if resp.status_code == 401:
        raise FullEnrichError("Invalid FULLENRICH_API_KEY.")
    if resp.status_code == 429:
        raise FullEnrichError("FullEnrich rate limit exceeded. Try again in a minute.")
    if not resp.ok:
        detail = _error_message(resp)
        raise FullEnrichError(detail or f"FullEnrich returned HTTP {resp.status_code}")

    data = resp.json()
    enrichment_id = data.get("enrichment_id")
    if not enrichment_id:
        raise FullEnrichError("FullEnrich did not return an enrichment_id.")
    logger.info("FullEnrich enrichment started: %s (%s contacts)", enrichment_id, len(contacts))
    return enrichment_id


def poll_enrichment_until_done(
    enrichment_id: str,
    on_progress: Callable[[int, int, str], None] | None = None,
    expected_total: int = 1,
) -> dict[str, Any]:
    url = f"{BASE_URL}/contact/enrich/bulk/{enrichment_id}"
    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=_headers(), timeout=90)
        except requests.RequestException as exc:
            raise FullEnrichError(f"FullEnrich poll failed: {exc}") from exc

        if resp.status_code == 400:
            err = _parse_error(resp)
            if err.get("code") == "error.enrichment.in_progress":
                done = min(attempt, expected_total)
                if on_progress:
                    on_progress(done, expected_total, "Waiting for FullEnrich…")
                logger.info(
                    "FullEnrich %s in progress (poll %s/%s)",
                    enrichment_id,
                    attempt,
                    MAX_POLL_ATTEMPTS,
                )
                time.sleep(POLL_INTERVAL_SEC)
                continue
            raise FullEnrichError(err.get("message") or "FullEnrich enrichment failed.")

        if resp.status_code == 404:
            raise FullEnrichError("FullEnrich enrichment not found.")
        if resp.status_code == 401:
            raise FullEnrichError("Invalid FULLENRICH_API_KEY.")
        if resp.status_code == 429:
            raise FullEnrichError("FullEnrich rate limit exceeded. Try again in a minute.")
        if not resp.ok:
            detail = _error_message(resp)
            raise FullEnrichError(detail or f"FullEnrich poll returned HTTP {resp.status_code}")

        payload = resp.json()
        status = (payload.get("status") or "").upper()
        if status in {"CREATED", "IN_PROGRESS", "RATE_LIMIT"}:
            done = min(attempt, expected_total)
            if on_progress:
                on_progress(done, expected_total, "Waiting for FullEnrich…")
            time.sleep(POLL_INTERVAL_SEC)
            continue
        if status in {"CANCELED", "CREDITS_INSUFFICIENT", "UNKNOWN"}:
            raise FullEnrichError(f"FullEnrich enrichment ended with status {status}.")
        if on_progress:
            on_progress(expected_total, expected_total, "Enrichment complete")
        return payload

    raise FullEnrichError("FullEnrich enrichment timed out. Try again later.")


def enrich_contacts(
    rows: list[dict[str, str]],
    on_progress: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, str]]:
    """
    Enrich contacts via FullEnrich (batches of 100).
    Each input row must have person, company, linkedin_url, row_index.
    """
    if not rows:
        return []

    results_by_index: dict[int, dict[str, str]] = {}
    total = len(rows)
    processed = 0

    for batch_start in range(0, total, BATCH_SIZE):
        batch = rows[batch_start : batch_start + BATCH_SIZE]
        contacts = [
            build_contact_payload(
                row["person"],
                row.get("company") or "",
                row["linkedin_url"],
                int(row["row_index"]),
            )
            for row in batch
        ]
        batch_name = f"Dashboard email enrichment {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        enrichment_id = start_bulk_enrichment(contacts, batch_name)
        payload = poll_enrichment_until_done(
            enrichment_id,
            on_progress=on_progress,
            expected_total=total,
        )
        for item in payload.get("data") or []:
            mapped = _map_result_item(item)
            if mapped is not None:
                results_by_index[int(mapped["row_index"])] = mapped

        processed += len(batch)
        if on_progress:
            on_progress(processed, total, f"Batch complete ({processed}/{total})")

    output: list[dict[str, str]] = []
    for row in rows:
        idx = int(row["row_index"])
        if idx in results_by_index:
            mapped = results_by_index[idx]
            output.append(
                {
                    "person": row["person"],
                    "company": row.get("company") or "",
                    "linkedin_url": row["linkedin_url"],
                    "work_email": mapped.get("work_email") or "",
                    "email_status": mapped.get("email_status") or "",
                    "all_work_emails": mapped.get("all_work_emails") or "",
                    "job_title": mapped.get("job_title") or "",
                    "status": mapped.get("status") or "no_email_found",
                }
            )
        else:
            output.append(
                {
                    "person": row["person"],
                    "company": row.get("company") or "",
                    "linkedin_url": row["linkedin_url"],
                    "work_email": "",
                    "email_status": "",
                    "all_work_emails": "",
                    "job_title": "",
                    "status": "not_enriched",
                }
            )
    return output


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
