"""Email enrichment via MoltSets, verified by Bouncer."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from bouncer_client import BouncerError, bouncer_configured, verify_email
from molster_client import (
    BATCH_SIZE as MOLSTER_BATCH_SIZE,
    MolsterError,
    MolsterFairUseExhausted,
    fair_use_reset_ts,
    is_valid_linkedin_url,
    linkedin_match_key,
    lookup_linkedin_urls,
    molster_configured,
)

logger = logging.getLogger(__name__)

STATUS_NOT_ENRICHED = "not_enriched"
STATUS_FOUND = "found"
STATUS_NO_EMAIL = "no_email_found"

ProgressCb = Callable[[int, int, str], None]


class EmailEnrichmentError(Exception):
    """Raised when MoltSets or Bouncer enrichment cannot continue."""

    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        retry_after_ts: float = 0,
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.retry_after_ts = retry_after_ts


@dataclass
class StepResult:
    done: bool
    newly_finished: list[dict[str, Any]] = field(default_factory=list)
    progress_item: str = ""


def email_providers_configured() -> bool:
    return molster_configured() and bouncer_configured()


def _base_result_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "person": row.get("person") or "",
        "company": row.get("company") or "",
        "linkedin_url": row.get("linkedin_url") or "",
        "original": row.get("original") or {},
        "_fieldnames": row.get("_fieldnames") or [],
        "work_email": "",
        "email_status": "",
        "all_work_emails": "",
        "job_title": "",
        "status": STATUS_NOT_ENRICHED,
        "email_source": "",
        "molster_status": "",
        "molster_risk_score": "",
        "molster_last_validated_at": "",
    }


def empty_result_row(row: dict[str, Any]) -> dict[str, Any]:
    return _base_result_row(row)


def needs_molster(row: dict[str, Any]) -> bool:
    if (row.get("work_email") or "").strip():
        return False
    status = (row.get("status") or STATUS_NOT_ENRICHED).strip() or STATUS_NOT_ENRICHED
    return status == STATUS_NOT_ENRICHED


def _finished_count(results: list[dict[str, Any]]) -> int:
    done = {STATUS_FOUND, STATUS_NO_EMAIL}
    return sum(1 for row in results if (row.get("status") or "") in done)


def _indexes(results: list[dict[str, Any]], predicate) -> list[int]:
    return [i for i, row in enumerate(results) if predicate(row)]


def _mark_molster_miss(row: dict[str, Any], *, molster_status: str = "not_found") -> dict[str, Any]:
    updated = dict(row)
    updated["status"] = STATUS_NO_EMAIL
    updated["molster_status"] = molster_status
    return updated


def _mark_molster_hit(
    row: dict[str, Any],
    hit: dict[str, str],
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    email = (hit.get("email") or "").strip()
    risk = (hit.get("risk_score") or "").strip()
    updated = dict(row)
    if verification is None:
        try:
            verification = verify_email(email)
        except BouncerError as exc:
            raise EmailEnrichmentError(str(exc), transient=exc.transient) from exc

    verification_status = str(verification.get("status") or "").strip().lower()
    deliverable = verification_status == "deliverable"
    updated.update(
        {
            "work_email": email if deliverable else "",
            "email_status": verification_status,
            "all_work_emails": email if deliverable else "",
            "status": STATUS_FOUND if deliverable else STATUS_NO_EMAIL,
            "email_source": "molster" if deliverable else "",
            "molster_status": hit.get("status") or "ok",
            "molster_risk_score": risk,
            "molster_last_validated_at": hit.get("last_validated_at") or "",
        }
    )
    if not deliverable:
        logger.info(
            "Bouncer suppressed MoltSets email: status=%s reason=%s",
            verification_status or "missing",
            verification.get("reason") or "",
        )
    return updated


def _quota_error(exc: MolsterFairUseExhausted | None = None) -> EmailEnrichmentError:
    retry_after_ts = (exc.retry_after_ts if exc else 0) or fair_use_reset_ts()
    wait_min = 60
    if retry_after_ts:
        wait_min = max(1, int(max(0.0, retry_after_ts - time.time()) / 60))
    message = (
        str(exc)
        if exc and str(exc)
        else (
            "Molster fair-use limit reached (5k emails / 5 hours). "
            f"Resuming after window reset in ~{wait_min} min."
        )
    )
    return EmailEnrichmentError(message, transient=True, retry_after_ts=retry_after_ts)


def take_enrichment_step(
    input_rows: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    wait_for_molster_quota: bool = True,
    on_progress: ProgressCb | None = None,
    expected_total: int | None = None,
) -> StepResult:
    """Run one MoltSets batch and verify every returned email with Bouncer."""
    total = expected_total or len(input_rows)

    def progress(item: str) -> None:
        if on_progress:
            on_progress(_finished_count(results), total, item)

    molster_idxs = _indexes(results, needs_molster)
    if not molster_idxs:
        return StepResult(done=True, progress_item="Enrichment complete")
    if not email_providers_configured():
        raise EmailEnrichmentError(
            "Missing MOLSTER_API_KEY or BOUNCER_API_KEY in environment."
        )

    try:
        batch_idxs = molster_idxs[:MOLSTER_BATCH_SIZE]
        progress(f"MoltSets + Bouncer batch ({len(batch_idxs)} LinkedIn URLs)")
        return _run_molster(input_rows, results, batch_idxs)
    except MolsterFairUseExhausted as exc:
        if wait_for_molster_quota:
            raise _quota_error(exc) from exc
        raise EmailEnrichmentError(str(exc), transient=True, retry_after_ts=exc.retry_after_ts) from exc


def _run_molster(
    input_rows: list[dict[str, Any]],
    results: list[dict[str, Any]],
    indexes: list[int],
) -> StepResult:
    url_to_indexes: dict[str, list[int]] = {}
    ordered_urls: list[str] = []
    for i in indexes:
        url = (input_rows[i].get("linkedin_url") or results[i].get("linkedin_url") or "").strip()
        if not url or not is_valid_linkedin_url(url):
            results[i] = _mark_molster_miss(results[i], molster_status="no_linkedin_url")
            continue
        key = linkedin_match_key(url)
        if key not in url_to_indexes:
            url_to_indexes[key] = []
            ordered_urls.append(url)
        url_to_indexes[key].append(i)

    if ordered_urls:
        try:
            hits = lookup_linkedin_urls(ordered_urls)
        except MolsterFairUseExhausted:
            raise
        except MolsterError as exc:
            raise EmailEnrichmentError(
                str(exc),
                transient=exc.transient,
                retry_after_ts=exc.retry_after_ts,
            ) from exc

        by_key: dict[str, dict[str, str]] = {}
        for hit in hits:
            key = linkedin_match_key(hit.get("input") or "")
            if key:
                by_key[key] = hit

        verification_by_email: dict[str, dict[str, Any]] = {}
        for url in ordered_urls:
            key = linkedin_match_key(url)
            hit = by_key.get(key) or {}
            email = (hit.get("email") or "").strip()
            if email and email not in verification_by_email:
                try:
                    verification_by_email[email] = verify_email(email)
                except BouncerError as exc:
                    raise EmailEnrichmentError(str(exc), transient=exc.transient) from exc
            for i in url_to_indexes.get(key, []):
                if email:
                    results[i] = _mark_molster_hit(
                        results[i], hit, verification_by_email[email]
                    )
                else:
                    results[i] = _mark_molster_miss(
                        results[i],
                        molster_status=hit.get("status") or "not_found",
                    )

    newly = [
        results[i]
        for i in indexes
        if results[i].get("status") in {STATUS_FOUND, STATUS_NO_EMAIL}
    ]

    found = sum(1 for i in indexes if results[i].get("email_source") == "molster")
    logger.info("Molster step: %s urls, %s hits", len(indexes), found)
    return StepResult(
        done=False,
        newly_finished=newly,
        progress_item=f"Molster looked up {len(indexes)} contacts ({found} emails)",
    )


def enrich_contacts(
    rows: list[dict[str, str]],
    on_progress: Callable[[int, int, str], None] | None = None,
    *,
    wait_for_molster_quota: bool = False,
) -> list[dict[str, str]]:
    """Enrich contacts via MoltSets and retain Bouncer-deliverable emails."""
    if not rows:
        return []
    if not email_providers_configured():
        raise EmailEnrichmentError(
            "Missing MOLSTER_API_KEY or BOUNCER_API_KEY in environment."
        )

    results = [empty_result_row(row) for row in rows]
    total = len(rows)
    safety = 0
    max_steps = max(8, total * 2)

    while safety < max_steps:
        safety += 1
        step = take_enrichment_step(
            rows,
            results,
            wait_for_molster_quota=wait_for_molster_quota,
            on_progress=on_progress,
            expected_total=total,
        )
        if on_progress:
            on_progress(_finished_count(results), total, step.progress_item or "Enriching")
        if step.done:
            break
    else:
        raise EmailEnrichmentError("Email enrichment stopped before all rows finished.", transient=True)

    return results
