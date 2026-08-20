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


_UP_COLS = ("ats", "company_key", "company", "external_id", "title", "location",
            "department", "workplace", "is_remote", "url", "description",
            "description_html", "questions", "q_count")
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
           "q_count=GREATEST(EXCLUDED.q_count, job_catalog.q_count), last_seen=now()")
    with _cur(False) as cur:
        cur.executemany(sql, vals)
        return cur.rowcount


_LIST_COLS = ("id", "ats", "company_key", "company", "title", "location", "department",
              "workplace", "is_remote", "url", "description", "description_html",
              "questions", "q_count")


def list_jobs(company: str | None = None, q: str | None = None, remote_only: bool = True,
              limit: int = 30, offset: int = 0) -> list:
    where, args = ["TRUE"], []
    if remote_only:
        where.append("is_remote=TRUE")
    if company:
        where.append("company_key=%s")
        args.append(company)
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
    w = " WHERE is_remote=TRUE" if remote_only else ""
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


def rows_missing_questions(ats: str) -> list:
    """(external_id, company_key, url, title) for rows of this ATS that still have no
    questions — the backfill work-list."""
    with _cur() as cur:
        cur.execute("SELECT external_id, company_key, url, title FROM job_catalog "
                    "WHERE ats=%s AND (q_count=0 OR q_count IS NULL)", (ats,))
        return [dict(r) for r in cur.fetchall()]


def counts() -> dict:
    with _cur(False) as cur:
        cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE is_remote), "
                    "COUNT(*) FILTER (WHERE q_count > 0) FROM job_catalog")
        t, rem, wq = cur.fetchone()
    return {"total": t, "remote": rem, "with_questions": wq}
