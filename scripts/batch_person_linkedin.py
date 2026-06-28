#!/usr/bin/env python3
"""Batch person LinkedIn lookup: Serper strict + RapidAPI fallback."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings
from person_linkedin_finder import find_person_linkedin

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

OUTPUT_DIR = ROOT / "data" / "person_linkedin"
CHECKPOINT_DIR = OUTPUT_DIR / "checkpoints"
FIELDNAMES = ["Person", "Company", "LinkedIn_URL", "Search_Query", "Status", "Match_Score", "Source"]


def load_pairs(path: Path) -> list[tuple[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"No header row in {path}")
        fields = {h.strip().lower(): h for h in reader.fieldnames}
        person_key = fields.get("person") or fields.get("person name") or fields.get("name")
        company_key = fields.get("company") or fields.get("company name") or fields.get("account")
        if not person_key or not company_key:
            raise ValueError(f"{path} must have Person and Company columns")
        pairs: list[tuple[str, str]] = []
        for row in reader:
            person = (row.get(person_key) or "").strip()
            company = (row.get(company_key) or "").strip()
            if person and company:
                pairs.append((person, company))
        return pairs


def checkpoint_path(batch_name: str) -> Path:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    return CHECKPOINT_DIR / f"{batch_name}.json"


def load_checkpoint(batch_name: str) -> dict:
    path = checkpoint_path(batch_name)
    if not path.exists():
        return {"next_index": 0, "rows": [], "output_path": ""}
    return json.loads(path.read_text(encoding="utf-8"))


def save_checkpoint(batch_name: str, state: dict) -> None:
    checkpoint_path(batch_name).write_text(json.dumps(state, indent=2), encoding="utf-8")


def write_output_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})


def _lookup_row(idx: int, person: str, company: str, api_key: str) -> tuple[int, dict[str, str]]:
    match = find_person_linkedin(person, company, serper_api_key=api_key)
    return idx, {
        "Person": person,
        "Company": company,
        "LinkedIn_URL": match.url or "",
        "Search_Query": match.search_query,
        "Status": match.status,
        "Match_Score": str(match.score),
        "Source": match.source,
    }


def _log_progress(batch_name: str, done: int, total: int, rows: list[dict[str, str]]) -> None:
    logger.info(
        "[%s] %s/%s done — found=%s low_conf=%s no_match=%s invalid=%s rapidapi=%s",
        batch_name,
        done,
        total,
        sum(1 for r in rows if r.get("Status") == "found"),
        sum(1 for r in rows if r.get("Status") == "low_confidence"),
        sum(1 for r in rows if r.get("Status") == "no_profile_in_top_10"),
        sum(1 for r in rows if r.get("Status") == "invalid_input"),
        sum(1 for r in rows if r.get("Source") == "rapidapi"),
    )


def process_batch(
    input_path: Path,
    batch_name: str,
    *,
    workers: int = 2,
    checkpoint_every: int = 50,
    resume: bool = True,
) -> Path:
    api_key = settings.serper_api_key or ""
    if not api_key or api_key.strip() in {".", "..", "..."}:
        raise SystemExit("SERPER_API_KEY is missing or still a placeholder in .env")
    if not (settings.rapidapi_key or settings.rapidapi_key2):
        raise SystemExit("RAPIDAPI_KEY is missing in .env (required for fallback lookups)")

    pairs = load_pairs(input_path)
    state = load_checkpoint(batch_name) if resume else {"next_index": 0, "rows": [], "output_path": ""}
    rows: list[dict[str, str]] = state.get("rows", [])
    start_index = int(state.get("next_index", 0))

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path(state["output_path"]) if state.get("output_path") else OUTPUT_DIR / f"{batch_name}_{timestamp}.csv"
    total = len(pairs)
    workers = max(1, workers)

    logger.info(
        "Batch %s: %s rows, workers=%s, starting at %s/%s -> %s",
        batch_name,
        total,
        workers,
        start_index,
        total,
        output_path,
    )

    idx = start_index
    while idx < total:
        chunk_end = min(idx + checkpoint_every, total)
        chunk_results: list[tuple[int, dict[str, str]]] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_lookup_row, i, pairs[i][0], pairs[i][1], api_key)
                for i in range(idx, chunk_end)
            ]
            for future in as_completed(futures):
                chunk_results.append(future.result())

        chunk_results.sort(key=lambda item: item[0])
        rows.extend(row for _, row in chunk_results)
        idx = chunk_end

        save_checkpoint(batch_name, {"next_index": idx, "rows": rows, "output_path": str(output_path)})
        write_output_csv(output_path, rows)
        if idx % 100 == 0 or idx == total:
            _log_progress(batch_name, idx, total, rows)

    save_checkpoint(batch_name, {"next_index": total, "rows": rows, "output_path": str(output_path), "complete": True})
    logger.info(
        "Finished %s -> %s (%s rows, %s with URLs)",
        batch_name,
        output_path,
        len(rows),
        sum(1 for r in rows if r.get("LinkedIn_URL")),
    )
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch scored person LinkedIn lookup")
    parser.add_argument("csv_paths", nargs="*", type=Path, help="Input CSV files (Person, Company)")
    parser.add_argument("--austin", type=Path, default=Path("/Users/manikaransingh/Downloads/Book2 2.csv"))
    parser.add_argument("--chicago", type=Path, default=Path("/Users/manikaransingh/Downloads/chicago  1.csv"))
    parser.add_argument("--atlanta", type=Path, default=Path("/Users/manikaransingh/Downloads/Copy of atlanta cohort .csv"))
    parser.add_argument("--all-three", action="store_true", help="Run Austin, Chicago, and Atlanta default files")
    parser.add_argument("--workers", type=int, default=2, help="Parallel lookup workers (default 2)")
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()

    jobs: list[tuple[Path, str]] = []
    if args.all_three:
        jobs = [
            (args.austin, "austin_fast"),
            (args.chicago, "chicago_fast"),
            (args.atlanta, "atlanta_fast"),
        ]
    elif args.csv_paths:
        for path in args.csv_paths:
            jobs.append((path, path.stem.lower().replace(" ", "_")[:40]))
    else:
        parser.error("Provide csv_paths or --all-three")

    for path, name in jobs:
        if not path.exists():
            logger.error("Missing input file: %s", path)
            return 1
        process_batch(path, name, workers=args.workers, resume=not args.no_resume)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
