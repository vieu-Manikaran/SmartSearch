"""Persistent storage for resumable email enrichment jobs."""

from __future__ import annotations

import csv
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

JOBS_ROOT = Path("data/email_enrichment_jobs")

EMAIL_ENRICHMENT_EXTRA_COLUMNS = [
    "Work_Email",
    "Email_Status",
    "All_Work_Emails",
    "Job_Title",
    "Enrichment_Status",
]

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_INTERRUPTED = "interrupted"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

RESUMABLE_STATUSES = {STATUS_PENDING, STATUS_RUNNING, STATUS_INTERRUPTED, STATUS_FAILED}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def job_dir(job_id: str) -> Path:
    return JOBS_ROOT / job_id


def create_job(rows: list[dict[str, Any]], recipient_email: str) -> str:
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    path = job_dir(job_id)
    path.mkdir(parents=True, exist_ok=False)

    meta = {
        "job_id": job_id,
        "recipient_email": recipient_email,
        "status": STATUS_PENDING,
        "total": len(rows),
        "processed": 0,
        "batches_completed": 0,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "started_at": "",
        "completed_at": "",
        "error": "",
        "summary": "",
        "email_sent": False,
        "results_file": "results.csv",
    }
    save_meta(job_id, meta)
    save_input_rows(job_id, rows)
    save_checkpoint(job_id, {"batches_completed": 0, "rows_processed": 0})
    logger.info("Created email enrichment job %s (%s rows) for %s", job_id, len(rows), recipient_email)
    return job_id


def save_meta(job_id: str, meta: dict[str, Any]) -> None:
    meta["updated_at"] = _utc_now()
    path = job_dir(job_id) / "meta.json"
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def load_meta(job_id: str) -> dict[str, Any] | None:
    path = job_dir(job_id) / "meta.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_input_rows(job_id: str, rows: list[dict[str, Any]]) -> None:
    path = job_dir(job_id) / "input.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def load_input_rows(job_id: str) -> list[dict[str, Any]]:
    path = job_dir(job_id) / "input.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def save_checkpoint(job_id: str, checkpoint: dict[str, Any]) -> None:
    path = job_dir(job_id) / "checkpoint.json"
    path.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")


def load_checkpoint(job_id: str) -> dict[str, Any]:
    path = job_dir(job_id) / "checkpoint.json"
    if not path.is_file():
        return {"batches_completed": 0, "rows_processed": 0}
    return json.loads(path.read_text(encoding="utf-8"))


def results_path(job_id: str) -> Path:
    return job_dir(job_id) / "results.csv"


def write_results_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original_fieldnames: list[str] = []
    if rows and rows[0].get("_fieldnames"):
        original_fieldnames = list(rows[0]["_fieldnames"])
    elif rows and rows[0].get("original"):
        original_fieldnames = list(rows[0]["original"].keys())
    else:
        original_fieldnames = ["Person", "Company", "LinkedIn_URL"]

    fieldnames = original_fieldnames + [
        col for col in EMAIL_ENRICHMENT_EXTRA_COLUMNS if col not in original_fieldnames
    ]

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row.get("original") or {})
            if not out:
                out = {
                    "Person": row.get("person") or "",
                    "Company": row.get("company") or "",
                    "LinkedIn_URL": row.get("linkedin_url") or "",
                }
            out.update(
                {
                    "Work_Email": row.get("work_email") or "",
                    "Email_Status": row.get("email_status") or "",
                    "All_Work_Emails": row.get("all_work_emails") or "",
                    "Job_Title": row.get("job_title") or "",
                    "Enrichment_Status": row.get("status") or "",
                }
            )
            writer.writerow(out)


def results_state_path(job_id: str) -> Path:
    return job_dir(job_id) / "results_state.json"


def save_results_state(job_id: str, rows: list[dict[str, Any]]) -> None:
    results_state_path(job_id).write_text(
        json.dumps(rows, ensure_ascii=False),
        encoding="utf-8",
    )


def load_results_state(job_id: str, input_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    path = results_state_path(job_id)
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list) and len(data) == len(input_rows):
            return data
    return [_empty_result_row(row) for row in input_rows]


def _base_result_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "person": row.get("person") or "",
        "company": row.get("company") or "",
        "linkedin_url": row.get("linkedin_url") or "",
        "original": row.get("original") or {},
        "_fieldnames": row.get("_fieldnames") or [],
    }


def _empty_result_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **_base_result_row(row),
        "work_email": "",
        "email_status": "",
        "all_work_emails": "",
        "job_title": "",
        "status": "not_enriched",
    }


def list_jobs_by_status(*statuses: str) -> list[dict[str, Any]]:
    if not JOBS_ROOT.is_dir():
        return []
    jobs: list[dict[str, Any]] = []
    for child in sorted(JOBS_ROOT.iterdir()):
        if not child.is_dir():
            continue
        meta = load_meta(child.name)
        if meta and meta.get("status") in statuses:
            jobs.append(meta)
    return jobs


def count_pending_jobs() -> int:
    return len(list_jobs_by_status(STATUS_PENDING))


def pick_next_job_id() -> str | None:
    """Oldest resumable job: interrupted/running first, then pending."""
    for status in (STATUS_INTERRUPTED, STATUS_RUNNING, STATUS_PENDING):
        jobs = list_jobs_by_status(status)
        if jobs:
            return jobs[0]["job_id"]
    return None
