#!/usr/bin/env python3
"""Resolve LinkedIn URLs for Contact Level deals CSV (name + account, with partner fallback)."""

from __future__ import annotations

import csv
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from person_linkedin_finder import find_person_linkedin
from rapidapi_person_deep import normalize_linkedin_profile_url

SRC = Path("/Users/manikaransingh/Downloads/Contact Level deals - Sheet1 (1).csv")
PRIOR = ROOT / "data/person_linkedin/contact_level_deals_input_20260728_132423.csv"
OUT = ROOT / "data/person_linkedin/contact_level_deals_linkedin.csv"
OUT_FULL = Path("/Users/manikaransingh/Downloads/Contact Level deals - LinkedIn.csv")

ALIASES: dict[str, list[str]] = {
    "orion engineered carbons": ["Orion Engineered Carbons", "Orion Carbons"],
    "seer interactive": ["Seer Interactive"],
    "public service division": ["Public Service Division Singapore", "PSD Singapore"],
    "broadview federal credit union": ["Broadview Federal Credit Union", "Broadview FCU"],
    "cdw uk": ["CDW", "CDW UK"],
    "fresenius medical care ag": ["Fresenius Medical Care", "Fresenius"],
    "carl zeiss vision international gmbh": ["Carl Zeiss Vision", "ZEISS"],
    "sauter-cumulus gmbh": ["Sauter Cumulus", "SAUTER"],
    "west fraser": ["West Fraser"],
    "ringcentral - embedded": ["RingCentral"],
    "logisteed": ["LOGISTEED"],
    "university of idaho foundation inc": ["University of Idaho Foundation", "University of Idaho"],
    "promigas": ["Promigas"],
    "ghafari associates": ["Ghafari Associates", "Ghafari"],
    "genpact - bpo": ["Genpact"],
    "project44": ["project44"],
    "rwg germany gmbh": ["RWG Germany", "RWG"],
    "nippon shinyaku": ["Nippon Shinyaku"],
    "sk specialty": ["SK Specialty"],
    "alphapet ventures": ["AlphaPet Ventures", "AlphaPet"],
    "macerich": ["Macerich"],
    "klicktipp limited - embedded": ["KlickTipp"],
    "archway group": ["Archway Group"],
    "ts tech": ["TS TECH"],
}

DOMAIN_COMPANY = {
    "allcloud.io": "AllCloud",
    "team.wrike.com": "Wrike",
    "wrike.com": "Wrike",
    "accenture.com": "Accenture",
    "amazon.com": "Amazon Web Services",
    "ncs.com.sg": "NCS",
    "leonardo.com.au": "Leonardo",
    "gulanga.com.au": "Gulanga",
    "davidsonwp.com": "Davidson",
    "horizon-five.com": "Horizon Five",
    "virtuoso-partners.io": "Virtuoso Partners",
    "workday.com": "Workday",
    "digitaldirections.io": "Digital Directions",
    "hitachi-solutions.com": "Hitachi Solutions",
}


def fix_mojibake(s: str) -> str:
    if not s:
        return s
    for enc in ("latin1", "cp1252"):
        try:
            fixed = s.encode(enc).decode("utf-8")
            if fixed != s:
                return fixed
        except Exception:
            pass
    return s


def account_key(account: str) -> str:
    a = fix_mojibake(account).casefold()
    a = re.sub(r"\([^)]*\)", " ", a)
    a = re.sub(r"[^a-z0-9&., -]+", " ", a)
    a = re.sub(r"\s+", " ", a).strip()
    for key in ALIASES:
        if a.startswith(key) or key in a:
            return key
    if "ts tech" in a or "テイ" in fix_mojibake(account):
        return "ts tech"
    if "logisteed" in a:
        return "logisteed"
    if "nippon shinyaku" in a or "日本新薬" in fix_mojibake(account):
        return "nippon shinyaku"
    if "ghafari" in a:
        return "ghafari associates"
    return a


def employer_from_email(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email:
        return ""
    dom = email.split("@", 1)[1].strip()
    if dom in DOMAIN_COMPANY:
        return DOMAIN_COMPANY[dom]
    parts = dom.split(".")
    if len(parts) >= 3 and parts[0] in {"uk", "us", "au", "mail", "team"}:
        return parts[1].replace("-", " ").title()
    if len(parts) >= 2:
        return parts[-2].replace("-", " ").title()
    return ""


def clean_person(first: str, last: str, email: str = "") -> str | None:
    first, last = fix_mojibake(first).strip(), fix_mojibake(last).strip()
    first = re.sub(r"\[\[unknown\]\]", "", first, flags=re.I).strip()
    last = re.sub(r"\[\[unknown\]\]", "", last, flags=re.I).strip()
    if not first or not last:
        return None
    if first.lower() in {"xxx", "tender"} or last.lower() in {"xxx", "contact"}:
        return None
    if first.lower() == last.lower() == "clintp":
        return None
    if "department" in first.lower() or "administrator" in (first + " " + last).lower():
        return None
    person = f"{first} {last}".strip()
    if len(re.findall(r"[A-Za-z]", person)) < 2 and email and "@" in email:
        local = email.split("@")[0]
        parts = [p for p in re.split(r"[._]+", local) if p.isalpha() and len(p) > 1]
        if len(parts) >= 2:
            person = f"{parts[0].title()} {parts[1].title()}"
    if len(re.findall(r"[A-Za-z]", person)) < 2:
        return None
    return person


def candidate_companies(account: str, email: str) -> list[str]:
    out: list[str] = []
    key = account_key(account)
    for a in ALIASES.get(key, [fix_mojibake(account)]):
        if a and a not in out:
            out.append(a)
    m = re.search(r"\(([A-Za-z][^)]*)\)", fix_mojibake(account))
    if m:
        alias = re.sub(r",?\s*(Ltd\.?|LLC|Inc\.?|GmbH|AG|Co\.?).*$", "", m.group(1), flags=re.I).strip()
        if alias and alias not in out:
            out.append(alias)
    emp = employer_from_email(email)
    if emp:
        acc_norm = re.sub(r"[^a-z0-9]+", "", fix_mojibake(account).casefold())
        emp_norm = re.sub(r"[^a-z0-9]+", "", emp.casefold())
        if emp_norm and emp_norm not in acc_norm and acc_norm[:5] not in emp_norm:
            out.append(emp)
    return out


def lookup(person: str, companies: list[str]):
    best = None
    for company in companies:
        match = find_person_linkedin(person, company)
        if match.status == "found":
            return match, company
        if match.status == "low_confidence" and match.url:
            if best is None or match.score > best[0].score:
                best = (match, company)
    return (best[0], best[1]) if best else (None, None)


def load_contacts() -> list[dict[str, str]]:
    contacts: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    with SRC.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            first = (row.get("ContactFirstName") or "").strip()
            last = (row.get("ContactLastName") or "").strip()
            account = (row.get("AccountName") or "").strip()
            email = (row.get("ContactEmail") or "").strip()
            person = clean_person(first, last, email)
            if not person or not account:
                continue
            company = fix_mojibake(account)
            key = (person.casefold(), company.casefold())
            if key in seen:
                continue
            seen.add(key)
            contacts.append(
                {
                    "Person": person,
                    "Company": company,
                    "Email": email,
                    "ContactId": (row.get("ContactId") or "").strip(),
                }
            )
    return contacts


def load_prior() -> dict[tuple[str, str], dict[str, str]]:
    if not PRIOR.exists():
        return {}
    out: dict[tuple[str, str], dict[str, str]] = {}
    with PRIOR.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[(row["Person"].casefold(), row["Company"].casefold())] = row
    return out


def main() -> int:
    contacts = load_contacts()
    prior = load_prior()
    retries: list[dict[str, str]] = []

    for contact in contacts:
        prev = prior.get((contact["Person"].casefold(), contact["Company"].casefold()))
        if not prev:
            for (person, _), row in prior.items():
                if person == contact["Person"].casefold() and row.get("Status") == "found":
                    prev = row
                    break
        if prev and prev.get("Status") == "found" and prev.get("LinkedIn_URL"):
            contact["LinkedIn_URL"] = normalize_linkedin_profile_url(prev["LinkedIn_URL"]).rstrip("/")
            contact["Status"] = "found"
            contact["Match_Score"] = prev.get("Match_Score", "")
            contact["Source"] = prev.get("Source", "")
            contact["Search_Company"] = contact["Company"]
        else:
            retries.append(contact)

    print(f"contacts={len(contacts)} prior_found={len(contacts) - len(retries)} retry={len(retries)}", flush=True)

    def do_retry(contact: dict[str, str]) -> dict[str, str]:
        companies = candidate_companies(contact["Company"], contact["Email"])
        match, used = lookup(contact["Person"], companies)
        if match and match.url:
            contact["LinkedIn_URL"] = normalize_linkedin_profile_url(match.url).rstrip("/")
            contact["Status"] = match.status
            contact["Match_Score"] = str(match.score)
            contact["Source"] = f"{match.source}|retry:{used}"
            contact["Search_Company"] = used or ""
        else:
            contact["LinkedIn_URL"] = ""
            contact["Status"] = "no_profile_in_top_10"
            contact["Match_Score"] = "0"
            contact["Source"] = ""
            contact["Search_Company"] = ";".join(companies[:3])
        return contact

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(do_retry, c) for c in retries]
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            print(
                f"[{i}/{len(retries)}] {res['Person'][:32]:32} {res['Status']:20} {res.get('LinkedIn_URL','')}",
                flush=True,
            )

    fields = [
        "Person",
        "Company",
        "Email",
        "ContactId",
        "LinkedIn_URL",
        "Status",
        "Match_Score",
        "Source",
        "Search_Company",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(contacts)

    # Expand onto original opportunity rows
    by_contact: dict[str, dict[str, str]] = {c["ContactId"]: c for c in contacts if c.get("ContactId")}
    by_name_co: dict[tuple[str, str], dict[str, str]] = {
        (c["Person"].casefold(), c["Company"].casefold()): c for c in contacts
    }

    with SRC.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        src_fields = list(reader.fieldnames or [])
        src_rows = list(reader)

    out_fields = src_fields + ["LinkedIn_URL", "LinkedIn_Status", "LinkedIn_Match_Score", "LinkedIn_Source"]
    with OUT_FULL.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        for row in src_rows:
            cid = (row.get("ContactId") or "").strip()
            match = by_contact.get(cid)
            if not match:
                person = clean_person(
                    row.get("ContactFirstName") or "",
                    row.get("ContactLastName") or "",
                    row.get("ContactEmail") or "",
                )
                company = fix_mojibake((row.get("AccountName") or "").strip())
                if person:
                    match = by_name_co.get((person.casefold(), company.casefold()))
            row = dict(row)
            if match:
                row["LinkedIn_URL"] = match.get("LinkedIn_URL", "")
                row["LinkedIn_Status"] = match.get("Status", "")
                row["LinkedIn_Match_Score"] = match.get("Match_Score", "")
                row["LinkedIn_Source"] = match.get("Source", "")
            else:
                row["LinkedIn_URL"] = ""
                row["LinkedIn_Status"] = "skipped_invalid"
                row["LinkedIn_Match_Score"] = ""
                row["LinkedIn_Source"] = ""
            writer.writerow(row)

    found = sum(1 for c in contacts if c.get("LinkedIn_URL"))
    print(f"done unique={len(contacts)} with_url={found} -> {OUT}", flush=True)
    print(f"full rows -> {OUT_FULL}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
