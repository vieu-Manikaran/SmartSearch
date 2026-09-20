import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from email_provider import STATUS_FOUND, empty_result_row, take_enrichment_step
from seeqe_email_callback import post_email_to_seeqe
from seeqe_contact_lookup import (
    ExistingContact,
    SeeqeContactLookupError,
    find_existing_contact,
    reset_lookup_circuit,
)
from vendor_file.product_emails import split_existing_product_emails


class SeeqeContactLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_lookup_circuit()
    @patch("seeqe_contact_lookup.settings.vieu_api_key", "test-key")
    @patch("seeqe_contact_lookup.requests.get")
    def test_searches_person_then_reads_work_email(self, get: Mock) -> None:
        search = Mock(status_code=200)
        search.json.return_value = {"data": {"id": "PERS-123"}}
        contact = Mock(status_code=200)
        contact.json.return_value = {
            "data": {
                "personalEmail": "jane@gmail.com",
                "professionalEmails": [{"email": "Jane@Acme.example"}],
            }
        }
        get.side_effect = [search, contact]

        result = find_existing_contact("https://www.linkedin.com/in/jane")

        self.assertEqual(result.person_id, "PERS-123")
        self.assertEqual(result.email, "jane@acme.example")
        self.assertEqual(len(get.call_args_list), 2)
        self.assertEqual(
            get.call_args_list[1].kwargs["params"],
            {"personId": "PERS-123"},
        )

    @patch("email_provider.find_existing_contact")
    def test_email_provider_skips_molster_for_product_hit(self, lookup: Mock) -> None:
        lookup.return_value = ExistingContact(
            person_id="PERS-123",
            email="jane@acme.example",
            all_emails=("jane@acme.example",),
        )
        source = {
            "person": "Jane",
            "company": "Acme",
            "linkedin_url": "https://www.linkedin.com/in/jane",
        }
        results = [empty_result_row(source)]

        first = take_enrichment_step([source], results)
        second = take_enrichment_step([source], results)

        self.assertFalse(first.done)
        self.assertTrue(second.done)
        self.assertEqual(results[0]["status"], STATUS_FOUND)
        self.assertEqual(results[0]["email_source"], "seeqe_product")
        self.assertEqual(results[0]["work_email"], "jane@acme.example")

    @patch("seeqe_email_callback.requests.post")
    def test_product_email_is_not_written_back_to_seeqe(self, post: Mock) -> None:
        self.assertTrue(
            post_email_to_seeqe(
                {
                    "email_source": "seeqe_product",
                    "work_email": "jane@acme.example",
                }
            )
        )
        post.assert_not_called()

    @patch("seeqe_contact_lookup.settings.vieu_api_key", "test-key")
    @patch("seeqe_contact_lookup.requests.get")
    def test_forbidden_lookup_returns_none_instead_of_raising(self, get: Mock) -> None:
        forbidden = Mock(status_code=403)
        forbidden.text = '{"code":"ERR_FORBIDDEN","reason":"SCOPE_INSUFFICIENT"}'
        get.return_value = forbidden

        result = find_existing_contact("https://www.linkedin.com/in/jane")

        self.assertIsNone(result)
        find_existing_contact("https://www.linkedin.com/in/john")
        self.assertEqual(get.call_count, 1)

    @patch("email_provider.find_existing_contact")
    def test_email_provider_continues_when_product_lookup_raises(self, lookup: Mock) -> None:
        lookup.side_effect = SeeqeContactLookupError(
            "Seeqe product lookup returned HTTP 403",
            transient=False,
        )
        source = {
            "person": "Jane",
            "company": "Acme",
            "linkedin_url": "https://www.linkedin.com/in/jane",
        }
        results = [empty_result_row(source)]

        first = take_enrichment_step([source], results)

        self.assertFalse(first.done)
        self.assertEqual(results[0]["product_lookup_status"], "not_found")
        self.assertNotEqual(results[0].get("status"), STATUS_FOUND)


class VendorProductSplitTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_lookup_circuit()
    @patch("vendor_file.product_emails.find_existing_contact")
    def test_existing_email_is_removed_and_written_separately(self, lookup: Mock) -> None:
        lookup.side_effect = [
            ExistingContact("PERS-123", "jane@acme.example", ("jane@acme.example",)),
            None,
        ]
        rows = [
            {
                "source_row": "2",
                "name": "Jane",
                "person_linkedin": "https://linkedin.com/in/jane",
                "company_name": "Acme",
                "company_linkedin": "https://linkedin.com/company/acme",
                "email_required": True,
                "phone_required": False,
            },
            {
                "source_row": "3",
                "name": "John",
                "person_linkedin": "https://linkedin.com/in/john",
                "company_name": "Acme",
                "company_linkedin": "https://linkedin.com/company/acme",
                "email_required": True,
                "phone_required": False,
            },
        ]
        with tempfile.TemporaryDirectory() as temp:
            remaining, path, count = split_existing_product_emails(
                rows,
                uid="VEN-TEST",
                out_dir=Path(temp),
            )
            with path.open(encoding="utf-8-sig") as handle:
                written = list(csv.DictReader(handle))

        self.assertEqual(count, 1)
        self.assertEqual([row["name"] for row in remaining], ["John"])
        self.assertEqual(written[0]["Stakeholder Vieu ID"], "PERS-123")
        self.assertEqual(written[0]["Work Email"], "jane@acme.example")

    @patch("vendor_file.product_emails.find_existing_contact")
    def test_existing_email_keeps_phone_request_but_disables_email(self, lookup: Mock) -> None:
        lookup.return_value = ExistingContact(
            "PERS-123",
            "jane@acme.example",
            ("jane@acme.example",),
        )
        row = {
            "source_row": "2",
            "name": "Jane",
            "person_linkedin": "https://linkedin.com/in/jane",
            "company_name": "Acme",
            "company_linkedin": "https://linkedin.com/company/acme",
            "email_required": True,
            "phone_required": True,
        }
        with tempfile.TemporaryDirectory() as temp:
            remaining, _path, count = split_existing_product_emails(
                [row],
                uid="VEN-TEST",
                out_dir=Path(temp),
            )

        self.assertEqual(count, 1)
        self.assertEqual(len(remaining), 1)
        self.assertFalse(remaining[0]["email_required"])
        self.assertTrue(remaining[0]["phone_required"])

    @patch("vendor_file.product_emails.find_existing_contact")
    def test_lookup_error_keeps_rows_for_vendor(self, lookup: Mock) -> None:
        lookup.side_effect = SeeqeContactLookupError(
            "Seeqe product lookup returned HTTP 403",
            transient=False,
        )
        rows = [
            {
                "source_row": "2",
                "name": "Jane",
                "person_linkedin": "https://linkedin.com/in/jane",
                "company_name": "Acme",
                "company_linkedin": "https://linkedin.com/company/acme",
                "email_required": True,
                "phone_required": False,
            }
        ]
        with tempfile.TemporaryDirectory() as temp:
            remaining, _path, count = split_existing_product_emails(
                rows,
                uid="VEN-TEST",
                out_dir=Path(temp),
            )

        self.assertEqual(count, 0)
        self.assertEqual([row["name"] for row in remaining], ["Jane"])


if __name__ == "__main__":
    unittest.main()
