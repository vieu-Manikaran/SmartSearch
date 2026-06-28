#!/usr/bin/env python3
"""Offline replay: score mock Serper results for known incorrect cases."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serper_search import find_linkedin_person_match, find_linkedin_person_url


def slug(url: str) -> str:
    if not url or str(url).lower() == "nan":
        return ""
    match = re.search(r"linkedin\.com/in/([^/?#]+)", str(url), re.I)
    if not match:
        return str(url)
    return unquote(match.group(1)).rstrip("/")


def linkedin_item(name_title: str, url_slug: str, snippet: str = "") -> dict[str, Any]:
    return {
        "title": f"{name_title} | LinkedIn",
        "link": f"https://www.linkedin.com/in/{url_slug}",
        "snippet": snippet,
    }


# Mock Serper pages: wrong profile typically ranked first; correct profile lower when known.
MOCK_RESULTS: dict[tuple[str, str], list[dict[str, Any]]] = {
    ("Riegina Stephens", "Citigroup"): [
        linkedin_item("Daniel Stephens - Citigroup", "daniel-stephens-0014", "Experience: Citigroup"),
        linkedin_item("Riegina Stephens - Citigroup", "riegina5t3ph3n5", "Citigroup · Atlanta"),
        linkedin_item("Stephens Riegina", "riegina-stephens-bank", "Banking professional"),
    ],
    ("Puneet Chandra", "PRA Group / PRAA"): [
        linkedin_item(
            "Sanchita Mahapatra, CSM® - PRA Group",
            "sanchita-mahapatra-csm%C2%AE-70109940",
            "PRA Group",
        ),
        linkedin_item("Puneet Chandra - PRA Group", "puneetc", "PRA Group / PRAA"),
    ],
    ("Michael Marcus", "Mission Lane"): [
        linkedin_item("Christine Cassimus - Mission Lane", "christine-cassimus-82727093", "Mission Lane"),
        linkedin_item("Michael Marcus - Mission Lane", "michael-marcus-abc123", "Mission Lane"),
    ],
    ("David Givens", "ADP, Inc. (FKA ADP, LLC)"): [
        linkedin_item("Louis Slagle - ADP", "louis-slagle-02aa5719", "ADP, Inc."),
        linkedin_item("David Givens - ADP", "davidtoddgivens", "ADP"),
    ],
    ("Rebecca McLennan", "JP Morgan Chase"): [
        linkedin_item("Rebecca V Miller - JPMorgan Chase", "rebecca-v-miller", "JP Morgan Chase"),
        linkedin_item("Rebecca McLennan - JPMorgan Chase", "rebecca-mclennan-jpm", "JP Morgan Chase"),
    ],
    ("Dinaker Yanamandala", "Aptos Retail"): [
        linkedin_item("Alex Pineda - Aptos Retail", "alex-pineda-a105109", "Aptos Retail"),
        linkedin_item("Dinaker Yanamandala - Aptos Retail", "dinaker-yanamandala", "Aptos Retail"),
    ],
    ("Elizabeth Billups", "Home depot"): [
        linkedin_item("Tovin Lewis - The Home Depot", "tovin-lewis-114b5487", "Home Depot"),
        linkedin_item("Elizabeth Billups - The Home Depot", "elizabeth-billups-hd", "Home Depot"),
    ],
    # Extra samples from CSV analysis (wrong URL only in original output)
    ("Sharon Knitter", "Ezcorp"): [
        linkedin_item("Geen Martindell - EZCORP", "geen-martindell", "EZCORP"),
        linkedin_item("Sharon Knitter - EZCORP", "sharon-knitter-ezcorp", "Ezcorp"),
    ],
    ("Stephanie Kennedy", "Nymbus"): [
        linkedin_item("Rebecca Sturges - Nymbus", "rebecca-sturges-729609271", "Nymbus"),
        linkedin_item("Stephanie Kennedy - Nymbus", "stephanie-kennedy-nymbus", "Nymbus"),
    ],
    ("Vipul Gaddamedi", "Voya Financial"): [
        linkedin_item("Abhinay P - Voya Financial", "abhinay-p-a7a810237", "Voya Financial"),
        linkedin_item("Vipul Gaddamedi - Voya Financial", "vipul-gaddamedi", "Voya Financial"),
    ],
}


def mock_search_serper(query: str, api_key: str, **kwargs: Any) -> list[dict[str, Any]]:
    del api_key, kwargs
    quoted = re.search(r'"([^"]+)"', query)
    if not quoted:
        return []
    person = quoted.group(1)
    for (p, c), items in MOCK_RESULTS.items():
        if p == person:
            return items
    return []


def load_xlsx_cases() -> list[dict]:
    xlsx = Path("/Users/manikaransingh/Downloads/Atlantic ( Error).xlsx")
    df = pd.read_excel(xlsx)
    rows = []
    for _, row in df.iterrows():
        rows.append(
            {
                "person": str(row["Person"]).strip(),
                "company": str(row["Company "]).strip(),
                "old_wrong": slug(str(row["Linkedin Url Found ( tool)"])),
                "correct": slug(str(row.get("Manually Found ", ""))),
            }
        )
    return rows


@patch("serper_search.search_serper", side_effect=mock_search_serper)
def run_replay(_mock: Any) -> None:
    cases = load_xlsx_cases()
    extra = [
        ("Sharon Knitter", "Ezcorp", "geen-martindell", "sharon-knitter-ezcorp"),
        ("Stephanie Kennedy", "Nymbus", "rebecca-sturges-729609271", "stephanie-kennedy-nymbus"),
        ("Vipul Gaddamedi", "Voya Financial", "abhinay-p-a7a810237", "vipul-gaddamedi"),
    ]
    all_cases = cases + [
        {"person": p, "company": c, "old_wrong": old, "correct": correct} for p, c, old, correct in extra
    ]

    print(f"Offline replay on {len(all_cases)} incorrect cases\n")
    print(f"{'Person':<22} {'Old wrong slug':<30} {'New slug':<30} {'Score':>5}  {'Status':<20} Result")
    print("-" * 130)

    fixed = rejected = still_wrong = 0
    for case in all_cases:
        person = case["person"]
        company = case["company"]
        match = find_linkedin_person_match(person, company, "fake-key", num=10, date_restrict=None)
        new_slug = slug(match.url or "")
        old_slug = case["old_wrong"]
        correct_slug = case["correct"]

        if correct_slug and new_slug == correct_slug:
            result = "FIXED"
            fixed += 1
        elif not new_slug and old_slug:
            result = "REJECTED_WRONG"
            rejected += 1
        elif new_slug == old_slug:
            result = "STILL_WRONG"
            still_wrong += 1
        elif new_slug:
            result = f"PICKED:{new_slug}"
            if correct_slug and new_slug != correct_slug:
                still_wrong += 1
            else:
                fixed += 1
        else:
            result = "NO_MATCH"
            rejected += 1

        print(
            f"{person[:21]:<22} {old_slug[:29]:<30} {new_slug[:29] or '(none)':<30} "
            f"{match.score:>5}  {match.status:<20} {result}"
        )
        if correct_slug:
            print(f"{'':22} expected: {correct_slug}")

    print("-" * 130)
    print(f"Summary: fixed={fixed}, rejected_wrong={rejected}, still_wrong={still_wrong}")


class ScoringUnitTests(unittest.TestCase):
    @patch("serper_search.search_serper", side_effect=mock_search_serper)
    def test_rejects_coworker_with_same_last_name(self, _mock: Any) -> None:
        url = find_linkedin_person_url("Riegina Stephens", "Citigroup", "fake")
        self.assertNotIn("daniel-stephens", url or "")
        self.assertTrue(url and "riegina" in url.casefold())

    @patch("serper_search.search_serper", side_effect=mock_search_serper)
    def test_rejects_unrelated_name(self, _mock: Any) -> None:
        url = find_linkedin_person_url("Puneet Chandra", "PRA Group / PRAA", "fake")
        self.assertEqual(url, "https://www.linkedin.com/in/puneetc")

    @patch("serper_search.search_serper", side_effect=mock_search_serper)
    def test_rejects_adp_coworker(self, _mock: Any) -> None:
        url = find_linkedin_person_url("David Givens", "ADP, Inc. (FKA ADP, LLC)", "fake")
        self.assertEqual(url, "https://www.linkedin.com/in/davidtoddgivens")


if __name__ == "__main__":
    if "--unittest" in sys.argv:
        unittest.main(argv=[sys.argv[0]])
    else:
        run_replay()
