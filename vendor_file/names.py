"""Split full names into first / middle / last without stripping Unicode."""

from __future__ import annotations

import re
from typing import Tuple

NAME_SUFFIXES = {
    "mba",
    "msc",
    "msc.",
    "ms",
    "ms.",
    "phd",
    "phd.",
    "cfa",
    "cpa",
    "frsa",
    "obe",
    "mbe",
    "cbe",
    "jr",
    "jr.",
    "sr",
    "sr.",
    "ii",
    "iii",
    "iv",
    "esq",
    "esq.",
}


def _strip_credentials(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if "," in text:
        left, right = text.rsplit(",", 1)
        if right.strip().lower().rstrip(".") in NAME_SUFFIXES or len(right.strip()) <= 6:
            text = left.strip()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_name(full_name: str) -> Tuple[str, str, str]:
    raw = _strip_credentials(full_name)
    parts = [p for p in raw.replace(",", " ").split() if p]
    while parts and parts[-1].lower().rstrip(".") in NAME_SUFFIXES:
        parts.pop()
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return parts[0], " ".join(parts[1:-1]), parts[-1]


def names_from_profile(
    full_name: str,
    first_name: str,
    last_name: str,
    fallback_full: str,
) -> Tuple[str, str, str, str]:
    """Return (full, first, middle, last). Prefer API name parts."""
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    full = (full_name or "").strip() or f"{first} {last}".strip()
    if not full:
        full = (fallback_full or "").strip()
    if first and last:
        middle = ""
        if full:
            remainder = full
            if remainder.lower().startswith(first.lower()):
                remainder = remainder[len(first) :].strip()
            if remainder.lower().endswith(last.lower()):
                remainder = remainder[: -len(last)].strip()
            middle = remainder.strip(" -")
        return full, first, middle, last
    parsed_first, parsed_middle, parsed_last = split_name(full or fallback_full)
    return (
        full or f"{parsed_first} {parsed_last}".strip(),
        first or parsed_first,
        parsed_middle,
        last or parsed_last,
    )
