"""Postgres schema + query layer for the interview scheduler.

Reuses the existing `jobfinder_crm` connection pool (`backend.tools.mail_db`) — this
module opens NO connections of its own. Tables (`iv_responsibles`, `iv_availability`,
`iv_interviews`) are documented in `docs/superpowers/specs/2026-08-28-interview-
scheduler-design.md` ("Data model"). Nothing imports this module yet (task 1 of the
interview-scheduler build); it is DB foundation only — no FastAPI, no auth, no slots.

Timestamps are timezone-aware UTC throughout (`timestamptz` columns; callers pass/
receive aware `datetime` objects — the owner asked for GMT/UTC only in the MVP).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from backend.tools import mail_db

DOW_COUNT = 7


# ---- schema ----------------------------------------------------------------------
def ensure_schema() -> None:
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS iv_responsibles (
          id               SERIAL PRIMARY KEY,
          login            TEXT NOT NULL UNIQUE,
          password_hash    TEXT NOT NULL,
          name             TEXT NOT NULL,
          tz               TEXT NOT NULL DEFAULT 'UTC',
          telegram_chat_id BIGINT,
          active           BOOLEAN NOT NULL DEFAULT TRUE,
          created_at       TIMESTAMPTZ DEFAULT now()
        );""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS iv_availability (
          id             SERIAL PRIMARY KEY,
          responsible_id INT NOT NULL REFERENCES iv_responsibles(id) ON DELETE CASCADE,
          dow            SMALLINT NOT NULL,
          start_min      INT NOT NULL,
          end_min        INT NOT NULL,
          enabled        BOOLEAN NOT NULL DEFAULT TRUE,
          UNIQUE (responsible_id, dow)
        );""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS iv_interviews (
          id                   SERIAL PRIMARY KEY,
          mailbox              TEXT NOT NULL,
          thread_key           TEXT,
          company              TEXT,
          jobid                TEXT,
          responsible_id       INT REFERENCES iv_responsibles(id),
          start_ts             TIMESTAMPTZ,
          end_ts               TIMESTAMPTZ,
          status               TEXT NOT NULL DEFAULT 'assigned',
          source_message_hash  TEXT,
          notes                TEXT,
          reminded_60          BOOLEAN DEFAULT FALSE,
          reminded_5           BOOLEAN DEFAULT FALSE,
          created_at           TIMESTAMPTZ DEFAULT now()
        );""")
        cur.execute("CREATE INDEX IF NOT EXISTS iv_interviews_responsible_idx "
                    "ON iv_interviews (responsible_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS iv_interviews_mailbox_idx "
                    "ON iv_interviews (mailbox);")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS iv_interviews_nodouble "
                    "ON iv_interviews (responsible_id, start_ts) "
                    "WHERE responsible_id IS NOT NULL AND status <> 'cancelled';")


# ---- responsibles ------------------------------------------------------------------
def add_responsible(login: str, password_hash: str, name: str, tz: str = "UTC") -> int:
    """Insert a new responsible. Raises psycopg2.IntegrityError on a duplicate login."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute(
            "INSERT INTO iv_responsibles (login, password_hash, name, tz) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (login, password_hash, name, tz))
        return cur.fetchone()[0]


def get_responsible_by_login(login: str) -> dict | None:
    with mail_db._cur() as cur:
        cur.execute("SELECT * FROM iv_responsibles WHERE login=%s", (login,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_responsible(rid: int) -> dict | None:
    with mail_db._cur() as cur:
        cur.execute("SELECT * FROM iv_responsibles WHERE id=%s", (rid,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_responsibles(active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM iv_responsibles"
    if active_only:
        sql += " WHERE active=TRUE"
    sql += " ORDER BY id"
    with mail_db._cur() as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def set_telegram_chat(rid: int, chat_id: int) -> None:
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_responsibles SET telegram_chat_id=%s WHERE id=%s",
                    (chat_id, rid))


def set_password_hash(rid: int, password_hash: str) -> None:
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_responsibles SET password_hash=%s WHERE id=%s",
                    (password_hash, rid))


def set_active(rid: int, active: bool) -> None:
    """Deactivate (active=False) or reactivate a responsible. A deactivated employee's
    existing session cookie stops working on the next request (see auth.current_responsible)."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_responsibles SET active=%s WHERE id=%s",
                    (active, rid))


# ---- availability --------------------------------------------------------------
def get_availability(rid: int) -> list[dict]:
    """7 rows (dow 0..6), missing days filled with enabled=False, start/end 0."""
    with mail_db._cur() as cur:
        cur.execute("SELECT dow, start_min, end_min, enabled FROM iv_availability "
                    "WHERE responsible_id=%s", (rid,))
        by_dow = {r["dow"]: dict(r) for r in cur.fetchall()}
    return [by_dow.get(d, {"dow": d, "start_min": 0, "end_min": 0, "enabled": False})
            for d in range(DOW_COUNT)]


def set_availability(rid: int, rows: list[dict]) -> None:
    """UPSERT each {dow,start_min,end_min,enabled} row on (responsible_id,dow)."""
    with mail_db._cur(dict_rows=False) as cur:
        for row in rows:
            cur.execute(
                "INSERT INTO iv_availability (responsible_id, dow, start_min, end_min, enabled) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (responsible_id, dow) DO UPDATE SET "
                "start_min=EXCLUDED.start_min, end_min=EXCLUDED.end_min, "
                "enabled=EXCLUDED.enabled",
                (rid, row["dow"], row["start_min"], row["end_min"], row["enabled"]))


# ---- interviews ------------------------------------------------------------------
def insert_interview(mailbox: str, responsible_id: int | None, start_ts: datetime,
                      end_ts: datetime, company: str | None, jobid: str | None,
                      thread_key: str | None, source_message_hash: str | None,
                      notes: str = "") -> int:
    """Raises psycopg2.IntegrityError (UniqueViolation) on a double-book of the same
    responsible at the same start_ts (the partial unique index `iv_interviews_nodouble`)."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute(
            "INSERT INTO iv_interviews "
            "(mailbox, thread_key, company, jobid, responsible_id, start_ts, end_ts, "
            " source_message_hash, notes) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (mailbox, thread_key, company, jobid, responsible_id, start_ts, end_ts,
             source_message_hash, notes))
        return cur.fetchone()[0]


def interviews_for_responsible(rid: int, upcoming_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM iv_interviews WHERE responsible_id=%s"
    args: list = [rid]
    if upcoming_only:
        sql += " AND start_ts > now() AND status <> 'cancelled'"
    sql += " ORDER BY start_ts ASC"
    with mail_db._cur() as cur:
        cur.execute(sql, tuple(args))
        return [dict(r) for r in cur.fetchall()]


def assigned_mailboxes(rid: int) -> set:
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("SELECT DISTINCT mailbox FROM iv_interviews "
                    "WHERE responsible_id=%s AND status <> 'cancelled'", (rid,))
        return {r[0] for r in cur.fetchall()}


def interview_for_thread(mailbox: str, thread_key: str) -> dict | None:
    with mail_db._cur() as cur:
        cur.execute("SELECT * FROM iv_interviews WHERE mailbox=%s AND thread_key=%s "
                    "ORDER BY created_at DESC LIMIT 1", (mailbox, thread_key))
        row = cur.fetchone()
        return dict(row) if row else None


def booked_intervals(rid: int, since: datetime, until: datetime) -> list[tuple]:
    """(start_ts, end_ts) pairs of this responsible's non-cancelled interviews that
    overlap [since, until)."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute(
            "SELECT start_ts, end_ts FROM iv_interviews "
            "WHERE responsible_id=%s AND status <> 'cancelled' "
            "AND start_ts < %s AND end_ts > %s ORDER BY start_ts",
            (rid, until, since))
        return [(r[0], r[1]) for r in cur.fetchall()]


# ---- reminders (layer 2) ----------------------------------------------------------
def due_reminders(now: datetime, window_min: int) -> list[dict]:
    """Assigned, not-cancelled interviews whose start_ts falls in (now, now+window_min]
    and whose reminder flag for THIS window hasn't been set yet. window_min selects the
    flag: 60 -> reminded_60, anything else (5) -> reminded_5."""
    flag_col = "reminded_60" if int(window_min) == 60 else "reminded_5"
    until = now + timedelta(minutes=window_min)
    with mail_db._cur() as cur:
        cur.execute(
            f"SELECT * FROM iv_interviews WHERE status='assigned' "
            f"AND responsible_id IS NOT NULL "
            f"AND start_ts > %s AND start_ts <= %s AND NOT {flag_col} "
            f"ORDER BY start_ts",
            (now, until))
        return [dict(r) for r in cur.fetchall()]


def mark_reminded(interview_id: int, which: str) -> None:
    """which ∈ {'60','5'} — sets reminded_60 when which=='60', else reminded_5."""
    col = "reminded_60" if str(which) == "60" else "reminded_5"
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute(f"UPDATE iv_interviews SET {col}=TRUE WHERE id=%s", (interview_id,))
