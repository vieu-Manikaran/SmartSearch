"""Map heterogeneous CSV rows to Seeqe callback fields."""

from __future__ import annotations

import re
from typing import Any

EMAIL_COLUMNS = (
    "Verified Email",
    "Email (FullEnrich)",
    "Work Email",
    "Work_Email",
    "Contact Email ID",
    "email",
)

LINKEDIN_COLUMNS = (
    "LinkedIn URL",
    "LinkedIn Profile Url",
    "Linkedin Url(FullEnrich)",
    "Linkedin Url",
    "Stakeholder LinkedIn Link",
    "Contact Linkedin URL",
    "Contact Linkedin",
    "LinkedIn Profile",
    "LinkedIn",
    "Linkedin",
)

STATUS_COLUMNS = (
    "Email Status",
    "Row Status (FullEnrich)",
    "fe_row_status",
)

BOUNCE_STATUS_COLUMNS = (
    "Bounce Status (FullEnrich)",
    "Email Bounce Status",
)

COMPANY_COLUMNS = (
    "Company Name",
    "Company",
    "companyName",
    "Account Name",
    "Account",
    "Company Name (Linkedin)",
)

CREATED_AT_COLUMNS = (
    "Clay Enrich Time",
    "Clay Create Time",
)

# Prefer highest deliverability when deduping the same LinkedIn profile.
_STATUS_RANK = {
    "success": 100,
    "valid & safe to send email": 100,
    "partial success": 50,
    "probably valid email": 40,
    "catch-all": 20,
    "catch all": 20,
    "not found": 0,
}


def _first_value(row: dict[str, Any], columns: tuple[str, ...]) -> str:
    for col in columns:
        val = (row.get(col) or "").strip()
        if val:
            return val
    return ""


def normalize_linkedin_url(url: str) -> str:
    u = (url or "").strip().lower().rstrip("/")
    if not u or "linkedin.com" not in u:
        return ""
    u = u.replace("http://", "https://")
    if u.startswith("https://linkedin.com"):
        u = u.replace("https://linkedin.com", "https://www.linkedin.com", 1)
    return u


def linkedin_key(url: str) -> str:
    u = normalize_linkedin_url(url)
    if not u:
        return ""
    m = re.search(r"linkedin\.com/(?:in|sales/lead)/([^/?#]+)", u)
    return m.group(1).lower() if m else u


def status_rank(status: str) -> int:
    return _STATUS_RANK.get((status or "").strip().lower(), 10 if (status or "").strip() else 0)


def csv_row_to_seeqe(row: dict[str, Any], *, source_file: str = "") -> dict[str, str] | None:
    email = _first_value(row, EMAIL_COLUMNS)
    linkedin_url = normalize_linkedin_url(_first_value(row, LINKEDIN_COLUMNS))
    if not email or not linkedin_url:
        return None

    created_at = _first_value(row, CREATED_AT_COLUMNS)
    out = {
        "linkedin_url": linkedin_url,
        "work_email": email,
        "email_status": _first_value(row, STATUS_COLUMNS),
        "bounce_status": _first_value(row, BOUNCE_STATUS_COLUMNS),
        "company_name": _first_value(row, COMPANY_COLUMNS),
        "created_at": created_at,
    }
    if source_file:
        out["source_file"] = source_file
    return out


def row_priority(row: dict[str, str]) -> tuple[int, int, int, str]:
    """Prefer Success / Valid&safe, then bounce quality, then created_at, then email."""
    status = row.get("email_status") or ""
    bounce = row.get("bounce_status") or ""
    return (
        status_rank(status),
        status_rank(bounce),
        1 if row.get("created_at") else 0,
        row.get("work_email") or "",
    )
