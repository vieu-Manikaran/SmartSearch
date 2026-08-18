"""Canonicalize person and company LinkedIn URLs to the vendor shapes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, unquote, urlparse


PERSON_JUNK_SEGMENTS = {
    "en",
    "es",
    "fr",
    "de",
    "pt",
    "it",
    "nl",
    "sv",
    "da",
    "fi",
    "no",
    "pl",
    "tr",
    "ru",
    "ja",
    "ko",
    "zh",
    "overlay",
    "details",
    "recent-activity",
    "edit",
    "about",
    "experience",
    "skills",
    "education",
    "featured",
    "interests",
}

COMPANY_JUNK_SEGMENTS = {
    "about",
    "people",
    "jobs",
    "life",
    "insights",
    "posts",
    "videos",
    "admin",
    "mycompany",
    "affiliates",
}


@dataclass(frozen=True)
class UrlResult:
    url: str
    ok: bool
    reason: str = ""


def _clean_raw(raw: str) -> str:
    text = (raw or "").strip()
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[\s\n\r]+", "", text)
    return text


def _ensure_http(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if not re.match(r"^https?://", url, re.I):
        return "https://" + url.lstrip("/")
    return url


def _linkedin_host(netloc: str) -> bool:
    host = (netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _quote_slug(slug: str) -> str:
    return quote(slug, safe="-._~")


def canonicalize_person_url(raw: str) -> UrlResult:
    """Return https://www.linkedin.com/in/{slug} or an error."""
    text = _clean_raw(raw)
    if not text:
        return UrlResult("", False, "Person LinkedIn is empty")

    url = _ensure_http(text)
    parsed = urlparse(url)
    if not _linkedin_host(parsed.netloc):
        return UrlResult("", False, "Person LinkedIn is not a linkedin.com URL")

    path = unquote(parsed.path or "")
    path = re.sub(r"/+", "/", path)
    lower = path.lower()

    if "/company/" in lower or "/school/" in lower or "/showcase/" in lower:
        return UrlResult("", False, "Person LinkedIn looks like a company page")
    if "/sales/" in lower:
        return UrlResult(
            "",
            False,
            "Sales Navigator person URLs cannot be converted to /in/{slug}",
        )

    match = re.search(r"/(?:mwlite/)?in/(?:in/)*([^/]+)", path, re.I)
    if not match:
        return UrlResult("", False, "Person LinkedIn has no /in/{slug}")

    slug = match.group(1).strip().strip("/")
    if not slug or slug.lower() in PERSON_JUNK_SEGMENTS:
        return UrlResult("", False, "Person LinkedIn slug is empty")
    if re.fullmatch(r"[A-Za-z]{2}", slug) and slug.lower() in PERSON_JUNK_SEGMENTS:
        return UrlResult("", False, "Person LinkedIn slug looks like a locale code")

    return UrlResult(f"https://www.linkedin.com/in/{_quote_slug(slug)}", True)


def canonicalize_company_url(raw: str) -> UrlResult:
    """Return https://www.linkedin.com/company/{slug} or an error."""
    text = _clean_raw(raw)
    if not text:
        return UrlResult("", False, "Company LinkedIn is empty")

    url = _ensure_http(text)
    parsed = urlparse(url)
    if not _linkedin_host(parsed.netloc):
        return UrlResult("", False, "Company LinkedIn is not a linkedin.com URL")

    path = unquote(parsed.path or "")
    path = re.sub(r"/+", "/", path)
    lower = path.lower()

    if re.search(r"/(?:mwlite/)?in/", lower):
        return UrlResult("", False, "Company LinkedIn looks like a person profile")

    slug: Optional[str] = None
    match = re.search(r"/sales/company/([^/]+)", path, re.I)
    if match:
        slug = match.group(1)
    if slug is None:
        match = re.search(r"/(?:company|school|showcase)/([^/]+)", path, re.I)
        if match:
            slug = match.group(1)

    if not slug:
        return UrlResult("", False, "Company LinkedIn has no /company/{slug}")

    slug = slug.strip().strip("/")
    if not slug or slug.lower() in COMPANY_JUNK_SEGMENTS:
        return UrlResult("", False, "Company LinkedIn slug is empty")

    return UrlResult(f"https://www.linkedin.com/company/{_quote_slug(slug)}", True)


def person_slug(url: str) -> str:
    result = canonicalize_person_url(url)
    if not result.ok:
        return ""
    return unquote(urlparse(result.url).path.rstrip("/").split("/")[-1]).lower()


def company_slug(url: str) -> str:
    result = canonicalize_company_url(url)
    if not result.ok:
        return ""
    return unquote(urlparse(result.url).path.rstrip("/").split("/")[-1]).lower()
