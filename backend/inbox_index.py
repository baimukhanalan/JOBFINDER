"""Read each profile's application Maildir and classify messages for the tracker.

Runs under the `mail` group (via `sg mail`) because the Maildir is vmail:mail 2770.
Reuses the proven maildir reader from the amaskills CRM, writes one flat JSON per
profile (uploads/inbox/<profile_id>.json) that the jobfinder dashboard (running as
`programmer`, no mail-group access) can read.

Which mailbox belongs to whom comes from the optional `mailbox` field in
backend/data/profiles.json. If NO profile declares one (e.g. an old profiles.json
on disk), it falls back to the original hardcoded michael mailbox so the cron
keeps working unchanged.

Categories: interview | assessment | rejection | ack | other
"""
import os, sys, json, re

# Make `backend.*` importable when run as a plain script from cron.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Reuse the CRM's maildir reader (handles parsing, safe paths, multipart bodies).
sys.path.insert(0, "/home/projects/amaskills/crm")
import maildir_reader as mr  # noqa: E402

MAIL_BASE = "/var/mail/vhosts"
# Legacy fallback (profiles.json without any `mailbox` fields).
FALLBACK = ("michael", "michaelheck@amaskills.com")
OUT_DIR = "/home/projects/jobfinder/uploads/inbox"

# Order matters: first match wins. Rejection before interview so "unfortunately ...
# we will not move to interview" is classified as a rejection, not an interview.
RULES = [
    ("rejection", re.compile(
        r"\b(unfortunately|regret to inform|not (?:be )?moving forward|will not be moving|"
        r"other candidates|decided (?:not|to pursue other)|won'?t be proceeding|"
        r"no longer (?:be )?consider|has been filled|not (?:been )?select|"
        r"pursue other candidates|not (?:a )?(?:match|fit) at this time|wish you (?:the best|luck))\b",
        re.I)),
    ("assessment", re.compile(
        r"\b(assessment|skills? (?:test|evaluation|assessment)|typing test|"
        r"online test|complete the (?:following|assessment|test)|hackerrank|coderbyte|"
        r"codility|testgorilla|take[- ]home|questionnaire|please complete|"
        r"pre[- ]employment|aptitude)\b",
        re.I)),
    ("interview", re.compile(
        r"\b(interview|phone screen|schedule (?:a )?(?:call|chat|time|meeting)|"
        r"book a time|calendly|availability|times that work|set up a (?:call|chat)|"
        r"speak with you|meet with|next steps?|move forward|like to (?:chat|talk|connect)|"
        r"hop on a call|video call|zoom (?:call|meeting)|google meet)\b",
        re.I)),
    ("ack", re.compile(
        r"\b(received your application|thank you for (?:applying|your application)|"
        r"we have received|application (?:has been )?received|"
        r"your application (?:to|for|has)|successfully (?:applied|submitted)|"
        r"thanks for applying)\b",
        re.I)),
]


def classify(subject, body):
    text = f"{subject}\n{body}"[:8000]
    for label, rx in RULES:
        if rx.search(text):
            return label
    return "other"


def targets():
    """[(profile_id, mailbox)] to index. Profiles with a `mailbox` field, or the
    legacy michael fallback when none declares one."""
    try:
        from backend.profiles.store import load_profiles
        pairs = [(p.id, p.mailbox) for p in load_profiles().values() if p.mailbox]
    except Exception as e:
        print(f"profiles unreadable ({e}) — using fallback", file=sys.stderr)
        pairs = []
    return pairs or [FALLBACK]


# ---------------------------------------------------------------------------
# N2: new-message differ
# ---------------------------------------------------------------------------

def _msg_key(m: dict) -> str:
    """Stable identity for a message: use message-id if present, else hash of
    date+from+subject so the differ works even without a message-id header."""
    mid = (m.get("id") or "").strip()
    if mid:
        return mid
    import hashlib
    raw = f"{m.get('date','')}\x00{m.get('from','')}\x00{m.get('subject','')}"
    return hashlib.sha1(raw.encode()).hexdigest()


def new_messages(prev_msgs: list, cur_msgs: list) -> list:
    """Return messages in cur_msgs whose key was NOT in prev_msgs."""
    prev_keys = {_msg_key(m) for m in prev_msgs}
    return [m for m in cur_msgs if _msg_key(m) not in prev_keys]


# ---------------------------------------------------------------------------
# N3: company-token matcher + advance-decision
# ---------------------------------------------------------------------------

_ALNUM_RE = re.compile(r"[^a-z0-9]")


def _company_token(name: str) -> str:
    """Lowercase alnum-only version of a company name."""
    return _ALNUM_RE.sub("", (name or "").lower())


def _registrable_part(domain: str) -> str:
    """'mail.greenhouse.io' -> 'greenhouse'  (strip one TLD suffix and subdomains).
    Best-effort: takes the second-to-last label, stripping the final TLD."""
    parts = domain.lower().rstrip(".").split(".")
    if len(parts) >= 2:
        return parts[-2]
    return domain.lower()


def match_company(sender: str, subject: str, tokens: dict) -> str | None:
    """Return the single jid whose company token appears in sender domain or subject.

    tokens: {company_token: jid}
    Rules:
    - Short tokens (< 4 chars) are never matched.
    - Zero or multiple matches -> None (never guess).
    """
    # Extract sender domain from "Name <addr>" or bare addr
    domain = ""
    addr_match = re.search(r"@([\w.\-]+)", sender or "")
    if addr_match:
        domain = _registrable_part(addr_match.group(1))

    subj_norm = _ALNUM_RE.sub("", (subject or "").lower())

    hits = []
    for tok, jid in tokens.items():
        if len(tok) < 4:
            continue
        if tok in domain or tok in subj_norm:
            hits.append(jid)

    if len(hits) == 1:
        return hits[0]
    return None


def advance_decision(kind: str, current_status: str) -> str | None:
    """Decide the new status for a job given an email kind and its current status.

    Returns new status string or None (no change).

    Rules:
    - interview/assessment -> "interview" when current is pending/submitted/""
    - rejection -> "rejected" unless current is already "rejected"
    - ack -> "submitted" when current is pending/"" (a confirmation email is
      evidence the submit happened without being recorded); never touches
      submitted/interview/rejected
    - other -> None
    - never demote "interview" to "submitted" etc. (only forward transitions)
    """
    if kind in ("interview", "assessment"):
        if current_status not in ("interview", "rejected"):
            return "interview"
    elif kind == "rejection":
        if current_status != "rejected":
            return "rejected"
    elif kind == "ack":
        if current_status in ("", "pending"):
            return "submitted"
    return None


def _build_company_tokens(profile_id: str) -> dict:
    """Scan uploads/prefill/<profile>/ and build {company_token: jid} map.

    Skips reports with company names shorter than 4 chars.
    """
    import json as _json
    from pathlib import Path
    prefill_root = Path(__file__).resolve().parents[1] / "uploads" / "prefill"
    tokens: dict[str, str] = {}
    for rep in (prefill_root / profile_id).glob("*/report.json"):
        try:
            r = _json.loads(rep.read_text(encoding="utf-8"))
        except Exception:
            continue
        tok = _company_token(r.get("company", ""))
        if len(tok) >= 4:
            jid = rep.parent.name
            tokens[tok] = jid  # last write wins if two jobs have same token
    return tokens


def _auto_advance(profile_id: str, messages: list) -> None:
    """For interview/assessment/rejection/ack messages, try to match to a job by
    company and advance its status automatically (conservative: skip if 0 or >1
    match). An ack (application-received confirmation) records a submit that
    happened without being tracked.

    Mirrors _apply_mark's shape via status_store — noted in comment there.
    """
    from backend import status_store

    tokens = _build_company_tokens(profile_id)
    if not tokens:
        return

    # Load current statuses once (status_store.load returns {jid: {"status": ...}})
    current_statuses = status_store.load(profile_id)

    for m in messages:
        kind = m.get("category", "other")
        if kind not in ("interview", "assessment", "rejection", "ack"):
            continue
        jid = match_company(m.get("from", ""), m.get("subject", ""), tokens)
        if jid is None:
            continue
        cur = (current_statuses.get(jid) or {}).get("status", "")
        new_status = advance_decision(kind, cur)
        if new_status:
            status_store.mark(profile_id, jid, new_status)
            # Keep the in-run view current so a later ack for the same jid
            # can't demote a status advanced earlier in this loop.
            current_statuses[jid] = {"status": new_status}
            print(f"auto-advance {profile_id}/{jid}: {cur!r} -> {new_status!r} "
                  f"(email kind={kind})")


# ---------------------------------------------------------------------------
# Core indexer
# ---------------------------------------------------------------------------

def index_mailbox(mailbox, out_path, profile_id=None):
    msgs = [m for m in mr.list_messages(MAIL_BASE) if m["mailbox"] == mailbox]
    out = []
    for m in msgs:
        full = mr.read_message(MAIL_BASE, m["id"])
        body = (full or {}).get("body", "") or ""
        cat = classify(m["subject"], body)
        out.append({
            "id": m["id"],
            "from": m["from"],
            "subject": m["subject"],
            "date": m["date"],
            "unread": m["unread"],
            "category": cat,
            "preview": re.sub(r"\s+", " ", body).strip()[:240],
        })

    # N2: load previous snapshot to diff for new interview/assessment messages.
    prev_msgs: list = []
    try:
        import json as _json
        prev_msgs = _json.loads(open(out_path).read()).get("messages", [])
    except Exception:
        pass

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    counts = {}
    for m in out:
        counts[m["category"]] = counts.get(m["category"], 0) + 1
    payload = {"mailbox": mailbox, "total": len(out), "counts": counts, "messages": out}
    # Write atomically so the dashboard never reads a half-written file.
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out_path)
    # Make readable by the dashboard user (script runs with mail group / possibly root).
    try:
        os.chmod(out_path, 0o644)
    except OSError:
        pass
    print(f"indexed {len(out)} msgs -> {out_path} | {counts}")

    # N2: ping for new interview/assessment messages (cap 5 per run).
    if profile_id:
        new = new_messages(prev_msgs, out)
        alerts = [m for m in new if m.get("category") in ("interview", "assessment")][:5]
        if alerts:
            try:
                from bot.notify import notify
                for m in alerts:
                    kind = m.get("category", "")
                    notify(f"\U0001f3af {profile_id}: {kind} — {m.get('from','')}\n{m.get('subject','')}")
            except Exception as e:
                print(f"notify failed: {e}", file=sys.stderr)

    # N3: auto-advance application statuses from email.
    if profile_id:
        try:
            _auto_advance(profile_id, out)
        except Exception as e:
            print(f"auto-advance failed: {e}", file=sys.stderr)


def main():
    for profile_id, mailbox in targets():
        index_mailbox(mailbox, os.path.join(OUT_DIR, f"{profile_id}.json"),
                      profile_id=profile_id)


if __name__ == "__main__":
    main()
