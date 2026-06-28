#!/usr/bin/env python3
"""Push consolidated Seeqe-ready CSV rows to the granite callback."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from seeqe_email_callback import post_email_to_seeqe
from scripts.seeqe_row_mapper import csv_row_to_seeqe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames and "linkedin_url" in reader.fieldnames:
            return [
                {
                    "linkedin_url": (r.get("linkedin_url") or "").strip(),
                    "work_email": (r.get("work_email") or "").strip(),
                    "email_status": (r.get("email_status") or "").strip(),
                    "created_at": (r.get("created_at") or "").strip(),
                }
                for r in reader
                if (r.get("work_email") or "").strip() and (r.get("linkedin_url") or "").strip()
            ]
        return [m for r in reader if (m := csv_row_to_seeqe(r))]


def main() -> int:
    parser = argparse.ArgumentParser(description="Push CSV emails to Seeqe DB")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--limit", type=int, default=0, help="Max rows to process (0 = all)")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N eligible rows")
    args = parser.parse_args()

    logging.getLogger("seeqe_email_callback").setLevel(logging.WARNING)

    mapped = load_rows(args.csv_path)
    if args.offset:
        mapped = mapped[args.offset :]
    if args.limit:
        mapped = mapped[: args.limit]

    total = len(mapped)
    ok = failed = 0
    logger.info("Pushing %s rows from %s", total, args.csv_path)

    for i, row in enumerate(mapped, 1):
        if post_email_to_seeqe(row):
            ok += 1
        else:
            failed += 1
        if i % 100 == 0 or i == total:
            logger.info("Progress %s/%s — posted: %s, failed: %s", i, total, ok, failed)

    logger.info("Done — posted: %s, failed: %s", ok, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
