"""Single shared background job for Company and Person LinkedIn finders."""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from mailer import send_results_email
from config import settings
from fullenrich_client import enrich_contacts
from serper_search import find_linkedin_company_url, find_linkedin_person_url

logger = logging.getLogger(__name__)

_worker_lock = threading.Lock()
_state_lock = threading.Lock()

_job: dict[str, Any] = {
    "running": False,
    "job_type": "",  # company | person | email
    "email": "",
    "current": 0,
    "total": 0,
    "current_item": "",
    "error": None,
    "last_summary": "",
    "email_sent": False,
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_email(raw: str) -> str | None:
    email = (raw or "").strip()
    if not email:
        return "Email is required."
    if not EMAIL_RE.match(email):
        return "Enter a valid email address."
    return None


def job_snapshot() -> dict[str, Any]:
    with _state_lock:
        return dict(_job)


def is_job_running() -> bool:
    with _state_lock:
        return bool(_job["running"])


def is_system_busy() -> bool:
    return is_job_running() or _worker_lock.locked()


def try_acquire_worker() -> bool:
    """Exclusive Serper access (background job or synchronous single lookup)."""
    return _worker_lock.acquire(blocking=False)


def release_worker() -> None:
    if _worker_lock.locked():
        _worker_lock.release()


def _update_progress(current: int, total: int, current_item: str) -> None:
    with _state_lock:
        _job["current"] = current
        _job["total"] = total
        _job["current_item"] = current_item


def _run_company(companies: list[str], email: str, save_csv: Callable) -> tuple[str, str]:
    api_key = settings.serper_api_key or ""
    rows: list[dict[str, str]] = []
    total = len(companies)
    for idx, name in enumerate(companies, start=1):
        _update_progress(idx, total, name)
        logger.info("Company LinkedIn [%s/%s] %s", idx, total, name)
        search_query = f"{name} site:linkedin.com"
        found_url = find_linkedin_company_url(name, api_key, num=10, date_restrict=None)
        rows.append(
            {
                "company": name,
                "search_query": search_query,
                "linkedin_url": found_url or "",
                "status": "found" if found_url else "no_company_page_in_top_10",
            }
        )
    path = save_csv(rows)
    found_ct = sum(1 for r in rows if r.get("linkedin_url"))
    summary = (
        f"Processed {len(rows)} companies; {found_ct} LinkedIn company URLs found in the first 10 results."
    )
    return path, summary


def _run_email(rows: list[dict[str, str]], email: str, save_csv: Callable) -> tuple[str, str]:
    def on_progress(current: int, total: int, current_item: str) -> None:
        _update_progress(current, total, current_item)

    result_rows = enrich_contacts(rows, on_progress=on_progress)
    path = save_csv(result_rows)
    found_ct = sum(1 for r in result_rows if r.get("work_email"))
    summary = (
        f"Processed {len(result_rows)} contacts; {found_ct} verified work emails found via FullEnrich."
    )
    return path, summary


def _run_person(pairs: list[tuple[str, str]], email: str, save_csv: Callable) -> tuple[str, str]:
    api_key = settings.serper_api_key or ""
    rows: list[dict[str, str]] = []
    total = len(pairs)
    for idx, (person, company) in enumerate(pairs, start=1):
        label = f"{person} @ {company}"
        _update_progress(idx, total, label)
        logger.info("Person LinkedIn [%s/%s] %s", idx, total, label)
        search_query = f"{person} {company} site:linkedin.com"
        found_url = find_linkedin_person_url(person, company, api_key, num=10, date_restrict=None)
        rows.append(
            {
                "person": person,
                "company": company,
                "search_query": search_query,
                "linkedin_url": found_url or "",
                "status": "found" if found_url else "no_profile_in_top_10",
            }
        )
    path = save_csv(rows)
    found_ct = sum(1 for r in rows if r.get("linkedin_url"))
    summary = (
        f"Processed {len(rows)} rows; {found_ct} LinkedIn profile URLs found in the first 10 results."
    )
    return path, summary


def _worker(
    job_type: str,
    email: str,
    save_csv: Callable[[list], str],
    run_fn: Callable,
    work_arg: Any,
) -> None:
    if job_type == "company":
        subject_label = "Company"
    elif job_type == "email":
        subject_label = "Email"
    else:
        subject_label = "Person"
    try:
        job_name = "email enrichment" if job_type == "email" else f"{subject_label} LinkedIn"
        logger.info("%s job started for %s", job_name, email)
        path_str, summary = run_fn(work_arg, email, save_csv)
        path = Path(path_str)
        body = (
            f"Your {subject_label} LinkedIn finder job is complete.\n\n"
            f"{summary}\n\n"
            f"The CSV is attached.\n"
        )
        if job_type == "email":
            subject = "Email Finder — results ready"
            body = (
                "Your email enrichment job is complete.\n\n"
                f"{summary}\n\n"
                "The CSV is attached.\n"
            )
        else:
            subject = f"LinkedIn {subject_label} Finder — results ready"
        ok, err = send_results_email(
            email,
            subject=subject,
            body=body,
            attachment_path=path,
        )
        with _state_lock:
            _job["last_summary"] = summary
            _job["email_sent"] = ok
            if not ok:
                _job["error"] = err or "Failed to send email"
        if ok:
            logger.info("%s LinkedIn job finished; emailed %s", subject_label, email)
        else:
            logger.error("%s LinkedIn job finished but email failed: %s", subject_label, err)
    except Exception as exc:
        logger.exception("%s LinkedIn job failed", subject_label)
        with _state_lock:
            _job["error"] = str(exc)
            _job["last_summary"] = f"Job failed: {exc}"
    finally:
        with _state_lock:
            _job["running"] = False
            _job["current_item"] = ""
        _worker_lock.release()
        logger.info("LinkedIn job lock released")


def start_company_job(companies: list[str], email: str, save_csv: Callable[[list], str]) -> tuple[bool, str | None]:
    if not companies:
        return False, "No companies to process."
    if not _worker_lock.acquire(blocking=False):
        return False, _busy_message()
    with _state_lock:
        if _job["running"]:
            _worker_lock.release()
            return False, _busy_message()
        _job.update(
            running=True,
            job_type="company",
            email=email,
            current=0,
            total=len(companies),
            current_item="",
            error=None,
            last_summary="",
            email_sent=False,
        )
    thread = threading.Thread(
        target=_worker,
        args=("company", email, save_csv, _run_company, companies),
        daemon=True,
        name="linkedin-company-job",
    )
    thread.start()
    return True, None


def start_email_job(
    rows: list[dict[str, str]],
    email: str,
    save_csv: Callable[[list], str],
) -> tuple[bool, str | None]:
    if not rows:
        return False, "No rows to process."
    if not _worker_lock.acquire(blocking=False):
        return False, _busy_message()
    with _state_lock:
        if _job["running"]:
            _worker_lock.release()
            return False, _busy_message()
        _job.update(
            running=True,
            job_type="email",
            email=email,
            current=0,
            total=len(rows),
            current_item="",
            error=None,
            last_summary="",
            email_sent=False,
        )
    thread = threading.Thread(
        target=_worker,
        args=("email", email, save_csv, _run_email, rows),
        daemon=True,
        name="email-enrichment-job",
    )
    thread.start()
    return True, None


def start_person_job(
    pairs: list[tuple[str, str]],
    email: str,
    save_csv: Callable[[list], str],
) -> tuple[bool, str | None]:
    if not pairs:
        return False, "No rows to process."
    if not _worker_lock.acquire(blocking=False):
        return False, _busy_message()
    with _state_lock:
        if _job["running"]:
            _worker_lock.release()
            return False, _busy_message()
        _job.update(
            running=True,
            job_type="person",
            email=email,
            current=0,
            total=len(pairs),
            current_item="",
            error=None,
            last_summary="",
            email_sent=False,
        )
    thread = threading.Thread(
        target=_worker,
        args=("person", email, save_csv, _run_person, pairs),
        daemon=True,
        name="linkedin-person-job",
    )
    thread.start()
    return True, None


def _busy_message() -> str:
    snap = job_snapshot()
    if snap.get("running"):
        jt = snap.get("job_type") or "LinkedIn"
        if jt == "company":
            label = "Company"
        elif jt == "person":
            label = "Person"
        elif jt == "email":
            label = "Email"
        else:
            label = "LinkedIn"
        return (
            f"A {label} LinkedIn lookup is already in progress "
            f"({snap.get('current', 0)} / {snap.get('total', 0)}). "
            "Please wait until it finishes."
        )
    return "Another lookup is in progress. Please wait and try again."


def progress_display() -> dict[str, Any]:
    """Template-friendly progress block for both finder pages."""
    snap = job_snapshot()
    running = bool(snap.get("running"))
    job_type = snap.get("job_type") or ""
    if job_type == "company":
        type_label = "Company LinkedIn finder"
    elif job_type == "person":
        type_label = "Person LinkedIn finder"
    elif job_type == "email":
        type_label = "Email finder"
    else:
        type_label = "LinkedIn finder"

    line = ""
    if running:
        cur = snap.get("current") or 0
        tot = snap.get("total") or 0
        item = snap.get("current_item") or ""
        line = f"{type_label}: processing {cur} / {tot}"
        if item:
            line += f" — {item}"

    return {
        "job_running": running,
        "server_busy": is_system_busy(),
        "progress_line": line,
        "job_type": job_type,
        "job_email_masked": _mask_email(snap.get("email") or ""),
        "last_summary": snap.get("last_summary") or "",
        "last_error": snap.get("error"),
        "finished_at_hint": datetime.now().strftime("%Y-%m-%d %H:%M UTC") if not running and snap.get("last_summary") else "",
    }


def _mask_email(email: str) -> str:
    if "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "***"
    else:
        masked_local = local[0] + "***" + local[-1]
    return f"{masked_local}@{domain}"
