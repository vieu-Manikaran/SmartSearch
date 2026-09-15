from __future__ import annotations

import unittest
from unittest.mock import patch

from company_website_linkedin import (
    crawl_website_for_company_linkedin,
    extract_company_linkedin_from_html,
    find_company_linkedin,
    seed_website_url,
)


class ExtractTests(unittest.TestCase):
    def test_href_slug(self) -> None:
        html = '<footer><a href="https://www.linkedin.com/company/acme-corp/">LI</a></footer>'
        self.assertEqual(
            extract_company_linkedin_from_html(html),
            "https://www.linkedin.com/company/acme-corp",
        )

    def test_numeric_id(self) -> None:
        html = "Follow us on https://linkedin.com/company/1035/"
        self.assertEqual(
            extract_company_linkedin_from_html(html),
            "https://www.linkedin.com/company/1035",
        )

    def test_ignores_person_profile(self) -> None:
        html = '<a href="https://www.linkedin.com/in/jane-doe">Jane</a>'
        self.assertIsNone(extract_company_linkedin_from_html(html))

    def test_escaped_json(self) -> None:
        html = r'{"sameAs":"https:\/\/www.linkedin.com\/company\/walmart"}'
        self.assertEqual(
            extract_company_linkedin_from_html(html),
            "https://www.linkedin.com/company/walmart",
        )


class CrawlTests(unittest.TestCase):
    def test_seed_from_domain(self) -> None:
        self.assertEqual(seed_website_url("acme.test"), "https://acme.test")

    def test_walks_pages_until_found(self) -> None:
        pages = {
            "https://acme.test/sitemap.xml": (404, "https://acme.test/sitemap.xml", ""),
            "https://acme.test/sitemap_index.xml": (404, "https://acme.test/sitemap_index.xml", ""),
            "https://acme.test": (
                200,
                "https://acme.test/",
                '<html><a href="/about">About</a></html>',
            ),
            "https://acme.test/about": (
                200,
                "https://acme.test/about",
                '<a href="https://www.linkedin.com/company/acme">LinkedIn</a>',
            ),
        }

        def fetch(url: str) -> tuple[int, str, str]:
            return pages.get(url, (404, url, ""))

        found, found_on = crawl_website_for_company_linkedin("acme.test", fetch=fetch)
        self.assertEqual(found, "https://www.linkedin.com/company/acme")
        self.assertEqual(found_on, "https://acme.test/about")

    def test_no_linkedin_returns_none(self) -> None:
        def fetch(url: str) -> tuple[int, str, str]:
            if "sitemap" in url:
                return 404, url, ""
            return 200, url, "<html><p>No social links</p></html>"

        found, _ = crawl_website_for_company_linkedin("https://none.test", fetch=fetch)
        self.assertIsNone(found)


class ResolveTests(unittest.TestCase):
    def test_website_hit_skips_serper(self) -> None:
        def fetch(url: str) -> tuple[int, str, str]:
            if "sitemap" in url:
                return 404, url, ""
            return 200, url, '<a href="https://www.linkedin.com/company/acme">'

        with patch("company_website_linkedin.find_linkedin_company_url") as serper:
            row = find_company_linkedin("Acme", "acme.test", serper_api_key="k", fetch=fetch)
        serper.assert_not_called()
        self.assertEqual(row["linkedin_url"], "https://www.linkedin.com/company/acme")
        self.assertEqual(row["source"], "website")
        self.assertEqual(row["status"], "found_on_website")

    def test_website_miss_falls_back_to_serper(self) -> None:
        def fetch(url: str) -> tuple[int, str, str]:
            if "sitemap" in url:
                return 404, url, ""
            return 200, url, "<html></html>"

        with patch(
            "company_website_linkedin.find_linkedin_company_url",
            return_value="https://www.linkedin.com/company/acme",
        ) as serper:
            row = find_company_linkedin("Acme", "acme.test", serper_api_key="k", fetch=fetch)
        serper.assert_called_once()
        self.assertEqual(row["source"], "serper")
        self.assertEqual(row["status"], "found")

    def test_no_website_uses_serper(self) -> None:
        with patch(
            "company_website_linkedin.find_linkedin_company_url",
            return_value="https://www.linkedin.com/company/ibm",
        ) as serper:
            row = find_company_linkedin("IBM", "", serper_api_key="k")
        serper.assert_called_once()
        self.assertEqual(row["source"], "serper")
        self.assertEqual(row["website"], "")


if __name__ == "__main__":
    unittest.main()
