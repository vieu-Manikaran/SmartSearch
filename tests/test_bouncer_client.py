import unittest
from unittest.mock import Mock, patch

from bouncer_client import verify_email


class BouncerClientTests(unittest.TestCase):
    @patch("bouncer_client.settings.bouncer_api_key", "test-key")
    @patch("bouncer_client.requests.get")
    def test_verify_email_uses_query_params_and_api_key(self, get: Mock) -> None:
        response = Mock(ok=True)
        response.json.return_value = {
            "email": "jane+sales@acme.example",
            "status": "deliverable",
            "reason": "accepted_email",
        }
        get.return_value = response

        result = verify_email("jane+sales@acme.example")

        self.assertEqual(result["status"], "deliverable")
        get.assert_called_once_with(
            "https://api.usebouncer.com/v1.1/email/verify",
            params={"email": "jane+sales@acme.example", "timeout": 10},
            headers={"x-api-key": "test-key"},
            timeout=30,
        )


if __name__ == "__main__":
    unittest.main()
