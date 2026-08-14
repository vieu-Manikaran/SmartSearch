#!/usr/bin/env python3
"""Merge recovered LinkedIn URLs into Honeycomb EBM CSV and check current employment at Account_AccountName."""

from __future__ import annotations

import csv
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rapidapi_person_deep import collect_rapidapi_keys, fetch_person_deep, normalize_linkedin_profile_url

MAIN_CSV = Path("/Users/manikaransingh/Downloads/Honeycomb_EBM(Matched Account People).csv")
BATCH_CSV = Path(
    "/Users/manikaransingh/linkedin-tenure-weekly/ashutosh/data/person_linkedin/"
    "honeycomb_ebm_linkedin_misses_20260714_145824.csv"
)
OUTPUT_CSV = Path("/Users/manikaransingh/Downloads/Honeycomb_EBM(Matched Account People)_merged.csv")

TITLE_COL = "Perosn Current Job title"  # existing typo column in source file
STILL_AT_COL = "Still_Works_At_Account_AccountName"
PRESENT_TITLES_COL = "Present_Titles_At_Account_AccountName"

LEGAL_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|group|plc|gmbh|ag|sa|nv|bv)\b\.?",
    re.I,
)
COMPANY_SLUG_RE = re.compile(r"linkedin\.com/company/([^/?#]+)", re.I)
PRESENT_RE = re.compile(r"\bpresent\b", re.I)


def company_slug(url: str) -> str:
    m = COMPANY_SLUG_RE.search(url or "")
    return (m.group(1) if m else "").strip("/").casefold()


def normalize_company_name(name: str) -> str:
    text = (name or "").casefold()
    text = text.replace("&", " and ")
    text = LEGAL_SUFFIX_RE.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def company_names_match(account_name: str, experience_company: str) -> bool:
    a = normalize_company_name(account_name)
    b = normalize_company_name(experience_company)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    a_tokens = {t for t in a.split() if len(t) >= 3}
    b_tokens = {t for t in b.split() if len(t) >= 3}
    if not a_tokens or not b_tokens:
        return False
    # Require the distinctive account tokens to appear in the LinkedIn company name.
    overlap = a_tokens & b_tokens
    return bool(overlap) and (overlap == a_tokens or overlap == b_tokens or len(overlap) >= 2)


def extract_company_from_subtitle(subtitle: str) -> str:
    # e.g. "Coinbase · Full-time"
    return (subtitle or "").split("·")[0].strip()


def iter_roles(experiences: list[dict[str, Any]]) -> list[dict[str, str]]:
    roles: list[dict[str, str]] = []
    for exp in experiences:
        if not isinstance(exp, dict):
            continue
        company_id = str(exp.get("companyId") or "").strip()
        company_link = str(exp.get("companyLink1") or exp.get("companyUrl") or "").strip()
        if exp.get("breakdown"):
            company_name = str(exp.get("title") or "").strip()
            for sub in exp.get("subComponents") or []:
                if not isinstance(sub, dict):
                    continue
                title = str(sub.get("title") or "").strip()
                caption = str(sub.get("caption") or "").strip()
                if not title:
                    continue
                roles.append(
                    {
                        "title": title,
                        "company_name": company_name,
                        "company_id": company_id,
                        "company_link": company_link,
                        "caption": caption,
                        "is_current": "yes" if PRESENT_RE.search(caption) else "no",
                    }
                )
        else:
            title = str(exp.get("title") or "").strip()
            caption = str(exp.get("caption") or "").strip()
            company_name = extract_company_from_subtitle(str(exp.get("subtitle") or ""))
            if not title:
                continue
            roles.append(
                {
                    "title": title,
                    "company_name": company_name,
                    "company_id": company_id,
                    "company_link": company_link,
                    "caption": caption,
                    "is_current": "yes" if PRESENT_RE.search(caption) else "no",
                }
            )
    return roles


def role_matches_account(role: dict[str, str], account_name: str, account_company_url: str) -> bool:
    account_slug = company_slug(account_company_url)
    role_slug = company_slug(role.get("company_link") or "")
    if account_slug and role_slug and account_slug == role_slug:
        return True
    return company_names_match(account_name, role.get("company_name") or "")


def presence_at_account(
    data: dict[str, Any],
    account_name: str,
    account_company_url: str,
) -> tuple[str, str, str]:
    """
    Returns (still_works Yes/No, present_titles, all_current_titles_debug).
    Still_works is Yes only when at least one Present role matches Account_AccountName.
    """
    experiences = data.get("experiences") if isinstance(data.get("experiences"), list) else []
    roles = iter_roles(experiences)
    present_roles = [r for r in roles if r["is_current"] == "yes"]
    matched_present = [r for r in present_roles if role_matches_account(r, account_name, account_company_url)]

    titles = []
    seen = set()
    for r in matched_present:
        t = r["title"]
        key = t.casefold()
        if t and key not in seen:
            seen.add(key)
            titles.append(t)

    still = "Yes" if titles else "No"
    title_str = " | ".join(titles)
    return still, title_str, title_str


def load_found_urls(path: Path) -> dict[tuple[str, str], str]:
    mapping: dict[tuple[str, str], str] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            url = (row.get("LinkedIn_URL") or "").strip()
            if not url:
                continue
            key = (
                (row.get("Person") or "").strip().casefold(),
                (row.get("Company") or "").strip().casefold(),
            )
            mapping[key] = normalize_linkedin_profile_url(url).rstrip("/")
    return mapping


def main() -> int:
    keys = collect_rapidapi_keys()
    if not keys:
        raise SystemExit("RAPIDAPI_KEY missing")

    found = load_found_urls(BATCH_CSV)
    with MAIN_CSV.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    for col in (TITLE_COL, STILL_AT_COL, PRESENT_TITLES_COL):
        if col not in fieldnames:
            fieldnames.append(col)

    # Merge LinkedIn URLs for the 18 recovered rows.
    merge_idxs: list[int] = []
    for idx, row in enumerate(rows):
        if (row.get("Person Linkeidn") or "").strip():
            continue
        key = (
            (row.get("Person_FullName") or "").strip().casefold(),
            (row.get("Person_Company") or "").strip().casefold(),
        )
        url = found.get(key)
        if not url:
            continue
        row["Person Linkeidn"] = url
        merge_idxs.append(idx)

    print(f"Merged LinkedIn URLs into {len(merge_idxs)} rows")

    def _enrich(i: int) -> tuple[int, str, str, str, str]:
        row = rows[i]
        link = row["Person Linkeidn"]
        key = keys[i % len(keys)]
        result = fetch_person_deep(link, key)
        if not result.get("success"):
            err = str(result.get("error") or "fetch_failed")
            return i, "No", "", "", err
        still, titles, _ = presence_at_account(
            result["data"],
            row.get("Account_AccountName") or "",
            row.get("Account_Company_LinkedIn_url__c") or "",
        )
        return i, still, titles, titles, "ok"

    results: list[tuple[int, str, str, str, str]] = []
    with ThreadPoolExecutor(max_workers=min(2, len(keys), max(1, len(merge_idxs)))) as pool:
        futures = [pool.submit(_enrich, i) for i in merge_idxs]
        for fut in as_completed(futures):
            results.append(fut.result())
            time.sleep(0.05)

    ok = 0
    yes = 0
    for idx, still, title, present_titles, status in sorted(results, key=lambda x: x[0]):
        rows[idx][STILL_AT_COL] = still
        rows[idx][TITLE_COL] = title if still == "Yes" else ""
        rows[idx][PRESENT_TITLES_COL] = present_titles if still == "Yes" else ""
        person = rows[idx].get("Person_FullName")
        acct = rows[idx].get("Account_AccountName")
        print(f"  [{status}] {person} @ {acct}: still={still} title={title!r}")
        if status == "ok":
            ok += 1
        if still == "Yes":
            yes += 1

    # Leave non-merged rows blank for new columns.
    for idx, row in enumerate(rows):
        if idx in set(merge_idxs):
            continue
        row.setdefault(STILL_AT_COL, "")
        row.setdefault(PRESENT_TITLES_COL, "")
        row.setdefault(TITLE_COL, row.get(TITLE_COL) or "")

    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    filled = sum(1 for r in rows if (r.get("Person Linkeidn") or "").strip())
    print(
        f"Wrote {OUTPUT_CSV}\n"
        f"LinkedIn filled: {filled}/{len(rows)}; "
        f"presence checked: {ok}/{len(merge_idxs)}; still at account: {yes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
