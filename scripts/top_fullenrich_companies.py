#!/usr/bin/env python3
"""Rank companies by number of FullEnrich-enriched stakeholders."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.seeqe_row_mapper import (  # noqa: E402
    COMPANY_COLUMNS,
    csv_row_to_seeqe,
    linkedin_key,
    normalize_linkedin_url,
    status_rank,
)

COMPANY_URL_COLUMNS = (
    "Company LinkedIn URL",
    "Company LinkedIn Url",
    "Company Linkedin URL",
    "Company Linkedin Url",
)


def _first(row: dict, cols: tuple[str, ...]) -> str:
    for c in cols:
        v = (row.get(c) or "").strip()
        if v:
            return v
    return ""


def _norm_company(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s&]", " ", s)
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|plc|the|group)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def find_fullenrich_csvs(folder: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in ("FullEnrich*.csv", "fullenrich*.csv"):
        files.extend(folder.glob(pattern))
    # Skip derived/comparison helpers
    skip_substrings = (
        "no-email-found",
        "not-enriched",
        "rerun-still-failed",
        "email-mismatches",
        "forager-fullenrich",
        "consolidated_fullenrich",
    )
    out = []
    for p in sorted({p.resolve() for p in files if p.is_file()}, key=lambda x: x.name.lower()):
        low = p.name.lower()
        if any(s in low for s in skip_substrings):
            continue
        out.append(p)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Top companies by FullEnrich stakeholder count")
    parser.add_argument("--folder", type=Path, default=Path.home() / "Downloads")
    parser.add_argument("--top", type=int, default=100)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "top_100_fullenrich_companies.csv",
    )
    parser.add_argument(
        "--success-only",
        action="store_true",
        default=True,
        help="Only count stakeholders with Row Status Success (default)",
    )
    args = parser.parse_args()

    # unique person per company: company_key -> set(linkedin_keys)
    by_company: dict[str, set[str]] = defaultdict(set)
    display_name: dict[str, str] = {}
    company_li: dict[str, str] = {}
    source_files: dict[str, set[str]] = defaultdict(set)
    bounce_valid_safe: dict[str, int] = Counter()
    total_success_people: set[str] = set()
    files_read = 0
    rows_read = 0

    for path in find_fullenrich_csvs(args.folder):
        files_read += 1
        try:
            with path.open(newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
        except Exception as exc:
            print(f"Skipping {path.name}: {exc}")
            continue
        rows_read += len(rows)
        for row in rows:
            mapped = csv_row_to_seeqe(row, source_file=path.name)
            if not mapped:
                continue
            if args.success_only and (mapped.get("email_status") or "").strip() != "Success":
                continue
            li_key = linkedin_key(mapped["linkedin_url"])
            if not li_key:
                continue
            total_success_people.add(li_key)

            company = mapped.get("company_name") or _first(row, COMPANY_COLUMNS)
            if not company:
                continue
            key = _norm_company(company)
            if not key:
                continue
            by_company[key].add(li_key)
            # Prefer longer / title-cased display name
            prev = display_name.get(key, "")
            if len(company) > len(prev):
                display_name[key] = company
            cli = normalize_linkedin_url(_first(row, COMPANY_URL_COLUMNS))
            if cli and key not in company_li:
                company_li[key] = cli
            source_files[key].add(path.name)
            bounce = (mapped.get("bounce_status") or "").strip().lower()
            if bounce in {"valid & safe to send email", "valid and safe to send email"}:
                bounce_valid_safe[key] += 1

    ranked = sorted(by_company.items(), key=lambda kv: (-len(kv[1]), display_name.get(kv[0], kv[0])))
    top = ranked[: args.top]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "rank",
                "company_name",
                "enriched_stakeholders",
                "valid_safe_email_rows",
                "company_linkedin_url",
                "source_files",
            ],
        )
        writer.writeheader()
        for i, (key, people) in enumerate(top, start=1):
            writer.writerow(
                {
                    "rank": i,
                    "company_name": display_name.get(key, key),
                    "enriched_stakeholders": len(people),
                    "valid_safe_email_rows": bounce_valid_safe.get(key, 0),
                    "company_linkedin_url": company_li.get(key, ""),
                    "source_files": "; ".join(sorted(source_files[key])),
                }
            )

    print(
        f"Read {rows_read} rows from {files_read} FullEnrich files; "
        f"{len(total_success_people)} unique Success stakeholders; "
        f"{len(by_company)} companies with names -> wrote top {len(top)} to {args.output}"
    )
    print("\nTop 20:")
    for i, (key, people) in enumerate(top[:20], start=1):
        print(f"  {i:3d}. {display_name.get(key, key):40s}  {len(people):4d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
