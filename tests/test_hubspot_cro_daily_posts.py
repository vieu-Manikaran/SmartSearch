from __future__ import annotations

import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from hubspot_cro_daily_posts import in_window, ist_previous_calendar_day, parse_posted_at, POST_FIELDS


class HubspotCroDailyPostsTests(unittest.TestCase):
    def test_ist_window_is_previous_calendar_day(self) -> None:
        now = datetime(2026, 9, 16, 0, 5, tzinfo=ZoneInfo("Asia/Kolkata"))
        start, end = ist_previous_calendar_day(now)
        self.assertEqual(start.isoformat(), "2026-09-15T00:00:00+05:30")
        self.assertEqual(end.isoformat(), "2026-09-16T00:00:00+05:30")

    def test_in_window_includes_start_excludes_end(self) -> None:
        start = datetime(2026, 9, 15, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        end = datetime(2026, 9, 16, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        inside = datetime(2026, 9, 15, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        at_end = datetime(2026, 9, 16, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertTrue(in_window(inside, start, end))
        self.assertFalse(in_window(at_end, start, end))

    def test_parse_posted_at_iso(self) -> None:
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        posted = parse_posted_at({"postedAt": "2026-09-15T18:30:00Z"}, now=now)
        self.assertIsNotNone(posted)
        self.assertEqual(posted.isoformat(), "2026-09-15T18:30:00+00:00")

    def test_posts_csv_columns_match_30_day_file(self) -> None:
        self.assertEqual(
            POST_FIELDS,
            [
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
                "posted_at",
                "post_link",
                "post_summary",
                "people_mentioned",
                "days_since_last_post",
                "is_repost",
            ],
        )


if __name__ == "__main__":
    unittest.main()
