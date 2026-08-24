#!/usr/bin/env python3
"""Resolve every URN in the Fixed urls CSV via RapidAPI person_deep (no Serper)."""

from __future__ import annotations

import csv
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings
from rapidapi_person_deep import resolve_vanity_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

INPUT_PATH = Path("/Users/manikaransingh/Downloads/Fixed urls 2(in).csv")
OUTPUT_PATH = Path("/Users/manikaransingh/Downloads/Fixed urls 2(in)_resolved.csv")
CHECKPOINT_PATH = ROOT / "data" / "person_linkedin" / "checkpoints" / "fixed_urls_2_in_rapidapi.json"
CHECKPOINT_EVERY = 10


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    text = path.read_bytes().decode("latin-1")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise SystemExit(f"No header in {path}")
    fields = list(reader.fieldnames)
    rows = [{k: (row.get(k) or "") for k in fields} for row in reader]
    return fields, rows


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]], done: dict[int, dict[str, str]]) -> None:
    extra = ["LinkedIn_URL_Resolved", "Resolve_Status", "Source", "Match_Score"]
    out_fields = fields + [c for c in extra if c not in fields]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        for i, row in enumerate(rows):
            out = dict(row)
            out.update(done.get(i, {}))
            writer.writerow(out)


def main() -> None:
    key = (settings.rapidapi_key or "").strip()
    if not key:
        raise SystemExit("RAPIDAPI_KEY missing in .env")

    fields, rows = read_rows(INPUT_PATH)
    total = len(rows)
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    done: dict[int, dict[str, str]] = {}
    if CHECKPOINT_PATH.exists():
        saved = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        done = {int(k): v for k, v in saved.get("done", {}).items()}
        logger.info("Resuming with %s cached RapidAPI rows", len(done))

    pending = [i for i in range(total) if i not in done]
    logger.info("Rows=%s pending=%s -> %s", total, len(pending), OUTPUT_PATH)
    started = time.time()

    def persist() -> None:
        CHECKPOINT_PATH.write_text(
            json.dumps({"done": {str(k): v for k, v in done.items()}}, indent=2),
            encoding="utf-8",
        )
        write_csv(OUTPUT_PATH, fields, rows, done)

    for n, idx in enumerate(pending, start=1):
        row = rows[idx]
        urn = (row.get("LinkedIn_URL") or "").strip()
        person = (row.get("Person name ") or row.get("Person name") or "").strip()
        t0 = time.time()
        resolved = resolve_vanity_url(urn, api_key=key)
        result = {
            "LinkedIn_URL_Resolved": resolved.get("linkedin_url_resolved") or "",
            "Resolve_Status": resolved.get("status") or "resolve_failed",
            "Source": "rapidapi_person_deep",
            "Match_Score": "",
        }
        done[idx] = result
        url = result["LinkedIn_URL_Resolved"]
        status = result["Resolve_Status"]
        logger.info(
            "[%s/%s] %.1fs %s %s %s",
            n,
            len(pending),
            time.time() - t0,
            status,
            person,
            url or urn[:48],
        )
        if n % CHECKPOINT_EVERY == 0 or n == len(pending):
            persist()
            found = sum(1 for v in done.values() if v.get("LinkedIn_URL_Resolved"))
            logger.info(
                "checkpoint %s/%s found=%s elapsed=%.0fs",
                len(done),
                total,
                found,
                time.time() - started,
            )

    persist()
    found = sum(1 for v in done.values() if v.get("LinkedIn_URL_Resolved"))
    logger.info("Wrote %s (%s/%s resolved) in %.0fs", OUTPUT_PATH, found, total, time.time() - started)


if __name__ == "__main__":
    main()
