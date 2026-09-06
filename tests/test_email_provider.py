import unittest
from unittest.mock import patch

from email_provider import (
    STATUS_FOUND,
    STATUS_NO_EMAIL,
    _mark_molster_hit,
    empty_result_row,
)


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


if __name__ == "__main__":
    unittest.main()
