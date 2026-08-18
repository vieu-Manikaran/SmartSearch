"""Vendor output columns, input aliases, and the column-enrichment guide."""

from __future__ import annotations

from typing import Dict, List

# Exact vendor headers (including the double spaces in two names).
VENDOR_COLUMNS: List[str] = [
    "UID",
    "Stakeholder Vieu ID",
    "Stakeholder Full  Name",
    "Stakeholder First Name",
    "Stakeholder Middle Name",
    "Stakeholder Last Name",
    "Profile Linkedin",
    "Location",
    "Country",
    "Last Profile Refresh Date",
    "Target Company Vieu ID",
    "Target Company Name",
    "Target Company Website",
    "Target Company Linkedin URL",
    "Target Company Employee Count",
    "Target Company Title",
    "Target Company  Start Date",
    "Target Company Employee Count at Start Date",
    "Target Company Start Title",
    "Current Company Vieu ID",
    "Current Company Website",
    "Current Company Linkedin URL",
    "Current Company Title",
    "Current Company Empl Count",
    "Current Company HQ",
    "Email required",
    "Phone required",
]

# Extra columns we keep only on the QA sidecar, not on the vendor file.
QA_COLUMNS: List[str] = [
    "source_row",
    "status",
    "target_experience_matched",
    "person_fetch_status",
    "company_fetch_status",
    "person_vieu_id_status",
    "target_company_vieu_id_status",
    "current_company_vieu_id_status",
    "current_equals_target",
    "notes",
    "input_name",
    "input_person_linkedin",
    "input_company_name",
    "input_company_linkedin",
]

REJECT_COLUMNS: List[str] = [
    "source_row",
    "UID",
    "Stakeholder Name",
    "Profile Linkedin",
    "Target Company Name",
    "Target Company Linkedin",
    "reason",
]

INPUT_ALIASES: Dict[str, List[str]] = {
    "name": [
        "stakeholder name",
        "stakeholder full name",
        "full name",
        "fullname",
        "name",
        "contact name",
        "person name",
    ],
    "person_linkedin": [
        "profile linkedin",
        "stakeholder linkedin",
        "person linkedin",
        "linkedin",
        "linkedin url",
        "linkedin profile",
        "profile url",
        "person linkedin url",
    ],
    "company_name": [
        "target company name",
        "company name",
        "account name",
        "account",
        "company",
        "target company",
    ],
    "company_linkedin": [
        "target company linkedin",
        "target company linkedin url",
        "company linkedin",
        "company linkedin url",
        "account linkedin",
        "company url",
    ],
    "email_required": ["email required", "email_required", "need email"],
    "phone_required": ["phone required", "phone_required", "need phone"],
}

# Rows for the associate-facing column guide sheet.
COLUMN_GUIDE_ROWS: List[Dict[str, str]] = [
    {
        "Column": "UID",
        "Required on vendor file": "Yes",
        "Source": "Generated per upload",
        "How it is filled": (
            "One request ID for the whole file. Every row in that upload gets "
            "the same UID (format VEN-YYYYMMDD-XXXXXXXX) so you can see which "
            "batch a row came from."
        ),
        "If we cannot fill it": "Never blank — generated before enrichment starts.",
        "Example": "VEN-20260818-A1B2C3D4",
    },
    {
        "Column": "Stakeholder Vieu ID",
        "Required on vendor file": "No",
        "Source": "Graph person.linked_in_url → person.id",
        "How it is filled": (
            "Batched indexed lookup on the Seeqe graph person table. "
            "URL variants (trailing slash, www, http/https) are queried together. "
            "Returns PERS-… when the profile exists in graph."
        ),
        "If we cannot fill it": "Blank — we do not invent Vieu IDs.",
        "Example": "PERS-332f805f-de5f-4188-a460-c0e913ff54e5",
    },
    {
        "Column": "Stakeholder Full  Name",
        "Required on vendor file": "Yes",
        "Source": "RapidAPI /person.fullName (fallback: associate name)",
        "How it is filled": (
            "Prefer LinkedIn fullName after a successful profile fetch. Keep "
            "Unicode (José, Müller). Strip trailing credentials such as MBA/PhD, "
            "not accents. Header has two spaces after Full, matching the vendor spec."
        ),
        "If we cannot fill it": "Use the associate-provided name.",
        "Example": "Seema Swamy",
    },
    {
        "Column": "Stakeholder First Name",
        "Required on vendor file": "Yes",
        "Source": "RapidAPI /person.firstName",
        "How it is filled": (
            "LinkedIn firstName. If the profile fetch fails, first token of the "
            "associate name after dropping credentials."
        ),
        "If we cannot fill it": "Parsed from the associate name.",
        "Example": "Seema",
    },
    {
        "Column": "Stakeholder Middle Name",
        "Required on vendor file": "No",
        "Source": "Derived from fullName minus first + last",
        "How it is filled": (
            "Tokens between firstName and lastName. LinkedIn rarely has a middle "
            "name field. Blank is better than a guess."
        ),
        "If we cannot fill it": "Blank.",
        "Example": "Marie",
    },
    {
        "Column": "Stakeholder Last Name",
        "Required on vendor file": "Yes",
        "Source": "RapidAPI /person.lastName",
        "How it is filled": (
            "LinkedIn lastName (handles van/de particles). Fallback: last token "
            "of the associate name."
        ),
        "If we cannot fill it": "Parsed from the associate name.",
        "Example": "Swamy",
    },
    {
        "Column": "Profile Linkedin",
        "Required on vendor file": "Yes",
        "Source": "Associate input, canonicalized",
        "How it is filled": (
            "Rewritten to https://www.linkedin.com/in/{slug}. Query params, "
            "trailing slashes, /mwlite, and /in/in/ duplicates are stripped. "
            "Sales Nav lead URLs cannot be converted and the row is rejected."
        ),
        "If we cannot fill it": "Row rejected — not sent to the vendor.",
        "Example": "https://www.linkedin.com/in/seemaswamy",
    },
    {
        "Column": "Location",
        "Required on vendor file": "No",
        "Source": "RapidAPI /person.addressWithoutCountry",
        "How it is filled": (
            "LinkedIn metro/city without country. Fallback: addressWithCountry."
        ),
        "If we cannot fill it": "Blank.",
        "Example": "San Francisco Bay Area",
    },
    {
        "Column": "Country",
        "Required on vendor file": "No",
        "Source": "RapidAPI /person.primaryLocale.country",
        "How it is filled": (
            "ISO-2 code when LinkedIn provides it (US, IN, DE). Otherwise the "
            "country string from addressCountryOnly. One convention per file."
        ),
        "If we cannot fill it": "Blank.",
        "Example": "US",
    },
    {
        "Column": "Last Profile Refresh Date",
        "Required on vendor file": "No",
        "Source": "Date of our successful /person fetch",
        "How it is filled": (
            "YYYY-MM-DD of the day we fetched the profile. This is our refresh "
            "date, not LinkedIn’s last-updated timestamp (LinkedIn does not "
            "expose that on this API)."
        ),
        "If we cannot fill it": "Blank when the profile fetch failed.",
        "Example": "2026-08-18",
    },
    {
        "Column": "Target Company Vieu ID",
        "Required on vendor file": "No",
        "Source": "Graph company.linked_in_url → company.id",
        "How it is filled": (
            "Batched indexed lookup on company.linked_in_url (slash / host / "
            "company|school variants). If the slug is numeric and URL missed, "
            "fallback is company.linked_in_id. Duplicate URLs keep the row with "
            "the highest linked_in_followers. Returns COMP-…."
        ),
        "If we cannot fill it": "Blank — we do not invent Vieu IDs.",
        "Example": "COMP-332f805f-de5f-4188-a460-c0e913ff54e5",
    },
    {
        "Column": "Target Company Name",
        "Required on vendor file": "Yes",
        "Source": "Associate input (canonical name from company_pro optional)",
        "How it is filled": (
            "The company the associate asked to generate email for. If RapidAPI "
            "company_pro returns a canonical name for the same LinkedIn URL, we "
            "use that; we never rename from a fuzzy name-only match."
        ),
        "If we cannot fill it": "Row rejected if the associate left it empty.",
        "Example": "Walmart",
    },
    {
        "Column": "Target Company Website",
        "Required on vendor file": "Yes (when API has it)",
        "Source": "RapidAPI /company_pro.website",
        "How it is filled": (
            "Full https homepage. LinkedIn URLs are discarded. Careers/jobs "
            "hosts and paths are rewritten to the company site "
            "(careers.walmart.com/us/en/home → https://www.walmart.com). "
            "ATS boards (Greenhouse, Workday, Lever) are left blank. "
            "This is the email-domain signal."
        ),
        "If we cannot fill it": "Blank — row still sent; flag in the QA file.",
        "Example": "https://www.walmart.com",
    },
    {
        "Column": "Target Company Linkedin URL",
        "Required on vendor file": "Yes",
        "Source": "Associate input, canonicalized",
        "How it is filled": (
            "Rewritten to https://www.linkedin.com/company/{slug-or-id}. "
            "/about, /people, query params, and Sales Nav /sales/company/{id} "
            "are normalized. /school/{slug} is rewritten to /company/{slug}. "
            "A person /in/ URL in this column rejects the row."
        ),
        "If we cannot fill it": "Row rejected — not sent to the vendor.",
        "Example": "https://www.linkedin.com/company/walmart",
    },
    {
        "Column": "Target Company Employee Count",
        "Required on vendor file": "No",
        "Source": "RapidAPI /company_pro.employeeCount",
        "How it is filled": "Current headcount as an integer string.",
        "If we cannot fill it": "Blank. Do not invent a number from a range label.",
        "Example": "483725",
    },
    {
        "Column": "Target Company Title",
        "Required on vendor file": "Yes (when experience matches)",
        "Source": "Person experience matched to the target company",
        "How it is filled": (
            "Most recent title at the target company (present role if they still "
            "work there; otherwise last title before they left). Never the "
            "LinkedIn headline."
        ),
        "If we cannot fill it": "Blank; QA marks target_experience_matched=FALSE.",
        "Example": "Vice President, Security Engineering",
    },
    {
        "Column": "Target Company  Start Date",
        "Required on vendor file": "No",
        "Source": "Earliest experience start at the target company",
        "How it is filled": (
            "Tenure start across promotions at that company, not the latest "
            "title start. YYYY-MM-DD. Month-only LinkedIn dates become YYYY-MM-01. "
            "Header has two spaces after Company, matching the vendor spec."
        ),
        "If we cannot fill it": "Blank.",
        "Example": "2022-01-01",
    },
    {
        "Column": "Target Company Employee Count at Start Date",
        "Required on vendor file": "No",
        "Source": "Not available from RapidAPI",
        "How it is filled": (
            "Always blank. We do not copy current headcount — that would be wrong."
        ),
        "If we cannot fill it": "Blank.",
        "Example": "",
    },
    {
        "Column": "Target Company Start Title",
        "Required on vendor file": "No",
        "Source": "Earliest title in the target company experience group",
        "How it is filled": (
            "First role at that company (handles LinkedIn promotion/breakdown groups)."
        ),
        "If we cannot fill it": "Blank.",
        "Example": "Director, Security Engineering",
    },
    {
        "Column": "Current Company Vieu ID",
        "Required on vendor file": "No",
        "Source": "Graph company.id of the present employer",
        "How it is filled": (
            "Copied from target when current = target. Otherwise the same company "
            "URL lookup against the present-role company LinkedIn URL."
        ),
        "If we cannot fill it": "Blank.",
        "Example": "COMP-332f805f-de5f-4188-a460-c0e913ff54e5",
    },
    {
        "Column": "Current Company Website",
        "Required on vendor file": "No",
        "Source": "company_pro of the current (present) employer",
        "How it is filled": (
            "Copied from target website when current = target. Otherwise fetched "
            "for the present-role company LinkedIn URL."
        ),
        "If we cannot fill it": "Blank.",
        "Example": "https://www.walmart.com",
    },
    {
        "Column": "Current Company Linkedin URL",
        "Required on vendor file": "No",
        "Source": "Present experience companyLink1",
        "How it is filled": (
            "Canonical /company/{slug} of the present role. Copied from target "
            "when they still work at the target company."
        ),
        "If we cannot fill it": "Blank.",
        "Example": "https://www.linkedin.com/company/walmart",
    },
    {
        "Column": "Current Company Title",
        "Required on vendor file": "No",
        "Source": "Present experience title",
        "How it is filled": (
            "Top present role. Differs from target title if they left or moved."
        ),
        "If we cannot fill it": "Blank.",
        "Example": "Vice President, Security Engineering",
    },
    {
        "Column": "Current Company Empl Count",
        "Required on vendor file": "No",
        "Source": "company_pro of the current employer",
        "How it is filled": "Copied from target when current = target.",
        "If we cannot fill it": "Blank.",
        "Example": "483725",
    },
    {
        "Column": "Current Company HQ",
        "Required on vendor file": "No",
        "Source": "company_pro.headquarter",
        "How it is filled": "City, country (e.g. Bentonville, US). Copied from target when current = target.",
        "If we cannot fill it": "Blank.",
        "Example": "Bentonville, US",
    },
    {
        "Column": "Email required",
        "Required on vendor file": "Yes",
        "Source": "Upload default, or per-row column if present",
        "How it is filled": "TRUE or FALSE. Default TRUE unless the associate sets otherwise.",
        "If we cannot fill it": "TRUE.",
        "Example": "TRUE",
    },
    {
        "Column": "Phone required",
        "Required on vendor file": "Yes",
        "Source": "Upload default, or per-row column if present",
        "How it is filled": "TRUE or FALSE. Default TRUE unless the associate sets otherwise.",
        "If we cannot fill it": "TRUE.",
        "Example": "TRUE",
    },
]

GUIDE_HEADERS: List[str] = [
    "Column",
    "Required on vendor file",
    "Source",
    "How it is filled",
    "If we cannot fill it",
    "Example",
]
