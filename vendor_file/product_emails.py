"""Split vendor requests that already have email in the Seeqe product."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

from seeqe_contact_lookup import SeeqeContactLookupError, find_existing_contact
from vendor_file.urls import canonicalize_person_url

logger = logging.getLogger(__name__)

EXISTING_EMAIL_COLUMNS = [
    "source_row",
    "UID",
    "Stakeholder Name",
    "Profile Linkedin",
    "Target Company Name",
    "Target Company Linkedin",
    "Stakeholder Vieu ID",
    "Work Email",
    "All Work Emails",
    "Email Source",
    "Email required",
    "Phone required",
]


def split_existing_product_emails(
    rows: list[dict[str, Any]],
    *,
    uid: str,
    out_dir: Path,
) -> tuple[list[dict[str, Any]], Path, int]:
    """Return vendor-needed rows and write product-email hits for the requester.

    Existing-email rows are removed from email-only requests. If a phone is
    still requested, the row remains for the vendor with email disabled.
    """
    remaining: list[dict[str, Any]] = []
    existing: list[dict[str, str]] = []
    cache: dict[str, Any] = {}

    for row in rows:
        if not bool(row.get("email_required")):
            remaining.append(row)
            continue
        person = canonicalize_person_url(row.get("person_linkedin") or "")
        if not person.ok:
            remaining.append(row)
            continue
        if person.url not in cache:
            try:
                cache[person.url] = find_existing_contact(person.url)
            except SeeqeContactLookupError as exc:
                logger.warning(
                    "Seeqe product email lookup failed for %s; continuing: %s",
                    person.url,
                    exc,
                )
                cache[person.url] = None
        contact = cache[person.url]
        if not contact:
            remaining.append(row)
            continue
        existing.append(
            {
                "source_row": str(row.get("source_row") or ""),
                "UID": uid,
                "Stakeholder Name": str(row.get("name") or ""),
                "Profile Linkedin": person.url,
                "Target Company Name": str(row.get("company_name") or ""),
                "Target Company Linkedin": str(row.get("company_linkedin") or ""),
                "Stakeholder Vieu ID": contact.person_id,
                "Work Email": contact.email,
                "All Work Emails": ", ".join(contact.all_emails or (contact.email,)),
                "Email Source": "seeqe_product",
                "Email required": "TRUE" if row.get("email_required") else "FALSE",
                "Phone required": "TRUE" if row.get("phone_required") else "FALSE",
            }
        )
        if bool(row.get("phone_required")):
            remaining.append({**row, "email_required": False})

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{uid}_existing_emails.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXISTING_EMAIL_COLUMNS)
        writer.writeheader()
        writer.writerows(existing)
    return remaining, path, len(existing)
