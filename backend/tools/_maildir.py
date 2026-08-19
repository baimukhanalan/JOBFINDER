"""Minimal, dependency-free Maildir reader for the self-hosted mail provider.

Vendored (stdlib only) so JOBFINDER stays self-contained and portable — it does
NOT reach into another project's code. Reads mail that our own Postfix/Dovecot
delivered to `<base>/<domain>/<local>/{new,cur}` (the standard virtual-mailbox
Maildir layout) and yields one dict per message with everything mail_sink needs
to record and thread it.

The reader is deliberately read-only and side-effect free: it never moves a
message new -> cur or sets flags. Dedup happens upstream in mail_sink by a stable
id (RFC Message-ID, else a content hash), so re-reading the same file is harmless.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Iterator

_ADDR_RE = re.compile(r"[\w.+-]+@[\w.-]+")


def _addresses(*header_values: str) -> list[str]:
    """Bare lowercased addresses pulled out of any number of address headers.
    Includes Delivered-To / X-Original-To so a Postfix plus-address envelope
    recipient (`local+jid@domain`) is captured even if the visible To differs."""
    out: list[str] = []
    seen: set[str] = set()
    for hv in header_values:
        for m in _ADDR_RE.findall(hv or ""):
            a = m.lower()
            if a not in seen:
                seen.add(a)
                out.append(a)
    return out


def _body(msg) -> str:
    """Best-effort plain-text body (falls back to HTML), never raising."""
    try:
        if msg.is_multipart():
            part = msg.get_body(preferencelist=("plain", "html"))
            return part.get_content() if part is not None else ""
        return msg.get_content()
    except Exception:
        try:
            payload = msg.get_payload(decode=True)
            return payload.decode("utf-8", "replace") if payload else ""
        except Exception:
            return ""


def _received_iso(msg, path: str) -> str:
    """Message Date as ISO, falling back to the file mtime so ordering still works
    when a Date header is missing or unparseable."""
    try:
        d = parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
        if d is not None:
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.isoformat()
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _stable_id(msg, raw: bytes) -> str:
    """Dedup key stable across a message's new -> cur move: the RFC Message-ID when
    present (stripped of <>), else a sha1 of the raw bytes."""
    mid = (str(msg["Message-ID"] or "")).strip().strip("<>")
    if mid:
        return mid
    return "sha1:" + hashlib.sha1(raw).hexdigest()


def iter_domain_messages(base: str, domain: str) -> Iterator[dict]:
    """Yield one parsed message dict for every mail under `<base>/<domain>/*/{new,cur}`.

    Each dict: {message_id, from, to (list[str]), subject, body, received, unread,
    path}. `to` unions To/Cc/Delivered-To/X-Original-To so plus-addressing resolves.
    A file that cannot be read/parsed is skipped, never fatal."""
    ddir = os.path.join(base, domain)
    if not os.path.isdir(ddir):
        return
    for local in sorted(os.listdir(ddir)):
        for sub in ("new", "cur"):
            mdir = os.path.join(ddir, local, sub)
            if not os.path.isdir(mdir):
                continue
            for fn in sorted(os.listdir(mdir)):
                full = os.path.join(mdir, fn)
                if not os.path.isfile(full):
                    continue
                try:
                    with open(full, "rb") as f:
                        raw = f.read()
                    msg = BytesParser(policy=policy.default).parsebytes(raw)
                except Exception:
                    continue
                to = _addresses(str(msg["To"] or ""), str(msg["Cc"] or ""),
                                str(msg["Delivered-To"] or ""),
                                str(msg["X-Original-To"] or ""))
                yield {
                    "message_id": _stable_id(msg, raw),
                    "from": str(msg["From"] or ""),
                    "to": to,
                    "subject": str(msg["Subject"] or ""),
                    "body": _body(msg) or "",
                    "received": _received_iso(msg, full),
                    "unread": sub == "new",
                    "path": full,
                }
