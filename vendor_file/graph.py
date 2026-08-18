"""Resolve Vieu IDs (PERS-… / COMP-…) from the Seeqe graph (Postgres).

Indexed URL lookups only — never a company_name scan. Misses stay blank.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from config import settings
from vendor_file.urls import canonicalize_company_url, canonicalize_person_url

logger = logging.getLogger(__name__)

BATCH = 500
NUMERIC_SLUG = re.compile(r"^\d{2,12}$")


def graph_configured() -> bool:
    return bool(settings.postgres_host and settings.postgres_db and settings.postgres_user)


def lookup_key(url: str) -> str:
    """Stable key for a person or company LinkedIn URL."""
    person = canonicalize_person_url(url)
    if person.ok:
        slug = unquote(urlparse(person.url).path.rstrip("/").split("/")[-1]).lower()
        return f"person:{slug}" if slug else ""
    company = canonicalize_company_url(url)
    if company.ok:
        slug = unquote(urlparse(company.url).path.rstrip("/").split("/")[-1]).lower()
        return f"company:{slug}" if slug else ""
    return ""


def company_numeric_id(url: str) -> Optional[int]:
    company = canonicalize_company_url(url)
    if not company.ok:
        return None
    slug = unquote(urlparse(company.url).path.rstrip("/").split("/")[-1])
    if NUMERIC_SLUG.fullmatch(slug):
        return int(slug)
    return None


def _url_variants(url: str) -> List[str]:
    """Slash / host / company|school variants that may be stored in graph."""
    raw = unquote((url or "").strip())
    if not raw:
        return []
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw.lstrip("/")
    parsed = urlparse(raw)
    path = re.sub(r"/+", "/", parsed.path or "").rstrip("/")
    if not path:
        return []
    bases = {path}
    if "/company/" in path:
        bases.add(path.replace("/company/", "/school/", 1))
    if "/school/" in path:
        bases.add(path.replace("/school/", "/company/", 1))
    hosts = (
        "https://www.linkedin.com",
        "https://linkedin.com",
        "http://www.linkedin.com",
        "http://linkedin.com",
    )
    out: List[str] = []
    seen: set[str] = set()
    for host in hosts:
        for p in bases:
            for candidate in (f"{host}{p}", f"{host}{p}/"):
                if candidate not in seen:
                    seen.add(candidate)
                    out.append(candidate)
    return out


def _get_connection():
    import psycopg2

    return psycopg2.connect(
        host=settings.postgres_host,
        port=settings.postgres_port or "5432",
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
        connect_timeout=30,
        sslmode="require",
    )


def _followers(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _keep_better(current: Optional[Tuple[str, int]], vieu_id: str, followers: int) -> Tuple[str, int]:
    if current is None:
        return vieu_id, followers
    if followers > current[1]:
        return vieu_id, followers
    if followers == current[1] and vieu_id and not current[0]:
        return vieu_id, followers
    return current


def _index_company(
    best: Dict[str, Tuple[str, int]],
    *,
    vieu_id: str,
    db_url: str,
    linked_in_id: Any,
    followers: int,
) -> None:
    keys = set()
    key = lookup_key(db_url)
    if key:
        keys.add(key)
    if linked_in_id is not None and str(linked_in_id).strip():
        try:
            keys.add(f"company:{int(linked_in_id)}")
        except (TypeError, ValueError):
            pass
    for k in keys:
        best[k] = _keep_better(best.get(k), vieu_id, followers)


def _chunks(items: Sequence[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def resolve_person_vieu_ids(urls: Sequence[str]) -> Dict[str, str]:
    """Map canonical person LinkedIn URL → PERS-… Vieu ID."""
    if not urls or not graph_configured():
        if urls and not graph_configured():
            logger.warning("Graph person lookup skipped: POSTGRES_* is not set")
        return {}

    lookup: List[str] = []
    seen: set[str] = set()
    input_keys: Dict[str, str] = {}
    for url in urls:
        key = lookup_key(url)
        if key:
            input_keys[url] = key
        for variant in _url_variants(url):
            if variant not in seen:
                seen.add(variant)
                lookup.append(variant)
    if not lookup:
        return {}

    best: Dict[str, Tuple[str, int]] = {}
    try:
        conn = _get_connection()
    except Exception:
        logger.exception("Graph person lookup: connection failed")
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '60s'")
            for chunk in _chunks(lookup, BATCH):
                cur.execute(
                    "SELECT id, linked_in_url FROM person WHERE linked_in_url = ANY(%s)",
                    (chunk,),
                )
                for pid, db_url in cur.fetchall():
                    vieu_id = str(pid or "").strip()
                    key = lookup_key(str(db_url or ""))
                    if not vieu_id or not key:
                        continue
                    best[key] = _keep_better(best.get(key), vieu_id, 0)
    except Exception:
        logger.exception("Graph person lookup query failed")
        return {}
    finally:
        conn.close()

    return {url: best[key][0] for url, key in input_keys.items() if key in best and best[key][0]}


def resolve_company_vieu_ids(urls: Sequence[str]) -> Dict[str, str]:
    """Map canonical company LinkedIn URL → COMP-… Vieu ID."""
    if not urls or not graph_configured():
        if urls and not graph_configured():
            logger.warning("Graph company lookup skipped: POSTGRES_* is not set")
        return {}

    lookup: List[str] = []
    seen: set[str] = set()
    input_keys: Dict[str, str] = {}
    numeric_ids: List[int] = []
    numeric_seen: set[int] = set()
    for url in urls:
        key = lookup_key(url)
        if key:
            input_keys[url] = key
        for variant in _url_variants(url):
            if variant not in seen:
                seen.add(variant)
                lookup.append(variant)
        nid = company_numeric_id(url)
        if nid is not None and nid not in numeric_seen:
            numeric_seen.add(nid)
            numeric_ids.append(nid)
    if not lookup and not numeric_ids:
        return {}

    best: Dict[str, Tuple[str, int]] = {}
    try:
        conn = _get_connection()
    except Exception:
        logger.exception("Graph company lookup: connection failed")
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '60s'")
            for chunk in _chunks(lookup, BATCH):
                cur.execute(
                    """
                    SELECT id, linked_in_url, linked_in_id, linked_in_followers
                    FROM company
                    WHERE linked_in_url = ANY(%s)
                    """,
                    (chunk,),
                )
                for cid, db_url, linked_in_id, followers in cur.fetchall():
                    vieu_id = str(cid or "").strip()
                    if not vieu_id:
                        continue
                    _index_company(
                        best,
                        vieu_id=vieu_id,
                        db_url=str(db_url or ""),
                        linked_in_id=linked_in_id,
                        followers=_followers(followers),
                    )

            missed_numeric = [
                nid for nid in numeric_ids if f"company:{nid}" not in best
            ]
            if missed_numeric:
                cur.execute(
                    """
                    SELECT id, linked_in_url, linked_in_id, linked_in_followers
                    FROM company
                    WHERE linked_in_id = ANY(%s)
                    """,
                    (missed_numeric,),
                )
                for cid, db_url, linked_in_id, followers in cur.fetchall():
                    vieu_id = str(cid or "").strip()
                    if not vieu_id:
                        continue
                    _index_company(
                        best,
                        vieu_id=vieu_id,
                        db_url=str(db_url or ""),
                        linked_in_id=linked_in_id,
                        followers=_followers(followers),
                    )
    except Exception:
        logger.exception("Graph company lookup query failed")
        return {}
    finally:
        conn.close()

    return {url: best[key][0] for url, key in input_keys.items() if key in best and best[key][0]}


def _to_date(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "date") and not isinstance(value, str):
        try:
            return value.date()
        except Exception:
            return value
    return value


def _format_hq(city: Any, country: Any) -> str:
    city_s = str(city or "").strip()
    country_s = str(country or "").strip()
    if city_s and country_s:
        return f"{city_s}, {country_s}"
    return city_s or country_s


def _headcount_str(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value).strip()


def _company_payload(row: Dict[str, Any]) -> Dict[str, str]:
    from vendor_file.website import website_from_email_domains

    cid = str(row.get("id") or "").strip()
    db_url = str(row.get("linked_in_url") or "").strip()
    canon = canonicalize_company_url(db_url)
    return {
        "id": cid,
        "name": str(row.get("company_name") or "").strip(),
        "linkedin": canon.url if canon.ok else db_url,
        "website": website_from_email_domains(
            str(row.get("email_domain") or ""),
            list(row.get("email_domains") or []),
        ),
        "headcount": _headcount_str(row.get("linked_in_employees")),
        "hq": _format_hq(row.get("hq_city"), row.get("hq_country")),
        "followers": str(_followers(row.get("linked_in_followers"))),
    }


def _index_company_payload(
    best: Dict[str, Tuple[Dict[str, str], int]],
    row: Dict[str, Any],
) -> None:
    payload = _company_payload(row)
    if not payload.get("id"):
        return
    followers = _followers(row.get("linked_in_followers"))
    keys = set()
    key = lookup_key(str(row.get("linked_in_url") or ""))
    if key:
        keys.add(key)
    linked_in_id = row.get("linked_in_id")
    if linked_in_id is not None and str(linked_in_id).strip():
        try:
            keys.add(f"company:{int(linked_in_id)}")
        except (TypeError, ValueError):
            pass
    for k in keys:
        prev = best.get(k)
        if prev is None or followers > prev[1] or (
            followers == prev[1] and payload["id"] and not prev[0].get("id")
        ):
            best[k] = (payload, followers)


COMPANY_SELECT = """
    SELECT id, company_name, linked_in_url, linked_in_id, linked_in_followers,
           linked_in_employees, hq_city, hq_country, email_domain, email_domains
    FROM company
"""


class GraphClient:
    """One Postgres session for batched vendor-file graph enrichment."""

    def __init__(self) -> None:
        self._conn = None

    def __enter__(self) -> "GraphClient":
        if not graph_configured():
            raise RuntimeError("POSTGRES_* is not set")
        self._conn = _get_connection()
        with self._conn.cursor() as cur:
            cur.execute("SET statement_timeout = '60s'")
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _cursor(self):
        from psycopg2.extras import RealDictCursor

        return self._conn.cursor(cursor_factory=RealDictCursor)

    def fetch_people(self, urls: Sequence[str]) -> Dict[str, Dict[str, str]]:
        lookup: List[str] = []
        seen: set[str] = set()
        input_keys: Dict[str, str] = {}
        for url in urls:
            key = lookup_key(url)
            if key:
                input_keys[url] = key
            for variant in _url_variants(url):
                if variant not in seen:
                    seen.add(variant)
                    lookup.append(variant)
        if not lookup:
            return {}
        best: Dict[str, Dict[str, str]] = {}
        with self._cursor() as cur:
            for chunk in _chunks(lookup, BATCH):
                cur.execute(
                    """
                    SELECT id, person_name, loc, loc_country_code, linked_in_url, updated_at
                    FROM person
                    WHERE linked_in_url = ANY(%s)
                    """,
                    (chunk,),
                )
                for row in cur.fetchall():
                    key = lookup_key(str(row.get("linked_in_url") or ""))
                    if not key:
                        continue
                    payload = {
                        "id": str(row.get("id") or "").strip(),
                        "name": str(row.get("person_name") or "").strip(),
                        "loc": str(row.get("loc") or "").strip(),
                        "country": str(row.get("loc_country_code") or "").strip().upper(),
                        "linkedin": str(row.get("linked_in_url") or "").strip(),
                    }
                    if payload["id"]:
                        best[key] = payload
        return {
            url: best[key]
            for url, key in input_keys.items()
            if key in best
        }

    def fetch_companies(self, urls: Sequence[str]) -> Dict[str, Dict[str, str]]:
        lookup: List[str] = []
        seen: set[str] = set()
        input_keys: Dict[str, str] = {}
        numeric_ids: List[int] = []
        numeric_seen: set[int] = set()
        for url in urls:
            key = lookup_key(url)
            if key:
                input_keys[url] = key
            for variant in _url_variants(url):
                if variant not in seen:
                    seen.add(variant)
                    lookup.append(variant)
            nid = company_numeric_id(url)
            if nid is not None and nid not in numeric_seen:
                numeric_seen.add(nid)
                numeric_ids.append(nid)
        best: Dict[str, Tuple[Dict[str, str], int]] = {}
        with self._cursor() as cur:
            for chunk in _chunks(lookup, BATCH):
                cur.execute(COMPANY_SELECT + " WHERE linked_in_url = ANY(%s)", (chunk,))
                for row in cur.fetchall():
                    _index_company_payload(best, dict(row))
            missed = [nid for nid in numeric_ids if f"company:{nid}" not in best]
            if missed:
                cur.execute(COMPANY_SELECT + " WHERE linked_in_id = ANY(%s)", (missed,))
                for row in cur.fetchall():
                    _index_company_payload(best, dict(row))
        return {
            url: best[key][0]
            for url, key in input_keys.items()
            if key in best
        }

    def fetch_companies_by_ids(self, ids: Sequence[str]) -> Dict[str, Dict[str, str]]:
        wanted = [i for i in {str(x).strip() for x in ids} if i]
        if not wanted:
            return {}
        out: Dict[str, Dict[str, str]] = {}
        with self._cursor() as cur:
            for chunk in _chunks(wanted, BATCH):
                cur.execute(COMPANY_SELECT + " WHERE id = ANY(%s)", (chunk,))
                for row in cur.fetchall():
                    payload = _company_payload(dict(row))
                    if payload["id"]:
                        out[payload["id"]] = payload
        return out

    def fetch_experiences(self, person_ids: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
        wanted = [i for i in {str(x).strip() for x in person_ids} if i]
        out: Dict[str, List[Dict[str, Any]]] = {pid: [] for pid in wanted}
        if not wanted:
            return out
        with self._cursor() as cur:
            for chunk in _chunks(wanted, 300):
                cur.execute(
                    """
                    SELECT e.person_id, e.company_id, e.title, e.dates_from, e.dates_to,
                           e.priority, e.updated_at, e.fallback_company_identifier,
                           c.company_name, c.linked_in_url AS company_url
                    FROM experience e
                    LEFT JOIN company c ON c.id = e.company_id
                    WHERE e.person_id = ANY(%s)
                    ORDER BY e.person_id, e.priority ASC NULLS LAST, e.dates_from DESC NULLS LAST
                    """,
                    (chunk,),
                )
                for row in cur.fetchall():
                    pid = str(row.get("person_id") or "").strip()
                    if pid not in out:
                        continue
                    curl = str(row.get("company_url") or "").strip()
                    canon = canonicalize_company_url(curl)
                    try:
                        pri = int(
                            str(row.get("priority") if row.get("priority") is not None else "9999")
                        )
                    except (TypeError, ValueError):
                        pri = 9999
                    out[pid].append(
                        {
                            "company_id": str(row.get("company_id") or "").strip(),
                            "title": str(row.get("title") or "").strip(),
                            "dates_from": _to_date(row.get("dates_from")),
                            "dates_to": _to_date(row.get("dates_to")),
                            "priority": pri,
                            "updated_at": row.get("updated_at"),
                            "fallback_company_identifier": str(
                                row.get("fallback_company_identifier") or ""
                            ).strip(),
                            "company_name": str(row.get("company_name") or "").strip(),
                            "company_url": canon.url if canon.ok else curl,
                        }
                    )
        return out

    def fetch_headcount_at_years(
        self, pairs: Sequence[Tuple[str, str]]
    ) -> Dict[Tuple[str, str], str]:
        """(company_id, YYYY) → employee_ct. Only 19xx/20xx years."""
        year_re = re.compile(r"^(19|20)\d{2}$")
        wanted = {
            (cid.strip(), year)
            for cid, year in pairs
            if cid and year and year_re.fullmatch(year)
        }
        if not wanted:
            return {}
        company_ids = sorted({cid for cid, _year in wanted})
        years = sorted({year for _cid, year in wanted})
        found: Dict[Tuple[str, str], str] = {}
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT company_id, dt_year, employee_ct
                FROM company_history_employee_ct
                WHERE company_id = ANY(%s) AND dt_year = ANY(%s)
                """,
                (company_ids, years),
            )
            for row in cur.fetchall():
                cid = str(row.get("company_id") or "").strip()
                year = str(row.get("dt_year") or "").strip()
                if (cid, year) not in wanted:
                    continue
                found[(cid, year)] = _headcount_str(row.get("employee_ct"))
        return found
