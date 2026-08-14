"""Resumable queued email enrichment jobs with disk checkpoints."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from email_enrichment_store import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_PENDING,
    STATUS_RUNNING,
    count_pending_jobs,
    create_job,
    load_checkpoint,
    load_input_rows,
    load_meta,
    load_results_state,
    pick_next_job_id,
    results_path,
    save_checkpoint,
    save_meta,
    save_results_state,
    write_results_csv,
)
from email_provider import EmailEnrichmentError, take_enrichment_step
from fullenrich_client import FullEnrichError, sanitize_error_message
from linkedin_jobs import _email_job, _state_lock, _update_progress, _worker_lock
from mailer import send_results_email
from molster_client import MolsterError
from seeqe_email_callback import sync_rows_to_seeqe

logger = logging.getLogger(__name__)

_queue_lock = threading.Lock()
_worker_started = False


def submit_email_enrichment_job(rows: list[dict[str, Any]], recipient_email: str) -> tuple[bool, str | None, str | None]:
    """
    Queue a persistent enrichment job. Returns (ok, error_message, job_id).
    Jobs are processed one at a time and survive server restarts.
    """
    if not rows:
        return False, "No rows to process.", None
    job_id = create_job(rows, recipient_email)
    _ensure_queue_worker()
    with _state_lock:
        if not _email_job.get("running"):
            meta = load_meta(job_id) or {}
            _email_job.update(
                running=False,
                job_type="email",
                email=recipient_email,
                current=0,
                total=meta.get("total") or len(rows),
                current_item="Queued",
                error=None,
                last_summary="",
                email_sent=False,
            )
    return True, None, job_id


def resume_pending_jobs_on_startup() -> None:
    """Resume interrupted or queued jobs after deploy/restart."""
    from email_enrichment_store import JOBS_ROOT, list_jobs_by_status, requeue_retryable_failed_jobs

    if not JOBS_ROOT.is_dir():
        return
    requeue_retryable_failed_jobs()
    for meta in list_jobs_by_status(STATUS_RUNNING, STATUS_INTERRUPTED):
        job_id = meta["job_id"]
        meta["status"] = STATUS_INTERRUPTED
        meta["retry_after_ts"] = 0
        meta["error"] = meta.get("error") or "Interrupted by server restart; resuming."
        save_meta(job_id, meta)
        logger.info("Marked job %s for resume after restart", job_id)
    _ensure_queue_worker()


def retry_email_job(job_id: str) -> tuple[bool, str | None]:
    """Re-queue a failed or interrupted job."""
    meta = load_meta(job_id)
    if not meta:
        return False, "Job not found."
    if meta.get("status") == STATUS_COMPLETED:
        return False, "Job already completed."
    meta["status"] = STATUS_INTERRUPTED
    meta["retry_count"] = 0
    meta["retry_after_ts"] = 0
    meta["error"] = ""
    meta["summary"] = "Re-queued for processing."
    save_meta(job_id, meta)
    _ensure_queue_worker()
    return True, None


def get_job_public_status(job_id: str) -> dict[str, Any] | None:
    meta = load_meta(job_id)
    if not meta:
        return None
    checkpoint = load_checkpoint(job_id)
    error = sanitize_error_message(meta.get("error") or "")
    return {
        "job_id": job_id,
        "status": meta.get("status"),
        "total": meta.get("total"),
        "processed": meta.get("processed"),
        "batches_completed": checkpoint.get("batches_completed"),
        "recipient_email": meta.get("recipient_email"),
        "email_sent": bool(meta.get("email_sent")),
        "error": error,
        "summary": meta.get("summary"),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "queue_pending": count_pending_jobs(),
    }


def _ensure_queue_worker() -> None:
    global _worker_started
    with _queue_lock:
        if _worker_started:
            return
        _worker_started = True
        thread = threading.Thread(target=_queue_worker_loop, daemon=True, name="email-enrichment-queue")
        thread.start()


def _queue_worker_loop() -> None:
    while True:
        job_id = pick_next_job_id()
        if not job_id:
            time.sleep(5)
            continue
        if not _worker_lock.acquire(blocking=False):
            time.sleep(3)
            continue
        try:
            _process_job(job_id)
        except Exception:
            logger.exception("Unexpected error in queue worker for job %s", job_id)
            time.sleep(30)
        finally:
            if _worker_lock.locked():
                _worker_lock.release()
            time.sleep(10)


def _process_job(job_id: str) -> None:
    meta = load_meta(job_id)
    if not meta:
        return

    input_rows = load_input_rows(job_id)
    if not input_rows:
        meta["status"] = STATUS_FAILED
        meta["error"] = "Job input missing."
        save_meta(job_id, meta)
        return

    recipient = meta.get("recipient_email") or ""
    total = len(input_rows)
    checkpoint = load_checkpoint(job_id)
    batches_completed = int(checkpoint.get("batches_completed") or 0)
    results = load_results_state(job_id, input_rows)

    meta["status"] = STATUS_RUNNING
    if not meta.get("started_at"):
        meta["started_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    meta["error"] = ""
    save_meta(job_id, meta)

    with _state_lock:
        _email_job.update(
            running=True,
            job_type="email",
            email=recipient,
            current=int(checkpoint.get("rows_processed") or 0),
            total=total,
            current_item=f"Resuming batch {batches_completed + 1}" if batches_completed else "Starting",
            error=None,
            last_summary="",
            email_sent=False,
        )

    logger.info(
        "Processing email job %s: %s rows, %s batches already done",
        job_id,
        total,
        batches_completed,
    )

    pending_enrichment_id = (checkpoint.get("pending_enrichment_id") or "").strip()
    pending_fullenrich_indexes = [
        int(i) for i in (checkpoint.get("pending_fullenrich_indexes") or [])
    ]
    if pending_enrichment_id and not pending_fullenrich_indexes:
        start = int(checkpoint.get("pending_batch_start") or -1)
        if start >= 0:
            pending_fullenrich_indexes = list(range(start, min(start + 50, total)))

    def _save_checkpoint(
        *,
        rows_processed: int,
        pending_id: str = "",
        pending_indexes: list[int] | None = None,
    ) -> None:
        save_checkpoint(
            job_id,
            {
                "batches_completed": int(rows_processed // 100),
                "rows_processed": rows_processed,
                "pending_enrichment_id": pending_id,
                "pending_batch_start": pending_indexes[0] if pending_indexes else -1,
                "pending_fullenrich_indexes": pending_indexes or [],
            },
        )

    try:
        safety = 0
        max_steps = max(16, total * 3)
        while safety < max_steps:
            safety += 1

            def on_progress(current: int, tot: int, item: str) -> None:
                _update_progress("email", min(current, total), total, item or "Enriching")

            def on_fullenrich_started(enrichment_id: str, indexes: list[int]) -> None:
                nonlocal pending_enrichment_id, pending_fullenrich_indexes
                pending_enrichment_id = enrichment_id
                pending_fullenrich_indexes = list(indexes)
                rows_now = sum(
                    1
                    for row in results
                    if (row.get("status") or "") in {"found", "no_email_found"}
                )
                _save_checkpoint(
                    rows_processed=rows_now,
                    pending_id=enrichment_id,
                    pending_indexes=indexes,
                )
                logger.info(
                    "Job %s saved pending FullEnrich %s (%s contacts)",
                    job_id,
                    enrichment_id,
                    len(indexes),
                )

            step = take_enrichment_step(
                input_rows,
                results,
                wait_for_molster_quota=True,
                existing_enrichment_id=pending_enrichment_id or None,
                pending_fullenrich_indexes=pending_fullenrich_indexes,
                on_progress=on_progress,
                on_fullenrich_started=on_fullenrich_started,
                expected_total=total,
            )

            pending_enrichment_id = ""
            pending_fullenrich_indexes = []
            if step.newly_finished:
                posted, callback_failed = sync_rows_to_seeqe(step.newly_finished)
                if posted or callback_failed:
                    logger.info(
                        "Job %s Seeqe callbacks: %s posted, %s failed",
                        job_id,
                        posted,
                        callback_failed,
                    )

            rows_processed = sum(
                1
                for row in results
                if (row.get("status") or "") in {"found", "no_email_found"}
            )
            save_results_state(job_id, results)
            write_results_csv(results_path(job_id), results)
            _save_checkpoint(rows_processed=rows_processed)

            meta = load_meta(job_id) or meta
            meta["processed"] = rows_processed
            meta["batches_completed"] = int(rows_processed // 100)
            save_meta(job_id, meta)
            _update_progress(
                "email",
                rows_processed,
                total,
                step.progress_item or f"Saved checkpoint ({rows_processed}/{total})",
            )
            logger.info(
                "Job %s checkpoint: %s/%s rows (%s)",
                job_id,
                rows_processed,
                total,
                step.progress_item,
            )

            if step.done:
                break
        else:
            raise EmailEnrichmentError(
                "Email enrichment stopped before all rows finished.",
                transient=True,
            )

        molster_ct = sum(1 for r in results if r.get("email_source") == "molster")
        fullenrich_ct = sum(1 for r in results if r.get("email_source") == "fullenrich")
        found_ct = sum(1 for r in results if r.get("work_email"))
        for row in results:
            if (row.get("status") or "") == "molster_miss":
                row["status"] = "no_email_found"
        summary = (
            f"Processed {len(results)} contacts; {found_ct} work emails found "
            f"({molster_ct} via Molster, {fullenrich_ct} via FullEnrich fallback)."
        )
        final_path = results_path(job_id)
        write_results_csv(final_path, results)

        ok, err = send_results_email(
            recipient,
            subject="Email Finder — results ready",
            body=(
                "Your email enrichment job is complete.\n\n"
                f"Job ID: {job_id}\n"
                f"{summary}\n\n"
                "The CSV is attached.\n"
            ),
            attachment_path=final_path,
        )

        meta = load_meta(job_id) or meta
        meta["status"] = STATUS_COMPLETED
        meta["processed"] = total
        meta["summary"] = summary
        meta["email_sent"] = ok
        meta["completed_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        meta["retry_after_ts"] = 0
        if ok:
            meta["error"] = ""
        else:
            meta["error"] = err or "Failed to send email"
        save_meta(job_id, meta)

        archive_copy = Path("data/email_enrichment") / f"{job_id}_email_enrichment.csv"
        archive_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(final_path, archive_copy)

        with _state_lock:
            _email_job["last_summary"] = summary
            _email_job["email_sent"] = ok
            if not ok:
                _email_job["error"] = err or "Failed to send email"

        if ok:
            logger.info("Job %s completed; emailed %s", job_id, recipient)
        else:
            logger.error("Job %s completed but email failed: %s", job_id, err)

    except Exception as exc:
        logger.exception("Job %s failed after %s/%s contacts", job_id, meta.get("processed") or 0, total)
        meta = load_meta(job_id) or meta
        retry_after_ts = float(getattr(exc, "retry_after_ts", 0) or 0)
        transient = bool(getattr(exc, "transient", False))
        if isinstance(exc, (EmailEnrichmentError, FullEnrichError, MolsterError)):
            transient = transient or bool(getattr(exc, "transient", False))

        if retry_after_ts > time.time():
            wait_sec = int(retry_after_ts - time.time())
            meta["status"] = STATUS_INTERRUPTED
            meta["retry_after_ts"] = retry_after_ts
            meta["summary"] = (
                f"Paused after {meta.get('processed', 0)}/{total} contacts; "
                f"Molster window resets in ~{max(1, wait_sec // 60)} min."
            )
        else:
            retry_count = int(meta.get("retry_count") or 0) + 1
            meta["retry_count"] = retry_count
            backoff_sec = min(300, 30 * (2 ** min(retry_count - 1, 4)))
            if retry_count > 20 and not transient:
                meta["status"] = STATUS_FAILED
                meta["summary"] = f"Job stopped after {retry_count} errors."
            else:
                meta["status"] = STATUS_INTERRUPTED
                meta["retry_after_ts"] = time.time() + backoff_sec
                meta["summary"] = (
                    f"Paused after {meta.get('processed', 0)}/{total} contacts; "
                    f"retrying in ~{backoff_sec // 60 or 1} min."
                )
        meta["error"] = sanitize_error_message(str(exc))
        save_meta(job_id, meta)
        save_results_state(job_id, results)
        write_results_csv(results_path(job_id), results)
        with _state_lock:
            _email_job["error"] = str(exc)
            _email_job["last_summary"] = meta["summary"]
    finally:
        with _state_lock:
            _email_job["running"] = False
            _email_job["current_item"] = ""
