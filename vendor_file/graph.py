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
