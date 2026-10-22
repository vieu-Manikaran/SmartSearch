import unittest
from unittest.mock import patch

from email_provider import (
    STATUS_FOUND,
    STATUS_MOLSTER_MISS,
    STATUS_NO_EMAIL,
    _apply_fullenrich_row,
    _mark_molster_hit,
    empty_result_row,
    enrich_contacts,
    fullenrich_password_matches,
)
from fullenrich_client import is_valid_work_email_status, work_email_candidates


class BouncerFilteringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.row = empty_result_row(
            {
                "person": "Jane Doe",
                "company": "Acme",
                "linkedin_url": "https://www.linkedin.com/in/jane-doe",
            }
        )
        self.hit = {
            "email": "jane@acme.example",
            "status": "ok",
            "risk_score": "A",
            "last_validated_at": "2026-09-01",
        }

    @patch(
        "email_provider.verify_email",
        return_value={"status": "deliverable", "reason": "accepted_email"},
    )
    def test_deliverable_email_is_retained(self, _verify) -> None:
        result = _mark_molster_hit(self.row, self.hit)

        self.assertEqual(result["status"], STATUS_FOUND)
        self.assertEqual(result["work_email"], "jane@acme.example")
        self.assertEqual(result["all_work_emails"], "jane@acme.example")
        self.assertEqual(result["email_status"], "deliverable")
        self.assertEqual(result["email_source"], "molster")

    @patch(
        "email_provider.verify_email",
        return_value={"status": "risky", "reason": "low_deliverability"},
    )
    def test_non_deliverable_email_is_suppressed(self, _verify) -> None:
        result = _mark_molster_hit(self.row, self.hit)

        self.assertEqual(result["status"], STATUS_NO_EMAIL)
        self.assertEqual(result["work_email"], "")
        self.assertEqual(result["all_work_emails"], "")
        self.assertEqual(result["email_status"], "risky")
        self.assertEqual(result["email_source"], "")
        self.assertEqual(result["molster_risk_score"], "A")
        self.assertEqual(result["molster_email"], "jane@acme.example")

    @patch(
        "email_provider.verify_email",
        return_value={"status": "risky", "reason": "low_deliverability"},
    )
    def test_rejected_molster_email_can_fall_through_to_fullenrich(self, _verify) -> None:
        result = _mark_molster_hit(self.row, self.hit, allow_fullenrich=True)

        self.assertEqual(result["status"], STATUS_MOLSTER_MISS)
        self.assertEqual(result["work_email"], "")
        self.assertEqual(result["email_source"], "")


class FullEnrichFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.row = empty_result_row(
            {
                "person": "Jane Doe",
                "company": "Acme",
                "linkedin_url": "https://www.linkedin.com/in/jane-doe",
            }
        )

    def test_only_valid_statuses_are_accepted(self) -> None:
        self.assertTrue(is_valid_work_email_status("DELIVERABLE"))
        self.assertTrue(is_valid_work_email_status("Valid & safe to send email"))
        self.assertFalse(is_valid_work_email_status("HIGH_PROBABILITY"))
        self.assertFalse(is_valid_work_email_status("Probably Valid Email"))
        self.assertFalse(is_valid_work_email_status("CATCH_ALL"))
        self.assertFalse(is_valid_work_email_status("INVALID"))

    def test_candidates_prefer_most_probable_then_dedupe(self) -> None:
        candidates = work_email_candidates(
            {
                "most_probable_work_email": {
                    "email": "probably@acme.example",
                    "status": "HIGH_PROBABILITY",
                },
                "work_emails": [
                    {"email": "probably@acme.example", "status": "HIGH_PROBABILITY"},
                    {"email": "jane@acme.example", "status": "DELIVERABLE"},
                ],
            }
        )
        self.assertEqual(
            candidates,
            [
                {"email": "probably@acme.example", "status": "HIGH_PROBABILITY"},
                {"email": "jane@acme.example", "status": "DELIVERABLE"},
            ],
        )

    @patch("email_provider.verify_email")
    def test_probably_valid_is_not_sent_to_bouncer(self, verify) -> None:
        result = _apply_fullenrich_row(
            self.row,
            {
                "candidates": [
                    {"email": "probably@acme.example", "status": "HIGH_PROBABILITY"},
                ],
                "job_title": "VP",
            },
            {},
        )

        verify.assert_not_called()
        self.assertEqual(result["status"], STATUS_NO_EMAIL)
        self.assertEqual(result["work_email"], "")
        self.assertEqual(result["email_source"], "")
        self.assertEqual(result["fullenrich_email"], "probably@acme.example")
        self.assertEqual(result["fullenrich_status"], "HIGH_PROBABILITY")

    @patch(
        "email_provider.verify_email",
        return_value={"status": "deliverable", "reason": "accepted_email"},
    )
    def test_valid_email_is_kept_when_bouncer_says_deliverable(self, verify) -> None:
        result = _apply_fullenrich_row(
            self.row,
            {
                "candidates": [
                    {"email": "probably@acme.example", "status": "HIGH_PROBABILITY"},
                    {"email": "jane@acme.example", "status": "DELIVERABLE"},
                ],
            },
            {},
        )

        verify.assert_called_once_with("jane@acme.example")
        self.assertEqual(result["status"], STATUS_FOUND)
        self.assertEqual(result["work_email"], "jane@acme.example")
        self.assertEqual(result["all_work_emails"], "jane@acme.example")
        self.assertEqual(result["email_status"], "deliverable")
        self.assertEqual(result["email_source"], "fullenrich")
        self.assertEqual(result["fullenrich_status"], "DELIVERABLE")

    @patch(
        "email_provider.verify_email",
        return_value={"status": "undeliverable", "reason": "rejected_email"},
    )
    def test_valid_email_dropped_when_bouncer_is_not_deliverable(self, _verify) -> None:
        result = _apply_fullenrich_row(
            self.row,
            {
                "candidates": [
                    {"email": "jane@acme.example", "status": "DELIVERABLE"},
                ],
            },
            {},
        )

        self.assertEqual(result["status"], STATUS_NO_EMAIL)
        self.assertEqual(result["work_email"], "")
        self.assertEqual(result["email_status"], "undeliverable")
        self.assertEqual(result["email_source"], "")
        self.assertEqual(result["fullenrich_email"], "jane@acme.example")

    @patch("email_provider.fullenrich_batch")
    @patch("email_provider.lookup_linkedin_urls", return_value=[])
    @patch("email_provider.find_existing_contact", return_value=None)
    @patch("email_provider.email_providers_configured", return_value=True)
    @patch("email_provider.fullenrich_configured", return_value=True)
    @patch("email_provider.bouncer_configured", return_value=True)
    def test_enrich_contacts_uses_fullenrich_only_after_misses(
        self,
        _bouncer,
        _fullenrich_ready,
        _providers,
        _product,
        _molster,
        fullenrich_batch,
    ) -> None:
        fullenrich_batch.return_value = [
            {
                "candidates": [
                    {"email": "catch@acme.example", "status": "CATCH_ALL"},
                ],
                "job_title": "",
                "status": "found",
            }
        ]

        with patch("email_provider.verify_email") as verify:
            results = enrich_contacts(
                [
                    {
                        "person": "Jane Doe",
                        "company": "Acme",
                        "linkedin_url": "https://www.linkedin.com/in/jane-doe",
                    }
                ],
                use_fullenrich=True,
            )

        fullenrich_batch.assert_called_once()
        verify.assert_not_called()
        self.assertEqual(results[0]["status"], STATUS_NO_EMAIL)
        self.assertEqual(results[0]["work_email"], "")
        self.assertEqual(results[0]["fullenrich_status"], "CATCH_ALL")


class FullEnrichPasswordTests(unittest.TestCase):
    @patch("email_provider.settings.fullenrich_password", "secret-pass")
    def test_password_match_is_exact(self) -> None:
        self.assertTrue(fullenrich_password_matches("secret-pass"))
        self.assertTrue(fullenrich_password_matches(" secret-pass "))
        self.assertFalse(fullenrich_password_matches("nope"))
        self.assertFalse(fullenrich_password_matches(""))

    @patch("email_provider.settings.fullenrich_password", "")
    def test_unset_password_never_matches(self) -> None:
        self.assertFalse(fullenrich_password_matches("secret-pass"))


if __name__ == "__main__":
    unittest.main()
