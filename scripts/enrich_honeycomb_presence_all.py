#!/usr/bin/env python3
"""Enrich all Honeycomb EBM rows: current title at Account_AccountName + still-works flag via person_deep."""

from __future__ import annotations

import csv
import json
import logging
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_rate_lock = threading.Lock()
_last_request_by_key: dict[str, float] = {}
_checkpoint_lock = threading.Lock()
_csv_lock = threading.Lock()
_rows_ref: list[dict[str, str]] | None = None
_fieldnames_ref: list[str] | None = None
_checkpoint_ref: dict[str, dict[str, str]] | None = None
_write_counter = 0

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rapidapi_person_deep import collect_rapidapi_keys, fetch_person_deep, normalize_linkedin_profile_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

INPUT_CSV = Path("/Users/manikaransingh/Downloads/Honeycomb_EBM(Matched Account People).csv")
OUTPUT_CSV = ROOT / "data" / "Honeycomb_EBM(Matched Account People)_enriched.csv"
DOWNLOADS_COPY = Path("/Users/manikaransingh/Downloads/Honeycomb_EBM(Matched Account People)_enriched.csv")
CHECKPOINT = ROOT / "data" / "honeycomb_presence_checkpoint.json"
JSON_DIR = ROOT / "data" / "honeycomb_person_deep_json"
REQUEST_GAP_SEC = 0.8  # per-key gap; two keys run in parallel
WORKERS = 2

TITLE_COL = "Perosn Current Job title"
STILL_AT_COL = "Still_Works_At_Account_AccountName"
PRESENT_TITLES_COL = "Present_Titles_At_Account_AccountName"
STATUS_COL = "Presence_Lookup_Status"

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
    overlap = a_tokens & b_tokens
    return bool(overlap) and (overlap == a_tokens or overlap == b_tokens or len(overlap) >= 2)


def extract_company_from_subtitle(subtitle: str) -> str:
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
) -> tuple[str, str]:
    experiences = data.get("experiences") if isinstance(data.get("experiences"), list) else []
    roles = iter_roles(experiences)
    present_roles = [r for r in roles if r["is_current"] == "yes"]
    matched_present = [r for r in present_roles if role_matches_account(r, account_name, account_company_url)]

    titles: list[str] = []
    seen: set[str] = set()
    for r in matched_present:
        t = r["title"]
        key = t.casefold()
        if t and key not in seen:
            seen.add(key)
            titles.append(t)

    still = "Yes" if titles else "No"
    return still, " | ".join(titles)


def load_checkpoint() -> dict[str, dict[str, str]]:
    if not CHECKPOINT.exists():
        return {}
    try:
        return json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_checkpoint(state: dict[str, dict[str, str]]) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(json.dumps(state, indent=2), encoding="utf-8")


def write_output(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def row_key(row: dict[str, str], idx: int) -> str:
    pid = (row.get("Person_Id") or "").strip()
    if pid:
        return f"id:{pid}"
    email = (row.get("Person_Email") or "").strip().casefold()
    if email:
        return f"email:{email}"
    return f"idx:{idx}"


def _throttle(api_key: str) -> None:
    """Pace requests per RapidAPI key so both keys can run in parallel safely."""
    with _rate_lock:
        now = time.time()
        last = _last_request_by_key.get(api_key, 0.0)
        wait = REQUEST_GAP_SEC - (now - last)
        if wait > 0:
            time.sleep(wait)
        _last_request_by_key[api_key] = time.time()


def _slug_for_json(link: str, row_key_value: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", link or "", re.I)
    slug = (m.group(1) if m else row_key_value).strip("/")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", slug)[:120]
    return slug or "unknown"


def save_person_deep_json(link: str, row_key_value: str, payload: dict[str, Any]) -> Path:
    JSON_DIR.mkdir(parents=True, exist_ok=True)
    path = JSON_DIR / f"{_slug_for_json(link, row_key_value)}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def persist_progress(force: bool = False) -> None:
    """Flush checkpoint + CSV frequently so crash/rate-limit does not lose work."""
    global _write_counter
    if _rows_ref is None or _fieldnames_ref is None or _checkpoint_ref is None:
        return
    with _csv_lock:
        _write_counter += 1
        if not force and _write_counter % 5 != 0:
            # Still always save checkpoint; CSV every 5 completions.
            save_checkpoint(_checkpoint_ref)
            return
        save_checkpoint(_checkpoint_ref)
        write_output(OUTPUT_CSV, _fieldnames_ref, _rows_ref)
        try:
            shutil.copyfile(OUTPUT_CSV, DOWNLOADS_COPY)
        except OSError as exc:
            logger.warning("Could not copy to Downloads: %s", exc)


def enrich_one(
    idx: int,
    row: dict[str, str],
    api_key: str,
) -> tuple[int, str, dict[str, str]]:
    link = normalize_linkedin_profile_url(row.get("Person Linkeidn") or "").rstrip("/")
    key = row_key(row, idx)
    if not link:
        return idx, key, {
            STILL_AT_COL: "",
            TITLE_COL: "",
            PRESENT_TITLES_COL: "",
            STATUS_COL: "no_linkedin",
        }

    _throttle(api_key)
    result = fetch_person_deep(link, api_key)

    # Persist raw API payload immediately (success or structured failure body).
    save_person_deep_json(
        link,
        key,
        {
            "linkedin_url": link,
            "row_key": key,
            "person": row.get("Person_FullName") or "",
            "account": row.get("Account_AccountName") or "",
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "result": result,
        },
    )

    if not result.get("success"):
        err = str(result.get("error") or "fetch_failed")
        return idx, key, {
            STILL_AT_COL: "",
            TITLE_COL: "",
            PRESENT_TITLES_COL: "",
            STATUS_COL: err,
        }

    still, titles = presence_at_account(
        result["data"],
        row.get("Account_AccountName") or "",
        row.get("Account_Company_LinkedIn_url__c") or "",
    )
    return idx, key, {
        STILL_AT_COL: still,
        TITLE_COL: titles if still == "Yes" else "",
        PRESENT_TITLES_COL: titles if still == "Yes" else "",
        STATUS_COL: "ok",
    }


def main() -> int:
    keys = collect_rapidapi_keys()
    if not keys:
        raise SystemExit("RAPIDAPI_KEY missing")

    with INPUT_CSV.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    for col in (TITLE_COL, STILL_AT_COL, PRESENT_TITLES_COL, STATUS_COL):
        if col not in fieldnames:
            fieldnames.append(col)

    checkpoint = load_checkpoint()
    todo: list[int] = []
    for idx, row in enumerate(rows):
        key = row_key(row, idx)
        link = (row.get("Person Linkeidn") or "").strip()
        if not link:
            row[STILL_AT_COL] = ""
            row[TITLE_COL] = row.get(TITLE_COL) or ""
            row[PRESENT_TITLES_COL] = ""
            row[STATUS_COL] = "no_linkedin"
            continue
        if key in checkpoint and checkpoint[key].get(STATUS_COL) == "ok":
            cached = checkpoint[key]
            row[STILL_AT_COL] = cached.get(STILL_AT_COL, "")
            row[TITLE_COL] = cached.get(TITLE_COL, "")
            row[PRESENT_TITLES_COL] = cached.get(PRESENT_TITLES_COL, "")
            row[STATUS_COL] = "ok"
            continue
        todo.append(idx)

    logger.info(
        "Rows=%s with LinkedIn=%s already cached=%s todo=%s workers=%s keys=%s -> %s",
        len(rows),
        sum(1 for r in rows if (r.get("Person Linkeidn") or "").strip()),
        sum(1 for r in rows if r.get(STATUS_COL) == "ok"),
        len(todo),
        WORKERS,
        len(keys),
        OUTPUT_CSV,
    )

    write_output(OUTPUT_CSV, fieldnames, rows)
    if not todo:
        logger.info("Nothing to do; output already complete.")
        return 0

    global _rows_ref, _fieldnames_ref, _checkpoint_ref
    _rows_ref = rows
    _fieldnames_ref = fieldnames
    _checkpoint_ref = checkpoint

    # One dedicated RapidAPI key per worker → both keys in parallel.
    workers = min(WORKERS, len(keys), max(1, len(todo)))
    done = 0
    yes_ct = sum(1 for r in rows if r.get(STILL_AT_COL) == "Yes")
    fail_ct = 0
    chunk_size = 40

    for start in range(0, len(todo), chunk_size):
        chunk = todo[start : start + chunk_size]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(enrich_one, idx, rows[idx], keys[i % len(keys)])
                for i, idx in enumerate(chunk)
            ]
            for fut in as_completed(futures):
                idx, key, fields = fut.result()
                rows[idx].update(fields)
                done += 1
                if fields.get(STILL_AT_COL) == "Yes":
                    yes_ct += 1
                if fields.get(STATUS_COL) != "ok":
                    fail_ct += 1
                    checkpoint.pop(key, None)
                else:
                    with _checkpoint_lock:
                        checkpoint[key] = fields
                persist_progress(force=(done % 10 == 0))

        persist_progress(force=True)
        logger.info(
            "Progress %s/%s (chunk end %s/%s) yes=%s fail_this_pass=%s json_dir=%s",
            done,
            len(todo),
            min(start + chunk_size, len(todo)),
            len(todo),
            yes_ct,
            fail_ct,
            JSON_DIR,
        )
        time.sleep(1.0)

    # Final counts
    with_li = sum(1 for r in rows if (r.get("Person Linkeidn") or "").strip())
    ok = sum(1 for r in rows if r.get(STATUS_COL) == "ok")
    yes = sum(1 for r in rows if r.get(STILL_AT_COL) == "Yes")
    no = sum(1 for r in rows if r.get(STILL_AT_COL) == "No")
    failed = sum(
        1
        for r in rows
        if (r.get("Person Linkeidn") or "").strip() and r.get(STATUS_COL) not in {"ok", "no_linkedin"}
    )
    try:
        shutil.copyfile(OUTPUT_CSV, DOWNLOADS_COPY)
        shutil.copyfile(OUTPUT_CSV, INPUT_CSV)
    except OSError as exc:
        logger.warning("Final Downloads/input copy failed: %s", exc)
    logger.info(
        "Finished -> %s | linkedin=%s ok=%s yes=%s no=%s failed=%s",
        OUTPUT_CSV,
        with_li,
        ok,
        yes,
        no,
        failed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
