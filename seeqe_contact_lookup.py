"""Read existing person contact emails from the Seeqe product API."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable

import requests

from config import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
WORK_MARKERS = ("work", "professional", "business", "corporate")
PERSONAL_MARKERS = ("personal", "private", "home")


class SeeqeContactLookupError(RuntimeError):
    """Raised when the product lookup cannot reliably complete."""

    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class ExistingContact:
    person_id: str
    email: str
    all_emails: tuple[str, ...] = ()


def configured() -> bool:
    return bool((settings.vieu_api_key or "").strip())


# Skip further Seeqe reads after a non-transient failure (e.g. 403 SCOPE_INSUFFICIENT).
_lookup_disabled_reason: str | None = None


def reset_lookup_circuit() -> None:
    global _lookup_disabled_reason
    _lookup_disabled_reason = None


def _get(path: str, params: dict[str, str]) -> Any:
    api_key = (settings.vieu_api_key or "").strip()
    if not api_key:
        raise SeeqeContactLookupError(
            "VIEU_API_KEY is required for product email lookup.",
            transient=False,
        )
    url = f"{settings.seeqe_api_base_url.rstrip('/')}{path}"
    try:
        response = requests.get(
            url,
            params=params,
            headers={"x-api-key": api_key, "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise SeeqeContactLookupError(f"Seeqe product lookup failed: {exc}") from exc
    if response.status_code >= 400:
        detail = (response.text or "").strip()
        if len(detail) > 200:
            detail = detail[:197] + "..."
        raise SeeqeContactLookupError(
            f"Seeqe product lookup returned HTTP {response.status_code}"
            + (f": {detail}" if detail else ""),
            transient=response.status_code >= 500 or response.status_code in {408, 429},
        )
    try:
        return response.json()
    except ValueError as exc:
        raise SeeqeContactLookupError("Seeqe product lookup returned invalid JSON.") from exc


def _walk(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, (*path, str(key).lower()))
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child, path)
    elif isinstance(value, str):
        yield path, value.strip()


def _person_id(payload: Any) -> str:
    for _path, value in _walk(payload):
        if value.upper().startswith("PERS-"):
            return value
    return ""


def _emails(payload: Any) -> tuple[str, ...]:
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for path, value in _walk(payload):
        email = value.lower()
        if email in seen or not EMAIL_RE.fullmatch(email):
            continue
        context = " ".join(path)
        if any(marker in context for marker in PERSONAL_MARKERS):
            continue
        rank = 0 if any(marker in context for marker in WORK_MARKERS) else 1
        ranked.append((rank, email))
        seen.add(email)
    ranked.sort(key=lambda item: item[0])
    return tuple(email for _, email in ranked)


def find_existing_contact(linkedin_url: str, *, person_id: str = "") -> ExistingContact | None:
    """Resolve a person by LinkedIn URL, then return an existing product email.

    Lookup failures are swallowed so vendor files and email jobs can continue.
    """
    global _lookup_disabled_reason
    if _lookup_disabled_reason:
        return None
    try:
        resolved_id = (person_id or "").strip()
        if not resolved_id:
            search = _get(
                "/api/v2/persons/search",
                {"linkedInUrl": (linkedin_url or "").strip()},
            )
            resolved_id = _person_id(search)
        if not resolved_id:
            return None

        contact = _get("/api/v2/persons/contact", {"personId": resolved_id})
        emails = _emails(contact)
        if not emails:
            return None
        return ExistingContact(person_id=resolved_id, email=emails[0], all_emails=emails)
    except SeeqeContactLookupError as exc:
        logger.warning("Seeqe product email lookup failed; continuing without it: %s", exc)
        if not exc.transient:
            _lookup_disabled_reason = str(exc)
        return None
