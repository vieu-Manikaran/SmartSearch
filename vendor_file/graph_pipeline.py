"""Vendor-file enrichment from Seeqe graph (Postgres) only — no RapidAPI."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from vendor_file.experience import (
    match_target,
    pick_current_graph_role,
    positions_from_graph_rows,
    refresh_date_from_graph_rows,
    target_from_positions,
)
from vendor_file.graph import GraphClient, graph_configured
from vendor_file.names import names_from_profile
from vendor_file.pipeline import empty_vendor_row, flag, write_csv
from vendor_file.schema import QA_COLUMNS, REJECT_COLUMNS, VENDOR_COLUMNS
from vendor_file.urls import canonicalize_company_url, canonicalize_person_url

ProgressFn = Callable[[int, int, str], None]


def new_graph_request_id(now: Optional[datetime] = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
    return f"VNG-{stamp}-{uuid.uuid4().hex[:8].upper()}"


def _apply_company_fields(vendor: Dict[str, str], rec: Dict[str, str], prefix: str) -> None:
    if prefix == "target":
        vendor["Target Company Vieu ID"] = rec.get("id") or ""
        vendor["Target Company Name"] = rec.get("name") or vendor["Target Company Name"]
        vendor["Target Company Website"] = rec.get("website") or ""
        vendor["Target Company Linkedin URL"] = rec.get("linkedin") or vendor["Target Company Linkedin URL"]
        vendor["Target Company Employee Count"] = rec.get("headcount") or ""
        return
    vendor["Current Company Vieu ID"] = rec.get("id") or ""
    vendor["Current Company Website"] = rec.get("website") or ""
    vendor["Current Company Linkedin URL"] = rec.get("linkedin") or vendor["Current Company Linkedin URL"]
    vendor["Current Company Empl Count"] = rec.get("headcount") or ""
    vendor["Current Company HQ"] = rec.get("hq") or ""


def run_batch_graph(
    *,
    input_rows: List[Dict[str, Any]],
    uid: str,
    out_dir: Path,
    progress: Optional[ProgressFn] = None,
) -> Dict[str, Any]:
    if not graph_configured():
        raise RuntimeError("POSTGRES_* is not set")

    def log(current: int, total: int, item: str) -> None:
        if progress:
            progress(current, total, item)

    vendor_rows: List[Dict[str, str]] = []
    reject_rows: List[Dict[str, str]] = []
    qa_rows: List[Dict[str, str]] = []
    accepted: List[Dict[str, Any]] = []

    for row in input_rows:
        person = canonicalize_person_url(row["person_linkedin"])
        company = canonicalize_company_url(row["company_linkedin"])
        reasons = []
        if not (row["name"] or "").strip():
            reasons.append("Stakeholder name is empty")
        if not person.ok:
            reasons.append(person.reason)
        if not (row["company_name"] or "").strip():
            reasons.append("Target company name is empty")
        if not company.ok:
            reasons.append(company.reason)
        if reasons:
            reject_rows.append(
                {
                    "source_row": row["source_row"],
                    "UID": uid,
                    "Stakeholder Name": row["name"],
                    "Profile Linkedin": row["person_linkedin"],
                    "Target Company Name": row["company_name"],
                    "Target Company Linkedin": row["company_linkedin"],
                    "reason": "; ".join(reasons),
                }
            )
            continue
        accepted.append({**row, "person_url": person.url, "company_url": company.url})

    person_urls = sorted({r["person_url"] for r in accepted})
    company_urls = sorted({r["company_url"] for r in accepted})

    log(0, 4, "Connecting to graph")
    with GraphClient() as graph:
        log(1, 4, "Fetching people and companies")
        people = graph.fetch_people(person_urls)
        companies = graph.fetch_companies(company_urls)
        person_ids = [p["id"] for p in people.values() if p.get("id")]
        log(2, 4, "Fetching experience")
        experiences = graph.fetch_experiences(person_ids)

        extra_ids: List[str] = []
        headcount_pairs: List[tuple[str, str]] = []
        staged: List[Dict[str, Any]] = []

        for row in accepted:
            person = people.get(row["person_url"]) or {}
            target = companies.get(row["company_url"]) or {}
            exp_rows = experiences.get(person.get("id") or "", []) if person else []
            positions = positions_from_graph_rows(exp_rows)
            target_hits = match_target(
                positions,
                target_name=row["company_name"],
                target_url=row["company_url"],
                target_company_id=str(target.get("id") or ""),
            )
            target_match = target_from_positions(
                positions,
                target_name=row["company_name"],
                target_url=row["company_url"],
                target_company_id=str(target.get("id") or ""),
            )
            current, current_equals = pick_current_graph_role(positions, target_hits)
            current_id = current.company_id
            if current_equals:
                current_id = str(target.get("id") or current_id)
            elif current_id and current_id not in {c.get("id") for c in companies.values()}:
                extra_ids.append(current_id)
            start_year = (target_match.start_date or "")[:4]
            if target.get("id") and start_year:
                headcount_pairs.append((str(target["id"]), start_year))
            staged.append(
                {
                    "row": row,
                    "person": person,
                    "target": target,
                    "exp_rows": exp_rows,
                    "positions": positions,
                    "target_match": target_match,
                    "current": current,
                    "current_equals": current_equals,
                    "current_id": current_id,
                }
            )

        log(3, 4, "Fetching current companies and historical headcount")
        extra_companies = graph.fetch_companies_by_ids(extra_ids)
        hist = graph.fetch_headcount_at_years(headcount_pairs)
    log(4, 4, "Graph fetches complete")

    companies_by_id = {c["id"]: c for c in companies.values() if c.get("id")}
    companies_by_id.update(extra_companies)

    assemble_total = max(1, len(staged))
    for i, item in enumerate(staged, start=1):
        log(i, assemble_total, f"Assembling row {i}")
        row = item["row"]
        person = item["person"]
        target = item["target"]
        target_match = item["target_match"]
        current = item["current"]
        current_equals = item["current_equals"]
        current_id = item["current_id"]

        full, first, middle, last = names_from_profile(
            person.get("name") or "",
            "",
            "",
            row["name"],
        )
        vendor = empty_vendor_row(uid)
        vendor["Stakeholder Vieu ID"] = person.get("id") or ""
        vendor["Stakeholder Full  Name"] = full
        vendor["Stakeholder First Name"] = first
        vendor["Stakeholder Middle Name"] = middle
        vendor["Stakeholder Last Name"] = last
        vendor["Profile Linkedin"] = row["person_url"]
        vendor["Location"] = person.get("loc") or ""
        vendor["Country"] = person.get("country") or ""
        vendor["Last Profile Refresh Date"] = refresh_date_from_graph_rows(item["exp_rows"])
        vendor["Target Company Name"] = row["company_name"]
        vendor["Target Company Linkedin URL"] = row["company_url"]
        if target:
            _apply_company_fields(vendor, target, "target")
        vendor["Target Company Title"] = target_match.title
        vendor["Target Company  Start Date"] = target_match.start_date
        vendor["Target Company Start Title"] = target_match.start_title
        start_year = (target_match.start_date or "")[:4]
        if target.get("id") and start_year:
            vendor["Target Company Employee Count at Start Date"] = hist.get(
                (str(target["id"]), start_year), ""
            )

        current_rec = companies_by_id.get(current_id or "") if current_id else {}
        if current_equals and target:
            vendor["Current Company Vieu ID"] = vendor["Target Company Vieu ID"]
            vendor["Current Company Website"] = vendor["Target Company Website"]
            vendor["Current Company Linkedin URL"] = vendor["Target Company Linkedin URL"]
            vendor["Current Company Title"] = current.title or target_match.title
            vendor["Current Company Empl Count"] = vendor["Target Company Employee Count"]
            vendor["Current Company HQ"] = target.get("hq") or ""
        elif current_rec:
            _apply_company_fields(vendor, current_rec, "current")
            vendor["Current Company Title"] = current.title
            if current.company_url and not vendor["Current Company Linkedin URL"]:
                vendor["Current Company Linkedin URL"] = current.company_url
        elif current.title or current.company_url:
            vendor["Current Company Vieu ID"] = current.company_id
            vendor["Current Company Linkedin URL"] = current.company_url
            vendor["Current Company Title"] = current.title

        vendor["Email required"] = flag(row["email_required"])
        vendor["Phone required"] = flag(row["phone_required"])
        vendor_rows.append(vendor)

        notes = []
        if not person:
            notes.append("stakeholder not in graph; names taken from input")
        if person and not item["exp_rows"]:
            notes.append("no experience rows in graph")
        if not target_match.matched:
            notes.append("target company not found in experience")
        if not target:
            notes.append("target company not in graph")
        if current.company_id and not vendor["Current Company Vieu ID"]:
            notes.append("current company not in graph")
        qa_rows.append(
            {
                "source_row": row["source_row"],
                "status": "ok" if person and target_match.matched else "partial",
                "target_experience_matched": flag(target_match.matched),
                "person_fetch_status": "ok" if person else "not_found",
                "company_fetch_status": "ok" if target else "not_found",
                "person_vieu_id_status": "ok" if vendor["Stakeholder Vieu ID"] else "not_found",
                "target_company_vieu_id_status": (
                    "ok" if vendor["Target Company Vieu ID"] else "not_found"
                ),
                "current_company_vieu_id_status": (
                    "ok"
                    if vendor["Current Company Vieu ID"]
                    else ("blank" if not (current.title or current.company_url) else "not_found")
                ),
                "current_equals_target": flag(current_equals),
                "notes": "; ".join(notes),
                "input_name": row["name"],
                "input_person_linkedin": row["person_linkedin"],
                "input_company_name": row["company_name"],
                "input_company_linkedin": row["company_linkedin"],
                **vendor,
            }
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    vendor_path = out_dir / f"{uid}_vendor.csv"
    reject_path = out_dir / f"{uid}_rejects.csv"
    qa_path = out_dir / f"{uid}_qa.csv"
    write_csv(vendor_path, VENDOR_COLUMNS, vendor_rows)
    write_csv(reject_path, REJECT_COLUMNS, reject_rows)
    write_csv(qa_path, QA_COLUMNS + VENDOR_COLUMNS, qa_rows)
    return {
        "uid": uid,
        "ok_rows": len(vendor_rows),
        "rejected_rows": len(reject_rows),
        "person_vieu_ids": sum(1 for r in vendor_rows if r.get("Stakeholder Vieu ID")),
        "target_company_vieu_ids": sum(
            1 for r in vendor_rows if r.get("Target Company Vieu ID")
        ),
        "current_company_vieu_ids": sum(
            1 for r in vendor_rows if r.get("Current Company Vieu ID")
        ),
        "historical_headcounts": sum(
            1 for r in vendor_rows if r.get("Target Company Employee Count at Start Date")
        ),
        "vendor_path": str(vendor_path),
        "rejects_path": str(reject_path),
        "qa_path": str(qa_path),
    }
