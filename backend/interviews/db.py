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
def _has_column(cur, table: str, column: str) -> bool:
    """True if `table.column` already exists — the information_schema check that gates
    every additive ALTER (repo DDL rule: never a bare ALTER; add only a genuinely-missing
    column, under a bounded lock_timeout, so a nightly/boot ensure_schema can't hostage the
    table)."""
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name=%s AND column_name=%s", (table, column))
    return cur.fetchone() is not None


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
        # additive reminder windows (-2h prep/bring-ID, -15m join-now) beside the original -60/-5.
        # boolean DEFAULT FALSE => a constant default, no table rewrite (fast ALTER).
        cur.execute("ALTER TABLE iv_interviews "
                    "ADD COLUMN IF NOT EXISTS reminded_120 BOOLEAN NOT NULL DEFAULT FALSE;")
        cur.execute("ALTER TABLE iv_interviews "
                    "ADD COLUMN IF NOT EXISTS reminded_15 BOOLEAN NOT NULL DEFAULT FALSE;")
        cur.execute("CREATE INDEX IF NOT EXISTS iv_interviews_responsible_idx "
                    "ON iv_interviews (responsible_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS iv_interviews_mailbox_idx "
                    "ON iv_interviews (mailbox);")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS iv_interviews_nodouble "
                    "ON iv_interviews (responsible_id, start_ts) "
                    "WHERE responsible_id IS NOT NULL AND status <> 'cancelled';")

        # ---- manager tier (role 'manager'; hierarchy admin > manager > employee) -------
        # A subordinate's supervising manager, and which manager an interview is
        # allocated to. Both nullable self-FKs on iv_responsibles, added only when
        # genuinely missing and under a bounded lock (SET LOCAL is transaction-scoped; the
        # mail_db pool commits per-_cur block, so it applies to the ALTER that follows).
        if not _has_column(cur, "iv_responsibles", "manager_id"):
            cur.execute("SET LOCAL lock_timeout='15s'")
            cur.execute("ALTER TABLE iv_responsibles "
                        "ADD COLUMN manager_id INTEGER REFERENCES iv_responsibles(id)")
        if not _has_column(cur, "iv_interviews", "manager_id"):
            cur.execute("SET LOCAL lock_timeout='15s'")
            cur.execute("ALTER TABLE iv_interviews "
                        "ADD COLUMN manager_id INTEGER REFERENCES iv_responsibles(id)")
        cur.execute("CREATE INDEX IF NOT EXISTS iv_responsibles_manager_idx "
                    "ON iv_responsibles (manager_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS iv_interviews_manager_idx "
                    "ON iv_interviews (manager_id);")

        # ---- multi-role: a user may hold SEVERAL roles at once (admin AND manager AND
        # employee); capabilities are the UNION. `roles TEXT[]` is authoritative; the legacy
        # single `role` column is kept mirrored to the PRIMARY (highest-precedence) role for
        # any reader that still reads it. Added under the DDL rule, then backfilled from role.
        if not _has_column(cur, "iv_responsibles", "roles"):
            cur.execute("SET LOCAL lock_timeout='15s'")
            cur.execute("ALTER TABLE iv_responsibles ADD COLUMN roles TEXT[]")
        cur.execute("UPDATE iv_responsibles SET roles=ARRAY[role] "
                    "WHERE roles IS NULL OR cardinality(roles)=0")


# ---- roles (multi-role: a user may hold several at once; capabilities = the UNION) -----
VALID_ROLES = ("admin", "manager", "employee")
_ROLE_RANK = {"admin": 3, "manager": 2, "employee": 1}


def primary_role(roles) -> str:
    """The highest-precedence role in a set (admin > manager > employee) — the value mirrored
    into the legacy `role` column and used for HOME routing. Empty → 'employee'."""
    rs = [r for r in (roles or []) if r in _ROLE_RANK]
    return max(rs, key=lambda r: _ROLE_RANK[r]) if rs else "employee"


def normalize_roles(roles) -> list[str]:
    """Clean a role set: keep only valid roles, dedup, order high→low, default ['employee']
    when empty. So a user is never role-less (they'd be unreachable at the gate)."""
    seen = {r for r in (roles or []) if r in _ROLE_RANK}
    ordered = [r for r in ("admin", "manager", "employee") if r in seen]
    return ordered or ["employee"]


def roles_of(resp: dict | None) -> list[str]:
    """A responsible's role SET — `roles` if present, else the legacy single `role`. The one
    place callers should read roles from, so the array/legacy fallback lives in one spot."""
    if not resp:
        return []
    rs = resp.get("roles")
    if rs:
        return list(rs)
    r = resp.get("role")
    return [r] if r else []


def has_role(resp: dict | None, role: str) -> bool:
    """True if the responsible holds `role` (checks the full set, not just the primary)."""
    return role in roles_of(resp)


# ---- responsibles ------------------------------------------------------------------
def add_responsible(login: str, password_hash: str, name: str, tz: str = "UTC",
                    role: str = "employee", manager_id: int | None = None,
                    roles: list[str] | None = None) -> int:
    """Insert a new responsible. Raises psycopg2.IntegrityError on a duplicate login.

    `roles` (optional) is the multi-role set; when omitted it defaults to [`role`] for
    backward compatibility. The legacy `role` column is stored as the PRIMARY of the set.
    `manager_id` (optional) links a subordinate to their supervising manager (the manager
    tier): a manager creating an employee passes their own id, so the employee shows up in
    the manager's portal and only that manager (or an admin) may assign their interviews."""
    norm = normalize_roles(roles if roles is not None else [role])
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute(
            "INSERT INTO iv_responsibles (login, password_hash, name, tz, role, roles, manager_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (login, password_hash, name, tz, primary_role(norm), norm, manager_id))
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


def set_roles(rid: int, roles: list[str]) -> None:
    """REPLACE a responsible's role SET (multi-role). Normalised (valid, dedup, never empty);
    the legacy `role` column is kept mirrored to the PRIMARY. Capabilities are the UNION of
    the set — see dash_auth (access) and routes_manage (`has_role`)."""
    norm = normalize_roles(roles)
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_responsibles SET roles=%s, role=%s WHERE id=%s",
                    (norm, primary_role(norm), rid))


def set_role(rid: int, role: str) -> None:
    """Set a responsible to a SINGLE role (backward-compat shim over set_roles)."""
    set_roles(rid, [role])


def set_manager(rid: int, manager_id: int | None) -> None:
    """Set (or clear, with None) which manager supervises this responsible. A subordinate
    with a manager appears in that manager's portal; the manager (or an admin) may assign
    their interviews. Clearing it (None) detaches them back to the admin's direct pool."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_responsibles SET manager_id=%s WHERE id=%s",
                    (manager_id, rid))


def subordinates(manager_id: int, active_only: bool = True) -> list[dict]:
    """Every responsible whose manager_id is `manager_id` (a manager's team), ordered by id."""
    sql = "SELECT * FROM iv_responsibles WHERE manager_id=%s"
    if active_only:
        sql += " AND active=TRUE"
    sql += " ORDER BY id"
    with mail_db._cur() as cur:
        cur.execute(sql, (manager_id,))
        return [dict(r) for r in cur.fetchall()]


def list_managers(active_only: bool = True) -> list[dict]:
    """All responsibles who hold the 'manager' role (checks the full role SET, so an
    admin+manager is listed too)."""
    sql = "SELECT * FROM iv_responsibles WHERE 'manager' = ANY(roles)"
    if active_only:
        sql += " AND active=TRUE"
    sql += " ORDER BY id"
    with mail_db._cur() as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


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


def delete_responsible_cascade(rid: int) -> None:
    """Hard-delete ANY responsible (incl. deactivated / with interview history) by first
    SAFELY clearing every FK that references them, in ONE transaction:
      1. detach their subordinates (`iv_responsibles.manager_id` → NULL);
      2. drop the delegation rows THEY manage (`iv_interviews.manager_id`=rid) — the
         underlying interview mailbox returns to the free pool for re-allocation;
      3. their still-managed ASSIGNED interviews (someone else's pool) go back to that
         manager's pool (`responsible_id`→NULL, status='pool', time cleared, announced);
      4. delete every remaining row that still references them as attendee (direct «Собес»
         bookings + cancelled history) so the responsible_id FK no longer blocks the delete;
      5. delete the account (iv_availability cascades).
    All in one _cur() block → atomic (mail_db.conn commits on success, rolls back on error)."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_responsibles SET manager_id=NULL WHERE manager_id=%s", (rid,))
        cur.execute("DELETE FROM iv_interviews WHERE manager_id=%s", (rid,))
        cur.execute(
            "UPDATE iv_interviews SET responsible_id=NULL, status='pool', start_ts=NULL, "
            "end_ts=NULL, announced=TRUE "
            "WHERE responsible_id=%s AND status <> 'cancelled' AND manager_id IS NOT NULL",
            (rid,))
        cur.execute("DELETE FROM iv_interviews WHERE responsible_id=%s", (rid,))
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
    """Interviews assigned to a responsible, ordered by start_ts.

    `upcoming_only=True` (the cabinet «Мои собеседования» view) returns every
    ACTIVE (non-cancelled) собес assigned to them — NOT strictly `start_ts > now()`.
    A собес must stay visible to the interviewer it's assigned to until it is
    cancelled or reassigned away: the operator week grid spans the whole current
    Mon–Sun week, so an already-passed day of THIS week is bookable and yields a
    past start_ts, and a strict future filter silently hid a just-assigned собес
    (the interviewer's cabinet read empty though a собес was assigned). Only the
    cabinet dashboard renders past ones distinctly; here we just stop hiding them.
    `upcoming_only=False` returns ALL rows for the responsible (incl. cancelled)."""
    sql = "SELECT * FROM iv_interviews WHERE responsible_id=%s"
    args: list = [rid]
    if upcoming_only:
        sql += " AND status <> 'cancelled'"
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


# ---- manager tier: allocation + delegated assignment -----------------------------
# An "interview" in the delegation model is one iv_interviews row per persona mailbox:
#   * allocated to a manager     → manager_id set, status='pool', responsible_id NULL
#   * assigned by that manager   → responsible_id set (self or a subordinate), status='assigned'
# The pool of allocatable interviews is the set of persona mailboxes with an interview
# invitation (mail_index kind='interview'); a mailbox is "handled" (out of the free pool)
# once it has ANY non-cancelled iv_interviews row — a delegation row OR a direct «Собес»
# booking — so the two paths never double-serve the same interview.
def handled_pool_mailboxes() -> set:
    """Persona mailboxes that already have a non-cancelled interview row (delegated OR
    directly booked) — excluded from the free/unallocated pool the admin splits."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("SELECT DISTINCT mailbox FROM iv_interviews WHERE status <> 'cancelled'")
        return {r[0] for r in cur.fetchall()}


def allocate_interview(mailbox: str, manager_id: int, company: str = "",
                       jobid: str = "", subject: str = "",
                       source_message_hash: str = "") -> int:
    """Allocate one pool interview (a persona mailbox) to a manager: insert an unassigned
    delegation row (status='pool', responsible_id NULL). `subject` is stashed in notes for
    display. Returns the new row id. The caller guarantees the mailbox is unallocated."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute(
            "INSERT INTO iv_interviews "
            "(mailbox, thread_key, company, jobid, responsible_id, manager_id, status, "
            " source_message_hash, notes, announced) "
            "VALUES (%s,'',%s,%s,NULL,%s,'pool',%s,%s,TRUE) RETURNING id",
            (mailbox, company, jobid, manager_id, source_message_hash, subject))
        return cur.fetchone()[0]


def manager_interviews(manager_id: int) -> list[dict]:
    """Every non-cancelled interview allocated to this manager (pool + assigned), newest
    first. The spine of the manager portal + the admin read-through."""
    with mail_db._cur() as cur:
        cur.execute(
            "SELECT * FROM iv_interviews WHERE manager_id=%s AND status <> 'cancelled' "
            "ORDER BY created_at DESC, id DESC", (manager_id,))
        return [dict(r) for r in cur.fetchall()]


def interview_by_id(iid: int) -> dict | None:
    with mail_db._cur() as cur:
        cur.execute("SELECT * FROM iv_interviews WHERE id=%s", (iid,))
        row = cur.fetchone()
        return dict(row) if row else None


def manager_assign_interview(iid: int, responsible_id: int,
                             start_ts: datetime | None = None,
                             end_ts: datetime | None = None) -> None:
    """Assign (or reassign) an allocated interview to an interviewer (the manager himself or
    a subordinate). Sets responsible_id + status='assigned' and re-arms the notifier
    (announced=FALSE). `start_ts` is optional — an interview may be assigned before its exact
    time is fixed (it then shows «время не указано» until scheduled). Raises
    psycopg2.IntegrityError on a start_ts double-book (the partial unique index)."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute(
            "UPDATE iv_interviews SET responsible_id=%s, start_ts=%s, end_ts=%s, "
            "status='assigned', announced=FALSE WHERE id=%s",
            (responsible_id, start_ts, end_ts, iid))


def manager_unassign_interview(iid: int) -> None:
    """Pull an assigned interview back into the manager's unassigned pool (clears the
    interviewer + time, status='pool'). The allocation to the manager is kept."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute(
            "UPDATE iv_interviews SET responsible_id=NULL, start_ts=NULL, end_ts=NULL, "
            "status='pool', announced=TRUE WHERE id=%s", (iid,))


def deallocate_interview(iid: int) -> None:
    """Remove an UNASSIGNED delegation row (admin returns it to the global free pool). Only
    a pool row (never assigned) is deleted; an assigned one must be unassigned first."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM iv_interviews WHERE id=%s AND status='pool' "
                    "AND responsible_id IS NULL", (iid,))


def assigned_load(rids) -> dict:
    """{responsible_id: count of its non-cancelled ASSIGNED interviews} over the given ids —
    the per-subordinate load shown in the manager portal. Empty input → {}."""
    ids = [r for r in (rids or []) if r]
    if not ids:
        return {}
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute(
            "SELECT responsible_id, COUNT(*) FROM iv_interviews "
            "WHERE responsible_id = ANY(%s) AND status <> 'cancelled' "
            "GROUP BY responsible_id", (ids,))
        return {r[0]: int(r[1]) for r in cur.fetchall()}


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
    flag: 120 -> reminded_120, 60 -> reminded_60, 15 -> reminded_15, else (5) -> reminded_5."""
    flag_col = {120: "reminded_120", 60: "reminded_60", 15: "reminded_15"}.get(
        int(window_min), "reminded_5")
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
    """which ∈ {'120','60','15','5'} — sets the matching reminded_* column (default reminded_5)."""
    col = {"120": "reminded_120", "60": "reminded_60", "15": "reminded_15"}.get(str(which), "reminded_5")
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
