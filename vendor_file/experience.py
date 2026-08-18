"""Parse LinkedIn experiences and match the target vs current company."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from vendor_file.urls import canonicalize_company_url, company_slug

COMPANY_STOPWORDS = {
    "the", "inc", "inc.", "llc", "ltd", "ltd.", "limited", "corp", "corp.",
    "corporation", "company", "companies", "co", "co.", "group", "holdings",
    "plc", "ag", "sa", "nv", "bv", "gmbh", "as", "a/s", "spa", "pte", "pvt",
    "of", "and", "&", "na", "n.a.", "systems", "system", "services", "service",
    "technologies", "technology", "solutions", "international", "global",
    "enterprises", "enterprise", "usa", "us", "america", "americas",
}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

URN_ID = re.compile(r"urn:li:(?:organization|fsd_company|company):(\d+)", re.I)


@dataclass
class Position:
    company: str
    title: str
    company_id: str = ""
    company_url: str = ""
    start: Optional[date] = None
    end: Optional[date] = None
    present: bool = False
    group: int = 0
    priority: int = 9999


@dataclass
class TargetMatch:
    title: str = ""
    start_date: str = ""
    start_title: str = ""
    matched: bool = False
    current_equals_target: bool = False


@dataclass
class CurrentRole:
    title: str = ""
    company: str = ""
    company_url: str = ""
    company_id: str = ""


def parse_token(tok: str) -> Optional[date]:
    tok = (tok or "").strip()
    if not tok:
        return None
    match = re.match(r"([A-Za-z]{3,})\.?\s+(\d{4})", tok)
    if match:
        mon = MONTHS.get(match.group(1)[:3].lower())
        if mon:
            return date(int(match.group(2)), mon, 1)
    match = re.match(r"(\d{4})$", tok)
    if match:
        return date(int(match.group(1)), 1, 1)
    return None


def parse_caption(caption: str) -> Tuple[Optional[date], Optional[date], bool]:
    caption = (caption or "").strip()
    left = re.split(r"·", caption)[0].strip()
    parts = re.split(r"\s+[-\u2013]\s+", left)
    start = parse_token(parts[0]) if parts else None
    if len(parts) > 1:
        end_tok = parts[1].strip()
        if re.search(r"present", end_tok, re.I):
            return start, None, True
        return start, parse_token(end_tok), False
    return start, None, True


def _company_from_subtitle(subtitle: str) -> str:
    subtitle = (subtitle or "").strip()
    return re.split(r"\s[·.]\s", subtitle)[0].strip()


def _id_from_exp(exp: Dict[str, Any]) -> str:
    cid = exp.get("companyId") or exp.get("CompanyID") or ""
    if cid:
        return str(cid)
    for key in ("companyUrn", "urn", "companyUrn1"):
        match = URN_ID.search(str(exp.get(key) or ""))
        if match:
            return match.group(1)
    return ""


def _url_from_exp(exp: Dict[str, Any]) -> str:
    for key in ("companyLink1", "companyUrl", "companyURL", "url"):
        val = exp.get(key)
        if isinstance(val, str) and val.strip():
            result = canonicalize_company_url(val)
            if result.ok:
                return result.url
    return ""


def extract_positions(experiences: List[Dict[str, Any]]) -> List[Position]:
    positions: List[Position] = []
    for gi, exp in enumerate(experiences or []):
        if not isinstance(exp, dict):
            continue
        cid = _id_from_exp(exp)
        curl = _url_from_exp(exp)
        if exp.get("breakdown"):
            company = (exp.get("title") or "").strip()
            for sub in exp.get("subComponents") or []:
                if not isinstance(sub, dict):
                    continue
                title = (sub.get("title") or "").strip()
                if not title:
                    continue
                start, end, present = parse_caption(sub.get("caption") or "")
                positions.append(
                    Position(
                        company=company,
                        title=title,
                        company_id=cid,
                        company_url=curl,
                        start=start,
                        end=end,
                        present=present,
                        group=gi,
                    )
                )
            continue
        company = _company_from_subtitle(exp.get("subtitle") or "")
        title = (exp.get("title") or "").strip()
        if not title and not company:
            continue
        start, end, present = parse_caption(exp.get("caption") or "")
        positions.append(
            Position(
                company=company,
                title=title,
                company_id=cid,
                company_url=curl,
                start=start,
                end=end,
                present=present,
                group=gi,
            )
        )
    return positions


def _norm_tokens(name: str) -> set:
    text = (name or "").lower()
    text = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return {t for t in text.split() if t and t not in COMPANY_STOPWORDS}


def company_name_matches(account: str, exp_company: str, slack: int = 2) -> bool:
    left, right = _norm_tokens(account), _norm_tokens(exp_company)
    if not left or not right:
        return False
    small, big = (left, right) if len(left) <= len(right) else (right, left)
    return small.issubset(big) and (len(big) - len(small)) <= slack


def _same_company(a: Position, b: Position) -> bool:
    if a.company_id and b.company_id:
        return a.company_id == b.company_id
    if a.company_url and b.company_url:
        return company_slug(a.company_url) == company_slug(b.company_url)
    return bool(a.company) and _norm_tokens(a.company) == _norm_tokens(b.company)


def _fmt(d: Optional[date]) -> str:
    return d.strftime("%Y-%m-%d") if d else ""


def match_target(
    positions: List[Position],
    *,
    target_name: str,
    target_url: str,
    target_company_id: str,
) -> List[Position]:
    target_slug = company_slug(target_url)
    hits: List[Position] = []
    for pos in positions:
        if target_company_id and pos.company_id and pos.company_id == str(target_company_id):
            hits.append(pos)
            continue
        if target_slug and pos.company_url and company_slug(pos.company_url) == target_slug:
            hits.append(pos)
            continue
        if target_name and company_name_matches(target_name, pos.company):
            hits.append(pos)
    return hits


def target_from_positions(
    positions: List[Position],
    *,
    target_name: str,
    target_url: str,
    target_company_id: str,
) -> TargetMatch:
    hits = match_target(
        positions,
        target_name=target_name,
        target_url=target_url,
        target_company_id=target_company_id,
    )
    if not hits:
        return TargetMatch()
    present = [p for p in hits if p.present]
    newest = max(
        present or hits,
        key=lambda p: p.start or date.min,
    )
    dated = [p for p in hits if p.start]
    earliest = min(dated, key=lambda p: p.start) if dated else newest
    return TargetMatch(
        title=newest.title,
        start_date=_fmt(earliest.start),
        start_title=earliest.title,
        matched=True,
        current_equals_target=any(p.present for p in hits),
    )


BOARD_OR_ADVISOR = re.compile(
    r"\b("
    r"board|trustee|trustees|advisor|adviser|advisory|"
    r"non[- ]?executive|independent director|non[- ]?exec"
    r")\b",
    re.I,
)


def is_board_or_advisor(title: str) -> bool:
    return bool(BOARD_OR_ADVISOR.search(title or ""))


def _priority_key(pos: Position) -> Tuple[int, int]:
    start_ord = pos.start.toordinal() if pos.start else 0
    return (pos.priority, -start_ord)


def pick_current_graph_role(
    positions: List[Position],
    target_hits: List[Position],
) -> Tuple[CurrentRole, bool]:
    """Current employer from graph experience.

    Skip board/advisor present roles when another present employer exists.
    If the only present role is board/advisor, keep it — even at the target.
    """
    present = [p for p in positions if p.present]
    if not present:
        return CurrentRole(), False
    employers = [p for p in present if not is_board_or_advisor(p.title)]
    pool = employers if employers else present
    chosen = min(pool, key=_priority_key)
    current_equals = any(_same_company(chosen, hit) for hit in target_hits)
    return _current_from_position(chosen), current_equals


def _current_from_position(pos: Position) -> CurrentRole:
    return CurrentRole(
        title=pos.title,
        company=pos.company,
        company_url=pos.company_url,
        company_id=pos.company_id,
    )


def current_from_positions(positions: List[Position]) -> CurrentRole:
    if not positions:
        return CurrentRole()
    present = [p for p in positions if p.present]
    if present:
        latest = max(present, key=lambda p: p.start or date.min)
    else:
        latest = max(positions, key=lambda p: p.start or date.min)
    return CurrentRole(
        title=latest.title,
        company=latest.company,
        company_url=latest.company_url,
        company_id=latest.company_id,
    )


def positions_from_graph_rows(rows: List[Dict[str, Any]]) -> List[Position]:
    """Turn experience ⟕ company rows into Position objects."""
    positions: List[Position] = []
    for gi, row in enumerate(rows or []):
        company = (
            str(row.get("company_name") or "").strip()
            or str(row.get("fallback_company_identifier") or "").strip()
        )
        title = str(row.get("title") or "").strip()
        if not title and not company:
            continue
        start = _coerce_date(row.get("dates_from"))
        end = _coerce_date(row.get("dates_to"))
        try:
            priority = int(row.get("priority") if row.get("priority") is not None else 9999)
        except (TypeError, ValueError):
            priority = 9999
        positions.append(
            Position(
                company=company,
                title=title,
                company_id=str(row.get("company_id") or "").strip(),
                company_url=str(row.get("company_url") or "").strip(),
                start=start,
                end=end,
                present=end is None,
                group=gi,
                priority=priority,
            )
        )
    return positions


def refresh_date_from_graph_rows(rows: List[Dict[str, Any]]) -> str:
    """MAX(experience.updated_at) as YYYY-MM-DD."""
    stamps: List[date] = []
    for row in rows or []:
        parsed = _coerce_date(row.get("updated_at"))
        if parsed:
            stamps.append(parsed)
    return max(stamps).isoformat() if stamps else ""


def _coerce_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) >= 10:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def location_from_person(data: Dict[str, Any]) -> str:
    for key in ("addressWithoutCountry", "addressWithCountry", "address", "addressCountryOnly"):
        val = data.get(key)
        if val and str(val).strip():
            return str(val).strip()
    return ""


def country_from_person(data: Dict[str, Any]) -> str:
    locale = data.get("primaryLocale") or {}
    if isinstance(locale, dict):
        code = str(locale.get("country") or "").strip()
        if len(code) == 2:
            return code.upper()
    country = str(data.get("addressCountryOnly") or "").strip()
    return country
