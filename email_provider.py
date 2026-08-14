"""Email enrichment waterfall: Molster first, FullEnrich fallback."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from fullenrich_client import (
    BATCH_SIZE as FULLENRICH_BATCH_SIZE,
    FullEnrichError,
    enrich_batch as fullenrich_batch,
    is_valid_linkedin_url,
)
from molster_client import (
    BATCH_SIZE as MOLSTER_BATCH_SIZE,
    MolsterError,
    MolsterFairUseExhausted,
    fair_use_reset_ts,
    linkedin_match_key,
    lookup_linkedin_urls,
    molster_configured,
)
from config import settings

logger = logging.getLogger(__name__)

STATUS_NOT_ENRICHED = "not_enriched"
STATUS_MOLSTER_MISS = "molster_miss"
STATUS_FOUND = "found"
STATUS_NO_EMAIL = "no_email_found"

ProgressCb = Callable[[int, int, str], None]
StartedCb = Callable[[str], None]
FullEnrichStartedCb = Callable[[str, list[int]], None]


class EmailEnrichmentError(Exception):
    """Raised when the Molster → FullEnrich waterfall cannot continue."""

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
    pending_enrichment_id: str = ""
    pending_fullenrich_indexes: list[int] = field(default_factory=list)
    newly_finished: list[dict[str, Any]] = field(default_factory=list)
    progress_item: str = ""


def fullenrich_configured() -> bool:
    return bool((settings.fullenrich_api_key or "").strip())


def email_providers_configured() -> bool:
    return molster_configured() or fullenrich_configured()


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


def needs_fullenrich(row: dict[str, Any]) -> bool:
    if (row.get("work_email") or "").strip():
        return False
    status = (row.get("status") or "").strip()
    return status == STATUS_MOLSTER_MISS


def _finished_count(results: list[dict[str, Any]]) -> int:
    done = {STATUS_FOUND, STATUS_NO_EMAIL}
    return sum(1 for row in results if (row.get("status") or "") in done)


def _indexes(results: list[dict[str, Any]], predicate) -> list[int]:
    return [i for i, row in enumerate(results) if predicate(row)]


def _mark_molster_miss(row: dict[str, Any], *, molster_status: str = "not_found") -> dict[str, Any]:
    updated = dict(row)
    updated["status"] = STATUS_MOLSTER_MISS
    updated["molster_status"] = molster_status
    if not fullenrich_configured():
        updated["status"] = STATUS_NO_EMAIL
    return updated


def _mark_molster_hit(row: dict[str, Any], hit: dict[str, str]) -> dict[str, Any]:
    email = (hit.get("email") or "").strip()
    risk = (hit.get("risk_score") or "").strip()
    updated = dict(row)
    updated.update(
        {
            "work_email": email,
            "email_status": risk,
            "all_work_emails": email,
            "status": STATUS_FOUND,
            "email_source": "molster",
            "molster_status": hit.get("status") or "ok",
            "molster_risk_score": risk,
            "molster_last_validated_at": hit.get("last_validated_at") or "",
        }
    )
    return updated


def _apply_fullenrich_row(row: dict[str, Any], fe_row: dict[str, Any]) -> dict[str, Any]:
    updated = dict(row)
    email = (fe_row.get("work_email") or "").strip()
    updated["job_title"] = fe_row.get("job_title") or updated.get("job_title") or ""
    if email:
        updated.update(
            {
                "work_email": email,
                "email_status": fe_row.get("email_status") or "",
                "all_work_emails": fe_row.get("all_work_emails") or email,
                "status": STATUS_FOUND,
                "email_source": "fullenrich",
            }
        )
    else:
        updated["status"] = STATUS_NO_EMAIL
        if not updated.get("email_status"):
            updated["email_status"] = fe_row.get("email_status") or ""
        if not updated.get("all_work_emails"):
            updated["all_work_emails"] = fe_row.get("all_work_emails") or ""
    return updated


def _skip_molster_to_fallback(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """When Molster cannot run, send remaining rows straight to FullEnrich."""
    newly: list[dict[str, Any]] = []
    for i, row in enumerate(results):
        if not needs_molster(row):
            continue
        updated = _mark_molster_miss(row, molster_status="skipped")
        results[i] = updated
        if updated.get("status") == STATUS_NO_EMAIL:
            newly.append(updated)
    return newly


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
    existing_enrichment_id: str | None = None,
    pending_fullenrich_indexes: list[int] | None = None,
    on_progress: ProgressCb | None = None,
    on_enrichment_started: StartedCb | None = None,
    on_fullenrich_started: FullEnrichStartedCb | None = None,
    expected_total: int | None = None,
) -> StepResult:
    """
    One unit of work: Molster batch (up to 100) or FullEnrich batch (up to 50).

    Prefers Molster while quota remains so the 5h window is used as a burst.
    FullEnrich runs on Molster misses, including while Molster quota is exhausted.
    """
    total = expected_total or len(input_rows)
    pending_indexes = list(pending_fullenrich_indexes or [])

    def progress(item: str) -> None:
        if on_progress:
            on_progress(_finished_count(results), total, item)

    if existing_enrichment_id and pending_indexes:
        progress(f"Resuming FullEnrich ({len(pending_indexes)} contacts)")
        return _run_fullenrich(
            input_rows,
            results,
            pending_indexes,
            existing_enrichment_id=existing_enrichment_id,
            on_progress=on_progress,
            on_enrichment_started=on_enrichment_started,
            on_fullenrich_started=on_fullenrich_started,
            expected_total=total,
        )

    molster_idxs = _indexes(results, needs_molster)
    fullenrich_idxs = _indexes(results, needs_fullenrich)
    molster_blocked = False
    skipped_newly: list[dict[str, Any]] = []

    if molster_idxs and not molster_configured():
        skipped_newly = _skip_molster_to_fallback(results)
        fullenrich_idxs = _indexes(results, needs_fullenrich)
        molster_idxs = []
        if not fullenrich_idxs:
            return StepResult(
                done=_finished_count(results) >= total,
                newly_finished=skipped_newly,
                progress_item="Molster not configured",
            )

    if molster_idxs and molster_configured():
        try:
            batch_idxs = molster_idxs[:MOLSTER_BATCH_SIZE]
            progress(f"Molster batch ({len(batch_idxs)} LinkedIn URLs)")
            return _run_molster(input_rows, results, batch_idxs)
        except MolsterFairUseExhausted as exc:
            molster_blocked = True
            if fullenrich_idxs and fullenrich_configured():
                logger.info(
                    "Molster quota exhausted; FullEnriching %s misses while waiting",
                    len(fullenrich_idxs),
                )
            elif wait_for_molster_quota:
                raise _quota_error(exc) from exc
            else:
                skipped_newly = _skip_molster_to_fallback(results)
                fullenrich_idxs = _indexes(results, needs_fullenrich)
                if not fullenrich_idxs:
                    return StepResult(
                        done=True,
                        newly_finished=skipped_newly,
                        progress_item="Molster skipped",
                    )

    fullenrich_idxs = _indexes(results, needs_fullenrich)
    if fullenrich_idxs and fullenrich_configured():
        batch_idxs = fullenrich_idxs[:FULLENRICH_BATCH_SIZE]
        progress(f"FullEnrich fallback ({len(batch_idxs)} contacts)")
        return _run_fullenrich(
            input_rows,
            results,
            batch_idxs,
            existing_enrichment_id=None,
            on_progress=on_progress,
            on_enrichment_started=on_enrichment_started,
            on_fullenrich_started=on_fullenrich_started,
            expected_total=total,
        )

    if fullenrich_idxs and not fullenrich_configured():
        newly = list(skipped_newly)
        for i in fullenrich_idxs:
            results[i] = dict(results[i])
            results[i]["status"] = STATUS_NO_EMAIL
            newly.append(results[i])
        return StepResult(
            done=_finished_count(results) >= total,
            newly_finished=newly,
            progress_item="FullEnrich not configured",
        )

    still_molster = _indexes(results, needs_molster)
    if still_molster and molster_blocked and wait_for_molster_quota:
        raise _quota_error()

    if still_molster and not wait_for_molster_quota:
        newly = _skip_molster_to_fallback(results)
        return StepResult(
            done=_finished_count(results) >= total and not _indexes(results, needs_fullenrich),
            newly_finished=newly,
            progress_item="Molster skipped",
        )

    return StepResult(done=True, newly_finished=skipped_newly, progress_item="Enrichment complete")


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

        for url in ordered_urls:
            key = linkedin_match_key(url)
            hit = by_key.get(key) or {}
            email = (hit.get("email") or "").strip()
            for i in url_to_indexes.get(key, []):
                if email:
                    results[i] = _mark_molster_hit(results[i], hit)
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


def _run_fullenrich(
    input_rows: list[dict[str, Any]],
    results: list[dict[str, Any]],
    indexes: list[int],
    *,
    existing_enrichment_id: str | None,
    on_progress: ProgressCb | None,
    on_enrichment_started: StartedCb | None,
    on_fullenrich_started: FullEnrichStartedCb | None = None,
    expected_total: int,
) -> StepResult:
    batch = []
    for i in indexes:
        src = input_rows[i]
        batch.append(
            {
                "person": src.get("person") or results[i].get("person") or "",
                "company": src.get("company") or results[i].get("company") or "",
                "linkedin_url": src.get("linkedin_url") or results[i].get("linkedin_url") or "",
                "row_index": str(i),
                "original": src.get("original") or results[i].get("original") or {},
                "_fieldnames": src.get("_fieldnames") or results[i].get("_fieldnames") or [],
            }
        )

    def _started(enrichment_id: str) -> None:
        if on_fullenrich_started:
            on_fullenrich_started(enrichment_id, indexes)
        if on_enrichment_started:
            on_enrichment_started(enrichment_id)

    try:
        enriched = fullenrich_batch(
            batch,
            batch_label="Molster fallback FullEnrich",
            on_progress=on_progress,
            expected_total=expected_total,
            existing_enrichment_id=existing_enrichment_id,
            on_enrichment_started=_started if not existing_enrichment_id else None,
        )
    except FullEnrichError as exc:
        raise EmailEnrichmentError(str(exc), transient=exc.transient) from exc

    newly: list[dict[str, Any]] = []
    for i, fe_row in zip(indexes, enriched):
        results[i] = _apply_fullenrich_row(results[i], fe_row)
        newly.append(results[i])

    found = sum(1 for row in newly if row.get("email_source") == "fullenrich")
    logger.info("FullEnrich fallback step: %s contacts, %s emails", len(indexes), found)
    return StepResult(
        done=False,
        newly_finished=newly,
        progress_item=f"FullEnrich fallback {len(indexes)} contacts ({found} emails)",
    )


def enrich_contacts(
    rows: list[dict[str, str]],
    on_progress: Callable[[int, int, str], None] | None = None,
    *,
    wait_for_molster_quota: bool = False,
) -> list[dict[str, str]]:
    """Enrich contacts via Molster then FullEnrich (non-resumable)."""
    if not rows:
        return []
    if not email_providers_configured():
        raise EmailEnrichmentError("Missing MOLSTER_API_KEY and FULLENRICH_API_KEY in environment.")

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

    for i, row in enumerate(results):
        if row.get("status") == STATUS_MOLSTER_MISS:
            results[i] = dict(row)
            results[i]["status"] = STATUS_NO_EMAIL
    return results
