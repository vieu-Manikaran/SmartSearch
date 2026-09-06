"""Bouncer real-time email verification client."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.usebouncer.com/v1.1/email/verify"
REQUEST_TIMEOUT_SEC = 30
MAX_ATTEMPTS = 3


class BouncerError(Exception):
    """Raised when Bouncer cannot verify an email."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def bouncer_configured() -> bool:
    return bool((settings.bouncer_api_key or "").strip())


def _headers() -> dict[str, str]:
    api_key = (settings.bouncer_api_key or "").strip()
    if not api_key:
        raise BouncerError("Missing BOUNCER_API_KEY in environment.")
    return {"x-api-key": api_key}


def _error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("error") or "")[:300]
    except ValueError:
        pass
    return (response.text or "")[:300]


def verify_email(email: str) -> dict[str, Any]:
    """Verify one address and return Bouncer's response payload."""
    address = (email or "").strip()
    if not address:
        raise BouncerError("Email address is required for Bouncer verification.")

    last_error = "Bouncer verification failed."
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(
                BASE_URL,
                params={"email": address, "timeout": 10},
                headers=_headers(),
                timeout=REQUEST_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            last_error = f"Bouncer request failed: {exc}"
            logger.warning("%s (attempt %s/%s)", last_error, attempt, MAX_ATTEMPTS)
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 * attempt)
            continue

        if response.ok:
            try:
                payload = response.json()
            except ValueError as exc:
                last_error = "Bouncer returned a non-JSON response."
                logger.warning("%s (attempt %s/%s)", last_error, attempt, MAX_ATTEMPTS)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2 * attempt)
                continue
            if not isinstance(payload, dict) or not payload.get("status"):
                last_error = "Bouncer returned an unexpected response."
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2 * attempt)
                    continue
                break
            return payload

        detail = _error_message(response)
        if response.status_code == 401:
            raise BouncerError("Invalid BOUNCER_API_KEY.")
        if response.status_code == 402:
            raise BouncerError(detail or "Bouncer account has insufficient credits.")
        if response.status_code in {429, 500, 502, 503, 504}:
            last_error = detail or f"Bouncer returned HTTP {response.status_code}"
            logger.warning(
                "Bouncer transient HTTP %s (attempt %s/%s)",
                response.status_code,
                attempt,
                MAX_ATTEMPTS,
            )
            if attempt < MAX_ATTEMPTS:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = max(1, min(60, int(retry_after)))
                except (TypeError, ValueError):
                    delay = 2 * attempt
                time.sleep(delay)
                continue
            break
        raise BouncerError(detail or f"Bouncer returned HTTP {response.status_code}")

    raise BouncerError(last_error, transient=True)
