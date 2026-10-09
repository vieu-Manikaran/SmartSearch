from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from rapidapi_person_deep import (
    fetch_person_deep,
    reset_rapidapi_key_health,
    resolve_profiles_batch,
    resolve_vanity_url,
    vanity_identifier_from_person_data,
)


class VanityExtractionTests(unittest.TestCase):
    def test_prefers_vanity_over_member_urn(self) -> None:
        slug = vanity_identifier_from_person_data(
            {
                "publicIdentifier": "ACwAAACH0QcBJJ6rWWPQOtkcPZ_uowmGGzval58",
                "linkedinUrl": "https://www.linkedin.com/in/jane-doe/",
            }
        )
        self.assertEqual(slug, "jane-doe")

    def test_accepts_plain_vanity_public_identifier(self) -> None:
        slug = vanity_identifier_from_person_data({"publicIdentifier": "jane-doe"})
        self.assertEqual(slug, "jane-doe")


class KeyFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_rapidapi_key_health()

    def tearDown(self) -> None:
        reset_rapidapi_key_health()

    @patch("rapidapi_person_deep.collect_rapidapi_keys", return_value=["key-a", "key-b"])
    @patch("rapidapi_person_deep.fetch_person_deep")
    def test_second_key_is_tried_when_first_returns_403(self, fetch, _keys) -> None:
        def fake(_link: str, api_key: str, pace: bool = False) -> dict:
            if api_key == "key-b":
                return {"success": False, "error": "http_403"}
            return {"success": True, "data": {"publicIdentifier": "jane-doe"}}

        fetch.side_effect = fake
        result = resolve_vanity_url(
            "https://www.linkedin.com/in/ACwAAACH0QcBJJ6rWWPQOtkcPZ_uowmGGzval58/",
            api_key="key-b",
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["public_identifier"], "jane-doe")
        self.assertEqual(result["linkedin_url_resolved"], "https://www.linkedin.com/in/jane-doe/")
        self.assertEqual([call.args[1] for call in fetch.call_args_list], ["key-b", "key-a"])

    @patch("rapidapi_person_deep.collect_rapidapi_keys", return_value=["key-a", "key-b"])
    @patch("rapidapi_person_deep.fetch_person_deep")
    def test_batch_does_not_leave_every_other_row_unresolved(self, fetch, _keys) -> None:
        def fake(link: str, api_key: str, pace: bool = False) -> dict:
            if api_key == "key-b":
                return {"success": False, "error": "unauthorized"}
            slug = "odd-row" if "odd" in link else "even-row"
            return {"success": True, "data": {"publicIdentifier": slug}}

        fetch.side_effect = fake
        rows = [
            {"linkedin_url": f"https://www.linkedin.com/in/odd-{idx}/"}
            if idx % 2 == 1
            else {"linkedin_url": f"https://www.linkedin.com/in/even-{idx}/"}
            for idx in range(1, 6)
        ]
        out = resolve_profiles_batch(rows)
        self.assertEqual(len(out), 5)
        self.assertEqual({row["status"] for row in out}, {"resolved"})
        self.assertTrue(all(row["linkedin_url_resolved"] for row in out))

    @patch("rapidapi_person_deep.collect_rapidapi_keys", return_value=["key-a", "key-b"])
    @patch("rapidapi_person_deep.fetch_person_deep")
    def test_still_urn_then_vanity_from_other_key(self, fetch, _keys) -> None:
        def fake(_link: str, api_key: str, pace: bool = False) -> dict:
            if api_key == "key-a":
                return {
                    "success": True,
                    "data": {"publicIdentifier": "ACwAAACH0QcBJJ6rWWPQOtkcPZ_uowmGGzval58"},
                }
            return {"success": True, "data": {"publicIdentifier": "jane-doe"}}

        fetch.side_effect = fake
        result = resolve_vanity_url("https://www.linkedin.com/in/ACwAAACH0QcBJJ6rWWPQOtkcPZ_uowmGGzval58/")
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["public_identifier"], "jane-doe")

    @patch("rapidapi_person_deep.collect_rapidapi_keys", return_value=["key-a", "key-b"])
    @patch("rapidapi_person_deep.fetch_person_deep")
    def test_unauthorized_key_is_skipped_on_later_rows(self, fetch, _keys) -> None:
        def fake(_link: str, api_key: str, pace: bool = False) -> dict:
            if api_key == "key-b":
                return {"success": False, "error": "unauthorized"}
            return {"success": True, "data": {"publicIdentifier": "jane-doe"}}

        fetch.side_effect = fake
        resolve_vanity_url("https://www.linkedin.com/in/probe/", api_key="key-b")
        fetch.reset_mock()
        rows = [{"linkedin_url": f"https://www.linkedin.com/in/p{idx}/"} for idx in range(4)]
        out = resolve_profiles_batch(rows)
        self.assertEqual({row["status"] for row in out}, {"resolved"})
        self.assertEqual([call.args[1] for call in fetch.call_args_list], ["key-a"] * 4)


class ProviderBusyTests(unittest.TestCase):
    @patch("rapidapi_person_deep.time.sleep")
    @patch("rapidapi_person_deep.requests.post")
    def test_provider_busy_stops_after_two_attempts(self, post, sleep) -> None:
        resp = MagicMock()
        resp.status_code = 500
        resp.json.return_value = {
            "success": False,
            "status": 500,
            "message": "No free account spotted. Please inform developer team.",
        }
        resp.text = '{"message":"No free account spotted. Please inform developer team."}'
        post.return_value = resp
        result = fetch_person_deep("https://www.linkedin.com/in/jane/", "key-a")
        self.assertEqual(result, {"success": False, "error": "provider_busy"})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    @patch("rapidapi_person_deep.time.sleep")
    @patch("rapidapi_person_deep.requests.post")
    def test_paced_calls_wait_between_requests(self, post, sleep) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"success": True, "data": {"publicIdentifier": "jane-doe"}}
        post.return_value = resp
        fetch_person_deep("https://www.linkedin.com/in/jane/", "key-pace", pace=True)
        fetch_person_deep("https://www.linkedin.com/in/jane/", "key-pace", pace=True)
        self.assertGreaterEqual(sleep.call_count, 1)

    @patch("rapidapi_person_deep.collect_rapidapi_keys", return_value=["key-a", "key-b"])
    @patch("rapidapi_person_deep.fetch_person_deep")
    def test_provider_busy_does_not_try_the_other_key(self, fetch, _keys) -> None:
        fetch.return_value = {"success": False, "error": "provider_busy"}
        result = resolve_vanity_url("https://www.linkedin.com/in/ACwAAACH0QcBJJ6rWWPQOtkcPZ_uowmGGzval58/")
        self.assertEqual(result["status"], "provider_busy")
        self.assertEqual(fetch.call_count, 1)

    @patch("rapidapi_person_deep.time.sleep")
    @patch("rapidapi_person_deep.collect_rapidapi_keys", return_value=["key-a"])
    @patch("rapidapi_person_deep.fetch_person_deep")
    def test_batch_retries_provider_busy_instead_of_dropping_the_row(self, fetch, _keys, _sleep) -> None:
        calls = {"n": 0}

        def fake(_link: str, _api_key: str, pace: bool = False) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                return {"success": False, "error": "provider_busy"}
            return {"success": True, "data": {"publicIdentifier": "jane-doe"}}

        fetch.side_effect = fake
        out = resolve_profiles_batch(
            [{"linkedin_url": "https://www.linkedin.com/in/ACwAAACH0QcBJJ6rWWPQOtkcPZ_uowmGGzval58/"}]
        )
        self.assertEqual(out[0]["status"], "resolved")
        self.assertEqual(out[0]["public_identifier"], "jane-doe")
        self.assertGreaterEqual(calls["n"], 2)
