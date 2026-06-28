#!/usr/bin/env python3
"""Consolidate emails from all FullEnrich* CSVs in a folder."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.seeqe_row_mapper import csv_row_to_seeqe, linkedin_key, row_priority

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

OUTPUT_FIELDS = [
    "linkedin_url",
    "work_email",
    "email_status",
    "created_at",
    "source_file",
]


def find_fullenrich_csvs(folder: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in ("FullEnrich*.csv", "fullenrich*.csv"):
        files.extend(folder.glob(pattern))
    return sorted({p.resolve() for p in files if p.is_file()}, key=lambda p: p.name.lower())


def consolidate(folder: Path) -> tuple[list[dict[str, str]], dict[str, int]]:
    by_linkedin: dict[str, dict[str, str]] = {}
    stats = {"files": 0, "rows_read": 0, "eligible": 0, "duplicates": 0}

    for path in find_fullenrich_csvs(folder):
        stats["files"] += 1
        try:
            with path.open(newline="", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
        except Exception as exc:
            logger.warning("Skipping %s: %s", path.name, exc)
            continue

        stats["rows_read"] += len(rows)
        for row in rows:
            mapped = csv_row_to_seeqe(row, source_file=path.name)
            if not mapped:
                continue
            stats["eligible"] += 1
            key = linkedin_key(mapped["linkedin_url"])
            if not key:
                continue
            existing = by_linkedin.get(key)
            if existing is None:
                by_linkedin[key] = mapped
            else:
                stats["duplicates"] += 1
                if row_priority(mapped) > row_priority(existing):
                    by_linkedin[key] = mapped

    consolidated = sorted(by_linkedin.values(), key=lambda r: (r["linkedin_url"], r["work_email"]))
    return consolidated, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Consolidate FullEnrich CSV emails")
    parser.add_argument(
        "--folder",
        type=Path,
        default=Path.home() / "Downloads",
        help="Folder containing FullEnrich*.csv files",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "consolidated_fullenrich_seeqe.csv",
    )
    args = parser.parse_args()

    rows, stats = consolidate(args.folder)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    logger.info(
        "Consolidated %s unique emails from %s files (%s rows read, %s eligible, %s duplicates dropped) -> %s",
        len(rows),
        stats["files"],
        stats["rows_read"],
        stats["eligible"],
        stats["duplicates"],
        args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
