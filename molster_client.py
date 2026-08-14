"""MoltSets (Molster) client for LinkedIn → business-email enrichment."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.moltsets.com/api/v1/tools/linkedin_to_business_email"
BATCH_SIZE = 100  # API max per request
FAIR_USE_PATH = Path("data/molster_fair_use.json")
REQUEST_TIMEOUT_SEC = 90
MAX_ATTEMPTS = 5


class MolsterError(Exception):
    """Raised when the MoltSets API returns an error."""

    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        retry_after_ts: float = 0,
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.retry_after_ts = retry_after_ts


class MolsterFairUseExhausted(MolsterError):
    """Fair-use record window is empty; wait until retry_after_ts."""

    def __init__(self, message: str, *, retry_after_ts: float) -> None:
        super().__init__(message, transient=True, retry_after_ts=retry_after_ts)


def molster_configured() -> bool:
    return bool((settings.molster_api_key or "").strip())


def linkedin_match_key(url: str) -> str:
    """Normalize a LinkedIn profile URL/slug so batch results can be joined."""
    raw = (url or "").strip()
    if not raw:
        return ""
    candidate = raw
    if "://" not in candidate and "linkedin.com" in candidate.lower():
        candidate = "https://" + candidate.lstrip("/")
    parsed = urlparse(candidate)
    path = (parsed.path or "").lower().strip("/")
    if "/in/" in f"/{path}/":
        parts = path.split("in/", 1)[-1].split("/")
        slug = (parts[0] if parts else "").strip()
        if slug:
            return slug
    if path.startswith("in/"):
        slug = path.split("/", 1)[-1].split("/")[0].strip()
        if slug:
            return slug
    return path or raw.strip().lower().rstrip("/")


def _headers() -> dict[str, str]:
    api_key = (settings.molster_api_key or "").strip()
    if not api_key:
        raise MolsterError("Missing MOLSTER_API_KEY in environment.")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _is_transient_http(status_code: int) -> bool:
    return status_code in {408, 425, 429, 500, 502, 503, 504}


def _parse_reset_ts(value: str) -> float:
    text = (value or "").strip()
    if not text:
        return 0.0
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


def load_fair_use() -> dict[str, Any]:
    if not FAIR_USE_PATH.is_file():
        return {}
    try:
        data = json.loads(FAIR_USE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_fair_use(fair_use: dict[str, Any] | None) -> None:
    if not fair_use:
        return
    FAIR_USE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(fair_use)
    payload["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    FAIR_USE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def remaining_records(fair_use: dict[str, Any] | None = None) -> int | None:
    """None means unknown (no snapshot yet)."""
    data = fair_use if fair_use is not None else load_fair_use()
    if not data:
        return None
    values: list[int] = []
    for key in ("records_remaining_5h", "records_remaining_1w"):
        raw = data.get(key)
        if raw is None or raw == "":
            continue
        try:
            values.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return min(values)


def fair_use_reset_ts(fair_use: dict[str, Any] | None = None) -> float:
    data = fair_use if fair_use is not None else load_fair_use()
    now = time.time()
    candidates: list[float] = []
    remaining_5h = data.get("records_remaining_5h")
    remaining_1w = data.get("records_remaining_1w")
    if remaining_5h is not None:
        try:
            if int(remaining_5h) <= 0:
                ts = _parse_reset_ts(str(data.get("records_reset_5h") or ""))
                if ts > now:
                    candidates.append(ts)
        except (TypeError, ValueError):
            pass
    if remaining_1w is not None:
        try:
            if int(remaining_1w) <= 0:
                ts = _parse_reset_ts(str(data.get("records_reset_1w") or ""))
                if ts > now:
                    candidates.append(ts)
        except (TypeError, ValueError):
            pass
    return min(candidates) if candidates else 0.0


def raise_if_fair_use_exhausted() -> None:
    """Raise if a saved snapshot says the record window is empty."""
    fair = load_fair_use()
    remaining = remaining_records(fair)
    if remaining is None or remaining > 0:
        return
    reset_ts = fair_use_reset_ts(fair)
    if reset_ts <= time.time():
        return
    wait_min = max(1, int((reset_ts - time.time()) / 60))
    raise MolsterFairUseExhausted(
        f"Molster fair-use limit reached (5k emails / 5 hours). "
        f"Resuming after window reset in ~{wait_min} min.",
        retry_after_ts=reset_ts,
    )


def _error_message(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return str(data.get("message") or data.get("error") or "")[:300]
    except ValueError:
        pass
    return (resp.text or "")[:300]


def _retry_after_ts(resp: requests.Response) -> float:
    header = (resp.headers.get("Retry-After") or "").strip()
    if not header:
        reset = fair_use_reset_ts()
        return reset if reset > time.time() else time.time() + 60
    try:
        return time.time() + max(1, int(header))
    except ValueError:
        parsed = _parse_reset_ts(header)
        return parsed if parsed > time.time() else time.time() + 60


def _result_from_item(item: dict[str, Any], fallback_input: str = "") -> dict[str, str]:
    data = item.get("data") if isinstance(item.get("data"), dict) else item
    email = str((data or {}).get("email") or "").strip()
    status = str(item.get("status") or "").strip() or ("ok" if email else "not_found")
    return {
        "input": str(item.get("input") or fallback_input or "").strip(),
        "email": email,
        "status": status,
        "risk_score": str((data or {}).get("risk_score") or "").strip(),
        "last_validated_at": str((data or {}).get("last_validated_at") or "").strip(),
    }


def _parse_results(payload: dict[str, Any], requested_urls: list[str]) -> list[dict[str, str]]:
    raw = payload.get("results")
    parsed: list[dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                parsed.append(_result_from_item(item))
    elif isinstance(raw, dict):
        fallback = requested_urls[0] if requested_urls else ""
        parsed.append(_result_from_item(raw, fallback_input=fallback))
        if not parsed[0]["status"]:
            parsed[0]["status"] = str(payload.get("status") or parsed[0]["status"])
        if not parsed[0]["email"] and payload.get("status") == "ok":
            parsed[0]["email"] = str(raw.get("email") or "").strip()
    return parsed


def lookup_linkedin_urls(urls: list[str], *, use_external_tokens: bool = True) -> list[dict[str, str]]:
    """
    Batch-lookup business emails for up to BATCH_SIZE LinkedIn URLs.
    Misses are free of record credits; successful finds consume fair-use records.
    """
    cleaned = [(u or "").strip() for u in urls if (u or "").strip()]
    if not cleaned:
        return []
    if len(cleaned) > BATCH_SIZE:
        raise MolsterError(f"Molster batch size cannot exceed {BATCH_SIZE}.")

    raise_if_fair_use_exhausted()

    body: dict[str, Any] = {
        "linkedin_urls": cleaned,
        "use_external_tokens": use_external_tokens,
    }
    last_error = "Molster request failed."

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(BASE_URL, json=body, headers=_headers(), timeout=REQUEST_TIMEOUT_SEC)
        except requests.RequestException as exc:
            last_error = f"Molster request failed: {exc}"
            logger.warning("Molster POST attempt %s failed: %s", attempt, exc)
            time.sleep(min(30, 5 * attempt))
            continue

        if resp.status_code == 401:
            raise MolsterError("Invalid MOLSTER_API_KEY.")

        if resp.status_code == 429:
            retry_ts = _retry_after_ts(resp)
            wait = max(1, retry_ts - time.time())
            remaining = remaining_records()
            if remaining == 0 or wait >= 120:
                wait_min = max(1, int(wait / 60))
                raise MolsterFairUseExhausted(
                    f"Molster rate/fair-use limit reached. Resuming in ~{wait_min} min.",
                    retry_after_ts=retry_ts,
                )
            logger.warning("Molster HTTP 429; sleeping %.0fs then retrying", wait)
            time.sleep(wait)
            continue

        if resp.status_code == 404:
            # Single-style not-found; treat the whole requested set as misses.
            return [
                {
                    "input": url,
                    "email": "",
                    "status": "not_found",
                    "risk_score": "",
                    "last_validated_at": "",
                }
                for url in cleaned
            ]

        if not resp.ok:
            if _is_transient_http(resp.status_code):
                last_error = f"Molster returned HTTP {resp.status_code}"
                logger.warning("Molster POST attempt %s transient: HTTP %s", attempt, resp.status_code)
                time.sleep(min(30, 5 * attempt))
                continue
            detail = _error_message(resp) or f"Molster returned HTTP {resp.status_code}"
            raise MolsterError(detail)

        try:
            payload = resp.json()
        except ValueError:
            last_error = "Molster returned a non-JSON response."
            time.sleep(min(30, 5 * attempt))
            continue

        if not isinstance(payload, dict):
            last_error = "Molster returned an unexpected payload."
            time.sleep(min(30, 5 * attempt))
            continue

        fair_use = (payload.get("metadata") or {}).get("fair_use") if isinstance(payload.get("metadata"), dict) else None
        if isinstance(fair_use, dict):
            save_fair_use(fair_use)
            logger.info(
                "Molster fair-use: remaining_5h=%s remaining_1w=%s",
                fair_use.get("records_remaining_5h"),
                fair_use.get("records_remaining_1w"),
            )

        parsed = _parse_results(payload, cleaned)
        by_key: dict[str, dict[str, str]] = {}
        for item in parsed:
            key = linkedin_match_key(item.get("input") or "")
            if key:
                by_key[key] = item

        output: list[dict[str, str]] = []
        for url in cleaned:
            key = linkedin_match_key(url)
            matched = by_key.get(key)
            if matched:
                row = dict(matched)
                row["input"] = url
                output.append(row)
            else:
                output.append(
                    {
                        "input": url,
                        "email": "",
                        "status": "not_found",
                        "risk_score": "",
                        "last_validated_at": "",
                    }
                )
        found = sum(1 for row in output if row.get("email"))
        logger.info("Molster batch: %s urls, %s emails found", len(cleaned), found)
        return output

    raise MolsterError(last_error, transient=True)
