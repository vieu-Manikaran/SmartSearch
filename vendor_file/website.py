"""Turn LinkedIn/API website fields into a company homepage URL."""

from __future__ import annotations

import re
from typing import Any, List, Optional
from urllib.parse import urlparse

CAREER_HOST_LABELS = {
    "career",
    "careers",
    "job",
    "jobs",
    "join",
    "joinus",
    "recruit",
    "recruiting",
    "recruitment",
    "vacancies",
    "vacature",
    "vacatures",
    "werkenbij",
    "karriere",
    "emploi",
    "emplois",
}

ATS_REGISTRABLE = {
    "applytojob.com",
    "ashbyhq.com",
    "bamboohr.com",
    "brassring.com",
    "comeet.com",
    "greenhouse.io",
    "icims.com",
    "jobvite.com",
    "lever.co",
    "myworkday.com",
    "myworkdayjobs.com",
    "recruitee.com",
    "smartrecruiters.com",
    "successfactors.com",
    "taleo.net",
    "teamtailor.com",
    "workable.com",
}

SECOND_LEVEL_TLDS = {
    ("ac", "uk"),
    ("co", "id"),
    ("co", "il"),
    ("co", "in"),
    ("co", "jp"),
    ("co", "kr"),
    ("co", "nz"),
    ("co", "th"),
    ("co", "uk"),
    ("co", "za"),
    ("com", "ar"),
    ("com", "au"),
    ("com", "br"),
    ("com", "cn"),
    ("com", "hk"),
    ("com", "mx"),
    ("com", "sg"),
    ("com", "tr"),
    ("net", "uk"),
    ("org", "uk"),
}

CAREER_PATH = re.compile(
    r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?"
    r"(?:careers?|jobs?|vacancies|vacatures|recruit(?:ing|ment)?|"
    r"join[-_]?us|werkenbij|karriere|emplois?)"
    r"(?:/|$)",
    re.I,
)


def _ensure_http(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return "https:" + text
    if not re.match(r"^https?://", text, re.I):
        return "https://" + text.lstrip("/")
    return text


def registrable_domain(host: str) -> str:
    labels = [p for p in (host or "").lower().split(".") if p]
    if len(labels) < 2:
        return ".".join(labels)
    if len(labels) >= 3 and tuple(labels[-2:]) in SECOND_LEVEL_TLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def canonicalize_website(raw: str) -> str:
    """Return the company homepage, not a careers/jobs/ATS URL.

    careers.walmart.com/us/en/home → https://www.walmart.com
    www.careers.frieslandcampina.com → https://www.frieslandcampina.com
    """
    text = (raw or "").strip()
    if not text:
        return ""
    parsed = urlparse(_ensure_http(text))
    host = (parsed.netloc or "").split("@")[-1]
    host = host.split(":")[0].lower().rstrip(".")
    if host.startswith("www."):
        host_no_www = host[4:]
    else:
        host_no_www = host
    if not host or "linkedin.com" in host:
        return ""

    registrable = registrable_domain(host_no_www)
    if not registrable or registrable in ATS_REGISTRABLE:
        return ""

    labels = [p for p in host.split(".") if p]
    registrable_labels = set(registrable.split("."))
    career_host = any(
        label in CAREER_HOST_LABELS and label not in registrable_labels
        for label in labels
    )
    career_path = bool(CAREER_PATH.match(parsed.path or "/"))
    if career_host or career_path:
        return f"https://www.{registrable}"
    return f"https://{host}"


JUNK_DOMAIN_LABELS = CAREER_HOST_LABELS | {
    "news",
    "blog",
    "blogs",
    "press",
    "ir",
    "investor",
    "investors",
    "go",
    "lnk",
}
SKIP_REGISTRABLE = ATS_REGISTRABLE | {"bit.ly", "lnkd.in", "t.co", "ow.ly"}


def website_from_email_domains(
    email_domain: str = "",
    email_domains: Optional[list] = None,
) -> str:
    """Homepage URL from company.email_domains (not website_url)."""
    ordered: List[str] = []
    candidates: List[Any] = [email_domain]
    if isinstance(email_domains, (list, tuple, set)):
        candidates.extend(email_domains)
    elif email_domains:
        text = str(email_domains).strip()
        if text.startswith("{") and text.endswith("}"):
            candidates.extend(
                part.strip().strip('"').strip("'")
                for part in text[1:-1].split(",")
                if part.strip()
            )
        else:
            candidates.append(text)
    for raw in candidates:
        text = str(raw or "").strip().lower().lstrip("@")
        if text and text not in ordered:
            ordered.append(text)
    cleaned: List[str] = []
    seen: set[str] = set()
    for raw in ordered:
        site = canonicalize_website(raw if "://" in raw else f"https://{raw}")
        if not site:
            continue
        host = urlparse(site).netloc.lower()
        host_no_www = host[4:] if host.startswith("www.") else host
        registrable = registrable_domain(host_no_www)
        if not registrable or registrable in SKIP_REGISTRABLE:
            continue
        labels = [p for p in host_no_www.split(".") if p]
        junk_sub = any(
            label in JUNK_DOMAIN_LABELS and label not in registrable.split(".")
            for label in labels
        )
        if junk_sub:
            rewritten = f"https://www.{registrable}"
            if rewritten not in seen:
                seen.add(rewritten)
                cleaned.append(rewritten)
            continue
        if site not in seen:
            seen.add(site)
            cleaned.append(site)
    return cleaned[0] if cleaned else ""
