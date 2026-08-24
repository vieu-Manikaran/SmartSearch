#!/usr/bin/env python3
"""Resolve URN LinkedIn URLs to vanity slugs via Serper (name + company)."""

from __future__ import annotations

import csv
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings
from person_linkedin_finder import _serper_loose_match, find_person_linkedin
from rapidapi_person_deep import (
    _is_member_urn_slug,
    _slug_from_profile_value,
    normalize_linkedin_profile_url,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

INPUT_PATH = Path("/Users/manikaransingh/Downloads/Fixed urls 2(in).csv")
OUTPUT_PATH = Path("/Users/manikaransingh/Downloads/Fixed urls 2(in)_resolved.csv")
CHECKPOINT_PATH = ROOT / "data" / "person_linkedin" / "checkpoints" / "fixed_urls_2_in.json"
WORKERS = 4
CHECKPOINT_EVERY = 25


def vanity_url(raw: str) -> str:
    normalized = normalize_linkedin_profile_url(raw or "")
    slug = _slug_from_profile_value(normalized)
    if not slug or _is_member_urn_slug(slug):
        return ""
    return f"https://www.linkedin.com/in/{slug}/"


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    raw = path.read_bytes()
    text = raw.decode("latin-1")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise SystemExit(f"No header in {path}")
    fields = list(reader.fieldnames)
    rows = [{k: (row.get(k) or "") for k in fields} for row in reader]
    return fields, rows


def lookup(person: str, company: str) -> dict[str, str]:
    match = find_person_linkedin(person, company, use_rapidapi_fallback=False)
    resolved = vanity_url(match.url or "")
    if match.status == "invalid_input":
        return {
            "LinkedIn_URL_Resolved": "",
            "Resolve_Status": "invalid_input",
            "Source": match.source,
            "Match_Score": str(match.score or ""),
        }
    if match.status != "found" or not resolved:
        loose = _serper_loose_match(person, company, settings.serper_api_key or "")
        loose_url = vanity_url(loose.url or "")
        if loose_url and (
            match.status != "found"
            or loose.status == "found"
            or (loose.score or 0) > (match.score or 0)
        ):
            match = loose
            resolved = loose_url
    status = match.status or "no_profile_in_top_10"
    if match.url and not resolved:
        status = "still_urn"
    elif resolved and status not in {"found", "low_confidence"}:
        status = "found"
    return {
        "LinkedIn_URL_Resolved": resolved,
        "Resolve_Status": status,
        "Source": match.source,
        "Match_Score": str(match.score or ""),
    }


def main() -> None:
    fields, rows = read_rows(INPUT_PATH)
    total = len(rows)
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    done: dict[int, dict[str, str]] = {}
    if CHECKPOINT_PATH.exists():
        saved = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        done = {int(k): v for k, v in saved.get("done", {}).items()}
        logger.info("Resuming with %s cached rows", len(done))

    pending = [i for i in range(total) if i not in done]
    logger.info("Rows=%s pending=%s workers=%s -> %s", total, len(pending), WORKERS, OUTPUT_PATH)

    started = time.time()

    def _one(idx: int) -> tuple[int, dict[str, str]]:
        row = rows[idx]
        person = (row.get("Person name ") or row.get("Person name") or "").strip()
        company = (row.get("Company name") or "").strip()
        result = lookup(person, company)
        return idx, result

    processed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_one, i): i for i in pending}
        for future in as_completed(futures):
            idx, result = future.result()
            done[idx] = result
            processed += 1
            if processed % 10 == 0 or processed == len(pending):
                found = sum(1 for v in done.values() if v.get("LinkedIn_URL_Resolved"))
                logger.info(
                    "%s/%s this run (%s/%s total) found=%s elapsed=%.0fs",
                    processed,
                    len(pending),
                    len(done),
                    total,
                    found,
                    time.time() - started,
                )
            if processed % CHECKPOINT_EVERY == 0:
                CHECKPOINT_PATH.write_text(
                    json.dumps({"done": {str(k): v for k, v in done.items()}}, indent=2),
                    encoding="utf-8",
                )

    extra = ["LinkedIn_URL_Resolved", "Resolve_Status", "Source", "Match_Score"]
    out_fields = fields + [c for c in extra if c not in fields]
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        for i, row in enumerate(rows):
            out = dict(row)
            out.update(done.get(i, {}))
            writer.writerow(out)

    CHECKPOINT_PATH.write_text(
        json.dumps({"done": {str(k): v for k, v in done.items()}}, indent=2),
        encoding="utf-8",
    )
    found = sum(1 for v in done.values() if v.get("LinkedIn_URL_Resolved"))
    logger.info("Wrote %s (%s/%s resolved) in %.0fs", OUTPUT_PATH, found, total, time.time() - started)


if __name__ == "__main__":
    main()
