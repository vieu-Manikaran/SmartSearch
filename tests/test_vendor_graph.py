from __future__ import annotations

import unittest

from vendor_file.graph import company_numeric_id, lookup_key, _url_variants
from vendor_file.pipeline import new_request_id, parse_bool, parse_input_csv
from vendor_file.urls import canonicalize_company_url, canonicalize_person_url
from vendor_file.website import canonicalize_website


class VendorGraphTests(unittest.TestCase):
    def test_lookup_key_person_and_company(self) -> None:
        self.assertEqual(
            lookup_key("https://www.linkedin.com/in/SeemaSwamy/?trk=abc"),
            "person:seemaswamy",
        )
        self.assertEqual(
            lookup_key("https://linkedin.com/company/Walmart/about/"),
            "company:walmart",
        )
        self.assertEqual(
            lookup_key("https://www.linkedin.com/school/stanford-university"),
            "company:stanford-university",
        )

    def test_company_numeric_id(self) -> None:
        self.assertEqual(
            company_numeric_id("https://www.linkedin.com/company/1035/"),
            1035,
        )
        self.assertIsNone(company_numeric_id("https://www.linkedin.com/company/microsoft"))

    def test_url_variants_include_slash_and_school(self) -> None:
        variants = _url_variants("https://www.linkedin.com/company/microsoft")
        self.assertIn("https://www.linkedin.com/company/microsoft", variants)
        self.assertIn("https://www.linkedin.com/company/microsoft/", variants)
        self.assertIn("https://www.linkedin.com/school/microsoft", variants)
        self.assertIn("https://linkedin.com/company/microsoft", variants)


class VendorPipelineHelpersTests(unittest.TestCase):
    def test_person_url_canonical(self) -> None:
        self.assertEqual(
            canonicalize_person_url("linkedin.com/in/seemaswamy/?trk=abc").url,
            "https://www.linkedin.com/in/seemaswamy",
        )
        self.assertFalse(canonicalize_person_url("https://www.linkedin.com/sales/lead/ACwAAAxyz").ok)

    def test_company_url_canonical(self) -> None:
        self.assertEqual(
            canonicalize_company_url("https://www.linkedin.com/company/walmart/about/?trk=x").url,
            "https://www.linkedin.com/company/walmart",
        )
        self.assertEqual(
            canonicalize_company_url("https://www.linkedin.com/school/stanford-university").url,
            "https://www.linkedin.com/company/stanford-university",
        )

    def test_website_strips_careers(self) -> None:
        self.assertEqual(
            canonicalize_website("https://careers.walmart.com/us/en/home"),
            "https://www.walmart.com",
        )

    def test_parse_bool_and_uid(self) -> None:
        self.assertTrue(parse_bool("", True))
        self.assertFalse(parse_bool("no", True))
        uid = new_request_id()
        self.assertTrue(uid.startswith("VEN-"))

    def test_parse_input_csv(self) -> None:
        raw = (
            b"Name,LinkedIn,Company,Company LinkedIn\n"
            b"Jane Doe,https://www.linkedin.com/in/jane-doe/,Acme,"
            b"https://www.linkedin.com/company/acme\n"
        )
        rows = parse_input_csv(raw)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Jane Doe")
        self.assertTrue(rows[0]["email_required"])


if __name__ == "__main__":
    unittest.main()
