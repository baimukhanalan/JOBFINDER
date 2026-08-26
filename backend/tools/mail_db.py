"""Postgres-backed index for the candidate mail CRM.

The CRM used to scan every candidate Maildir and parse every .eml on each request,
which does not scale past a few hundred messages. This module is the fast index:
one row per message in an ISOLATED database (`jobfinder_crm`, its own role — NOT the
shared amasmail DB, NOT the legacy `jobfinder` DB), kept in sync by an inotify
indexer. The CRM reads from here; the raw .eml is still read from disk by path only
when a message/thread is opened.

Sync (psycopg2) so the existing sync CRM code, the standalone indexer and the cron
retention job can all share it. DSN comes from `CRM_PG_DSN` in backend/.env.

Public API (stable contract used by mail_indexer.py, mailcrm.py, mail_retention.py):
  ensure_schema()
  upsert_message(**fields) / delete_paths(hashes) / all_path_hashes() / mark_seen(...)
  get_meta(key) / set_meta(key, value)
  list_messages(...) / get_row(h) / thread_rows(mailbox, thread_key)
  counts() / stage_counts() / unread_by_mailbox()
  indexed_paths() / update_kinds(...)
  protected_thread_keys() / deletable_rows(kinds, cutoff_ts)
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.pool

_ENV = Path(__file__).resolve().parents[1] / ".env"


def _dsn() -> str:
    dsn = os.environ.get("CRM_PG_DSN")
    if dsn:
        return dsn
    try:
        for line in _ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("CRM_PG_DSN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    raise RuntimeError("CRM_PG_DSN not set (backend/.env or environment)")


_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(1, 8, dsn=_dsn())
    return _pool


@contextmanager
def conn():
    """A pooled connection (returned to the pool on exit). Commits on success."""
    pool = _get_pool()
    c = pool.getconn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        pool.putconn(c)


@contextmanager
def _cur(dict_rows: bool = True):
    with conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor if dict_rows else None)
        try:
            yield cur
        finally:
            cur.close()


# ---- schema --------------------------------------------------------------------
def ensure_schema() -> None:
    with _cur(dict_rows=False) as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS mail_index (
          id           BIGSERIAL PRIMARY KEY,
          mailbox      TEXT NOT NULL,
          candidate    TEXT,
          candidate_id TEXT,
          path         TEXT NOT NULL,
          path_hash    TEXT NOT NULL UNIQUE,
          from_name    TEXT,
          from_email   TEXT,
          subject      TEXT,
          snippet      TEXT,
          kind         TEXT,
          thread_key   TEXT,
          has_att      BOOLEAN DEFAULT FALSE,
          outbound     BOOLEAN DEFAULT FALSE,
          date_ts      BIGINT DEFAULT 0,
          seen         BOOLEAN DEFAULT FALSE,
          indexed_at   TIMESTAMPTZ DEFAULT now()
        );""")
        cur.execute("CREATE INDEX IF NOT EXISTS mail_idx_dt ON mail_index (date_ts DESC, path_hash DESC);")
        cur.execute("CREATE INDEX IF NOT EXISTS mail_idx_mailbox ON mail_index (mailbox);")
        cur.execute("CREATE INDEX IF NOT EXISTS mail_idx_thread ON mail_index (mailbox, thread_key);")
        cur.execute("CREATE INDEX IF NOT EXISTS mail_idx_kind ON mail_index (kind);")
        cur.execute("CREATE INDEX IF NOT EXISTS mail_idx_fts ON mail_index USING gin "
                    "(to_tsvector('simple', coalesce(subject,'')||' '||coalesce(from_email,'')"
                    "||' '||coalesce(from_name,'')));")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS mail_meta (
          key   TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );""")


_COLS = ("mailbox", "candidate", "candidate_id", "path", "path_hash", "from_name",
         "from_email", "subject", "snippet", "kind", "thread_key", "has_att",
         "outbound", "date_ts", "seen")


# ---- writes (indexer) ----------------------------------------------------------
def upsert_message(**f) -> None:
    """Insert a message row or refresh all parsed metadata for an existing row."""
    vals = [f.get(c) for c in _COLS]
    ph = ",".join(["%s"] * len(_COLS))
    updates = ",".join(f"{c}=EXCLUDED.{c}" for c in _COLS if c != "path_hash")
    with _cur(dict_rows=False) as cur:
        cur.execute(
            f"INSERT INTO mail_index ({','.join(_COLS)}) VALUES ({ph}) "
            f"ON CONFLICT (path_hash) DO UPDATE SET {updates}",
            vals)


def get_meta(key: str) -> str | None:
    with _cur(dict_rows=False) as cur:
        cur.execute("SELECT value FROM mail_meta WHERE key=%s", (key,))
        row = cur.fetchone()
        return row[0] if row else None


def set_meta(key: str, value: str) -> None:
    with _cur(dict_rows=False) as cur:
        cur.execute("INSERT INTO mail_meta (key,value) VALUES (%s,%s) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (key, value))


def delete_paths(path_hashes) -> int:
    path_hashes = list(path_hashes or [])
    if not path_hashes:
        return 0
    with _cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM mail_index WHERE path_hash = ANY(%s)", (path_hashes,))
        return cur.rowcount


def all_path_hashes() -> set:
    with _cur(dict_rows=False) as cur:
        cur.execute("SELECT path_hash FROM mail_index")
        return {r[0] for r in cur.fetchall()}


def mark_seen(old_hash: str, new_path: str, new_hash: str) -> None:
    """After a new/ -> cur/ rename: point the row at the new path and flag it seen."""
    with _cur(dict_rows=False) as cur:
        cur.execute("UPDATE mail_index SET path=%s, path_hash=%s, seen=TRUE WHERE path_hash=%s",
                    (new_path, new_hash, old_hash))


def mark_read(path_hashes) -> int:
    """Flag the given messages seen=TRUE (the CRM 'mark as read' action). Returns rowcount."""
    ph = [h for h in (path_hashes or []) if h]
    if not ph:
        return 0
    with _cur(dict_rows=False) as cur:
        cur.execute("UPDATE mail_index SET seen=TRUE WHERE path_hash = ANY(%s)", (ph,))
        return cur.rowcount


# ---- reads (CRM) ---------------------------------------------------------------
def _dict(r) -> dict:
    d = dict(r)
    d["id"] = d.get("path_hash")
    d["thread"] = d.get("thread_key")
    return d


def list_messages(mailbox=None, q=None, limit=50, before_ts=None, before_id=None,
                  stage=None) -> list:
    """Newest-first message rows, keyset-paginated by (date_ts, id). Optional mailbox
    filter and full-text search on subject/from."""
    where, args = [], []
    if mailbox:
        where.append("mailbox=%s")
        args.append(mailbox.lower())
    if q:
        # match subject/from (full-text) OR the mailbox address OR the candidate name, so
        # typing a candidate's @takhet.com email in the inbox search surfaces that mailbox's
        # whole conversation (the address lives in `mailbox`, not in subject/from).
        where.append("(to_tsvector('simple', coalesce(subject,'')||' '||coalesce(from_email,'')"
                     "||' '||coalesce(from_name,'')) @@ plainto_tsquery('simple', %s)"
                     " OR mailbox ILIKE %s OR candidate ILIKE %s)")
        args += [q, f"%{q}%", f"%{q}%"]
    if stage == "sent":
        where.append("outbound=TRUE")
    elif stage in ("ack", "interview", "offer", "rejection", "other", "action_needed"):
        where.append("kind=%s AND outbound=FALSE")
        args.append(stage)
    if before_ts is not None and before_id is not None:
        # cursor is (date_ts, path_hash) — path_hash is the string id used in URLs
        where.append("(date_ts, path_hash) < (%s, %s)")
        args += [before_ts, before_id]
    w = (" WHERE " + " AND ".join(where)) if where else ""
    with _cur() as cur:
        cur.execute("SELECT * FROM mail_index" + w + " ORDER BY date_ts DESC, path_hash DESC LIMIT %s",
                    tuple(args) + (limit,))
        return [_dict(r) for r in cur.fetchall()]


def get_row(path_hash: str):
    with _cur() as cur:
        cur.execute("SELECT * FROM mail_index WHERE path_hash=%s", (path_hash,))
        r = cur.fetchone()
        return _dict(r) if r else None


def thread_rows(mailbox: str, thread_key: str) -> list:
    """All messages in a thread, oldest first (for the conversation view)."""
    with _cur() as cur:
        cur.execute("SELECT * FROM mail_index WHERE mailbox=%s AND thread_key=%s "
                    "ORDER BY date_ts ASC, id ASC", (mailbox, thread_key))
        return [_dict(r) for r in cur.fetchall()]


def counts() -> dict:
    with _cur(dict_rows=False) as cur:
        cur.execute("SELECT COUNT(*) FROM mail_index WHERE seen=FALSE")
        unread = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT mailbox) FROM mail_index")
        mboxes = cur.fetchone()[0]
    return {"unread": unread, "mailboxes": mboxes}


def stage_counts() -> dict:
    """DISTINCT-CANDIDATE totals for the inbox funnel — the same metric as the Candidates
    funnel (`kind_counts`), so the two screens show the SAME number per stage. Counting
    messages (COUNT(*)) here made the inbox show a bigger number than Candidates for the
    identical stage (a candidate with 3 interview mails counted as 3), which read as a
    'conflict in the numbers'. Outbound is its own 'sent' stage."""
    with _cur(dict_rows=False) as cur:
        cur.execute("SELECT kind, COUNT(DISTINCT mailbox) FROM mail_index "
                    "WHERE NOT outbound GROUP BY kind")
        out = {k: n for k, n in cur.fetchall()}
        cur.execute("SELECT COUNT(DISTINCT mailbox) FROM mail_index WHERE outbound=TRUE")
        out["sent"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT mailbox) FROM mail_index")
        out["all"] = cur.fetchone()[0]
        return out


def indexed_paths() -> list[tuple[str, bool]]:
    """Paths and seen flags used for a full-body classification refresh."""
    with _cur(dict_rows=False) as cur:
        cur.execute("SELECT path, seen FROM mail_index")
        return [(path, bool(seen)) for path, seen in cur.fetchall()]


def update_kinds(updates) -> int:
    """Bulk-update (kind, path_hash) pairs in one transaction."""
    updates = list(updates or [])
    if not updates:
        return 0
    with _cur(dict_rows=False) as cur:
        cur.executemany("UPDATE mail_index SET kind=%s WHERE path_hash=%s", updates)
        return len(updates)


def unread_by_mailbox() -> dict:
    with _cur(dict_rows=False) as cur:
        cur.execute("SELECT mailbox, COUNT(*) FROM mail_index WHERE seen=FALSE GROUP BY mailbox")
        return {r[0]: r[1] for r in cur.fetchall()}


def kind_counts() -> dict:
    """{kind: number of DISTINCT candidate mailboxes that have a message of that kind}
    — powers the funnel buttons on the candidates page."""
    with _cur(dict_rows=False) as cur:
        cur.execute("SELECT kind, COUNT(DISTINCT mailbox) FROM mail_index "
                    "WHERE NOT outbound GROUP BY kind")
        return {k: n for k, n in cur.fetchall()}


def mailboxes_with_kind(kind: str) -> set:
    """Candidate emails that have at least one INBOUND message of this kind."""
    with _cur(dict_rows=False) as cur:
        cur.execute("SELECT DISTINCT mailbox FROM mail_index WHERE kind=%s AND NOT outbound", (kind,))
        return {r[0] for r in cur.fetchall()}


# ---- retention (cron) ----------------------------------------------------------
def protected_thread_keys() -> set:
    """(mailbox, thread_key) pairs that must NEVER be auto-deleted: any thread that
    contains an interview/offer/rejection message, or any message the candidate sent
    (outbound = an active conversation)."""
    with _cur(dict_rows=False) as cur:
        cur.execute("SELECT DISTINCT mailbox, thread_key FROM mail_index "
                    "WHERE kind IN ('interview','offer','rejection') OR outbound=TRUE")
        return {(r[0], r[1]) for r in cur.fetchall()}


def deletable_rows(kinds, cutoff_ts: int) -> list:
    """Candidate rows for retention: given kinds, older than cutoff. Thread-level
    protection is applied by the caller against protected_thread_keys()."""
    with _cur() as cur:
        cur.execute("SELECT path, path_hash, mailbox, thread_key, kind, date_ts "
                    "FROM mail_index WHERE kind = ANY(%s) AND date_ts < %s",
                    (list(kinds), cutoff_ts))
        return [dict(r) for r in cur.fetchall()]
