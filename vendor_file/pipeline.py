"""Sync vendor-file enrichment using this app's RapidAPI + SMTP stack."""

from __future__ import annotations

import csv
import io
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from rapidapi_linkedin_company import fetch_linkedin_company
from rapidapi_person_deep import collect_rapidapi_keys, fetch_person_deep_with_fallback
from vendor_file.experience import (
    country_from_person,
    current_from_positions,
    extract_positions,
    location_from_person,
    target_from_positions,
)
from vendor_file.graph import GraphClient, graph_configured
from vendor_file.names import names_from_associate
from vendor_file.schema import (
    INGEST_COLUMNS,
    INPUT_ALIASES,
    QA_COLUMNS,
    REJECT_COLUMNS,
    VENDOR_COLUMNS,
)
from vendor_file.urls import canonicalize_company_url, canonicalize_person_url
from vendor_file.website import canonicalize_website

logger = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]


def new_request_id(now: Optional[datetime] = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
    return f"VEN-{stamp}-{uuid.uuid4().hex[:8].upper()}"


def _norm_header(name: str) -> str:
    return " ".join((name or "").replace("_", " ").strip().lower().split())


def _cell_text(value: Any) -> str:
    """Coerce a DictReader cell to text. Extra CSV columns arrive as a list."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(part for v in value if (part := _cell_text(v)))
    return str(value).strip()


def _pick(row: Dict[str, Any], aliases: List[str]) -> str:
    by_norm = {
        _norm_header(str(k)): _cell_text(v)
        for k, v in row.items()
        if k is not None
    }
    for alias in aliases:
        val = by_norm.get(_norm_header(alias), "")
        if val:
            return val
    return ""


def parse_bool(raw: str, default: bool) -> bool:
    text = (raw or "").strip().lower()
    if not text:
        return default
    if text in {"true", "t", "yes", "y", "1"}:
        return True
    if text in {"false", "f", "no", "n", "0"}:
        return False
    return default


def flag(value: bool) -> str:
    return "TRUE" if value else "FALSE"


EMPTY_ALIASES = {"", "n/a", "na", "null", "none", "nan", "nat", "-"}


def clean_cell(value: Any) -> str:
    """Blank empty/sentinel values; keep Unicode otherwise."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in EMPTY_ALIASES:
        return ""
    return text


CONTACT_NEED_CHOICES = {
    "email": (True, False),
    "phone": (False, True),
    "both": (True, True),
}


def contact_need_flags(choice: str) -> Tuple[bool, bool]:
    """Return (email_required, phone_required) for a form choice."""
    key = (choice or "").strip().lower()
    return CONTACT_NEED_CHOICES.get(key, CONTACT_NEED_CHOICES["both"])


def parse_input_csv(
    raw: bytes,
    *,
    email_required_default: bool = True,
    phone_required_default: bool = True,
    max_rows: int = 500,
) -> List[Dict[str, Any]]:
    text = raw.decode("utf-8-sig")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")

    rows: List[Dict[str, Any]] = []
    for i, row in enumerate(reader, start=2):
        if not any(_cell_text(v) for v in row.values()):
            continue
        rows.append(
            {
                "source_row": i,
                "name": _pick(row, INPUT_ALIASES["name"]),
                "person_linkedin": _pick(row, INPUT_ALIASES["person_linkedin"]),
                "company_name": _pick(row, INPUT_ALIASES["company_name"]),
                "company_linkedin": _pick(row, INPUT_ALIASES["company_linkedin"]),
                "email_required": parse_bool(
                    _pick(row, INPUT_ALIASES["email_required"]), email_required_default
                ),
                "phone_required": parse_bool(
                    _pick(row, INPUT_ALIASES["phone_required"]), phone_required_default
                ),
            }
        )
    if not rows:
        raise ValueError("CSV has no data rows")
    if len(rows) > max_rows:
        raise ValueError(
            f"CSV has {len(rows)} records. Upload at most {max_rows} records per file."
        )
    return rows


def write_csv(path: Path, headers: List[str], rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({h: clean_cell(row.get(h, "")) for h in headers})


def empty_vendor_row(uid: str) -> Dict[str, str]:
    return {col: "" for col in VENDOR_COLUMNS} | {"UID": uid}


def _unwrap_person(payload: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("data"), dict) and (
        data["data"].get("firstName") or data["data"].get("experiences")
    ):
        data = data["data"]
    if data.get("firstName") or data.get("fullName") or data.get("experiences"):
        return data
    return None


def _website_from_company(company: Dict[str, Any]) -> str:
    for key in ("website", "websiteUrl", "companyPageUrl", "url"):
        val = company.get(key)
        if isinstance(val, str):
            cleaned = canonicalize_website(val)
            if cleaned:
                return cleaned
    cta = company.get("callToAction") or {}
    if isinstance(cta, dict):
        for key in ("url", "link"):
            val = cta.get(key)
            if isinstance(val, str):
                cleaned = canonicalize_website(val)
                if cleaned:
                    return cleaned
    return ""


def _hq_from_company(company: Dict[str, Any]) -> str:
    hq = company.get("headquarter") or company.get("headquarters") or {}
    if not isinstance(hq, dict):
        return ""
    city = (hq.get("city") or hq.get("geographicArea") or "").strip()
    country = (hq.get("country") or hq.get("countryCode") or "").strip()
    if city and country:
        return f"{city}, {country}"
    return city or country


def _headcount(company: Dict[str, Any]) -> str:
    ec = company.get("employeeCount") or company.get("employee_count")
    if ec:
        try:
            return str(int(ec))
        except (TypeError, ValueError):
            pass
    rng = company.get("employeeCountRange") or {}
    if isinstance(rng, dict):
        val = rng.get("end") or rng.get("start")
        if val:
            try:
                return str(int(val))
            except (TypeError, ValueError):
                return str(val).strip()
    return ""


def _company_record(url: str, api_key: str) -> Dict[str, str]:
    result = fetch_linkedin_company(url, api_key)
    if not result.get("success"):
        return {
            "company_linkedin": url,
            "company_website": "",
            "company_headcount": "",
            "company_hq": "",
            "api_company_name": "",
            "company_id": "",
            "status": f"error: {result.get('error') or 'lookup_failed'}",
        }
    company = result.get("data") or {}
    if not isinstance(company, dict):
        company = {}
    cid = company.get("companyId") or company.get("id") or company.get("company_id") or ""
    return {
        "company_linkedin": url,
        "company_website": _website_from_company(company),
        "company_headcount": _headcount(company),
        "company_hq": _hq_from_company(company),
        "api_company_name": str(company.get("companyName") or company.get("name") or "").strip(),
        "company_id": str(cid or ""),
        "status": "ok",
    }


def _person_record(url: str, api_key: str) -> Tuple[Optional[Dict[str, Any]], str]:
    result = fetch_person_deep_with_fallback(url, api_key)
    if not result.get("success"):
        return None, str(result.get("error") or "lookup_failed")
    data = _unwrap_person(result)
    if not data:
        return None, "Empty profile data"
    return data, ""


def run_batch(
    *,
    input_rows: List[Dict[str, Any]],
    uid: str,
    out_dir: Path,
    progress: Optional[ProgressFn] = None,
) -> Dict[str, Any]:
    keys = collect_rapidapi_keys()
    if not keys:
        raise RuntimeError("RAPIDAPI_KEY is not set")

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

    company_urls = sorted({r["company_url"] for r in accepted})
    person_urls = sorted({r["person_url"] for r in accepted})
    company_cache: Dict[str, Dict[str, str]] = {}
    person_cache: Dict[str, Tuple[Optional[Dict[str, Any]], str]] = {}
    workers = max(1, min(len(keys), 2))

    def log(current: int, total: int, item: str) -> None:
        if progress:
            progress(current, total, item)

    total_fetch = max(1, len(company_urls) + len(person_urls))
    done = 0

    def fetch_companies() -> None:
        nonlocal done
        if not company_urls:
            return
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_company_record, url, keys[i % len(keys)]): url
                for i, url in enumerate(company_urls)
            }
            for fut in as_completed(futs):
                url = futs[fut]
                company_cache[url] = fut.result()
                done += 1
                log(done, total_fetch, f"Company {url}")

    def fetch_people() -> None:
        nonlocal done
        if not person_urls:
            return
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_person_record, url, keys[i % len(keys)]): url
                for i, url in enumerate(person_urls)
            }
            for fut in as_completed(futs):
                url = futs[fut]
                person_cache[url] = fut.result()
                done += 1
                log(done, total_fetch, f"Profile {url}")

    log(0, total_fetch, "Starting RapidAPI fetches")
    fetch_companies()
    fetch_people()

    assembled: List[Dict[str, Any]] = []
    for row in accepted:
        data, person_err = person_cache.get(row["person_url"], (None, "not fetched"))
        target = company_cache.get(row["company_url"]) or {}
        full, first, middle, last = names_from_associate(row["name"])
        positions = extract_positions((data or {}).get("experiences") or []) if data else []
        target_match = target_from_positions(
            positions,
            target_name=row["company_name"],
            target_url=row["company_url"],
            target_company_id=str(target.get("company_id") or ""),
        )
        current = current_from_positions(positions)
        current_url = current.company_url
        current_equals = bool(
            target_match.current_equals_target
            or (current_url and current_url == row["company_url"])
            or (
                current.company_id
                and target.get("company_id")
                and str(current.company_id) == str(target.get("company_id"))
            )
        )
        if current_equals:
            current_url = row["company_url"]
        current_rec = target if current_equals else None
        if current_url and not current_equals:
            if current_url not in company_cache:
                company_cache[current_url] = _company_record(current_url, keys[0])
            current_rec = company_cache.get(current_url)
        assembled.append(
            {
                "row": row,
                "data": data,
                "person_err": person_err,
                "target": target,
                "full": full,
                "first": first,
                "middle": middle,
                "last": last,
                "target_match": target_match,
                "current": current,
                "current_url": current_url,
                "current_equals": current_equals,
                "current_rec": current_rec,
            }
        )

    graph_person_urls = sorted({item["row"]["person_url"] for item in assembled})
    graph_company_urls = sorted(
        {
            url
            for item in assembled
            for url in (item["row"]["company_url"], item["current_url"])
            if url
        }
    )
    people: Dict[str, Dict[str, str]] = {}
    companies: Dict[str, Dict[str, str]] = {}
    hist: Dict[Tuple[str, str], str] = {}
    if graph_configured():
        log(0, 1, "Looking up graph person/company/headcount")
        try:
            with GraphClient() as graph:
                people = graph.fetch_people(graph_person_urls)
                companies = graph.fetch_companies(graph_company_urls)
                pairs = []
                for item in assembled:
                    cid = (companies.get(item["row"]["company_url"]) or {}).get("id") or ""
                    year = (item["target_match"].start_date or "")[:4]
                    if cid and year:
                        pairs.append((cid, year))
                hist = graph.fetch_headcount_at_years(pairs)
        except Exception:
            logger.exception("Graph enrichment failed; continuing with RapidAPI-only fields")
            people, companies, hist = {}, {}, {}
        log(1, 1, "Graph lookup complete")
    else:
        log(0, 1, "Graph lookup skipped (POSTGRES_* not set)")

    ingest_rows: List[Dict[str, str]] = []
    assemble_total = max(1, len(assembled))
    for i, item in enumerate(assembled, start=1):
        log(i, assemble_total, f"Assembling row {i}")
        row = item["row"]
        data = item["data"]
        person_err = item["person_err"]
        target = item["target"]
        target_match = item["target_match"]
        current = item["current"]
        current_url = item["current_url"]
        current_equals = item["current_equals"]
        current_rec = item["current_rec"]
        graph_person = people.get(row["person_url"]) or {}
        graph_target = companies.get(row["company_url"]) or {}
        graph_current = companies.get(current_url or "") or {}

        vendor = empty_vendor_row(uid)
        vendor["Stakeholder Vieu ID"] = graph_person.get("id") or ""
        vendor["Stakeholder Full  Name"] = item["full"]
        vendor["Stakeholder First Name"] = item["first"]
        vendor["Stakeholder Middle Name"] = item["middle"]
        vendor["Stakeholder Last Name"] = item["last"]
        vendor["Profile Linkedin"] = row["person_url"]
        vendor["Location"] = graph_person.get("loc") or location_from_person(data or {})
        vendor["Country"] = graph_person.get("country") or country_from_person(data or {})
        if data and not person_err:
            vendor["Last Profile Refresh Date"] = date.today().isoformat()
        vendor["Target Company Vieu ID"] = graph_target.get("id") or ""
        vendor["Target Company Name"] = (
            (target.get("api_company_name") or "").strip() or row["company_name"]
        )
        vendor["Target Company Website"] = target.get("company_website") or ""
        vendor["Target Company Linkedin URL"] = row["company_url"]
        vendor["Target Company Employee Count"] = target.get("company_headcount") or ""
        vendor["Target Company Title"] = target_match.title
        vendor["Target Company  Start Date"] = target_match.start_date
        vendor["Target Company Start Title"] = target_match.start_title
        start_year = (target_match.start_date or "")[:4]
        if vendor["Target Company Vieu ID"] and start_year:
            vendor["Target Company Employee Count at Start Date"] = hist.get(
                (vendor["Target Company Vieu ID"], start_year), ""
            )
        if current_equals:
            vendor["Current Company Vieu ID"] = vendor["Target Company Vieu ID"]
            vendor["Current Company Website"] = vendor["Target Company Website"]
            vendor["Current Company Linkedin URL"] = vendor["Target Company Linkedin URL"]
            vendor["Current Company Title"] = current.title or target_match.title
            vendor["Current Company Empl Count"] = vendor["Target Company Employee Count"]
            vendor["Current Company HQ"] = target.get("company_hq") or ""
        elif current_rec:
            vendor["Current Company Vieu ID"] = graph_current.get("id") or ""
            vendor["Current Company Website"] = current_rec.get("company_website") or ""
            vendor["Current Company Linkedin URL"] = current_url or ""
            vendor["Current Company Title"] = current.title
            vendor["Current Company Empl Count"] = current_rec.get("company_headcount") or ""
            vendor["Current Company HQ"] = current_rec.get("company_hq") or ""
        elif current.title or current_url:
            vendor["Current Company Vieu ID"] = graph_current.get("id") or ""
            vendor["Current Company Linkedin URL"] = current_url
            vendor["Current Company Title"] = current.title
        vendor["Email required"] = flag(row["email_required"])
        vendor["Phone required"] = flag(row["phone_required"])
        vendor_rows.append(vendor)

        if not vendor["Stakeholder Vieu ID"]:
            ingest_rows.append(
                {
                    "source_row": row["source_row"],
                    "UID": uid,
                    "Stakeholder Name": row["name"],
                    "Profile Linkedin": row["person_url"],
                    "Location": vendor["Location"],
                    "Country": vendor["Country"],
                    "Target Company Name": row["company_name"],
                    "Target Company Linkedin": row["company_url"],
                    "reason": (
                        "graph lookup skipped; POSTGRES_* not set"
                        if not graph_configured()
                        else "stakeholder not in graph person table"
                    ),
                }
            )

        notes = []
        if not data:
            notes.append("person fetch failed; names taken from input")
        notes.append("names from associate input")
        if graph_person.get("loc") or graph_person.get("country"):
            notes.append("location/country from graph")
        elif data:
            notes.append("location/country from RapidAPI")
        if not target_match.matched:
            notes.append("target company not found in experience")
        if str(target.get("status") or "").startswith("error"):
            notes.append("target company lookup failed")
        if not graph_configured():
            notes.append("graph lookup skipped; Vieu IDs blank")
        else:
            if not vendor["Stakeholder Vieu ID"]:
                notes.append("stakeholder not in graph")
            if not vendor["Target Company Vieu ID"]:
                notes.append("target company not in graph")
            if current_url and not vendor["Current Company Vieu ID"]:
                notes.append("current company not in graph")
        qa_rows.append(
            {
                "source_row": row["source_row"],
                "status": "ok" if data and target_match.matched else "partial",
                "target_experience_matched": flag(target_match.matched),
                "person_fetch_status": "ok" if data else f"error: {person_err}",
                "company_fetch_status": target.get("status") or "",
                "person_vieu_id_status": (
                    "ok"
                    if vendor["Stakeholder Vieu ID"]
                    else ("skipped" if not graph_configured() else "not_found")
                ),
                "target_company_vieu_id_status": (
                    "ok"
                    if vendor["Target Company Vieu ID"]
                    else ("skipped" if not graph_configured() else "not_found")
                ),
                "current_company_vieu_id_status": (
                    "ok"
                    if vendor["Current Company Vieu ID"]
                    else (
                        "skipped"
                        if not graph_configured()
                        else ("blank" if not current_url else "not_found")
                    )
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
    ingest_path = out_dir / f"{uid}_not_in_graph.csv"
    write_csv(vendor_path, VENDOR_COLUMNS, vendor_rows)
    write_csv(reject_path, REJECT_COLUMNS, reject_rows)
    write_csv(qa_path, QA_COLUMNS + VENDOR_COLUMNS, qa_rows)
    write_csv(ingest_path, INGEST_COLUMNS, ingest_rows)
    return {
        "uid": uid,
        "ok_rows": len(vendor_rows),
        "rejected_rows": len(reject_rows),
        "not_in_graph_rows": len(ingest_rows),
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
        "not_in_graph_path": str(ingest_path),
    }
