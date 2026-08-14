"""RapidAPI linkedin-data-scraper /company client (employee count + numeric org id)."""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.parse import unquote

import requests

from config import settings
from rapidapi_person_deep import RAPIDAPI_HOST, collect_rapidapi_keys

logger = logging.getLogger(__name__)

MAX_RETRIES = 6
RETRY_BACKOFF_SEC = 3.0
NUMERIC_ID_RE = re.compile(r"^\d{2,12}$")
COMPANY_PATH_RE = re.compile(
    r"linkedin\.com/(?:company|school|showcase)/([^/?#]+)",
    re.I,
)
SALES_COMPANY_RE = re.compile(
    r"linkedin\.com/sales/(?:company|accounts)/(\d{2,12})",
    re.I,
)
QUERY_ID_RE = re.compile(
    r"(?:companyId|company_id|orgId|org_id|id)=(\d{2,12})",
    re.I,
)


class CompanyLookupError(Exception):
    """Raised when the RapidAPI company endpoint cannot be called."""


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": RAPIDAPI_HOST,
        "Content-Type": "application/json",
    }


def _retryable_status(code: int) -> bool:
    return code in {408, 425, 429, 500, 502, 503, 504}


def extract_numeric_company_id(raw: str) -> str:
    """Return a numeric LinkedIn company/org id if `raw` already contains one."""
    value = (raw or "").strip().strip("/")
    if not value:
        return ""
    if value.endswith(".0") and value[:-2].isdigit():
        value = value[:-2]
    if NUMERIC_ID_RE.fullmatch(value):
        return value
    query_match = QUERY_ID_RE.search(value)
    if query_match:
        return query_match.group(1)
    sales_match = SALES_COMPANY_RE.search(value)
    if sales_match:
        return sales_match.group(1)
    path_match = COMPANY_PATH_RE.search(value)
    if path_match:
        slug = unquote(path_match.group(1)).strip().strip("/")
        if NUMERIC_ID_RE.fullmatch(slug):
            return slug
    return ""


def normalize_linkedin_company_url(raw: str) -> str:
    """Accept full company URLs, numeric ids, or bare slugs."""
    value = (raw or "").strip()
    if not value:
        return ""

    numeric = extract_numeric_company_id(value)
    if numeric:
        return f"https://www.linkedin.com/company/{numeric}/"

    path_match = COMPANY_PATH_RE.search(value)
    if path_match:
        slug = unquote(path_match.group(1)).strip().strip("/")
        if slug:
            return f"https://www.linkedin.com/company/{slug}/"

    lower = value.lower()
    if "linkedin.com/" in lower:
        return ""

    slug = value.strip("/").split("?")[0].split("#")[0]
    if not slug or " " in slug:
        return ""
    return f"https://www.linkedin.com/company/{slug}/"


def is_valid_linkedin_company_url(raw: str) -> bool:
    value = (raw or "").strip()
    if not value:
        return False
    if extract_numeric_company_id(value):
        return True
    lower = value.lower()
    return (
        "linkedin.com/company/" in lower
        or "linkedin.com/school/" in lower
        or "linkedin.com/showcase/" in lower
        or "linkedin.com/sales/company/" in lower
        or "linkedin.com/sales/accounts/" in lower
    )


def fetch_linkedin_company(link: str, api_key: str) -> dict[str, Any]:
    """
    Call POST /company for one company page URL.

    Returns {"success": True, "data": {...}} or {"success": False, "error": "<code>"}.
    """
    url = settings.rapidapi_company_url
    normalized = normalize_linkedin_company_url(link)
    if not normalized:
        return {"success": False, "error": "invalid_url"}

    last_error = "unknown_error"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                url,
                headers=_headers(api_key),
                json={"link": normalized},
                timeout=60,
            )
        except requests.RequestException as exc:
            last_error = str(exc)
            logger.warning("company request failed (attempt %s): %s", attempt, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
                continue
            return {"success": False, "error": "network_error"}

        if resp.status_code in {403, 404}:
            return {"success": False, "error": "company_not_found"}

        if _retryable_status(resp.status_code):
            last_error = f"http_{resp.status_code}"
            logger.warning("company HTTP %s (attempt %s)", resp.status_code, attempt)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
                continue
            return {"success": False, "error": "max_retries_exceeded"}

        if resp.status_code != 200:
            return {"success": False, "error": f"http_{resp.status_code}"}

        try:
            payload = resp.json()
        except ValueError:
            last_error = "invalid_json"
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
                continue
            return {"success": False, "error": "invalid_json"}

        if not payload.get("success"):
            return {"success": False, "error": "api_error"}

        data = payload.get("data")
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict):
            return {"success": False, "error": "missing_data"}

        return {"success": True, "data": data}

    return {"success": False, "error": last_error}


def _format_employee_count(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip().replace(",", "")
    if NUMERIC_ID_RE.fullmatch(text) or re.fullmatch(r"\d+", text):
        return str(int(text))
    return ""


def _format_company_id(value: Any) -> str:
    if value is None or value == "":
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text if NUMERIC_ID_RE.fullmatch(text) else ""


def lookup_company(
    link: str,
    *,
    existing_company_id: str = "",
    api_key: str | None = None,
) -> dict[str, str]:
    """
    Resolve employee count and numeric LinkedIn company id for one company URL.

    If `existing_company_id` (or a numeric slug already in the URL) is present,
    that id is written to LinkedIn_Company_ID even when RapidAPI returns another value.
    """
    input_id = extract_numeric_company_id(existing_company_id) or extract_numeric_company_id(link)
    lookup_link = f"https://www.linkedin.com/company/{input_id}/" if input_id else link
    normalized = normalize_linkedin_company_url(lookup_link)

    keys = collect_rapidapi_keys()
    key = api_key or (keys[0] if keys else "")
    if not key:
        return {
            "linkedin_url": normalized or (link or "").strip(),
            "employee_count": "",
            "linkedin_company_id": input_id,
            "status": "missing_rapidapi_key" if not input_id else "id_from_input",
        }

    if not normalized:
        return {
            "linkedin_url": (link or "").strip(),
            "employee_count": "",
            "linkedin_company_id": input_id,
            "status": "invalid_url",
        }

    result = fetch_linkedin_company(normalized, key)
    if not result.get("success"):
        error = str(result.get("error") or "lookup_failed")
        return {
            "linkedin_url": normalized,
            "employee_count": "",
            "linkedin_company_id": input_id,
            "status": "id_from_input" if input_id else error,
        }

    data = result["data"]
    api_id = _format_company_id(data.get("companyId") or data.get("id") or data.get("company_id"))
    employee_count = _format_employee_count(data.get("employeeCount") if "employeeCount" in data else data.get("employee_count"))
    company_id = input_id or api_id
    if employee_count:
        status = "found"
    elif company_id:
        status = "id_from_input" if input_id else "id_only"
    else:
        status = "not_found"
    return {
        "linkedin_url": normalized,
        "employee_count": employee_count,
        "linkedin_company_id": company_id,
        "status": status,
    }


def enrich_companies_batch(
    rows: list[dict[str, Any]],
    *,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    """Look up many company rows concurrently (one RapidAPI key per worker)."""
    keys = collect_rapidapi_keys()
    total = len(rows)
    if not keys:
        out: list[dict[str, Any]] = []
        for row in rows:
            merged = dict(row)
            input_id = extract_numeric_company_id(str(row.get("existing_company_id") or "")) or extract_numeric_company_id(
                str(row.get("linkedin_url") or "")
            )
            merged.update(
                {
                    "employee_count": "",
                    "linkedin_company_id": input_id,
                    "status": "id_from_input" if input_id else "missing_rapidapi_key",
                }
            )
            out.append(merged)
        return out

    workers = min(len(keys), total) if total else 1
    completed = 0
    results: list[dict[str, Any] | None] = [None] * total
    cache: dict[tuple[str, str], dict[str, str]] = {}
    cache_lock = threading.Lock()

    def _lookup_index(idx: int) -> tuple[int, dict[str, Any]]:
        row = rows[idx]
        link = str(row.get("linkedin_url") or "")
        existing_id = str(row.get("existing_company_id") or "")
        cache_key = (normalize_linkedin_company_url(link), extract_numeric_company_id(existing_id))
        with cache_lock:
            cached = cache.get(cache_key)
        if cached is None:
            looked_up = lookup_company(
                link,
                existing_company_id=existing_id,
                api_key=keys[idx % len(keys)],
            )
            with cache_lock:
                cache[cache_key] = looked_up
            cached = looked_up
        merged = dict(row)
        merged.update(
            {
                "linkedin_url": cached["linkedin_url"] or link,
                "employee_count": cached["employee_count"],
                "linkedin_company_id": cached["linkedin_company_id"],
                "status": cached["status"],
            }
        )
        return idx, merged

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_lookup_index, i): i for i in range(total)}
        for future in as_completed(futures):
            idx, merged = future.result()
            results[idx] = merged
            completed += 1
            if progress:
                label = str(merged.get("company") or merged.get("linkedin_url") or "")
                progress(completed, total, label)

    return [r for r in results if r is not None]
