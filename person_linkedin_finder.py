"""Resolve person LinkedIn profile URLs via Serper (scored) with RapidAPI fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass

from config import settings
from rapidapi_search_people import person_navigation_url, search_people_with_filters
from serper_search import (
    MIN_SCORE_FOUND,
    MIN_SCORE_LOW,
    _company_search_term,
    _first_name_matches,
    _is_invalid_person_input,
    _last_name_matches,
    _person_name_parts,
    _score_person_result,
    _slug_from_url,
    _slug_tokens,
    _token_in_text,
    find_linkedin_person_match,
    search_serper,
    LINKEDIN_PERSON_PATH,
)


@dataclass
class PersonLinkedInResult:
    url: str | None
    status: str
    score: int
    search_query: str
    source: str = ""


def _company_tokens(company: str) -> list[str]:
    term = _company_search_term(company)
    return [t for t in re.findall(r"[a-z0-9]+", term.casefold()) if len(t) >= 3]


def _score_rapidapi_person(
    first: str,
    last: str,
    alternates: set[str],
    company: str,
    person: dict,
    *,
    single_result: bool = False,
) -> tuple[int, str]:
    full_name = person.get("fullName") if isinstance(person.get("fullName"), str) else ""
    subtitle = " ".join(
        x
        for x in (
            person.get("primarySubtitle"),
            person.get("secondarySubtitle"),
        )
        if isinstance(x, str)
    )
    url = person_navigation_url(person)
    if not url or LINKEDIN_PERSON_PATH not in url.casefold():
        return 0, ""

    slug = _slug_from_url(url)
    slug_tokens = _slug_tokens(slug)
    first_ok = _first_name_matches(first, alternates, slug_tokens, full_name)
    last_in_name = _last_name_matches(last, slug_tokens, full_name)
    last_in_slug = _last_name_matches(last, slug_tokens, slug.replace("-", " "))

    if not first_ok:
        return 0, ""
    if not last_in_name and not last_in_slug:
        if not (single_result and _token_in_text(first, full_name)):
            return 0, ""

    score = 8
    if _token_in_text(first, full_name) and (_token_in_text(last, full_name) or last_in_slug):
        score += 3
    elif single_result:
        score += 2
    if _first_name_matches(first, alternates, slug_tokens, slug.replace("-", " ")):
        score += 2
    if last_in_slug:
        score += 2
    for token in _company_tokens(company):
        if token in subtitle.casefold() or token in full_name.casefold():
            score += 1
            break
    return score, url


def _serper_loose_match(person: str, company: str, api_key: str) -> PersonLinkedInResult:
    company_term = _company_search_term(company)
    queries = [
        f"{person} {company} site:linkedin.com",
        f'"{person}" {company} site:linkedin.com/in',
    ]
    first, last, alternates = _person_name_parts(person)
    best: tuple[int, int, str, str] | None = None

    for query in queries:
        items = search_serper(query, api_key, num=10, date_restrict=None, gl="us", page=1)
        needle = LINKEDIN_PERSON_PATH.casefold()
        for rank, item in enumerate(items):
            link = item.get("link") if isinstance(item.get("link"), str) else None
            if not link or needle not in link.casefold():
                continue
            title = item.get("title") if isinstance(item.get("title"), str) else ""
            snippet = item.get("snippet") if isinstance(item.get("snippet"), str) else ""
            score = _score_person_result(first, last, alternates, company_term, link, title, snippet)
            if score <= 0:
                continue
            candidate = (score, rank, link, query)
            if best is None or (candidate[0], -candidate[1]) > (best[0], -best[1]):
                best = candidate

    if not best:
        return PersonLinkedInResult(None, "no_profile_in_top_10", 0, queries[0], "serper_loose")

    score, _, url, query = best
    if score >= MIN_SCORE_FOUND:
        return PersonLinkedInResult(url, "found", score, query, "serper_loose")
    if score >= MIN_SCORE_LOW:
        return PersonLinkedInResult(url, "low_confidence", score, query, "serper_loose")
    return PersonLinkedInResult(None, "no_profile_in_top_10", score, query, "serper_loose")


def _rapidapi_match(person: str, company: str) -> PersonLinkedInResult:
    first, last, alternates = _person_name_parts(person)
    if not first or not last:
        return PersonLinkedInResult(None, "no_profile_in_top_10", 0, "", "rapidapi")

    people = search_people_with_filters(first, last, company)
    single = len(people) == 1
    best: tuple[int, str] | None = None
    for item in people:
        score, url = _score_rapidapi_person(first, last, alternates, company, item, single_result=single)
        if score <= 0:
            continue
        if best is None or score > best[0]:
            best = (score, url)

    if not best:
        return PersonLinkedInResult(
            None,
            "no_profile_in_top_10",
            0,
            f'rapidapi search_people_with_filters "{first}" "{last}" @ {company}',
            "rapidapi",
        )

    score, url = best
    query = f'rapidapi: {first} {last} @ {company}'
    if score >= MIN_SCORE_FOUND:
        return PersonLinkedInResult(url, "found", score, query, "rapidapi")
    if score >= MIN_SCORE_LOW:
        return PersonLinkedInResult(url, "low_confidence", score, query, "rapidapi")
    return PersonLinkedInResult(None, "no_profile_in_top_10", score, query, "rapidapi")


def find_person_linkedin(
    person_name: str,
    company_name: str,
    *,
    serper_api_key: str | None = None,
    use_rapidapi_fallback: bool = True,
) -> PersonLinkedInResult:
    person = person_name.strip()
    company = company_name.strip()
    if not person or not company:
        return PersonLinkedInResult(None, "no_profile_in_top_10", 0, "", "")
    if _is_invalid_person_input(person):
        return PersonLinkedInResult(None, "invalid_input", 0, "", "")

    api_key = serper_api_key or settings.serper_api_key or ""
    best_low: PersonLinkedInResult | None = None

    if api_key:
        strict = find_linkedin_person_match(person, company, api_key, num=10, date_restrict=None)
        if strict.status == "found":
            return PersonLinkedInResult(strict.url, strict.status, strict.score, strict.search_query, "serper_strict")
        if strict.status == "low_confidence" and strict.url:
            best_low = PersonLinkedInResult(strict.url, strict.status, strict.score, strict.search_query, "serper_strict")

    if use_rapidapi_fallback and (settings.rapidapi_key or settings.rapidapi_key2):
        rapid = _rapidapi_match(person, company)
        if rapid.status == "found":
            return rapid
        if rapid.status == "low_confidence" and rapid.url:
            if best_low is None or rapid.score > best_low.score:
                best_low = rapid

    if best_low:
        return best_low

    return PersonLinkedInResult(None, "no_profile_in_top_10", 0, "", "")
