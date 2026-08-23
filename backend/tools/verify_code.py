"""Read the email 'security code' a Greenhouse-style form mails to the applicant, from the
applicant's OWN provisioned Maildir, so the co-pilot can auto-fill it during the semi-auto
review. This confirms EMAIL CONTROL only (the mailbox is ours/theirs) — it does NOT touch the
form's reCAPTCHA/anti-bot and does NOT submit; the human still solves the captcha and clicks
the final Submit. Legitimate only for a candidate's own mailbox.
"""
from __future__ import annotations

import email
import os
import re
from email import policy
from html.parser import HTMLParser

MAILROOT = "/var/mail/vhosts"

# "Copy and paste this code ... : 7Uz2maL3" — the code follows a "code:" phrase.
_CODE_RE = re.compile(
    r"(?:this code[^:]{0,40}|security code[^:]{0,40}|verification code[^:]{0,40}|"
    r"\bcode\b[^:]{0,20})\s*:\s*([A-Za-z0-9]{5,12})\b")
_SUBJ_RE = re.compile(r"(?i)security code|verification|verify")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self._t: list[str] = []

    def handle_data(self, d):
        self._t.append(d)

    def text(self) -> str:
        return " ".join(self._t)


def _msg_text(msg) -> str:
    """Plain-text body if present, else HTML stripped to text."""
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_content()
                except Exception:
                    pass
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_content()
                except Exception:
                    html = ""
                break
    else:
        try:
            if msg.get_content_type() == "text/plain":
                return msg.get_content()
            html = msg.get_content()
        except Exception:
            html = ""
    p = _Text()
    try:
        p.feed(html or "")
    except Exception:
        return ""
    return p.text()


def read_code(email_addr: str, since_ts: float = 0.0) -> str | None:
    """The newest verification 'security code' emailed to `email_addr` at/after `since_ts`
    (unix seconds), read from its Maildir. None if no such email / no code found.

    Reads a candidate's OWN mailbox only — never enters the code, never submits."""
    local, _, domain = (email_addr or "").strip().partition("@")
    if not local or not domain:
        return None
    md = os.path.join(MAILROOT, domain, local)
    files: list[str] = []
    for sub in ("new", "cur"):
        d = os.path.join(md, sub)
        try:
            for n in os.listdir(d):
                p = os.path.join(d, n)
                try:
                    if os.path.getmtime(p) >= since_ts - 5:
                        files.append(p)
                except Exception:
                    continue
        except Exception:
            continue
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for p in files:
        try:
            with open(p, "rb") as f:
                msg = email.message_from_binary_file(f, policy=policy.default)
        except Exception:
            continue
        if not _SUBJ_RE.search(str(msg.get("Subject", ""))):
            continue
        m = _CODE_RE.search(re.sub(r"\s+", " ", _msg_text(msg)))
        if m:
            return m.group(1)
    return None
