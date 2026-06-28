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
    "Bounce Status (FullEnrich)",
)

CREATED_AT_COLUMNS = (
    "Clay Enrich Time",
    "Clay Create Time",
)


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
        "created_at": created_at,
    }
    if source_file:
        out["source_file"] = source_file
    return out


def row_priority(row: dict[str, str]) -> tuple[int, str]:
    """Prefer rows with created_at, then email_status, then email."""
    return (
        1 if row.get("created_at") else 0,
        1 if row.get("email_status") else 0,
        row.get("work_email") or "",
    )
