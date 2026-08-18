from __future__ import annotations

import unittest

from vendor_file.experience import (
    Position,
    is_board_or_advisor,
    pick_current_graph_role,
    positions_from_graph_rows,
)
from vendor_file.graph import company_numeric_id, lookup_key, _url_variants
from vendor_file.graph_pipeline import new_graph_request_id
from vendor_file.pipeline import (
    contact_need_flags,
    new_request_id,
    parse_bool,
    parse_input_csv,
)
from vendor_file.urls import canonicalize_company_url, canonicalize_person_url
from vendor_file.website import canonicalize_website, website_from_email_domains


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
        self.assertTrue(new_graph_request_id().startswith("VNG-"))

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
        self.assertTrue(rows[0]["phone_required"])

        email_only = parse_input_csv(
            raw, email_required_default=True, phone_required_default=False
        )
        self.assertTrue(email_only[0]["email_required"])
        self.assertFalse(email_only[0]["phone_required"])

    def test_contact_need_flags(self) -> None:
        self.assertEqual(contact_need_flags("email"), (True, False))
        self.assertEqual(contact_need_flags("phone"), (False, True))
        self.assertEqual(contact_need_flags("both"), (True, True))
        self.assertEqual(contact_need_flags(""), (True, True))


class VendorGraphCurrentRoleTests(unittest.TestCase):
    def _pos(
        self,
        *,
        company: str,
        title: str,
        present: bool,
        company_id: str = "",
        priority: int = 0,
        start=None,
    ) -> Position:
        from datetime import date as d

        return Position(
            company=company,
            title=title,
            company_id=company_id,
            company_url=f"https://www.linkedin.com/company/{company.lower()}",
            start=start or d(2020, 1, 1),
            present=present,
            priority=priority,
        )

    def test_board_detected(self) -> None:
        self.assertTrue(is_board_or_advisor("Member of the Board of Directors"))
        self.assertTrue(is_board_or_advisor("Independent Director"))
        self.assertTrue(is_board_or_advisor("Advisor"))
        self.assertTrue(is_board_or_advisor("Non-Executive Chairman"))
        self.assertFalse(is_board_or_advisor("Chief Executive Officer"))
        self.assertFalse(is_board_or_advisor("Onboard Engineer"))

    def test_skip_board_when_another_present_employer(self) -> None:
        target = self._pos(company="Acme", title="CFO", present=False, company_id="COMP-A", priority=2)
        board = self._pos(
            company="Acme", title="Board Member", present=True, company_id="COMP-A", priority=0
        )
        ceo = self._pos(
            company="NewCo", title="Chief Executive Officer", present=True, company_id="COMP-B", priority=1
        )
        current, equals = pick_current_graph_role([target, board, ceo], [target, board])
        self.assertFalse(equals)
        self.assertEqual(current.company, "NewCo")
        self.assertEqual(current.title, "Chief Executive Officer")

    def test_keep_board_when_it_is_the_only_present_role(self) -> None:
        past = self._pos(company="Acme", title="CFO", present=False, company_id="COMP-A", priority=1)
        board = self._pos(
            company="Acme", title="Board Member", present=True, company_id="COMP-A", priority=0
        )
        current, equals = pick_current_graph_role([past, board], [past, board])
        self.assertTrue(equals)
        self.assertEqual(current.title, "Board Member")

    def test_still_at_target_skips_board_for_title(self) -> None:
        board = self._pos(
            company="Microsoft",
            title="Member of the Board of Directors",
            present=True,
            company_id="COMP-M",
            priority=0,
        )
        ceo = self._pos(
            company="Microsoft",
            title="Chief Executive Officer",
            present=True,
            company_id="COMP-M",
            priority=1,
        )
        current, equals = pick_current_graph_role([board, ceo], [board, ceo])
        self.assertTrue(equals)
        self.assertEqual(current.title, "Chief Executive Officer")

    def test_positions_from_graph_null_dates_to_is_present(self) -> None:
        from datetime import date as d

        rows = [
            {
                "company_name": "Microsoft",
                "title": "CEO",
                "company_id": "COMP-M",
                "company_url": "https://www.linkedin.com/company/microsoft",
                "dates_from": d(2014, 2, 1),
                "dates_to": None,
                "priority": 1,
            }
        ]
        positions = positions_from_graph_rows(rows)
        self.assertEqual(len(positions), 1)
        self.assertTrue(positions[0].present)

    def test_website_from_email_domains_skips_news(self) -> None:
        self.assertEqual(
            website_from_email_domains("microsoft.com", ["news.microsoft.com"]),
            "https://microsoft.com",
        )
        self.assertEqual(
            website_from_email_domains("", ["news.microsoft.com"]),
            "https://www.microsoft.com",
        )
        self.assertEqual(
            website_from_email_domains("careers.walmart.com", ["walmart.com"]),
            "https://www.walmart.com",
        )


if __name__ == "__main__":
    unittest.main()
