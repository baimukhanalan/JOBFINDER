"""Postgres `job_catalog` — a persisted catalog of remote jobs across every known
ATS board: title, description, and (greenhouse) application-form questions.

Lives in the SAME isolated `jobfinder_crm` DB as the mail index (CRM_PG_DSN). Written
by catalog_collector.py (threaded scrape), read by the /catalog dashboard tab. Sync
psycopg2 so the collector, cron and the web app can all share it.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.pool
from psycopg2.extras import Json

_ENV = Path(__file__).resolve().parents[1] / ".env"


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


_pool = None
_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=_dsn())
    return _pool


@contextmanager
def conn():
    p = _get_pool()
    c = p.getconn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        p.putconn(c)


@contextmanager
def _cur(dict_rows: bool = True):
    with conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor if dict_rows else None)
        try:
            yield cur
        finally:
            cur.close()


def ensure_schema() -> None:
    with _cur(False) as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS job_catalog (
          id            BIGSERIAL PRIMARY KEY,
          ats           TEXT NOT NULL,
          company_key   TEXT,
          company       TEXT,
          external_id   TEXT NOT NULL,
          title         TEXT,
          location      TEXT,
          department    TEXT,
          workplace     TEXT,
          is_remote     BOOLEAN DEFAULT FALSE,
          url           TEXT,
          description   TEXT,
          description_html TEXT,
          questions     JSONB,
          q_count       INT DEFAULT 0,
          first_seen    TIMESTAMPTZ DEFAULT now(),
          last_seen     TIMESTAMPTZ DEFAULT now(),
          UNIQUE (ats, company_key, external_id)
        );""")
        cur.execute("CREATE INDEX IF NOT EXISTS jc_remote ON job_catalog (is_remote, company_key);")
        cur.execute("CREATE INDEX IF NOT EXISTS jc_company ON job_catalog (company_key);")
        cur.execute("CREATE INDEX IF NOT EXISTS jc_fts ON job_catalog USING gin "
                    "(to_tsvector('simple', coalesce(title,'')||' '||coalesce(company,'')"
                    "||' '||coalesce(description,'')));")
        cur.execute("ALTER TABLE job_catalog ADD COLUMN IF NOT EXISTS regions TEXT[]")
        cur.execute("ALTER TABLE job_catalog ADD COLUMN IF NOT EXISTS region_source TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS jc_regions ON job_catalog USING GIN (regions)")
        # draft: the full pre-generated application fill-packet (tailored résumé dict +
        # every question answered, per catalog_drafts.py). Reviewed on the /drafts page.
        cur.execute("ALTER TABLE job_catalog ADD COLUMN IF NOT EXISTS draft JSONB")
        cur.execute("ALTER TABLE job_catalog ADD COLUMN IF NOT EXISTS draft_at TIMESTAMPTZ")
        # dead: a posting confirmed gone at the source (e.g. greenhouse job id 404s).
        # Kept as a reversible blacklist marker (not deleted) — hidden from the catalog
        # browse + the draft work-list so a human never opens an apply page that 404s.
        cur.execute("ALTER TABLE job_catalog ADD COLUMN IF NOT EXISTS dead BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE job_catalog ADD COLUMN IF NOT EXISTS dead_reason TEXT")


_UP_COLS = ("ats", "company_key", "company", "external_id", "title", "location",
            "department", "workplace", "is_remote", "url", "description",
            "description_html", "questions", "q_count", "regions", "region_source")
_QI = _UP_COLS.index("questions")


def upsert_jobs(rows: list[dict]) -> int:
    """Batch insert/update by (ats, company_key, external_id). Questions and q_count
    are only ever raised (COALESCE/GREATEST), so a later description-only refresh
    never wipes questions already collected."""
    if not rows:
        return 0
    vals = []
    for r in rows:
        v = [r.get(c) for c in _UP_COLS]
        v[_QI] = Json(r["questions"]) if r.get("questions") is not None else None
        vals.append(v)
    ph = "(" + ",".join(["%s"] * len(_UP_COLS)) + ")"
    sql = ("INSERT INTO job_catalog (" + ",".join(_UP_COLS) + ") VALUES " + ph +
           " ON CONFLICT (ats, company_key, external_id) DO UPDATE SET "
           "title=EXCLUDED.title, location=EXCLUDED.location, department=EXCLUDED.department, "
           "workplace=EXCLUDED.workplace, is_remote=EXCLUDED.is_remote, url=EXCLUDED.url, "
           "description=EXCLUDED.description, description_html=EXCLUDED.description_html, "
           "questions=COALESCE(EXCLUDED.questions, job_catalog.questions), "
           "q_count=GREATEST(EXCLUDED.q_count, job_catalog.q_count), "
           "regions=COALESCE(EXCLUDED.regions, job_catalog.regions), "
           "region_source=COALESCE(EXCLUDED.region_source, job_catalog.region_source), "
           "last_seen=now()")
    with _cur(False) as cur:
        cur.executemany(sql, vals)
        return cur.rowcount


_LIST_COLS = ("id", "ats", "company_key", "company", "title", "location", "department",
              "workplace", "is_remote", "url", "description", "description_html",
              "questions", "q_count")


def list_jobs(company: str | None = None, q: str | None = None, remote_only: bool = True,
              limit: int = 30, offset: int = 0, region: str | None = None) -> list:
    where, args = ["NOT COALESCE(dead, FALSE)"], []
    if remote_only:
        where.append("is_remote=TRUE")
    if company:
        where.append("company_key=%s")
        args.append(company)
    if region:
        where.append("%s = ANY(regions)")
        args.append(region)
    if q:
        where.append("to_tsvector('simple', coalesce(title,'')||' '||coalesce(company,'')"
                     "||' '||coalesce(description,'')) @@ plainto_tsquery('simple', %s)")
        args.append(q)
    w = " AND ".join(where)
    with _cur() as cur:
        cur.execute("SELECT " + ",".join(_LIST_COLS) + " FROM job_catalog WHERE " + w +
                    " ORDER BY (q_count > 0) DESC, company ASC, title ASC LIMIT %s OFFSET %s",
                    tuple(args) + (limit, offset))
        return [dict(r) for r in cur.fetchall()]


def companies(remote_only: bool = True) -> list:
    conds = ["NOT COALESCE(dead, FALSE)"]
    if remote_only:
        conds.append("is_remote=TRUE")
    w = " WHERE " + " AND ".join(conds)
    with _cur() as cur:
        cur.execute("SELECT company_key, company, COUNT(*) AS n FROM job_catalog" + w +
                    " GROUP BY company_key, company ORDER BY n DESC")
        return [dict(r) for r in cur.fetchall()]


def set_questions(ats: str, company_key: str, external_id: str, questions: list) -> int:
    """Attach/refresh the application-form questions for one existing catalog row."""
    with _cur(False) as cur:
        cur.execute("UPDATE job_catalog SET questions=%s, q_count=%s "
                    "WHERE ats=%s AND company_key=%s AND external_id=%s",
                    (Json(questions), len(questions or []), ats, company_key, external_id))
        return cur.rowcount


def mark_dead(keys: list[tuple], reason: str) -> int:
    """Blacklist postings confirmed gone at the source. `keys` is a list of
    (ats, company_key, external_id). Sets dead=TRUE + dead_reason; reversible
    (UPDATE dead=FALSE). Dead rows are hidden from list_jobs + jobs_for_drafting."""
    if not keys:
        return 0
    with _cur(False) as cur:
        cur.executemany(
            "UPDATE job_catalog SET dead=TRUE, dead_reason=%s "
            "WHERE ats=%s AND company_key=%s AND external_id=%s",
            [(reason, a, c, e) for (a, c, e) in keys])
        return cur.rowcount


def rows_missing_questions(ats: str, missing_only: bool = True) -> list:
    """(external_id, company_key, url, title) for rows of this ATS. Default: only rows
    still missing questions (the backfill work-list). missing_only=False returns ALL
    rows of the ATS — used to REFRESH already-stored questions (e.g. to add options)."""
    where = "ats=%s" + (" AND (q_count=0 OR q_count IS NULL)" if missing_only else "")
    with _cur() as cur:
        cur.execute("SELECT external_id, company_key, url, title FROM job_catalog "
                    "WHERE " + where, (ats,))
        return [dict(r) for r in cur.fetchall()]


def set_regions(ats: str, company_key: str, external_id: str, regions: list, source: str,
                 only_if_null: bool = False) -> int:
    """only_if_null=True guards a backfill write against clobbering a tag a concurrent
    collect just set (no flock between the collect and backfill cron jobs)."""
    sql = ("UPDATE job_catalog SET regions=%s, region_source=%s "
           "WHERE ats=%s AND company_key=%s AND external_id=%s")
    if only_if_null:
        sql += " AND regions IS NULL"
    with _cur(False) as cur:
        cur.execute(sql, (regions, source, ats, company_key, external_id))
        return cur.rowcount


def rows_missing_regions(limit: int = 0) -> list:
    sql = ("SELECT ats, company_key, external_id, title, location, description "
           "FROM job_catalog WHERE regions IS NULL")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with _cur() as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


_JOB_COLS = ("id", "ats", "company_key", "company", "external_id", "title", "location",
             "department", "workplace", "is_remote", "url", "description",
             "questions", "q_count", "regions", "draft", "draft_at")


def get_job(job_id: int) -> dict | None:
    """One full catalog row by id (for the draft generator + review page)."""
    with _cur() as cur:
        cur.execute("SELECT " + ",".join(_JOB_COLS) + " FROM job_catalog WHERE id=%s",
                    (job_id,))
        r = cur.fetchone()
        return dict(r) if r else None


def jobs_for_drafting(limit: int = 150, regions=("US", "CA"),
                      min_q: int = 1) -> list:
    """A representative work-list for a draft batch: jobs with real questions whose
    regions overlap the given set, spread ACROSS the four ATS (so the review sample
    covers greenhouse/ashby/lever/workable + a range of question counts), not just
    the first board alphabetically."""
    per = max(1, limit // 4)
    picked: list[dict] = []
    with _cur() as cur:
        for ats in ("greenhouse", "ashby", "lever", "workable"):
            cur.execute(
                "SELECT " + ",".join(_JOB_COLS) + " FROM job_catalog "
                "WHERE ats=%s AND q_count >= %s AND is_remote=TRUE AND regions && %s "
                "AND NOT COALESCE(dead, FALSE) "
                "ORDER BY id LIMIT %s",
                (ats, min_q, list(regions), per))
            picked.extend(dict(r) for r in cur.fetchall())
    return picked[:limit]


def set_draft(job_id: int, draft: dict) -> int:
    with _cur(False) as cur:
        cur.execute("UPDATE job_catalog SET draft=%s, draft_at=now() WHERE id=%s",
                    (Json(draft), job_id))
        return cur.rowcount


def list_drafts(q: str | None = None, limit: int = 60, offset: int = 0) -> list:
    where, args = ["draft IS NOT NULL"], []
    if q:
        where.append("to_tsvector('simple', coalesce(title,'')||' '||coalesce(company,'')) "
                     "@@ plainto_tsquery('simple', %s)")
        args.append(q)
    w = " AND ".join(where)
    with _cur() as cur:
        cur.execute("SELECT id, ats, company, title, url, q_count, regions, "
                    "draft->'stats' AS stats, draft->'candidate' AS candidate "
                    "FROM job_catalog WHERE " + w +
                    " ORDER BY draft_at DESC LIMIT %s OFFSET %s",
                    tuple(args) + (limit, offset))
        return [dict(r) for r in cur.fetchall()]


def drafts_count() -> int:
    with _cur(False) as cur:
        cur.execute("SELECT COUNT(*) FROM job_catalog WHERE draft IS NOT NULL")
        return cur.fetchone()[0]


def counts() -> dict:
    with _cur(False) as cur:
        cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE is_remote), "
                    "COUNT(*) FILTER (WHERE q_count > 0), "
                    "COUNT(*) FILTER (WHERE cardinality(regions) = 0) FROM job_catalog")
        t, rem, wq, resolved_empty = cur.fetchone()
        by_region = {}
        for code in ("US", "CA", "UK", "OTHER"):
            cur.execute("SELECT COUNT(*) FROM job_catalog WHERE %s = ANY(regions)", (code,))
            by_region[code] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM job_catalog WHERE regions IS NULL")
        untagged = cur.fetchone()[0]
    # resolved_empty: rows backfilled to regions=[] (stored as '{}') match neither
    # `%s = ANY(regions)` nor `regions IS NULL`, so without this bucket by_region +
    # untagged don't reconcile to total and an "untagged: 0" log line is misleading.
    return {"total": t, "remote": rem, "with_questions": wq,
            "by_region": by_region, "untagged": untagged, "resolved_empty": resolved_empty}
