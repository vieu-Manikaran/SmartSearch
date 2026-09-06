import unittest

from seeqe_email_callback import _build_payload, _confidence_status, _iso_created_at, _normalize_linkedin_url


class SeeqePayloadTests(unittest.TestCase):
    def test_strips_trailing_slash_and_query(self) -> None:
        self.assertEqual(
            _normalize_linkedin_url("https://www.linkedin.com/in/mattthompson01/?trk=foo"),
            "https://www.linkedin.com/in/mattthompson01",
        )

    def test_adds_www(self) -> None:
        self.assertEqual(
            _normalize_linkedin_url("https://linkedin.com/in/mattthompson01/"),
            "https://www.linkedin.com/in/mattthompson01",
        )

    def test_molster_grade_maps_to_clay_status(self) -> None:
        self.assertEqual(_confidence_status("A"), "Success")
        self.assertEqual(_confidence_status("c"), "Partial success")
        self.assertEqual(_confidence_status(""), "Success")
        self.assertEqual(_confidence_status("Success"), "Success")
        self.assertEqual(_confidence_status("U"), "Partial success")

    def test_job_timestamp_to_iso(self) -> None:
        self.assertEqual(_iso_created_at("2026-08-24 14:09:30 UTC"), "2026-08-24T14:09:30Z")
        self.assertEqual(_iso_created_at("2026-08-24T14:09:30Z"), "2026-08-24T14:09:30Z")

    def test_build_payload_from_bouncer_deliverable_row(self) -> None:
        payload = _build_payload(
            {
                "work_email": "matt@acme.com",
                "linkedin_url": "https://www.linkedin.com/in/mattthompson01/",
                "email_status": "deliverable",
                "created_at": "2026-08-24 14:09:30 UTC",
            }
        )
        self.assertEqual(
            payload,
            {
                "linkedInUrl": "https://www.linkedin.com/in/mattthompson01",
                "email": "matt@acme.com",
                "createdAt": "2026-08-24T14:09:30Z",
                "confidence_status": "Success",
                "email_type": "professional",
            },
        )

    def test_build_payload_rejects_non_deliverable_email(self) -> None:
        payload = _build_payload(
            {
                "work_email": "matt@acme.com",
                "linkedin_url": "https://www.linkedin.com/in/mattthompson01/",
                "email_status": "risky",
            }
        )
        self.assertIsNone(payload)


if __name__ == "__main__":
    unittest.main()
