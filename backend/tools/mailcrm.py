"""Self-hosted candidate mail CRM engine — Gmail-style, zero third parties.

Reads each candidate's Dovecot Maildir straight off our own server
(/var/mail/vhosts/<domain>/<shard>/<local>/{new,cur}) and sends replies through
our own Postfix submission (127.0.0.1:587, SASL as the candidate mailbox). No
Mailgun, no API. Modeled on the amaskills CRM. Must run where the Maildir is
readable — the JOBFINDER dashboard runs under `sg mail` for exactly this.

Message id = sha1 of the absolute file path (stable dedup key used in URLs).
Opening a message marks it read (Maildir new/ -> cur/:2,S), like every webmail.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import (formatdate, make_msgid, parseaddr,
                         parsedate_to_datetime)
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "backend" / "data" / "profiles.json"
ADDR_FILE = ROOT / "backend" / "data" / "mail_addresses.json"
PW_FILE = ROOT / "backend" / "data" / "mailbox_passwords.json"
MAILDIR_ROOT = "/var/mail/vhosts"
SMTP_HOST, SMTP_PORT = "127.0.0.1", 587
MAX_BODY = 200_000

# ---- classification (RU/EN, offer > rejection > interview > ack > other) ----
_INTERVIEW = re.compile(r"\b(interview|schedule a call|book a time|calendly|assessment|next steps?|screening|phone screen|move forward|shortlist|собеседовани|интервью|назначить звонок|следующий этап|тестовое задание)\b", re.I)
_REJECT = re.compile(r"\b(unfortunately|not moving forward|decided not to|other candidates|won'?t be proceeding|not a (good )?fit|regret to inform|we will not|declined|к сожалению|отказ|не подошли)\b", re.I)
_OFFER = re.compile(r"\b(offer letter|pleased to offer|extend an offer|welcome aboard|предлагаем вам|оффер|job offer)\b", re.I)
_ACK = re.compile(r"\b(application received|thanks? for applying|we received your|has been received|получили ваш|заявка принята)\b", re.I)


def classify(subject: str, body: str) -> str:
    t = f"{subject}\n{body}"
    if _OFFER.search(t):
        return "offer"
    if _REJECT.search(t):
        return "rejection"
    if _INTERVIEW.search(t):
        return "interview"
    if _ACK.search(t):
        return "ack"
    return "other"


# ---- candidate registry ----------------------------------------------------
def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _maildir(local: str, domain: str) -> str:
    # UNSHARDED, matching Dovecot mail_location %d/%n (group-readable under the
    # setgid domain dir; a sharded intermediate dir is 0710 = unreadable by `mail`).
    return f"{MAILDIR_ROOT}/{domain}/{local}"


def candidates() -> list[dict]:
    """[{id,email,name,local,domain,maildir}] for every provisioned candidate."""
    profs = {p["id"]: p for p in _load(PROFILES, [])}
    out = []
    for pid, email in _load(ADDR_FILE, {}).items():
        p = profs.get(pid)
        if not p or p.get("is_sample"):
            continue
        local, _, domain = email.lower().partition("@")
        if not domain:
            continue
        out.append({"id": pid, "email": email.lower(), "name": p.get("full_name") or pid,
                    "local": local, "domain": domain, "maildir": _maildir(local, domain)})
    return out


def _by_email() -> dict[str, dict]:
    return {c["email"]: c for c in candidates()}


# ---- Maildir reading -------------------------------------------------------
def _pid(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()


def _display_name(addr: str) -> str:
    name, email = parseaddr(addr)
    return name or (email.split("@")[0] if email else addr)


def _email_only(addr: str) -> str:
    return parseaddr(addr)[1] or addr


_RE_PREFIX = re.compile(r"^(?:\s*(?:re|fwd|fw|аноним|ответ)\s*:\s*)+", re.I)


def _norm_subject(s: str) -> str:
    """Thread key: subject with Re:/Fwd: prefixes stripped, lowercased."""
    return _RE_PREFIX.sub("", (s or "").strip()).strip().lower() or "(без темы)"


def _attachments(msg) -> list[dict]:
    out = []
    i = 0
    for part in msg.walk():
        fn = part.get_filename()
        disp = (part.get_content_disposition() or "")
        if fn or disp == "attachment":
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:
                payload = b""
            out.append({"i": i, "filename": fn or f"attachment-{i}",
                        "type": part.get_content_type(), "size": len(payload)})
        i += 1
    return out


def _date_ts(msg, path: str) -> int:
    try:
        d = parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
        if d:
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return int(d.timestamp())
    except Exception:
        pass
    try:
        return int(os.path.getmtime(path))
    except Exception:
        return 0


def _iter_mailbox_files(maildir: str):
    """Yield (abs_path, seen) for every message file in a mailbox's new/ + cur/."""
    for sub, seen in (("new", 0), ("cur", 1)):
        d = os.path.join(maildir, sub)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            if fn.startswith("."):
                continue
            yield os.path.join(d, fn), seen


def _snippet(msg) -> str:
    try:
        body = msg.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body else ""
    except Exception:
        text = ""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:280]


def list_messages(mailbox: str | None = None, q: str = "",
                  limit: int = 50, before_ts: int | None = None,
                  before_id: str | None = None) -> list[dict]:
    """Newest-first message rows across candidate mailboxes (or one mailbox).
    Keyset paginated by (date_ts, id). Live off disk — no index needed."""
    boxes = candidates()
    if mailbox:
        boxes = [c for c in boxes if c["email"] == mailbox.lower()]
    ql = q.lower().strip()
    rows: list[dict] = []
    for c in boxes:
        for path, seen in _iter_mailbox_files(c["maildir"]):
            try:
                with open(path, "rb") as f:
                    msg = BytesParser(policy=policy.default).parse(f)
            except Exception:
                continue
            frm = str(msg["From"] or "")
            subj = str(msg["Subject"] or "")
            if ql and ql not in frm.lower() and ql not in subj.lower():
                continue
            snip = _snippet(msg)
            rows.append({
                "id": _pid(path), "path": path, "mailbox": c["email"],
                "candidate": c["name"], "candidate_id": c["id"],
                "from": frm, "from_name": _display_name(frm), "from_email": _email_only(frm),
                "subject": subj, "snippet": snip, "date_ts": _date_ts(msg, path),
                "seen": seen, "kind": classify(subj, snip),
                "thread": _norm_subject(subj),
                "has_att": any(p.get_filename() for p in msg.walk()),
            })
    rows.sort(key=lambda r: (r["date_ts"], r["id"]), reverse=True)
    if before_ts is not None:
        rows = [r for r in rows if (r["date_ts"], r["id"]) < (before_ts, before_id or "")]
    return rows[:limit]


def _find_by_id(path_hash: str) -> str | None:
    for c in candidates():
        for path, _ in _iter_mailbox_files(c["maildir"]):
            if _pid(path) == path_hash:
                return path
    return None


def _mark_read(path: str) -> str:
    """Move a Maildir file new/ -> cur/ with the Seen flag. Returns new path."""
    if os.sep + "new" + os.sep not in path:
        return path
    base = os.path.basename(path)
    cur_dir = os.path.join(os.path.dirname(os.path.dirname(path)), "cur")
    flagged = base if ":2," in base else base + ":2,S"
    if ":2," in flagged and "S" not in flagged.split(":2,")[1]:
        flagged = flagged + "S"
    dst = os.path.join(cur_dir, flagged)
    try:
        os.makedirs(cur_dir, exist_ok=True)
        os.rename(path, dst)
        return dst
    except OSError:
        return path


def _mailbox_of(path: str) -> dict | None:
    for c in candidates():
        if c["maildir"] in path:
            return c
    return None


def _parse_full(path: str, path_hash: str) -> dict | None:
    try:
        with open(path, "rb") as f:
            msg = BytesParser(policy=policy.default).parse(f)
    except Exception:
        return None
    plain = html = ""
    try:
        b = msg.get_body(preferencelist=("plain",))
        plain = b.get_content() if b else ""
    except Exception:
        pass
    try:
        h = msg.get_body(preferencelist=("html",))
        html = h.get_content() if h else ""
    except Exception:
        pass
    box = _mailbox_of(path)
    frm = str(msg["From"] or "")
    subj = str(msg["Subject"] or "")
    # a message is "outbound" (sent by the candidate) if From is the candidate's own address
    outbound = bool(box) and _email_only(frm).lower() == box["email"]
    return {
        "id": path_hash, "path": path,
        "mailbox": box["email"] if box else "", "candidate": box["name"] if box else "",
        "from": frm, "from_name": _display_name(frm), "from_email": _email_only(frm),
        "to": str(msg["To"] or ""), "subject": subj,
        "date": str(msg["Date"] or ""), "date_ts": _date_ts(msg, path),
        "message_id": str(msg["Message-ID"] or "").strip(),
        "plain": (plain or "")[:MAX_BODY], "html": (html or "")[:MAX_BODY],
        "attachments": _attachments(msg), "outbound": outbound,
        "kind": classify(subj, plain or _snippet(msg)), "thread": _norm_subject(subj),
    }


def get_message(path_hash: str, mark: bool = True) -> dict | None:
    path = _find_by_id(path_hash)
    if not path:
        return None
    if mark and (os.sep + "new" + os.sep) in path:
        path = _mark_read(path)
        path_hash = _pid(path)
    return _parse_full(path, path_hash)


def get_thread(path_hash: str, mark: bool = True) -> dict | None:
    """The whole conversation for a message: every mail in the SAME candidate
    mailbox sharing the normalized subject, oldest-first (Gmail-style thread)."""
    path = _find_by_id(path_hash)
    if not path:
        return None
    box = _mailbox_of(path)
    if not box:
        m = get_message(path_hash, mark=mark)
        return {"subject": m.get("subject", "") if m else "", "candidate": "",
                "mailbox": "", "messages": [m] if m else []} if m else None
    # thread key from the opened message
    opened = _parse_full(path, path_hash)
    key = opened["thread"] if opened else ""
    msgs = []
    for p, seen in _iter_mailbox_files(box["maildir"]):
        full = _parse_full(p, _pid(p))
        if not full or full["thread"] != key:
            continue
        full["seen"] = seen
        if mark and (os.sep + "new" + os.sep) in p:
            np = _mark_read(p)
            full["path"], full["id"], full["seen"] = np, _pid(np), 1
        msgs.append(full)
    msgs.sort(key=lambda m: m["date_ts"])
    subject = next((m["subject"] for m in reversed(msgs) if m["subject"]), opened.get("subject", "") if opened else "")
    return {"subject": subject, "candidate": box["name"], "mailbox": box["email"], "messages": msgs}


def get_attachment(path_hash: str, i: int):
    """Return (filename, content_type, bytes) for one attachment, or None."""
    path = _find_by_id(path_hash)
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            msg = BytesParser(policy=policy.default).parse(f)
    except Exception:
        return None
    for idx, part in enumerate(msg.walk()):
        if idx == i and (part.get_filename() or part.get_content_disposition() == "attachment"):
            try:
                data = part.get_payload(decode=True) or b""
            except Exception:
                data = b""
            return (part.get_filename() or f"attachment-{i}",
                    part.get_content_type() or "application/octet-stream", data)
    return None


# ---- sending (self-hosted Postfix submission, as the candidate) ------------
def send(from_email: str, to: str, subject: str, body: str,
         in_reply_to: str = "") -> dict[str, Any]:
    """Submit one message via our own Postfix (587, STARTTLS+SASL as the
    candidate mailbox). From = the candidate. OpenDKIM signs on the way out."""
    from_email = (from_email or "").lower().strip()
    cand = _by_email().get(from_email)
    if not cand:
        return {"ok": False, "error": f"unknown candidate mailbox {from_email}"}
    pw = _load(PW_FILE, {}).get(from_email)
    if not pw:
        return {"ok": False, "error": f"no submission password for {from_email}"}
    if not to:
        return {"ok": False, "error": "no recipient"}

    msg = EmailMessage()
    msg["From"] = f'{cand["name"]} <{from_email}>' if cand.get("name") else from_email
    msg["To"] = to
    msg["Subject"] = subject or ""
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_email.split("@")[1])
    msg["Reply-To"] = from_email
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body or "")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # loopback to our own Postfix
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(from_email, pw)
            s.send_message(msg)
    except Exception as exc:
        return {"ok": False, "error": f"send failed: {exc}"}
    # Save a copy into the candidate's own Maildir (cur, Seen) so the sent message
    # shows in the conversation thread — Gmail-style. Best-effort.
    try:
        _save_sent(cand["maildir"], msg)
    except Exception:
        pass
    return {"ok": True, "from": from_email, "to": _email_only(to), "subject": msg["Subject"]}


def _save_sent(maildir: str, msg) -> None:
    import time
    cur = os.path.join(maildir, "cur")
    os.makedirs(cur, exist_ok=True)
    name = f"{int(time.time())}.M{os.getpid()}Q{os.urandom(4).hex()}.crm:2,S"
    with open(os.path.join(cur, name), "wb") as f:
        f.write(msg.as_bytes())


# ---- aggregate counts (nav badges) -----------------------------------------
def counts() -> dict[str, int]:
    unread = 0
    mbx = 0
    for c in candidates():
        mbx += 1
        d = os.path.join(c["maildir"], "new")
        try:
            unread += sum(1 for n in os.listdir(d) if not n.startswith("."))
        except OSError:
            pass
    return {"unread": unread, "mailboxes": mbx}
