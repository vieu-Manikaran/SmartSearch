"""Background jobs for Serper LinkedIn finders, RapidAPI URN resolver, and shared progress."""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from mailer import send_results_email
from config import settings
from rapidapi_linkedin_company import enrich_companies_batch
from rapidapi_person_deep import resolve_profiles_batch, resolve_vanity_url
from person_linkedin_finder import find_person_linkedin
from serper_search import find_linkedin_company_url

RAPIDAPI_JOB_TYPES = {"urn_resolve", "company_enrich", "vendor_file"}

logger = logging.getLogger(__name__)

_serper_lock = threading.Lock()
_rapidapi_lock = threading.Lock()
_email_lock = threading.Lock()
_state_lock = threading.Lock()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _blank_job() -> dict[str, Any]:
    return {
        "running": False,
        "job_type": "",
        "email": "",
        "current": 0,
        "total": 0,
        "current_item": "",
        "error": None,
        "last_summary": "",
        "email_sent": False,
    }


_serper_job: dict[str, Any] = _blank_job()
_rapidapi_job: dict[str, Any] = _blank_job()
_email_job: dict[str, Any] = _blank_job()


def validate_email(raw: str) -> str | None:
    email = (raw or "").strip()
    if not email:
        return "Email is required."
    if not EMAIL_RE.match(email):
        return "Enter a valid email address."
    return None


def _job_for_type(job_type: str) -> dict[str, Any]:
    if job_type in RAPIDAPI_JOB_TYPES:
        return _rapidapi_job
    if job_type == "email":
        return _email_job
    return _serper_job


def _lock_for_type(job_type: str) -> threading.Lock:
    if job_type in RAPIDAPI_JOB_TYPES:
        return _rapidapi_lock
    if job_type == "email":
        return _email_lock
    return _serper_lock


def job_snapshot(job_type: str | None = None) -> dict[str, Any]:
    with _state_lock:
        if job_type in RAPIDAPI_JOB_TYPES or job_type == "rapidapi":
            return dict(_rapidapi_job)
        if job_type == "email":
            return dict(_email_job)
        if job_type in {"company", "person", "serper"}:
            return dict(_serper_job)
        for job in (_serper_job, _rapidapi_job, _email_job):
            if job["running"]:
                return dict(job)
        return {
            "serper": dict(_serper_job),
            "rapidapi": dict(_rapidapi_job),
            "email": dict(_email_job),
        }


def is_serper_job_running() -> bool:
    with _state_lock:
        return bool(_serper_job["running"])


def is_rapidapi_job_running() -> bool:
    with _state_lock:
        return bool(_rapidapi_job["running"])


def is_email_job_running() -> bool:
    with _state_lock:
        return bool(_email_job["running"])


def is_job_running() -> bool:
    """True if any Serper or RapidAPI background job is running."""
    return is_serper_job_running() or is_rapidapi_job_running()


def is_serper_busy() -> bool:
    return is_serper_job_running() or _serper_lock.locked()


def is_rapidapi_busy() -> bool:
    return is_rapidapi_job_running() or _rapidapi_lock.locked()


def is_email_busy() -> bool:
    return is_email_job_running() or _email_lock.locked()


def is_system_busy() -> bool:
    return is_serper_busy() or is_rapidapi_busy() or is_email_busy()


def try_acquire_serper_worker() -> bool:
    return _serper_lock.acquire(blocking=False)


def release_serper_worker() -> None:
    if _serper_lock.locked():
        _serper_lock.release()


def try_acquire_rapidapi_worker() -> bool:
    return _rapidapi_lock.acquire(blocking=False)


def release_rapidapi_worker() -> None:
    if _rapidapi_lock.locked():
        _rapidapi_lock.release()


def try_acquire_email_worker() -> bool:
    return _email_lock.acquire(blocking=False)


def release_email_worker() -> None:
    if _email_lock.locked():
        _email_lock.release()


# Backward-compatible aliases for email enrichment queue.
try_acquire_worker = try_acquire_email_worker
release_worker = release_email_worker
_worker_lock = _email_lock


def _update_progress(job_type: str, current: int, total: int, current_item: str) -> None:
    with _state_lock:
        job = _job_for_type(job_type)
        job["current"] = current
        job["total"] = total
        job["current_item"] = current_item


def _run_company(companies: list[str], email: str, save_csv: Callable) -> tuple[str, str]:
    api_key = settings.serper_api_key or ""
    rows: list[dict[str, str]] = []
    total = len(companies)
    for idx, name in enumerate(companies, start=1):
        _update_progress("company", idx, total, name)
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


def _run_person(pairs: list[tuple[str, str]], email: str, save_csv: Callable) -> tuple[str, str]:
    api_key = settings.serper_api_key or ""
    rows: list[dict[str, str]] = []
    total = len(pairs)
    for idx, (person, company) in enumerate(pairs, start=1):
        label = f"{person} @ {company}"
        _update_progress("person", idx, total, label)
        logger.info("Person LinkedIn [%s/%s] %s", idx, total, label)
        search_query = f"{person} {company} site:linkedin.com"
        match = find_person_linkedin(person, company, serper_api_key=api_key)
        rows.append(
            {
                "person": person,
                "company": company,
                "search_query": match.search_query,
                "linkedin_url": match.url or "",
                "status": match.status,
                "match_score": str(match.score),
                "source": match.source,
            }
        )
    path = save_csv(rows)
    found_ct = sum(1 for r in rows if r.get("linkedin_url"))
    summary = (
        f"Processed {len(rows)} rows; {found_ct} LinkedIn profile URLs found in the first 10 results."
    )
    return path, summary


def _run_urn_resolve(rows: list[dict], email: str, save_csv: Callable) -> tuple[str, str]:
    def _progress(current: int, total: int, current_item: str) -> None:
        _update_progress("urn_resolve", current, total, current_item)

    results = resolve_profiles_batch(rows, progress=_progress)
    path = save_csv(results)
    found_ct = sum(1 for r in results if r.get("linkedin_url_resolved"))
    summary = (
        f"Processed {len(results)} profiles; {found_ct} vanity LinkedIn URLs resolved via RapidAPI."
    )
    return path, summary


def _run_company_enrich(rows: list[dict], email: str, save_csv: Callable) -> tuple[str, str]:
    def _progress(current: int, total: int, current_item: str) -> None:
        _update_progress("company_enrich", current, total, current_item)

    results = enrich_companies_batch(rows, progress=_progress)
    path = save_csv(results)
    count_ct = sum(1 for r in results if r.get("employee_count"))
    id_ct = sum(1 for r in results if r.get("linkedin_company_id"))
    summary = (
        f"Processed {len(results)} companies; {count_ct} employee counts and "
        f"{id_ct} numeric LinkedIn IDs filled via RapidAPI."
    )
    return path, summary


def _worker(
    job_type: str,
    email: str,
    save_csv: Callable[[list], str],
    run_fn: Callable,
    work_arg: Any,
) -> None:
    lock = _lock_for_type(job_type)
    if job_type == "company":
        subject_label = "Company"
    elif job_type == "email":
        subject_label = "Email"
    elif job_type == "urn_resolve":
        subject_label = "LinkedIn URN"
    elif job_type == "company_enrich":
        subject_label = "Company employee count"
    elif job_type == "vendor_file":
        subject_label = "Vendor email file"
    else:
        subject_label = "Person"
    try:
        job_name = (
            "email enrichment"
            if job_type == "email"
            else "LinkedIn URN resolver"
            if job_type == "urn_resolve"
            else "company employee count"
            if job_type == "company_enrich"
            else "vendor email file"
            if job_type == "vendor_file"
            else f"{subject_label} LinkedIn"
        )
        logger.info("%s job started for %s", job_name, email)
        path_str, summary = run_fn(work_arg, email, save_csv)
        path = Path(path_str)
        if job_type == "email":
            subject = "Email Finder — results ready"
            body = (
                "Your email enrichment job is complete.\n\n"
                f"{summary}\n\n"
                "The CSV is attached.\n"
            )
        elif job_type == "urn_resolve":
            subject = "LinkedIn URN Resolver — results ready"
            body = (
                "Your LinkedIn URN resolver job is complete.\n\n"
                f"{summary}\n\n"
                "The CSV is attached.\n"
            )
        elif job_type == "company_enrich":
            subject = "Company Employee Count — results ready"
            body = (
                "Your company employee count / LinkedIn ID job is complete.\n\n"
                f"{summary}\n\n"
                "The CSV is attached.\n"
            )
        elif job_type == "vendor_file":
            subject = f"Vendor enrichment complete — {Path(path_str).stem.replace('_vendor', '')}"
            body = (
                "Your vendor email/phone file is ready.\n\n"
                f"{summary}\n\n"
                "Attachments:\n"
                "- *_vendor.csv — send this file to the vendor\n"
                "- *_rejects.csv — rows with unfixable LinkedIn URLs (if any)\n"
                "- *_qa.csv — match / fetch notes\n"
            )
        else:
            subject = f"LinkedIn {subject_label} Finder — results ready"
            body = (
                f"Your {subject_label} LinkedIn finder job is complete.\n\n"
                f"{summary}\n\n"
                "The CSV is attached.\n"
            )
        extra_paths: list[Path] = []
        if job_type == "vendor_file":
            uid = Path(path_str).name.replace("_vendor.csv", "")
            for extra_name in (f"{uid}_rejects.csv", f"{uid}_qa.csv"):
                extra = Path(path_str).with_name(extra_name)
                if extra.is_file() and extra.read_text(encoding="utf-8-sig").count("\n") > 1:
                    extra_paths.append(extra)
        ok, err = send_results_email(
            email,
            subject=subject,
            body=body,
            attachment_path=path,
            extra_paths=extra_paths,
        )
        with _state_lock:
            job = _job_for_type(job_type)
            job["last_summary"] = summary
            job["email_sent"] = ok
            if not ok:
                job["error"] = err or "Failed to send email"
        if ok:
            logger.info("%s job finished; emailed %s", job_name, email)
        else:
            logger.error("%s job finished but email failed: %s", job_name, err)
    except Exception as exc:
        logger.exception("%s job failed", job_name)
        with _state_lock:
            job = _job_for_type(job_type)
            job["error"] = str(exc)
            job["last_summary"] = f"Job failed: {exc}"
    finally:
        with _state_lock:
            job = _job_for_type(job_type)
            job["running"] = False
            job["current_item"] = ""
        lock.release()
        logger.info("%s job lock released", job_type)


def _start_job(
    job_type: str,
    total: int,
    email: str,
    save_csv: Callable[[list], str],
    run_fn: Callable,
    work_arg: Any,
    thread_name: str,
) -> tuple[bool, str | None]:
    if total <= 0:
        return False, "No rows to process."
    lock = _lock_for_type(job_type)
    if not lock.acquire(blocking=False):
        return False, _busy_message(job_type)
    with _state_lock:
        job = _job_for_type(job_type)
        if job["running"]:
            lock.release()
            return False, _busy_message(job_type)
        job.update(
            running=True,
            job_type=job_type,
            email=email,
            current=0,
            total=total,
            current_item="",
            error=None,
            last_summary="",
            email_sent=False,
        )
    thread = threading.Thread(
        target=_worker,
        args=(job_type, email, save_csv, run_fn, work_arg),
        daemon=True,
        name=thread_name,
    )
    thread.start()
    return True, None


def start_company_job(companies: list[str], email: str, save_csv: Callable[[list], str]) -> tuple[bool, str | None]:
    return _start_job(
        "company",
        len(companies),
        email,
        save_csv,
        _run_company,
        companies,
        "linkedin-company-job",
    )


def start_person_job(
    pairs: list[tuple[str, str]],
    email: str,
    save_csv: Callable[[list], str],
) -> tuple[bool, str | None]:
    return _start_job(
        "person",
        len(pairs),
        email,
        save_csv,
        _run_person,
        pairs,
        "linkedin-person-job",
    )


def start_urn_resolve_job(
    rows: list[dict],
    email: str,
    save_csv: Callable[[list], str],
) -> tuple[bool, str | None]:
    return _start_job(
        "urn_resolve",
        len(rows),
        email,
        save_csv,
        _run_urn_resolve,
        rows,
        "linkedin-urn-resolve-job",
    )


def start_company_enrich_job(
    rows: list[dict],
    email: str,
    save_csv: Callable[[list], str],
) -> tuple[bool, str | None]:
    return _start_job(
        "company_enrich",
        len(rows),
        email,
        save_csv,
        _run_company_enrich,
        rows,
        "company-enrich-job",
    )


def _run_vendor_file(payload: dict, email: str, save_csv: Callable) -> tuple[str, str]:
    from vendor_file.pipeline import run_batch

    def _progress(current: int, total: int, current_item: str) -> None:
        _update_progress("vendor_file", current, total, current_item)

    summary = run_batch(
        input_rows=payload["rows"],
        uid=payload["uid"],
        out_dir=Path("data/vendor_file"),
        progress=_progress,
    )
    text = (
        f"Request ID {summary['uid']}: {summary['ok_rows']} vendor rows, "
        f"{summary['rejected_rows']} rejected. "
        f"Vieu IDs — people {summary.get('person_vieu_ids', 0)}, "
        f"target companies {summary.get('target_company_vieu_ids', 0)}, "
        f"current companies {summary.get('current_company_vieu_ids', 0)}."
    )
    return summary["vendor_path"], text


def start_vendor_file_job(rows: list[dict], email: str, uid: str) -> tuple[bool, str | None]:
    return _start_job(
        "vendor_file",
        len(rows),
        email,
        lambda _rows: "",
        _run_vendor_file,
        {"rows": rows, "uid": uid},
        "vendor-file-job",
    )


def _busy_message(job_type: str) -> str:
    with _state_lock:
        if job_type in {"company", "person"}:
            snap = dict(_serper_job)
        elif job_type in RAPIDAPI_JOB_TYPES:
            snap = dict(_rapidapi_job)
        else:
            snap = dict(_email_job)
    if snap.get("running"):
        jt = snap.get("job_type") or job_type
        label = _type_label(jt)
        return (
            f"A {label} job is already in progress "
            f"({snap.get('current', 0)} / {snap.get('total', 0)}). "
            "Please wait until it finishes."
        )
    return "Another lookup is in progress. Please wait and try again."


def _type_label(job_type: str) -> str:
    if job_type == "company":
        return "Company LinkedIn finder"
    if job_type == "person":
        return "Person LinkedIn finder"
    if job_type == "urn_resolve":
        return "LinkedIn URN resolver"
    if job_type == "company_enrich":
        return "Company employee count"
    if job_type == "vendor_file":
        return "Vendor email file"
    if job_type == "email":
        return "Email finder"
    return "LinkedIn finder"


def progress_display(scope: str = "all") -> dict[str, Any]:
    """Template-friendly progress block. Scope: serper, rapidapi, email, or all."""
    try:
        from email_enrichment_store import count_pending_jobs
        pending_email_jobs = count_pending_jobs()
    except Exception:
        pending_email_jobs = 0

    with _state_lock:
        serper = dict(_serper_job)
        rapidapi = dict(_rapidapi_job)
        email = dict(_email_job)

    if scope == "serper":
        active = serper if serper.get("running") else None
        form_disabled = bool(serper.get("running")) or _serper_lock.locked()
    elif scope == "rapidapi":
        active = rapidapi if rapidapi.get("running") else None
        form_disabled = bool(rapidapi.get("running")) or _rapidapi_lock.locked()
    elif scope == "email":
        active = email if email.get("running") else None
        form_disabled = bool(email.get("running")) or _email_lock.locked() or pending_email_jobs > 0
    else:
        active = None
        for candidate in (serper, rapidapi, email):
            if candidate.get("running"):
                active = candidate
                break
        form_disabled = is_system_busy() or pending_email_jobs > 0

    line = ""
    if active and active.get("running"):
        cur = active.get("current") or 0
        tot = active.get("total") or 0
        item = active.get("current_item") or ""
        line = f"{_type_label(active.get('job_type') or '')}: processing {cur} / {tot}"
        if item:
            line += f" — {item}"
    elif scope == "email" and pending_email_jobs:
        line = f"Email finder: {pending_email_jobs} job(s) queued"
    elif scope == "all" and pending_email_jobs and not active:
        line = f"Email finder: {pending_email_jobs} job(s) queued"

    job_running = bool(active and active.get("running"))
    if scope == "email":
        job_running = job_running or pending_email_jobs > 0

    progress_note = "Job in progress on this tool (form disabled until it finishes)."
    if scope == "serper":
        progress_note = "Serper LinkedIn finder job in progress (this form is disabled until it finishes)."
    elif scope == "rapidapi":
        active_type = (active or {}).get("job_type") or ""
        if active_type == "company_enrich":
            progress_note = "Company employee count job in progress (this form is disabled until it finishes)."
        elif active_type == "vendor_file":
            progress_note = "Vendor email file job in progress (this form is disabled until it finishes)."
        else:
            progress_note = "RapidAPI URN resolver job in progress (this form is disabled until it finishes)."
    elif scope == "email":
        progress_note = "Email enrichment job in progress (this form is disabled until it finishes)."

    snap = active or {}
    return {
        "job_running": job_running,
        "server_busy": form_disabled,
        "progress_line": line,
        "progress_note": progress_note,
        "job_type": snap.get("job_type") or "",
        "job_email_masked": _mask_email(snap.get("email") or ""),
        "last_summary": snap.get("last_summary") or "",
        "last_error": snap.get("error"),
        "finished_at_hint": datetime.now().strftime("%Y-%m-%d %H:%M UTC")
        if not job_running and snap.get("last_summary")
        else "",
        "pending_email_jobs": pending_email_jobs,
        "scope": scope,
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
