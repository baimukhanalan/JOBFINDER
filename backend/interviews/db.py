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
        # additive column for the upcoming unified login: 'admin' | 'employee'
        cur.execute("ALTER TABLE iv_responsibles "
                    "ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'employee';")
        # one-time code for self-service Telegram linking (deep-link t.me/<bot>?start=<code>);
        # the notifier maps a /start <code> back to this responsible and stores their chat_id.
        cur.execute("ALTER TABLE iv_responsibles "
                    "ADD COLUMN IF NOT EXISTS tg_link_code TEXT;")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS iv_availability (
          id             SERIAL PRIMARY KEY,
          responsible_id INT NOT NULL REFERENCES iv_responsibles(id) ON DELETE CASCADE,
          dow            SMALLINT NOT NULL,
          start_min      INT NOT NULL,
          end_min        INT NOT NULL,
          enabled        BOOLEAN NOT NULL DEFAULT TRUE
        );""")
        # Availability is now MULTIPLE windows per weekday, so the old one-window-per-day
        # UNIQUE(responsible_id, dow) is dropped; a plain index serves the per-responsible read.
        cur.execute("ALTER TABLE iv_availability "
                    "DROP CONSTRAINT IF EXISTS iv_availability_responsible_id_dow_key")
        cur.execute("CREATE INDEX IF NOT EXISTS iv_avail_resp_dow "
                    "ON iv_availability (responsible_id, dow)")
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
        # additive column for the notifier daemon: one-time "interview assigned" ping
        cur.execute("ALTER TABLE iv_interviews "
                    "ADD COLUMN IF NOT EXISTS announced BOOLEAN NOT NULL DEFAULT FALSE;")
        cur.execute("CREATE INDEX IF NOT EXISTS iv_interviews_responsible_idx "
                    "ON iv_interviews (responsible_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS iv_interviews_mailbox_idx "
                    "ON iv_interviews (mailbox);")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS iv_interviews_nodouble "
                    "ON iv_interviews (responsible_id, start_ts) "
                    "WHERE responsible_id IS NOT NULL AND status <> 'cancelled';")


# ---- responsibles ------------------------------------------------------------------
def add_responsible(login: str, password_hash: str, name: str, tz: str = "UTC",
                    role: str = "employee") -> int:
    """Insert a new responsible. Raises psycopg2.IntegrityError on a duplicate login."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute(
            "INSERT INTO iv_responsibles (login, password_hash, name, tz, role) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (login, password_hash, name, tz, role))
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


def set_tz(rid: int, tz: str) -> None:
    """Set a responsible's IANA timezone (the anchor for their wall-clock availability
    and the zone their times are shown/reminded in). Auto-detected from their browser."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_responsibles SET tz=%s WHERE id=%s", (tz, rid))


def set_tg_link_code(rid: int, code: str) -> None:
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_responsibles SET tg_link_code=%s WHERE id=%s", (code, rid))


def link_telegram_by_code(code: str, chat_id: int) -> dict | None:
    """A responsible pressed Start on the bot with `/start <code>`: bind their chat_id
    and clear the one-time code. Returns the linked row, or None if the code is unknown."""
    with mail_db._cur() as cur:
        cur.execute("UPDATE iv_responsibles SET telegram_chat_id=%s, tg_link_code=NULL "
                    "WHERE tg_link_code=%s RETURNING *", (chat_id, code))
        row = cur.fetchone()
        return dict(row) if row else None


def set_active(rid: int, active: bool) -> None:
    """Deactivate (active=False) or reactivate a responsible. A deactivated employee's
    existing session cookie stops working on the next request (see auth.current_responsible)."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_responsibles SET active=%s WHERE id=%s",
                    (active, rid))


def set_role(rid: int, role: str) -> None:
    """Set a responsible's role ('admin' | 'employee') for the upcoming unified login."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_responsibles SET role=%s WHERE id=%s",
                    (role, rid))


def interview_count(rid: int) -> int:
    """How many iv_interviews rows reference this responsible (ANY status). Non-zero means
    the account can't be hard-deleted (its FK has no ON DELETE) — deactivate it instead so
    the interview history is preserved."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("SELECT COUNT(*) FROM iv_interviews WHERE responsible_id=%s", (rid,))
        return int(cur.fetchone()[0])


def delete_responsible(rid: int) -> None:
    """Hard-delete a responsible. Their iv_availability rows cascade (ON DELETE CASCADE);
    their iv_interviews rows do NOT (the FK has no cascade, on purpose — history is kept),
    so this raises psycopg2.IntegrityError when any interview still references them. Callers
    must check interview_count() first and deactivate such accounts instead of deleting."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM iv_responsibles WHERE id=%s", (rid,))


# ---- availability --------------------------------------------------------------
def get_availability(rid: int) -> list[dict]:
    """A responsible's availability WINDOWS (0..N per weekday), ordered by (dow, start). A
    weekday can hold several (e.g. 06:30–14:00 AND 18:00–01:00); a weekday with none is a day
    off. Returns only the STORED windows (not the old one-per-dow padded-to-7 shape)."""
    with mail_db._cur() as cur:
        cur.execute("SELECT dow, start_min, end_min, enabled FROM iv_availability "
                    "WHERE responsible_id=%s ORDER BY dow, start_min", (rid,))
        return [dict(r) for r in cur.fetchall()]


def set_availability(rid: int, rows: list[dict]) -> None:
    """REPLACE a responsible's ENTIRE weekly availability with `rows` (each = {dow, start_min,
    end_min, enabled?}), supporting MULTIPLE windows per weekday. Delete-all + insert in one
    transaction so removed windows/days actually clear. Only enabled windows are stored."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM iv_availability WHERE responsible_id=%s", (rid,))
        for row in rows or []:
            if not row.get("enabled", True):
                continue
            cur.execute(
                "INSERT INTO iv_availability (responsible_id, dow, start_min, end_min, enabled) "
                "VALUES (%s,%s,%s,%s,TRUE)",
                (rid, int(row["dow"]), int(row["start_min"]), int(row["end_min"])))


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


def interviews_for_week(since, until) -> list[dict]:
    """Non-cancelled interviews with start_ts in [since, until) across ALL responsibles —
    the per-interviewer weekly load view on /users. Caller groups by responsible_id."""
    with mail_db._cur() as cur:
        cur.execute(
            "SELECT id, responsible_id, mailbox, company, jobid, start_ts, end_ts "
            "FROM iv_interviews WHERE status <> 'cancelled' AND responsible_id IS NOT NULL "
            "AND start_ts >= %s AND start_ts < %s ORDER BY start_ts", (since, until))
        return [dict(r) for r in cur.fetchall()]


def week_signature(since, until) -> str:
    """A cheap change-signature of this week's interviews for the /users auto-refresh: count
    of non-cancelled interviews in the window + the latest created_at (epoch). Changes on any
    assign / reassign / cancel, so a polling tab knows when to refresh."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute(
            "SELECT count(*), COALESCE(EXTRACT(EPOCH FROM max(created_at))::bigint, 0) "
            "FROM iv_interviews WHERE status <> 'cancelled' AND responsible_id IS NOT NULL "
            "AND start_ts >= %s AND start_ts < %s", (since, until))
        n, mx = cur.fetchone()
    return f"{n}:{mx}"


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


def active_interview_for_thread(mailbox: str, thread_key: str) -> dict | None:
    """The latest NON-cancelled interview for (mailbox, thread_key), or None — the current
    booking a «Назначено» control edits (a cancelled one must read back as unassigned)."""
    with mail_db._cur() as cur:
        cur.execute("SELECT * FROM iv_interviews WHERE mailbox=%s AND thread_key=%s "
                    "AND status <> 'cancelled' ORDER BY created_at DESC LIMIT 1",
                    (mailbox, thread_key))
        row = cur.fetchone()
        return dict(row) if row else None


def cancel_active_for_thread(mailbox: str, thread_key: str, exclude_id: int | None = None) -> int:
    """Cancel every non-cancelled interview for (mailbox, thread_key) — optionally keeping
    `exclude_id` (the just-inserted replacement). Returns the number cancelled. Used by the
    explicit «Отменить» and by reassign (assign inserts the new booking, then cancels the old)."""
    sql = ("UPDATE iv_interviews SET status='cancelled' "
           "WHERE mailbox=%s AND thread_key=%s AND status <> 'cancelled'")
    args: list = [mailbox, thread_key]
    if exclude_id is not None:
        sql += " AND id <> %s"
        args.append(exclude_id)
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute(sql, tuple(args))
        return cur.rowcount


def assignments_for_mailboxes(mailboxes) -> dict:
    """{mailbox: {"id","responsible_id","responsible_name","start_ts","thread_key"}} — the
    latest NON-cancelled interview each persona mailbox has, for badging «Назначено · <name>»
    on the candidate cards. One row-query + one names-query; missing/empty input → {}."""
    mbs = [m for m in (mailboxes or []) if m]
    if not mbs:
        return {}
    with mail_db._cur() as cur:
        cur.execute(
            "SELECT DISTINCT ON (mailbox) mailbox, id, responsible_id, start_ts, thread_key "
            "FROM iv_interviews WHERE mailbox = ANY(%s) AND status <> 'cancelled' "
            "ORDER BY mailbox, created_at DESC", (mbs,))
        rows = [dict(r) for r in cur.fetchall()]
    rids = list({r["responsible_id"] for r in rows if r.get("responsible_id")})
    names: dict = {}
    if rids:
        with mail_db._cur() as cur:
            cur.execute("SELECT id, name FROM iv_responsibles WHERE id = ANY(%s)", (rids,))
            names = {r["id"]: r["name"] for r in cur.fetchall()}
    return {r["mailbox"]: {
        "id": r["id"],
        "responsible_id": r.get("responsible_id"),
        "responsible_name": names.get(r.get("responsible_id")),
        "start_ts": r.get("start_ts"),
        "thread_key": r.get("thread_key") or "",
    } for r in rows}


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


# ---- announcements (notifier daemon) ----------------------------------------------
def due_announcements() -> list[dict]:
    """Assigned interviews with a responsible that have not yet been announced — the
    one-time "interview assigned" notification, regardless of start_ts."""
    with mail_db._cur() as cur:
        cur.execute(
            "SELECT * FROM iv_interviews "
            "WHERE status='assigned' AND announced=FALSE AND responsible_id IS NOT NULL "
            "ORDER BY start_ts")
        return [dict(r) for r in cur.fetchall()]


def mark_announced(interview_id: int) -> None:
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_interviews SET announced=TRUE WHERE id=%s", (interview_id,))
