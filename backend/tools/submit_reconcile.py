"""Reconcile the «Незавершённые» ledger against GROUND TRUTH — the ATS confirmation
emails in each persona's Maildir.

A bulk-apply job clicks Submit and lands in the ledger as "unconfirmed" because our live
page-watch didn't SEE the confirmation in time — but the ATS emails "we received your
application" (or an interview/rejection) minutes later. That email is proof the submit
actually reached the ATS. This reads it, marks the job `submitted` (or interview/rejected)
in status_store, and removes it from the ledger — so «Незавершённые» reflects reality
instead of our detection latency.

Forward-only (never demotes a status). Read-only w.r.t. the Maildir. Must run under the
`mail` group (the dashboard does — it's launched with `sg mail`).
"""
import email
import glob
import json
import logging
import os
from email import policy
from pathlib import Path

from backend import status_store
from backend.tools import bulk_log, mailcrm

logger = logging.getLogger(__name__)

MAILROOT = "/var/mail/vhosts/takhet.com"
# backend/tools/submit_reconcile.py → parents[2] is the repo root (uploads/ is there).
_PREFILL = Path(__file__).resolve().parents[2] / "uploads" / "prefill"

# Any of these classifier kinds means the application REACHED the ATS (it emailed back).
_RECEIVED_KINDS = {"ack", "interview", "offer", "rejection"}
# Priority when a mailbox holds several: a decision/offer/interview outranks a bare ack.
_KIND_RANK = {"offer": 3, "interview": 3, "rejection": 2, "ack": 1}


def _advance(kind: str) -> str | None:
    """Map a classifier kind to the forward status it justifies."""
    if kind in ("offer", "interview"):
        return "interview"
    if kind == "rejection":
        return "rejected"
    if kind == "ack":
        return "submitted"
    return None


def _personas_for_job(jid: str) -> list[tuple[str, str]]:
    """ALL (profile_id, email) variants for a job, newest first. A job re-applied across
    bulk runs gets a FRESH persona each time (each with its own @takhet.com mailbox); the
    ATS receipt may sit in an EARLIER persona's mailbox, not the newest. Checking only the
    newest (which is mail-less on a re-run) is why a genuinely-submitted job stayed parked
    in the ledger. Dedup on (profile, addr)."""
    out: list[tuple[str, str]] = []
    seen: set = set()
    metas = sorted(_PREFILL.glob(f"demo_*/{jid}/persona.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for pj in metas:
        try:
            p = json.loads(pj.read_text(encoding="utf-8"))
            addr = ((p.get("profile") or {}).get("email") or "").strip()
            prof = pj.parent.parent.name
            key = (prof, addr)
            if addr and key not in seen:
                seen.add(key)
                out.append((prof, addr))   # (demo_xxx, first.last@takhet.com)
        except Exception:
            continue
    return out


def _persona_for_job(jid: str) -> tuple[str | None, str | None]:
    """(profile_id, email) for a job's NEWEST persona.json (back-compat single-variant)."""
    variants = _personas_for_job(jid)
    return variants[0] if variants else (None, None)


def _plain_body(msg) -> str:
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    return part.get_content()
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    return part.get_content()
            return ""
        return msg.get_content()
    except Exception:
        return ""


def _db_evidence(addr: str) -> tuple[str, str] | None:
    """Best (kind, subject) receipt for a mailbox from the Postgres mail_index. Preferred
    over the disk scan: it's already classified, faster, immune to a retention prune racing
    reconcile, and needs no `mail`-group disk access. Returns None on any error → caller
    falls back to the disk scan."""
    if not addr:
        return None
    try:
        from backend.tools import mail_db
        with mail_db._cur() as cur:
            cur.execute(
                "SELECT kind, subject FROM mail_index "
                "WHERE lower(mailbox)=%s AND NOT outbound AND kind = ANY(%s)",
                (addr.lower(), list(_RECEIVED_KINDS)))
            rows = cur.fetchall()
    except Exception:
        return None
    best, best_rank = None, 0
    for r in rows:
        kind = r["kind"] if isinstance(r, dict) else r[0]
        subj = (r["subject"] if isinstance(r, dict) else r[1]) or ""
        if _KIND_RANK.get(kind, 0) > best_rank:
            best, best_rank = (kind, subj), _KIND_RANK[kind]
    return best


def _evidence(addr: str) -> tuple[str, str] | None:
    """ATS-receipt evidence for a mailbox: Postgres mail_index first, disk scan fallback."""
    return _db_evidence(addr) or _mailbox_evidence(addr)


def _mailbox_evidence(addr: str) -> tuple[str, str] | None:
    """Best (kind, subject) proving the ATS received the application, or None (disk scan)."""
    local = (addr or "").split("@")[0]
    if not local:
        return None
    md = os.path.join(MAILROOT, local)
    best = None
    best_rank = 0
    for sub in ("new", "cur"):
        for f in glob.glob(os.path.join(md, sub, "*")):
            if not os.path.isfile(f):
                continue
            try:
                with open(f, "rb") as fh:
                    m = email.message_from_binary_file(fh, policy=policy.default)
            except Exception:
                continue
            subj = str(m["subject"] or "")
            kind = mailcrm.classify(subj, _plain_body(m))
            if kind in _RECEIVED_KINDS and _KIND_RANK.get(kind, 0) > best_rank:
                best, best_rank = (kind, subj), _KIND_RANK[kind]
    return best


def reconcile_ledger() -> dict:
    """Walk the «Незавершённые» ledger; for any job whose persona's Maildir already holds
    an ATS receipt/decision email, advance its status and clear it from the ledger.
    Returns {checked, reconciled, details:[...]} — never raises for one bad job."""
    jobs = bulk_log.unfinished()
    reconciled = 0
    details = []
    for job in jobs:
        jid = str(job.get("jobid") or "")
        if not jid or jid == "None":
            continue
        # Check EVERY persona that ever applied to this job (not just the newest) — the
        # receipt may be in an earlier persona's mailbox after a re-run. First variant with
        # an ATS receipt wins.
        variants = _personas_for_job(jid)
        hit = None
        for prof, a in variants:
            ev = _evidence(a)
            if ev:
                hit = (prof, a, ev)
                break
        if not hit:
            continue
        profile, addr, ev = hit
        kind, subj = ev
        new = _advance(kind)
        if not new:
            continue
        try:
            cur = (status_store.load(profile) or {}).get(jid) or {}
            cur_status = cur.get("status", "") if isinstance(cur, dict) else ""
            if new == "submitted" and cur_status in ("", "pending"):
                status_store.mark(profile, jid, "submitted")
            elif new in ("interview", "rejected"):
                status_store.mark(profile, jid, new)
            bulk_log.mark_done(jid)
            reconciled += 1
            details.append({"jobid": jid, "company": job.get("company"),
                            "kind": kind, "status": new, "subject": subj[:80]})
            logger.info("submit_reconcile: %s (%s) -> %s via %r",
                        jid, job.get("company"), new, subj[:60])
        except Exception:
            logger.warning("submit_reconcile failed for job %s", jid, exc_info=True)
    return {"checked": len(jobs), "reconciled": reconciled, "details": details}
