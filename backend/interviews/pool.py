"""The interview POOL for the manager-delegation tier.

An "interview" that the admin divides among managers is a persona MAILBOX that has an
interview invitation — i.e. a mailbox whose furthest inbound mail stage is `interview`
(mail_index kind='interview'; the same funnel ranking as tools/stats.py). A mailbox is
"unallocated" (in the free pool the admin can split) until it has a non-cancelled
iv_interviews row — a manager delegation row OR a direct «Собес» booking — so the pool
and the direct-booking path never serve the same interview twice.

This module only READS mail_index (via the shared mail_db pool) to enumerate the pool +
the latest interview message's metadata (subject / recruiter / hash) for display and for
linking the interview thread; the delegation rows themselves live in `db` (iv_interviews).
No stack names, neutral display. Nothing here mutates mail_index.
"""
from __future__ import annotations

from backend.interviews import db
from backend.tools import mail_db

# The per-candidate funnel ranking, reused so the pool agrees with the grouped inbox's
# «Собес» stage (a candidate that progressed PAST interview, e.g. to an offer, is not in
# the interview pool).
_STAGE_SQL = mail_db._FURTHEST_STAGE_SQL

# One SELECT: mailboxes at the interview stage, with the latest interview message's meta,
# minus any already-handled (non-cancelled iv_interviews row) mailbox. Ordered newest-first.
_UNALLOCATED_SQL = f"""
WITH stage AS (
    SELECT mailbox, {_STAGE_SQL} AS stage
      FROM mail_index GROUP BY mailbox
),
latest AS (
    SELECT DISTINCT ON (mailbox)
           mailbox, candidate, subject, from_email, from_name, date_ts, path_hash
      FROM mail_index
     WHERE kind='interview' AND NOT outbound
     ORDER BY mailbox, date_ts DESC, path_hash DESC
)
SELECT l.mailbox, l.candidate, l.subject, l.from_email, l.from_name, l.date_ts,
       l.path_hash AS source_hash
  FROM stage s
  JOIN latest l ON l.mailbox = s.mailbox
  LEFT JOIN (SELECT DISTINCT mailbox FROM iv_interviews WHERE status <> 'cancelled') iv
         ON iv.mailbox = s.mailbox
 WHERE s.stage = 'interview' AND iv.mailbox IS NULL
 ORDER BY l.date_ts DESC, l.mailbox
"""

# Common ATS / no-reply domains whose label is NOT the hiring company — don't show them
# as the "company" (better to show nothing than a misleading ATS name).
_ATS_DOMAINS = {
    "greenhouse", "greenhouse-mail", "ashbyhq", "hire", "lever", "myworkday",
    "myworkdayjobs", "icims", "smartrecruiters", "workable", "workablemail",
    "us", "taleo", "oraclecloud", "avature", "gmail", "googlemail", "notifications",
    "bounce", "no-reply", "noreply", "e", "mail", "email", "mkt", "info",
}


def company_hint(from_email: str, from_name: str = "") -> str:
    """A best-effort neutral company label for a pool row: the recruiter's org from the
    email domain, unless it is a generic ATS/no-reply domain (then blank). Never a stack
    name, never misleading — blank is fine, the subject already identifies the interview."""
    dom = (from_email or "").split("@")[-1].strip().lower()
    if not dom or "." not in dom:
        return ""
    label = dom.split(".")[0]
    # a subdomain like "jobs.acme.com" → prefer the registrable label "acme"
    parts = [p for p in dom.split(".") if p]
    if len(parts) >= 3 and parts[0] in _ATS_DOMAINS:
        label = parts[1]
    if label in _ATS_DOMAINS or len(label) <= 2:
        return ""
    return label[:40]


def unallocated(limit: int | None = 500, offset: int = 0) -> list[dict]:
    """The free interview pool (unallocated persona mailboxes), newest-invitation first.
    Each row: {mailbox, candidate, subject, from_email, from_name, date_ts, source_hash,
    company}. Best-effort — a DB hiccup returns []."""
    sql = _UNALLOCATED_SQL
    args: list = []
    if limit is not None:
        sql += " LIMIT %s OFFSET %s"
        args = [int(limit), int(offset)]
    try:
        with mail_db._cur() as cur:
            cur.execute(sql, tuple(args))
            rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    for r in rows:
        r["company"] = company_hint(r.get("from_email") or "", r.get("from_name") or "")
    return rows


def count_unallocated() -> int:
    """How many interviews are in the free pool right now."""
    try:
        with mail_db._cur(dict_rows=False) as cur:
            cur.execute(f"SELECT COUNT(*) FROM ({_UNALLOCATED_SQL}) q")
            return int(cur.fetchone()[0])
    except Exception:
        return 0


def allocate_specific(mailbox: str, manager_id: int) -> bool:
    """Send ONE specific pool interview to a manager. No-op (returns False) if the mailbox
    is not currently in the free pool (already handled / not an interview)."""
    row = None
    for r in unallocated(limit=None):
        if r["mailbox"] == mailbox:
            row = r
            break
    if row is None:
        return False
    db.allocate_interview(
        mailbox=row["mailbox"], manager_id=manager_id,
        company=row.get("company") or "", subject=(row.get("subject") or "")[:400],
        source_message_hash=row.get("source_hash") or "")
    return True


def split(manager_counts: dict[int, int]) -> dict[int, int]:
    """Divide the free pool across managers: allocate `manager_counts[mid]` interviews to
    each manager (blocks in newest-first order; equal or custom counts). Returns
    {manager_id: number actually allocated} (capped by the pool size). Admin-only tool."""
    wanted = {int(mid): max(0, int(n)) for mid, n in (manager_counts or {}).items() if int(n) > 0}
    total = sum(wanted.values())
    if total <= 0:
        return {}
    rows = unallocated(limit=total)   # fetch exactly as many as we intend to hand out
    out: dict[int, int] = {mid: 0 for mid in wanted}
    i = 0
    for mid, n in wanted.items():
        for _ in range(n):
            if i >= len(rows):
                return out
            r = rows[i]
            db.allocate_interview(
                mailbox=r["mailbox"], manager_id=mid,
                company=r.get("company") or "", subject=(r.get("subject") or "")[:400],
                source_message_hash=r.get("source_hash") or "")
            out[mid] += 1
            i += 1
    return out
