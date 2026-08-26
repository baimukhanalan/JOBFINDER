"""Persistence and deduplication for independent company discovery.

This store is intentionally separate from vacancy collection.  A source record is
identified by ``(source, source_external_id)`` and is reconciled *against* the
existing ``job_catalog`` only to decide whether it is new; it never writes to the
catalog.
"""
from __future__ import annotations

import os
import re
import threading
import unicodedata
from contextlib import contextmanager
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

try:  # Pure normalization/reconciliation helpers remain usable in lightweight jobs/tests.
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    from psycopg2.extras import Json
except ModuleNotFoundError:  # pragma: no cover - exercised implicitly at import time
    psycopg2 = None
    Json = None

_ENV = Path(__file__).resolve().parents[1] / ".env"
_pool = None
_lock = threading.Lock()

STATUSES = ("novel", "known", "possible_duplicate", "promoted")
_ATS_HOSTS = {
    "ashbyhq.com": "ashby",
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "myworkdayjobs.com": "workday",
    "smartrecruiters.com": "smartrecruiters",
    "icims.com": "icims",
    "oraclecloud.com": "oracle",
    "successfactors.com": "successfactors",
    "workable.com": "workable",
}
_COMPANY_SUFFIXES = {
    "co", "company", "corp", "corporation", "inc", "incorporated", "llc",
    "llp", "limited", "ltd", "plc",
}
_GENERIC_NAMES = {
    "american", "associates", "company", "consulting", "global", "group",
    "holdings", "international", "services", "solutions", "systems",
}
_MULTIPART_TLDS = {"co.uk", "com.au", "com.br", "com.mx", "co.nz", "co.jp", "co.in"}


def _dsn() -> str:
    dsn = os.environ.get("CRM_PG_DSN")
    if dsn:
        return dsn
    try:
        for line in _ENV.read_text().splitlines():
            if line.strip().startswith("CRM_PG_DSN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    raise RuntimeError("CRM_PG_DSN not set (backend/.env or environment)")


def _get_pool():
    global _pool
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is required for company discovery database access")
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=_dsn())
    return _pool


@contextmanager
def conn():
    pool = _get_pool()
    connection = pool.getconn()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        pool.putconn(connection)


@contextmanager
def _cur(dict_rows: bool = True):
    with conn() as connection:
        cursor = connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor if dict_rows else None)
        try:
            yield cursor
        finally:
            cursor.close()


def normalize_company_name(value: str | None) -> str:
    """Return a comparison key without treating punctuation/legal suffixes as identity."""
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch)).casefold()
    words = re.findall(r"[a-z0-9]+", value.replace("&", " and "))
    while words and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    if words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def normalize_domain(value: str | None) -> str:
    """Normalize a URL/domain to a registrable-looking host (without a network lookup)."""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else "//" + raw)
    host = (parsed.hostname or "").strip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    tail2 = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if tail2 in _MULTIPART_TLDS else tail2


def normalize_ats(value: str | None) -> str:
    value = re.sub(r"[^a-z0-9]+", "", (value or "").casefold())
    aliases = {"greenhouseio": "greenhouse", "ashbyhq": "ashby",
               "smartrecruiterscom": "smartrecruiters", "myworkdayjobs": "workday",
               "sap": "successfactors", "sapsuccessfactors": "successfactors"}
    return aliases.get(value, value)


def normalize_slug(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _external_ids(row: dict) -> dict[str, str]:
    ids = row.get("external_ids") or {}
    if isinstance(ids, dict):
        return {str(k).casefold(): str(v).casefold() for k, v in ids.items() if v is not None}
    return {}


def _catalog_url_identity(url: str | None) -> tuple[str, str, str]:
    """Extract (company domain, ATS, slug) from a catalog job URL.

    ATS hosts are deliberately not returned as company domains: otherwise every
    Greenhouse customer would appear to be the same company.
    """
    if not url:
        return "", "", ""
    parsed = urlsplit(url if "://" in url else "https://" + url)
    root = normalize_domain(parsed.hostname)
    ats = _ATS_HOSTS.get(root, "")
    parts = [p for p in parsed.path.split("/") if p]
    slug = parts[0] if ats and parts else ""
    if ats == "workday" and len(parts) > 1:
        slug = parts[0]
    return ("" if ats else root), ats, normalize_slug(slug)


def catalog_identity(row: dict) -> dict:
    """Convert a grouped job_catalog row to the identity shape used by reconciliation."""
    url_domain, url_ats, url_slug = _catalog_url_identity(row.get("url"))
    ats = normalize_ats(row.get("ats") or url_ats)
    slug = normalize_slug(row.get("company_key") or row.get("ats_slug") or url_slug)
    external_ids = dict(_external_ids(row))
    if ats and slug:
        external_ids.setdefault(ats, slug)
    return {
        "company_key": row.get("company_key"),
        "canonical_name": normalize_company_name(
            row.get("canonical_name") or row.get("company") or row.get("company_key")),
        "domain": normalize_domain(row.get("domain")) or url_domain,
        "ats": ats,
        "ats_slug": slug,
        "external_ids": external_ids,
    }


def _record_ats_identity(row: dict) -> tuple[str, str]:
    ats = normalize_ats(row.get("ats"))
    slug = normalize_slug(row.get("ats_slug"))
    for field in ("ats_url", "careers_url"):
        _domain, url_ats, url_slug = _catalog_url_identity(row.get(field))
        ats = ats or url_ats
        slug = slug or url_slug
        if ats and slug:
            break
    return ats, slug


def classify_record(record: dict, catalog_rows: list[dict]) -> dict:
    """Classify one discovered company against current catalog companies.

    IDs, official domains and ATS tenant identities are strong matches.  A name-only
    match is kept for review, never silently treated as known; this avoids collapsing
    unrelated businesses with common names such as "Global Solutions".
    """
    name = normalize_company_name(record.get("canonical_name") or record.get("trade_name")
                                  or record.get("legal_name") or record.get("company"))
    domain = normalize_domain(record.get("domain"))
    ats, slug = _record_ats_identity(record)
    external_ids = _external_ids(record)
    possible: tuple[dict, str] | None = None

    for raw in catalog_rows:
        known = catalog_identity(raw)
        known_ids = known["external_ids"]
        shared_namespaces = set(external_ids) & set(known_ids)
        if any(external_ids[k] == known_ids[k] for k in shared_namespaces):
            return _match("known", "external_id", known)
        if domain and known["domain"] and domain == known["domain"]:
            return _match("known", "domain", known)
        if ats and slug and ats == known["ats"] and slug == known["ats_slug"]:
            return _match("known", "ats_slug", known)

        known_name = known["canonical_name"]
        if name and known_name and name == known_name:
            possible = (known, "name_exact")
            continue
        if _plausibly_same_name(name, known_name):
            possible = possible or (known, "name_similar")

    if possible:
        return _match("possible_duplicate", possible[1], possible[0])
    return {"status": "novel", "match_reason": None,
            "matched_catalog_company_key": None}


def _plausibly_same_name(left: str, right: str) -> bool:
    if not left or not right or min(len(left), len(right)) < 7:
        return False
    left_words, right_words = set(left.split()), set(right.split())
    meaningful_left = left_words - _GENERIC_NAMES
    meaningful_right = right_words - _GENERIC_NAMES
    if not meaningful_left or not meaningful_right:
        return False
    return bool(meaningful_left & meaningful_right) and SequenceMatcher(
        None, left, right).ratio() >= 0.92


def _match(status: str, reason: str, known: dict) -> dict:
    return {"status": status, "match_reason": reason,
            "matched_catalog_company_key": known.get("company_key")}


def ensure_schema() -> None:
    with _cur(False) as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS company_discovery (
          id                    BIGSERIAL PRIMARY KEY,
          source                TEXT NOT NULL,
          source_external_id    TEXT NOT NULL,
          external_ids          JSONB NOT NULL DEFAULT '{}'::jsonb,
          source_url            TEXT,
          source_observed_at    TIMESTAMPTZ,
          legal_name            TEXT,
          trade_name            TEXT,
          canonical_name        TEXT NOT NULL,
          domain                TEXT,
          careers_url           TEXT,
          country               TEXT,
          states                TEXT[],
          industry              TEXT,
          naics                 TEXT,
          employee_size         TEXT,
          ats                   TEXT,
          ats_slug              TEXT,
          ats_url               TEXT,
          remote_supported      BOOLEAN,
          typical_roles         JSONB,
          discovery_confidence  DOUBLE PRECISION,
          domain_confidence     DOUBLE PRECISION,
          careers_confidence    DOUBLE PRECISION,
          status                TEXT NOT NULL DEFAULT 'novel'
                                CHECK (status IN ('novel','known','possible_duplicate','promoted')),
          match_reason          TEXT,
          matched_catalog_company_key TEXT,
          provenance            JSONB NOT NULL DEFAULT '{}'::jsonb,
          metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
          first_seen            TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_seen             TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (source, source_external_id)
        );""")
        cur.execute("ALTER TABLE company_discovery ADD COLUMN IF NOT EXISTS source_url TEXT")
        cur.execute(
            "ALTER TABLE company_discovery ADD COLUMN IF NOT EXISTS "
            "source_observed_at TIMESTAMPTZ")
        cur.execute("CREATE INDEX IF NOT EXISTS cd_status ON company_discovery (status)")
        cur.execute("CREATE INDEX IF NOT EXISTS cd_name ON company_discovery (canonical_name)")
        cur.execute("CREATE INDEX IF NOT EXISTS cd_domain ON company_discovery (domain)")
        cur.execute("CREATE INDEX IF NOT EXISTS cd_ats_slug ON company_discovery (ats, ats_slug)")


_UPSERT_COLS = (
    "source", "source_external_id", "external_ids", "source_url", "source_observed_at", "legal_name",
    "trade_name", "canonical_name", "domain", "careers_url", "country", "states",
    "industry", "naics", "employee_size", "ats", "ats_slug", "ats_url",
    "remote_supported", "typical_roles", "discovery_confidence", "domain_confidence",
    "careers_confidence", "status", "match_reason", "matched_catalog_company_key",
    "provenance", "metadata",
)
_JSON_COLS = {"external_ids", "typical_roles", "provenance", "metadata"}


def prepare_record(row: dict) -> dict:
    prepared = dict(row)
    prepared["source"] = str(row.get("source") or "").strip().casefold()
    prepared["source_external_id"] = str(row.get("source_external_id") or "").strip()
    prepared["canonical_name"] = normalize_company_name(
        row.get("canonical_name") or row.get("trade_name") or row.get("legal_name")
        or row.get("company"))
    prepared["domain"] = normalize_domain(row.get("domain")) or None
    ats, ats_slug = _record_ats_identity(row)
    prepared["ats"] = ats or None
    prepared["ats_slug"] = ats_slug or None
    prepared["external_ids"] = _external_ids(row)
    prepared["provenance"] = row.get("provenance") or {}
    prepared["metadata"] = row.get("metadata") or {}
    if not prepared["source"] or not prepared["source_external_id"]:
        raise ValueError("source and source_external_id are required")
    if not prepared["canonical_name"]:
        raise ValueError("a company name is required")
    status = prepared.get("status", "novel")
    if status not in STATUSES:
        raise ValueError(f"invalid company discovery status: {status}")
    prepared["status"] = status
    return prepared


def upsert_records(rows: list[dict]) -> int:
    if not rows:
        return 0
    values = []
    for row in rows:
        prepared = prepare_record(row)
        values.append(tuple(Json(prepared.get(col)) if Json is not None and col in _JSON_COLS
                            and prepared.get(col) is not None else prepared.get(col)
                            for col in _UPSERT_COLS))
    placeholders = "(" + ",".join(["%s"] * len(_UPSERT_COLS)) + ")"
    # A rediscovery refreshes descriptive/source fields, but must not undo a manual
    # promotion or a completed reconciliation decision.
    identity_cols = {"source", "source_external_id", "status", "match_reason",
                     "matched_catalog_company_key"}
    updates = [col for col in _UPSERT_COLS if col not in identity_cols]
    sql = ("INSERT INTO company_discovery (" + ",".join(_UPSERT_COLS) + ") VALUES "
           + placeholders + " ON CONFLICT (source, source_external_id) DO UPDATE SET "
           + ",".join(f"{col}=COALESCE(EXCLUDED.{col}, company_discovery.{col})"
                      for col in updates)
           + ",last_seen=now(),updated_at=now()")
    with _cur(False) as cur:
        cur.executemany(sql, values)
        return cur.rowcount


def catalog_companies() -> list[dict]:
    """Read the minimum identity surface from job_catalog; no discovery write-back."""
    with _cur() as cur:
        cur.execute("""
          SELECT ats, company_key, company, MIN(url) AS url
          FROM job_catalog
          WHERE company_key IS NOT NULL OR company IS NOT NULL
          GROUP BY ats, company_key, company
        """)
        return [dict(row) for row in cur.fetchall()]


def reconcile_records(limit: int = 0, include_promoted: bool = False) -> int:
    catalog = catalog_companies()
    where = "" if include_promoted else " WHERE status <> 'promoted'"
    sql = "SELECT * FROM company_discovery" + where + " ORDER BY id"
    args: tuple = ()
    if limit:
        sql += " LIMIT %s"
        args = (int(limit),)
    with _cur() as cur:
        cur.execute(sql, args)
        rows = [dict(row) for row in cur.fetchall()]
    updates = []
    for row in rows:
        result = classify_record(row, catalog)
        updates.append((result["status"], result["match_reason"],
                        result["matched_catalog_company_key"], row["id"]))
    if not updates:
        return 0
    with _cur(False) as cur:
        cur.executemany(
            "UPDATE company_discovery SET status=%s, match_reason=%s, "
            "matched_catalog_company_key=%s, updated_at=now() WHERE id=%s", updates)
        return cur.rowcount


def list_companies(status: str | None = None, source: str | None = None,
                   limit: int = 100, offset: int = 0) -> list[dict]:
    where, args = [], []
    if status:
        if status not in STATUSES:
            raise ValueError(f"invalid company discovery status: {status}")
        where.append("status=%s")
        args.append(status)
    if source:
        where.append("source=%s")
        args.append(source.casefold())
    clause = " WHERE " + " AND ".join(where) if where else ""
    with _cur() as cur:
        cur.execute("SELECT * FROM company_discovery" + clause
                    + " ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s",
                    tuple(args) + (int(limit), int(offset)))
        return [dict(row) for row in cur.fetchall()]


def list_enrichment_candidates(*, limit: int = 1000,
                               retry_attempted: bool = False) -> list[dict]:
    """Return a bounded stable batch with domains, without doing domain discovery."""
    attempted = "" if retry_attempted else \
        " AND NOT (provenance ? 'web_enrichment')"
    with _cur() as cur:
        cur.execute(
            "SELECT * FROM company_discovery WHERE domain IS NOT NULL AND domain<>''"
            + attempted + " ORDER BY id LIMIT %s", (max(1, int(limit)),))
        return [dict(row) for row in cur.fetchall()]


def update_enrichment_results(rows: list[dict]) -> int:
    """Persist only careers/ATS evidence; never overwrite acquisition identity."""
    if not rows:
        return 0
    values = []
    for row in rows:
        provenance = row.get("provenance") or {}
        evidence = {"web_enrichment": provenance.get("web_enrichment", {})}
        values.append((
            row.get("careers_url") or None,
            normalize_ats(row.get("ats")) or None,
            normalize_slug(row.get("ats_slug")) or None,
            row.get("ats_url") or None,
            row.get("domain_confidence"),
            row.get("careers_confidence"),
            Json(evidence) if Json is not None else evidence,
            int(row["id"]),
        ))
    with _cur(False) as cur:
        cur.executemany("""
          UPDATE company_discovery SET
            careers_url=COALESCE(%s,careers_url),
            ats=COALESCE(%s,ats), ats_slug=COALESCE(%s,ats_slug),
            ats_url=COALESCE(%s,ats_url),
            domain_confidence=GREATEST(COALESCE(domain_confidence,0),COALESCE(%s,0)),
            careers_confidence=GREATEST(COALESCE(careers_confidence,0),COALESCE(%s,0)),
            provenance=provenance || %s, updated_at=now()
          WHERE id=%s
        """, values)
        return cur.rowcount


def enrichment_counts() -> dict[str, int]:
    with _cur(False) as cur:
        cur.execute("""
          SELECT
            COUNT(*) FILTER (WHERE domain IS NOT NULL AND domain<>'') AS domains,
            COUNT(*) FILTER (WHERE careers_url IS NOT NULL AND careers_url<>'') AS careers,
            COUNT(*) FILTER (WHERE ats IS NOT NULL AND ats<>'') AS ats,
            COUNT(*) FILTER (WHERE provenance ? 'domain_resolution') AS domain_attempted,
            COUNT(*) FILTER (WHERE provenance ? 'web_enrichment') AS web_attempted
          FROM company_discovery
        """)
        domains, careers, ats, domain_attempted, web_attempted = cur.fetchone()
    return {"domains": domains, "careers": careers, "ats": ats,
            "domain_attempted": domain_attempted, "web_attempted": web_attempted,
            # Backwards-compatible alias for existing CLI/API consumers.
            "attempted": web_attempted}


def list_without_domain(*, limit: int = 100, source: str | None = None,
                        offset: int = 0, retry_attempted: bool = False) -> list[dict]:
    """Return only discovery rows which still need an official-domain lookup.

    This query intentionally touches no vacancy/catalog table.  A stable ID order
    makes bounded/resumable enrichment runs predictable.
    """
    where = ["NULLIF(BTRIM(domain), '') IS NULL"]
    if not retry_attempted:
        where.append("NOT (COALESCE(provenance, '{}'::jsonb) ? 'domain_resolution')")
    args: list[object] = []
    if source:
        where.append("source=%s")
        args.append(source.casefold())
    with _cur() as cur:
        cur.execute(
            "SELECT * FROM company_discovery WHERE " + " AND ".join(where)
            + " ORDER BY id LIMIT %s OFFSET %s",
            tuple(args) + (max(0, int(limit)), max(0, int(offset))),
        )
        return [dict(row) for row in cur.fetchall()]


def record_domain_resolution_attempts(rows: list[tuple[int, dict]]) -> int:
    """Persist completed negative lookups so the default batch is resumable."""
    if not rows:
        return 0
    values = [
        (Json(evidence) if Json is not None else evidence, int(company_id))
        for company_id, evidence in rows
    ]
    with _cur(False) as cur:
        cur.executemany(
            """
            UPDATE company_discovery SET
              provenance=COALESCE(provenance, '{}'::jsonb)
                || jsonb_build_object('domain_resolution', %s::jsonb),
              updated_at=now()
            WHERE id=%s AND NULLIF(BTRIM(domain), '') IS NULL
            """,
            values,
        )
        return cur.rowcount


def update_resolved_company(company_id: int, enrichment: dict) -> bool:
    """Persist a verified domain and optional careers/ATS signals for one row.

    The ``domain IS NULL`` predicate prevents a background run from overwriting a
    domain supplied by a newer source/manual decision.  Resolver evidence is
    merged below ``provenance.domain_resolution`` instead of replacing source
    provenance.
    """
    domain = normalize_domain(enrichment.get("domain"))
    confidence = float(enrichment.get("domain_confidence") or 0.0)
    evidence = enrichment.get("domain_resolution") or {}
    if not domain or not (0.0 <= confidence <= 1.0):
        raise ValueError("verified domain and confidence in [0,1] are required")
    with _cur(False) as cur:
        cur.execute(
            """
            UPDATE company_discovery SET
              domain=%s,
              careers_url=COALESCE(NULLIF(%s, ''), careers_url),
              ats=COALESCE(NULLIF(%s, ''), ats),
              ats_slug=COALESCE(NULLIF(%s, ''), ats_slug),
              ats_url=COALESCE(NULLIF(%s, ''), ats_url),
              domain_confidence=%s,
              careers_confidence=COALESCE(%s, careers_confidence),
              provenance=COALESCE(provenance, '{}'::jsonb)
                || jsonb_build_object('domain_resolution', %s::jsonb),
              updated_at=now()
            WHERE id=%s AND NULLIF(BTRIM(domain), '') IS NULL
            """,
            (domain, enrichment.get("careers_url"), enrichment.get("ats"),
             enrichment.get("ats_slug"), enrichment.get("ats_url"), confidence,
             enrichment.get("careers_confidence"), Json(evidence), int(company_id)),
        )
        return cur.rowcount == 1


def counts() -> dict:
    with _cur(False) as cur:
        cur.execute("SELECT status, COUNT(*) FROM company_discovery GROUP BY status")
        by_status = {status: 0 for status in STATUSES}
        for status, count in cur.fetchall():
            by_status[status] = count
    return {"total": sum(by_status.values()), "by_status": by_status}
