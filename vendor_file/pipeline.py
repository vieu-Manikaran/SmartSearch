"""Sync vendor-file enrichment using this app's RapidAPI + SMTP stack."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from rapidapi_linkedin_company import fetch_linkedin_company
from rapidapi_person_deep import (
    MEMBER_URN_SLUG_RE,
    collect_rapidapi_keys,
    fetch_person_deep,
    vanity_identifier_from_person_data,
)
from vendor_file.experience import (
    country_from_person,
    current_from_positions,
    extract_positions,
    location_from_person,
    target_from_positions,
)
from vendor_file.graph import GraphClient, company_numeric_id, graph_configured
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
RAPIDAPI_CACHE_DIR = Path("data/vendor_file/rapidapi_json")
_CACHE_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")
TERMINAL_PERSON_ERRORS = {"profile_not_found", "invalid_url", "Empty profile data", "empty profile data"}
TERMINAL_COMPANY_ERRORS = {"company_not_found", "invalid_url"}
FETCH_CHUNK = 40
REQUEST_GAP_SEC = 0.8
_rate_lock = threading.Lock()
_last_request_by_key: Dict[str, float] = {}


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


def _cache_slug(url: str) -> str:
    slug = unquote(urlparse(url or "").path.rstrip("/").split("/")[-1]).lower()
    slug = _CACHE_SLUG_RE.sub("_", slug).strip("._") or "unknown"
    return slug[:180]


def _cache_path(kind: str, url: str) -> Path:
    return RAPIDAPI_CACHE_DIR / kind / f"{_cache_slug(url)}.json"


def _read_json_cache(path: Path) -> Optional[Any]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_cache(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key_label(api_key: str, keys: List[str]) -> str:
    try:
        return f"key{keys.index(api_key) + 1}"
    except ValueError:
        return "key?"


def _throttle_key(api_key: str) -> None:
    """Keep one in-flight request per key and a small gap to limit 429s."""
    with _rate_lock:
        now = time.time()
        last = _last_request_by_key.get(api_key, 0.0)
        wait = REQUEST_GAP_SEC - (now - last)
        if wait > 0:
            time.sleep(wait)
        _last_request_by_key[api_key] = time.time()


def _person_data_from_cache(cached: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(cached, dict):
        return None
    if cached.get("success") is True and isinstance(cached.get("data"), dict):
        return cached["data"]
    if cached.get("ok") is True and isinstance(cached.get("data"), dict):
        return cached["data"]
    result = cached.get("result")
    if isinstance(result, dict) and result.get("success"):
        data = _unwrap_person(result)
        if isinstance(data, dict):
            return data
        if isinstance(result.get("data"), dict):
            return result["data"]
    return None


def _unwrap_company(payload: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None
    nested = data.get("data")
    if isinstance(nested, dict) and (
        nested.get("companyName") or nested.get("universalName") or nested.get("companyId")
    ):
        data = nested
    if data.get("companyName") or data.get("universalName") or data.get("companyId"):
        return data
    return None


def person_linkedin_from_rapidapi(
    data: Optional[Dict[str, Any]], fallback_url: str
) -> str:
    """Vanity /in/{slug} from person_deep. Member URNs (ACo/ACw) stay as fallback."""
    if isinstance(data, dict):
        slug = vanity_identifier_from_person_data(data)
        if slug and not MEMBER_URN_SLUG_RE.fullmatch(slug):
            canon = canonicalize_person_url(f"https://www.linkedin.com/in/{slug}")
            if canon.ok:
                return canon.url
    return (fallback_url or "").strip()


def company_linkedin_from_record(
    rec: Optional[Dict[str, str]], fallback_url: str
) -> str:
    """Prefer RapidAPI vanity company URL over a numeric /company/123 slug."""
    vanity = ((rec or {}).get("graph_linkedin") or "").strip()
    if vanity:
        return vanity
    return (fallback_url or "").strip()


def _first_graph_hit(
    mapping: Dict[str, Dict[str, str]], *urls: str
) -> Dict[str, str]:
    for url in urls:
        if url and url in mapping:
            return mapping[url]
    return {}


def graph_linkedin_from_rapidapi_company(company: Optional[Dict[str, Any]]) -> str:
    """Vanity LinkedIn URL from RapidAPI company_pro (never a numeric /company/123 slug)."""
    if not isinstance(company, dict):
        return ""
    candidates: List[str] = []
    url = company.get("url")
    if isinstance(url, str) and "linkedin.com" in url.lower():
        candidates.append(url)
    uname = str(company.get("universalName") or "").strip()
    if uname:
        candidates.append(f"https://www.linkedin.com/company/{uname}")
    for raw in candidates:
        canon = canonicalize_company_url(raw)
        if canon.ok and company_numeric_id(canon.url) is None:
            return canon.url
    return ""


def _with_graph_linkedin(
    rec: Dict[str, str], cached: Any = None, company: Optional[Dict[str, Any]] = None
) -> Dict[str, str]:
    out = dict(rec)
    existing = (out.get("graph_linkedin") or "").strip()
    if existing and company_numeric_id(existing) is None:
        return out
    data = company
    if data is None and isinstance(cached, dict):
        result = cached.get("result") if isinstance(cached.get("result"), dict) else cached
        data = _unwrap_company(result)
    vanity = graph_linkedin_from_rapidapi_company(data)
    if vanity:
        out["graph_linkedin"] = vanity
    return out


def _company_record_from_cache(cached: Any) -> Optional[Dict[str, str]]:
    if not isinstance(cached, dict):
        return None
    rec = cached.get("record")
    if isinstance(rec, dict) and rec.get("status") == "ok":
        return _with_graph_linkedin(rec, cached)
    if cached.get("status") == "ok" and cached.get("company_linkedin"):
        return _with_graph_linkedin(cached, cached)
    return None


def _cached_error(cached: Any, terminal: set[str]) -> Optional[str]:
    if not isinstance(cached, dict):
        return None
    if cached.get("success") is True or cached.get("ok") is True:
        return None
    result = cached.get("result") if isinstance(cached.get("result"), dict) else {}
    err = str(cached.get("error") or result.get("error") or "").strip()
    if err in terminal:
        return err
    return None


def _save_fetch_json(
    kind: str,
    url: str,
    *,
    result: Dict[str, Any],
    record: Optional[Dict[str, str]] = None,
    key_label: str = "",
) -> Path:
    payload: Dict[str, Any] = {
        "linkedin_url": url,
        "kind": kind,
        "fetched_at": _now_iso(),
        "key": key_label,
        "success": bool(result.get("success")),
        "error": "" if result.get("success") else str(result.get("error") or "lookup_failed"),
        "result": result,
    }
    if record is not None:
        payload["record"] = record
    path = _cache_path(kind, url)
    _write_json_cache(path, payload)
    return path


def _write_checkpoint(stats: Dict[str, Any]) -> None:
    payload = {"updated_at": _now_iso(), **stats}
    _write_json_cache(RAPIDAPI_CACHE_DIR / "checkpoint.json", payload)


def parse_input_csv(
    raw: bytes,
    *,
    email_required_default: bool = True,
    phone_required_default: bool = True,
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


def _company_record(url: str, api_key: str, keys: Optional[List[str]] = None) -> Dict[str, str]:
    keys = keys or collect_rapidapi_keys()
    cached = _read_json_cache(_cache_path("company", url))
    hit = _company_record_from_cache(cached)
    if hit is not None:
        logger.debug("company CACHE %s", url)
        return hit
    terminal = _cached_error(cached, TERMINAL_COMPANY_ERRORS)
    if terminal:
        logger.info("company CACHE-ERROR %s error=%s (not retrying)", url, terminal)
        rec = (cached or {}).get("record") if isinstance(cached, dict) else None
        if isinstance(rec, dict):
            return rec
        return {
            "company_linkedin": url,
            "company_website": "",
            "company_headcount": "",
            "company_hq": "",
            "api_company_name": "",
            "company_id": "",
            "status": f"error: {terminal}",
        }

    ordered = [api_key] + [k for k in keys if k != api_key] if api_key else list(keys)
    last_result: Dict[str, Any] = {"success": False, "error": "missing_rapidapi_key"}
    last_label = ""
    for key in ordered:
        last_label = _key_label(key, keys)
        logger.info("company FETCH %s via %s", url, last_label)
        _throttle_key(key)
        last_result = fetch_linkedin_company(url, key)
        if last_result.get("success"):
            break
        logger.info(
            "company FAIL %s via %s error=%s; trying next key if available",
            url,
            last_label,
            last_result.get("error"),
        )

    if not last_result.get("success"):
        record = {
            "company_linkedin": url,
            "company_website": "",
            "company_headcount": "",
            "company_hq": "",
            "api_company_name": "",
            "company_id": "",
            "status": f"error: {last_result.get('error') or 'lookup_failed'}",
        }
        path = _save_fetch_json(
            "company", url, result=last_result, record=record, key_label=last_label
        )
        logger.info("company SAVED %s success=false json=%s", url, path)
        return record

    company = last_result.get("data") or {}
    if not isinstance(company, dict):
        company = {}
    cid = company.get("companyId") or company.get("id") or company.get("company_id") or ""
    record = {
        "company_linkedin": url,
        "company_website": _website_from_company(company),
        "company_headcount": _headcount(company),
        "company_hq": _hq_from_company(company),
        "api_company_name": str(company.get("companyName") or company.get("name") or "").strip(),
        "company_id": str(cid or ""),
        "graph_linkedin": graph_linkedin_from_rapidapi_company(company),
        "status": "ok",
    }
    path = _save_fetch_json("company", url, result=last_result, record=record, key_label=last_label)
    logger.info("company SAVED %s success=true json=%s", url, path)
    return record


def _person_record(
    url: str, api_key: str, keys: Optional[List[str]] = None
) -> Tuple[Optional[Dict[str, Any]], str]:
    keys = keys or collect_rapidapi_keys()
    cached = _read_json_cache(_cache_path("person", url))
    data = _person_data_from_cache(cached)
    if data is not None:
        logger.debug("person CACHE %s", url)
        return data, ""
    terminal = _cached_error(cached, TERMINAL_PERSON_ERRORS)
    if terminal:
        logger.info("person CACHE-ERROR %s error=%s (not retrying)", url, terminal)
        return None, terminal

    logger.info("person FETCH %s via %s (then fallback to other keys)", url, _key_label(api_key, keys))
    ordered = [api_key] + [k for k in keys if k != api_key]
    result: Dict[str, Any] = {"success": False, "error": "missing_rapidapi_key"}
    used_label = _key_label(api_key, keys)
    for key in ordered:
        used_label = _key_label(key, keys)
        logger.info("person ATTEMPT %s via %s", url, used_label)
        _throttle_key(key)
        result = fetch_person_deep(url, key)
        if result.get("success"):
            break
        logger.info(
            "person FAIL %s via %s error=%s; trying next key if available",
            url,
            used_label,
            result.get("error"),
        )
    data = _unwrap_person(result) if result.get("success") else None
    if not result.get("success"):
        path = _save_fetch_json(
            "person",
            url,
            result=result,
            key_label=used_label,
        )
        err = str(result.get("error") or "lookup_failed")
        logger.info("person SAVED %s success=false error=%s json=%s", url, err, path)
        return None, err
    if not data:
        path = _save_fetch_json(
            "person",
            url,
            result={"success": False, "error": "Empty profile data", "data": result.get("data")},
            key_label=used_label,
        )
        logger.info("person SAVED %s success=false error=Empty profile data json=%s", url, path)
        return None, "Empty profile data"
    path = _save_fetch_json(
        "person",
        url,
        result=result,
        key_label=used_label,
    )
    logger.info("person SAVED %s success=true json=%s", url, path)
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
    logger.info(
        "Vendor batch %s: %s RapidAPI keys, workers=%s, accepted=%s, unique people=%s, unique companies=%s, cache_dir=%s",
        uid,
        len(keys),
        max(1, len(keys)),
        len(accepted),
        len(person_urls),
        len(company_urls),
        RAPIDAPI_CACHE_DIR.resolve(),
    )
    if len(keys) < 2:
        logger.warning(
            "Only %s RapidAPI key(s) loaded; set RAPIDAPI_KEY, RAPIDAPI_KEY2, RAPIDAPI_KEY3 for more workers",
            len(keys),
        )
    company_cache: Dict[str, Dict[str, str]] = {}
    person_cache: Dict[str, Tuple[Optional[Dict[str, Any]], str]] = {}
    workers = max(1, len(keys))

    def log(current: int, total: int, item: str) -> None:
        logger.info("progress %s/%s %s", current, total, item)
        if progress:
            progress(current, total, item)

    total_fetch = max(1, len(company_urls) + len(person_urls))
    done = 0

    def fetch_companies() -> None:
        nonlocal done
        if not company_urls:
            return
        pending: List[str] = []
        for url in company_urls:
            cached = _read_json_cache(_cache_path("company", url))
            hit = _company_record_from_cache(cached)
            if hit is not None:
                company_cache[url] = hit
                done += 1
                logger.debug("company CACHE %s", url)
                continue
            terminal = _cached_error(cached, TERMINAL_COMPANY_ERRORS)
            if terminal:
                company_cache[url] = {
                    "company_linkedin": url,
                    "company_website": "",
                    "company_headcount": "",
                    "company_hq": "",
                    "api_company_name": "",
                    "company_id": "",
                    "status": f"error: {terminal}",
                }
                done += 1
                logger.info("company CACHE-ERROR %s error=%s", url, terminal)
                log(done, total_fetch, f"Company cache-error {url}")
                continue
            pending.append(url)
        logger.info(
            "companies cache_hits=%s pending=%s workers=%s",
            len(company_urls) - len(pending),
            len(pending),
            workers,
        )
        log(done, total_fetch, "Company RapidAPI cache loaded")
        for start in range(0, len(pending), FETCH_CHUNK):
            chunk = pending[start : start + FETCH_CHUNK]
            logger.info(
                "company chunk %s-%s / %s",
                start + 1,
                start + len(chunk),
                len(pending),
            )
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {
                    pool.submit(_company_record, url, keys[i % len(keys)], keys): url
                    for i, url in enumerate(chunk)
                }
                for fut in as_completed(futs):
                    url = futs[fut]
                    try:
                        company_cache[url] = fut.result()
                    except Exception:
                        logger.exception("company crashed %s", url)
                        company_cache[url] = {
                            "company_linkedin": url,
                            "company_website": "",
                            "company_headcount": "",
                            "company_hq": "",
                            "api_company_name": "",
                            "company_id": "",
                            "status": "error: crashed",
                        }
                    done += 1
                    rec = company_cache[url]
                    log(done, total_fetch, f"Company {rec.get('status')} {url}")
            _write_checkpoint(
                {
                    "uid": uid,
                    "phase": "companies",
                    "done": done,
                    "total": total_fetch,
                    "company_pending_left": max(0, len(pending) - (start + len(chunk))),
                }
            )

    def fetch_people() -> None:
        nonlocal done
        if not person_urls:
            return
        pending: List[str] = []
        for url in person_urls:
            cached = _read_json_cache(_cache_path("person", url))
            data = _person_data_from_cache(cached)
            if data is not None:
                person_cache[url] = (data, "")
                done += 1
                logger.debug("person CACHE %s", url)
                continue
            terminal = _cached_error(cached, TERMINAL_PERSON_ERRORS)
            if terminal:
                person_cache[url] = (None, terminal)
                done += 1
                logger.info("person CACHE-ERROR %s error=%s", url, terminal)
                log(done, total_fetch, f"Profile cache-error {url}")
                continue
            pending.append(url)
        logger.info(
            "people cache_hits=%s pending=%s workers=%s",
            len(person_urls) - len(pending),
            len(pending),
            workers,
        )
        log(done, total_fetch, "Profile RapidAPI cache loaded")
        for start in range(0, len(pending), FETCH_CHUNK):
            chunk = pending[start : start + FETCH_CHUNK]
            logger.info(
                "person chunk %s-%s / %s",
                start + 1,
                start + len(chunk),
                len(pending),
            )
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {
                    pool.submit(_person_record, url, keys[i % len(keys)], keys): url
                    for i, url in enumerate(chunk)
                }
                for fut in as_completed(futs):
                    url = futs[fut]
                    try:
                        person_cache[url] = fut.result()
                    except Exception as exc:
                        logger.exception("person crashed %s", url)
                        person_cache[url] = (None, str(exc) or "crashed")
                    done += 1
                    _data, err = person_cache[url]
                    status = "ok" if _data else f"error:{err}"
                    log(done, total_fetch, f"Profile {status} {url}")
            ok_so_far = sum(1 for v in person_cache.values() if v[0])
            _write_checkpoint(
                {
                    "uid": uid,
                    "phase": "people",
                    "done": done,
                    "total": total_fetch,
                    "people_ok": ok_so_far,
                    "people_pending_left": max(0, len(pending) - (start + len(chunk))),
                }
            )

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
        person_graph_url = person_linkedin_from_rapidapi(data, row["person_url"])
        company_graph_url = company_linkedin_from_record(target, row["company_url"])
        if current_equals:
            current_url = company_graph_url or row["company_url"]
        current_rec = target if current_equals else None
        if current_url and not current_equals:
            if current_url not in company_cache:
                company_cache[current_url] = _company_record(current_url, keys[0], keys)
            current_rec = company_cache.get(current_url)
        current_graph_url = (
            company_graph_url
            if current_equals
            else company_linkedin_from_record(current_rec, current_url)
        )
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
                "person_graph_url": person_graph_url,
                "company_graph_url": company_graph_url,
                "current_graph_url": current_graph_url,
            }
        )

    vanity_people = sum(
        1
        for item in assembled
        if item["person_graph_url"] and item["person_graph_url"] != item["row"]["person_url"]
    )
    logger.info(
        "Person RapidAPI vanity URLs resolved %s/%s (URN/input → /in/{slug} for graph)",
        vanity_people,
        len(assembled),
    )

    graph_person_urls = sorted(
        {
            url
            for item in assembled
            for url in (item["person_graph_url"], item["row"]["person_url"])
            if url
        }
    )
    graph_company_urls = sorted(
        {
            url
            for item in assembled
            for url in (
                item["company_graph_url"],
                item["row"]["company_url"],
                item["current_url"],
                item["current_graph_url"],
            )
            if url
        }
        | {
            (rec.get("graph_linkedin") or "").strip()
            for rec in company_cache.values()
            if isinstance(rec, dict) and (rec.get("graph_linkedin") or "").strip()
        }
    )
    people: Dict[str, Dict[str, str]] = {}
    companies: Dict[str, Dict[str, str]] = {}
    hist: Dict[Tuple[str, str], str] = {}
    if graph_configured():
        log(0, 1, "Looking up graph person/company/headcount")
        try:
            with GraphClient() as graph:
                try:
                    people = graph.fetch_people(graph_person_urls)
                    logger.info(
                        "Graph people matched %s/%s",
                        len(people),
                        len(graph_person_urls),
                    )
                except Exception:
                    logger.exception("Graph person lookup failed")
                try:
                    companies = graph.fetch_companies(graph_company_urls)
                    logger.info(
                        "Graph companies matched %s/%s",
                        len(companies),
                        len(graph_company_urls),
                    )
                except Exception:
                    logger.exception("Graph company lookup failed")
                try:
                    pairs = []
                    for item in assembled:
                        hit = _first_graph_hit(
                            companies,
                            item["company_graph_url"],
                            item["row"]["company_url"],
                        )
                        cid = hit.get("id") or ""
                        year = (item["target_match"].start_date or "")[:4]
                        if cid and year:
                            pairs.append((cid, year))
                    hist = graph.fetch_headcount_at_years(pairs)
                    logger.info("Graph historical headcount matched %s", len(hist))
                except Exception:
                    logger.exception("Graph headcount lookup failed")
        except Exception:
            logger.exception("Graph client failed")
        log(1, 1, "Graph lookup complete")
    else:
        log(0, 1, "Graph lookup skipped (POSTGRES_* not set)")

    ingest_rows: List[Dict[str, str]] = []
    assemble_total = max(1, len(assembled))
    for i, item in enumerate(assembled, start=1):
        if i == 1 or i == assemble_total or i % 2000 == 0:
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
        graph_person = _first_graph_hit(
            people, item["person_graph_url"], row["person_url"]
        )
        graph_target = _first_graph_hit(
            companies, item["company_graph_url"], row["company_url"]
        )
        graph_current = _first_graph_hit(
            companies, item["current_graph_url"], current_url
        )

        vendor = empty_vendor_row(uid)
        vendor["Stakeholder Vieu ID"] = graph_person.get("id") or ""
        vendor["Stakeholder Full  Name"] = item["full"]
        vendor["Stakeholder First Name"] = item["first"]
        vendor["Stakeholder Middle Name"] = item["middle"]
        vendor["Stakeholder Last Name"] = item["last"]
        vendor["Profile Linkedin"] = item["person_graph_url"] or row["person_url"]
        vendor["Location"] = graph_person.get("loc") or location_from_person(data or {})
        vendor["Country"] = graph_person.get("country") or country_from_person(data or {})
        if data and not person_err:
            vendor["Last Profile Refresh Date"] = date.today().isoformat()
        vendor["Target Company Vieu ID"] = graph_target.get("id") or ""
        vendor["Target Company Name"] = (
            (target.get("api_company_name") or "").strip() or row["company_name"]
        )
        vendor["Target Company Website"] = target.get("company_website") or ""
        vendor["Target Company Linkedin URL"] = (
            item["company_graph_url"] or row["company_url"]
        )
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
            vendor["Current Company Linkedin URL"] = (
                item["current_graph_url"] or current_url or ""
            )
            vendor["Current Company Title"] = current.title
            vendor["Current Company Empl Count"] = current_rec.get("company_headcount") or ""
            vendor["Current Company HQ"] = current_rec.get("company_hq") or ""
        elif current.title or current_url:
            vendor["Current Company Vieu ID"] = graph_current.get("id") or ""
            vendor["Current Company Linkedin URL"] = (
                item["current_graph_url"] or current_url
            )
            vendor["Current Company Title"] = current.title
        vendor["Email required"] = flag(row["email_required"])
        vendor["Phone required"] = flag(row["phone_required"])

        if not vendor["Stakeholder Vieu ID"]:
            ingest_rows.append(
                {
                    "source_row": row["source_row"],
                    "UID": uid,
                    "Stakeholder Name": row["name"],
                    "Profile Linkedin": item["person_graph_url"] or row["person_url"],
                    "Location": vendor["Location"],
                    "Country": vendor["Country"],
                    "Target Company Name": row["company_name"],
                    "Target Company Linkedin": item["company_graph_url"] or row["company_url"],
                    "reason": (
                        "graph lookup skipped; POSTGRES_* not set"
                        if not graph_configured()
                        else "stakeholder not in graph person table"
                    ),
                }
            )
        else:
            vendor_rows.append(vendor)

        notes = []
        if not data:
            notes.append("person fetch failed; names taken from input")
        if item["person_graph_url"] and item["person_graph_url"] != row["person_url"]:
            notes.append("person LinkedIn vanity from RapidAPI")
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
                notes.append("stakeholder not in graph; held back from vendor file")
            if not vendor["Target Company Vieu ID"]:
                notes.append("target company not in graph")
            if current_url and not vendor["Current Company Vieu ID"]:
                notes.append("current company not in graph")
        qa_rows.append(
            {
                "source_row": row["source_row"],
                "status": (
                    "not_in_graph"
                    if not vendor["Stakeholder Vieu ID"]
                    else ("ok" if data and target_match.matched else "partial")
                ),
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
