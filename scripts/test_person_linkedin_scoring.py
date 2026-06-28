#!/usr/bin/env python3
"""Compare old vs new person LinkedIn matching on known incorrect cases."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serper_search import find_linkedin_person_match, search_serper, LINKEDIN_PERSON_PATH


def load_api_key() -> str:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return os.getenv("SERPER_API_KEY", "")


def slug(url: str) -> str:
    if not isinstance(url, str) or not url:
        return ""
    match = re.search(r"linkedin\.com/in/([^/?#]+)", url, re.I)
    if not match:
        return url
    return unquote(match.group(1)).rstrip("/")


def old_method(person: str, company: str, api_key: str) -> str | None:
    query = f"{person} {company} site:linkedin.com"
    items = search_serper(query, api_key, num=10, date_restrict=None, gl=None, page=1)
    needle = LINKEDIN_PERSON_PATH.casefold()
    for item in items:
        link = item.get("link") if isinstance(item.get("link"), str) else None
        if link and needle in link.casefold():
            return link
    return None


def load_error_cases() -> list[dict]:
    xlsx = Path("/Users/manikaransingh/Downloads/Atlantic ( Error).xlsx")
    df = pd.read_excel(xlsx)
    cases = []
    for _, row in df.iterrows():
        cases.append(
            {
                "person": str(row["Person"]).strip(),
                "company": str(row["Company "]).strip(),
                "old_wrong_url": str(row["Linkedin Url Found ( tool)"]).strip(),
                "correct_url": str(row.get("Manually Found ", "")).strip(),
                "source": "Atlantic (Error).xlsx",
            }
        )
    return cases


def outcome(new_slug: str, old_slug: str, correct_slug: str) -> str:
    if correct_slug and correct_slug.lower() not in ("nan", "rebecca m. | linkedin", "dinaker yanamandala | linkedin", "elizabeth b. | linkedin"):
        if new_slug and (new_slug == correct_slug or correct_slug in new_slug or new_slug in correct_slug):
            return "FIXED"
        if not new_slug and old_slug:
            return "REJECTED_WRONG (good)"
        if new_slug and new_slug == old_slug:
            return "STILL_WRONG"
        if new_slug and new_slug != old_slug:
            return "DIFFERENT"
    if not new_slug and old_slug:
        return "REJECTED_WRONG (good)"
    if new_slug and new_slug == old_slug:
        return "STILL_WRONG"
    if new_slug and new_slug != old_slug:
        return "CHANGED"
    return "NO_RESULT"


def main() -> None:
    api_key = load_api_key()
    if not api_key:
        print("Missing SERPER_API_KEY")
        sys.exit(1)

    cases = load_error_cases()
    print(f"Testing {len(cases)} known incorrect cases from Atlantic (Error).xlsx\n")
    print(f"{'Person':<22} {'Company':<22} {'Old (wrong)':<28} {'New result':<28} {'Score':>5}  {'Status':<22} {'Outcome'}")
    print("-" * 155)

    fixed = rejected_good = still_wrong = different = 0

    for case in cases:
        person = case["person"]
        company = case["company"]
        old_url = old_method(person, company, api_key)
        match = find_linkedin_person_match(person, company, api_key, num=10, date_restrict=None)
        new_url = match.url or ""
        old_slug = slug(old_url or case["old_wrong_url"])
        new_slug = slug(new_url)
        correct_raw = case["correct_url"]
        correct_slug = slug(correct_raw) if correct_raw and correct_raw.lower() != "nan" else ""

        result = outcome(new_slug, old_slug, correct_slug)
        if result == "FIXED":
            fixed += 1
        elif result == "REJECTED_WRONG (good)":
            rejected_good += 1
        elif result == "STILL_WRONG":
            still_wrong += 1
        else:
            different += 1

        print(
            f"{person[:21]:<22} {company[:21]:<22} {old_slug[:27]:<28} {new_slug[:27] or '(none)':<28} "
            f"{match.score:>5}  {match.status:<22} {result}"
        )
        if correct_slug:
            print(f"{'':22} {'':22} {'':28} correct: {correct_slug[:60]}")

    print("-" * 155)
    print(
        f"Summary: fixed={fixed}, rejected_wrong={rejected_good}, still_wrong={still_wrong}, "
        f"changed/other={different}"
    )


if __name__ == "__main__":
    main()
