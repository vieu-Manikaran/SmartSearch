"""Turn LinkedIn/API website fields into a company homepage URL."""

from __future__ import annotations

import re
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
