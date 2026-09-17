"""Daily HubSpot CRO LinkedIn posts digest (last 24 hours, IST).

Reads the watchlist CSV, calls RapidAPI /profile_updates, writes a posts CSV in
the same columns as posts_last_30_days.csv, and posts it to Slack once.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from rapidapi_person_deep import collect_rapidapi_keys, healthy_rapidapi_keys
from rapidapi_profile_updates import fetch_profile_updates
from vendor_file.slack import post_message, slack_configured, upload_file

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_PEOPLE = Path("data/hubspot_cro_outreach/people_days_since_last_post.csv")
DEFAULT_OUT_DIR = Path("data/hubspot_cro_outreach")

PERSON_FIELDS = (
    "person_name",
    "company",
    "company_industry",
    "job_title",
    "stakeholder_level",
    "authority_score",
    "authority_tier",
    "location",
    "geography",
    "linkedin_url",
)

POST_FIELDS = [
    *PERSON_FIELDS,
    "posted_at",
    "post_link",
    "post_summary",
    "people_mentioned",
    "days_since_last_post",
    "is_repost",
]


def ist_previous_calendar_day(
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return [yesterday 00:00 IST, today 00:00 IST) in aware datetimes."""
    current = now.astimezone(IST) if now else datetime.now(IST)
    end = current.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    return start, end


def parse_posted_at(post: dict[str, Any], *, now: datetime) -> datetime | None:
    raw = str(post.get("postedAt") or "").strip()
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
    ago = str(post.get("postedAgo") or "").strip().lower()
    match = re.search(r"(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|w|mo|mos|yr|yrs|y)\b", ago)
    if not match:
        return None
    n = int(match.group(1))
    unit = match.group(2)
    delta = {
        "s": timedelta(seconds=n),
        "sec": timedelta(seconds=n),
        "secs": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "min": timedelta(minutes=n),
        "mins": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "hr": timedelta(hours=n),
        "hrs": timedelta(hours=n),
        "d": timedelta(days=n),
        "w": timedelta(weeks=n),
        "mo": timedelta(days=30 * n),
        "mos": timedelta(days=30 * n),
        "yr": timedelta(days=365 * n),
        "yrs": timedelta(days=365 * n),
        "y": timedelta(days=365 * n),
    }.get(unit)
    if delta is None:
        return None
    return now - delta


def post_summary(post: dict[str, Any]) -> str:
    text = str(post.get("postText") or "").strip()
    if text:
        return re.sub(r"\s+", " ", text).strip()
    article = post.get("articleComponent") if isinstance(post.get("articleComponent"), dict) else {}
    title = str(article.get("title") or "").strip()
    desc = str(article.get("description") or "").strip()
    bits = [bit for bit in (title, desc) if bit]
    if bits:
        return re.sub(r"\s+", " ", " — ".join(bits)).strip()
    doc = post.get("documentComponent") if isinstance(post.get("documentComponent"), dict) else {}
    return str(doc.get("title") or "").strip()


def person_urls_from_links(links: Any) -> str:
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(links, list):
        return ""
    for raw in links:
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if "linkedin.com/in/" not in value.lower():
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return " | ".join(out)


def collect_posts(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        posts = data.get("posts")
        if isinstance(posts, list):
            return [post for post in posts if isinstance(post, dict)]
    return []


def load_people(csv_path: Path) -> list[dict[str, str]]:
    people: list[dict[str, str]] = []
    seen: set[str] = set()
    with csv_path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        for row in csv.DictReader(handle):
            url = (row.get("linkedin_url") or "").strip()
            if not url or "linkedin.com/in/" not in url.lower():
                continue
            key = url.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            people.append({field: (row.get(field) or "").strip() for field in PERSON_FIELDS})
            people[-1]["linkedin_url"] = url
    return people


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=POST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in POST_FIELDS})


def in_window(posted: datetime | None, start: datetime, end: datetime) -> bool:
    if posted is None:
        return False
    utc_start = start.astimezone(timezone.utc)
    utc_end = end.astimezone(timezone.utc)
    posted_utc = posted.astimezone(timezone.utc)
    return utc_start <= posted_utc < utc_end


def fetch_person_window(
    person: dict[str, str],
    *,
    api_key: str,
    now: datetime,
    start: datetime,
    end: datetime,
    max_pages: int,
) -> list[dict[str, Any]]:
    url = person["linkedin_url"]
    seen: set[str] = set()
    window_posts: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        resp = fetch_profile_updates(url, api_key, page=page, reposts=1, comments=0)
        if not resp.get("success"):
            logger.warning(
                "profile_updates failed for %s page=%s: %s",
                person.get("person_name") or url,
                page,
                resp.get("error"),
            )
            break
        batch = collect_posts(resp.get("data"))
        if not batch:
            break
        oldest: datetime | None = None
        new = 0
        for post in batch:
            urn = str(post.get("urn") or post.get("postLink") or "")
            if urn and urn in seen:
                continue
            if urn:
                seen.add(urn)
            new += 1
            posted = parse_posted_at(post, now=now)
            if posted is not None and (oldest is None or posted < oldest):
                oldest = posted
            if not in_window(posted, start, end):
                continue
            days = max(0, int((now - posted).total_seconds() // 86400)) if posted else ""
            window_posts.append(
                {
                    **person,
                    "posted_at": posted.isoformat() if posted else "",
                    "post_link": post.get("postLink") or "",
                    "post_summary": post_summary(post),
                    "people_mentioned": person_urls_from_links(post.get("linksInPost")),
                    "days_since_last_post": days,
                    "is_repost": "Yes" if post.get("is_repost") else "No",
                }
            )
        if new == 0:
            break
        if oldest is not None and oldest < start.astimezone(timezone.utc):
            break
    return window_posts


def slack_digest(
    path: Path,
    *,
    start: datetime,
    rows: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    day = start.date().isoformat()
    posters = sorted({row.get("person_name") or row.get("linkedin_url") or "" for row in rows})
    posters = [name for name in posters if name]
    if not rows:
        return post_message(
            f"HubSpot CRO LinkedIn posts — {day} IST\n"
            "No stakeholders from the watchlist posted in the last 24 hours."
        )
    names = ", ".join(posters[:20])
    extra = f" (+{len(posters) - 20} more)" if len(posters) > 20 else ""
    comment = (
        f"HubSpot CRO LinkedIn posts — {day} IST\n"
        f"{len(rows)} posts from {len(posters)} people in the last 24 hours.\n"
        f"{names}{extra}"
    )
    return upload_file(
        path,
        initial_comment=comment,
        filename=path.name,
        title=f"HubSpot CRO posts {day} IST",
    )


def run(args: argparse.Namespace) -> int:
    keys = healthy_rapidapi_keys() or collect_rapidapi_keys()
    if not keys:
        logger.error("RAPIDAPI_KEY is not set")
        return 1
    people = load_people(Path(args.people_csv))
    if args.limit:
        people = people[: args.limit]
    if not people:
        logger.error("No people in %s", args.people_csv)
        return 1

    now = datetime.now(timezone.utc)
    start, end = ist_previous_calendar_day(now)
    logger.info(
        "people=%s keys=%s window=%s .. %s IST",
        len(people),
        len(keys),
        start.isoformat(),
        end.isoformat(),
    )

    rows: list[dict[str, Any]] = []
    workers = max(1, min(args.concurrency or len(keys), len(keys), len(people)))

    def worker(idx: int, person: dict[str, str]) -> list[dict[str, Any]]:
        api_key = keys[idx % len(keys)]
        logger.info("[%s/%s] %s", idx + 1, len(people), person.get("person_name") or person["linkedin_url"])
        return fetch_person_window(
            person,
            api_key=api_key,
            now=now,
            start=start,
            end=end,
            max_pages=args.max_pages,
        )

    if workers == 1:
        for idx, person in enumerate(people):
            rows.extend(worker(idx, person))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(worker, idx, person): person for idx, person in enumerate(people)}
            for future in as_completed(futures):
                rows.extend(future.result())

    rows.sort(key=lambda row: row.get("posted_at") or "", reverse=True)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dated = out_dir / f"posts_last_24_hours_{start.date().isoformat()}.csv"
    latest = out_dir / "posts_last_24_hours.csv"
    write_csv(dated, rows)
    write_csv(latest, rows)
    logger.info("Wrote %s (%s posts)", dated, len(rows))

    if args.no_slack:
        return 0
    if not slack_configured():
        logger.error("Slack is not configured (SLACK_BOT_TOKEN, SLACK_CHANNEL_ID)")
        return 1
    ok, err = slack_digest(dated, start=start, rows=rows)
    if not ok:
        logger.error("Slack post failed: %s", err)
        return 1
    logger.info("Slack digest sent")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="HubSpot CRO last-24-hours LinkedIn posts → Slack")
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=0, help="Default: number of RapidAPI keys")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-slack", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
