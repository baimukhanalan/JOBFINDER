"""Self-hosted candidate mail CRM engine — Gmail-style, zero third parties.

Reads each candidate's Dovecot Maildir straight off our own server
(/var/mail/vhosts/<domain>/<shard>/<local>/{new,cur}) and sends replies through
our own Postfix submission (127.0.0.1:587, SASL as the candidate mailbox). No
Mailgun, no API. Modeled on the amaskills CRM. Must run where the Maildir is
readable — the JOBFINDER dashboard runs under `sg mail` for exactly this.

Message id = sha1 of the Maildir uniq filename (stable across new->cur + flag changes).
Opening a message marks it read (Maildir new/ -> cur/:2,S), like every webmail.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import ssl
import threading
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import (formatdate, make_msgid, parseaddr,
                         parsedate_to_datetime)
from pathlib import Path
from typing import Any

from backend.tools import mail_db

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "backend" / "data" / "profiles.json"
ADDR_FILE = ROOT / "backend" / "data" / "mail_addresses.json"
PW_FILE = ROOT / "backend" / "data" / "mailbox_passwords.json"
MAILDIR_ROOT = "/var/mail/vhosts"
SMTP_HOST, SMTP_PORT = "127.0.0.1", 587
MAX_BODY = 200_000

# ---- classification (RU/EN, offer > rejection > interview > ack > other) ----
# Rules are phrases, not regexes: they are editable from /mail/keywords and each
# saved phrase has transparent "text contains phrase" semantics.
CLASSIFIER_VERSION = "2026-08-31-assessment-actionneeded-v1"
KEYWORDS_FILE = ROOT / "uploads" / "mail_keywords.json"
# `code` is a transactional bucket for the ATS "here is your security/verification code"
# emails (Greenhouse's "Security code for your application to X", ~half of what used to be
# `other`). It is LAST so any real signal (offer/rejection/interview/action/ack) wins first;
# a bare code email matches none of those. Splitting it out keeps `other` = genuinely
# UNCLASSIFIED, so a real misclassification is visible instead of buried under code noise.
KEYWORD_KINDS = ("offer", "rejection", "interview", "action_needed", "ack", "code")
DEFAULT_KEYWORDS = {
    # NOTE: matching is plain SUBSTRING (casefold) over subject+body, priority
    # offer>rejection>interview>ack. Phrases must be recruiter-specific enough that
    # they can't sit inside a larger word or a marketing sentence — see the two 2026-08-24
    # keyword audits (scratchpad kw_audit_*): bare words like "оффер"/"отказ" and common
    # phrases like "к сожалению"/"welcome aboard"/"job offer"/"decided not to" were removed
    # because they false-match newsletters/support/promo mail; "technical interview" was
    # removed (it matched a *process description* in an ack — Cresta). "hr interview" and the
    # "move forward with your" family were added to catch real invites/rejections that were
    # being missed (Salmon HR-interview invite, GoFasti "won't be able to move forward").
    "interview": [
        "interview invitation", "invitation to interview", "invitation to an interview",
        "invitation to a technical interview", "hr interview", "schedule your interview", "to schedule your interview", "phone screen invitation", "invite you to an interview",
        "invite you for an interview", "invite you to schedule", "would like to invite you to an interview",
        "schedule a first interview", "confirmation of your upcoming interview",
        "choose a time for your interview", "select a time for your interview",
        "share your availability for an interview", "assessment invitation",
        "приглашение на собеседование", "приглашение на интервью",
        "приглашаем вас на собеседование", "приглашаем вас на интервью",
        "приглашаем на собеседование", "приглашаем на интервью",
        "собеседование назначено", "назначить собеседование", "назначить звонок",
        "приглашение на тестовое задание",
    ],
    "offer": [
        "offer letter", "pleased to offer", "we are pleased to offer you",
        "would like to offer you", "extend you an offer", "extend an offer of employment",
        "offer of employment", "offer you the position", "your job offer",
        "employment offer", "formal offer",
        "рады предложить вам работу", "рады предложить вам должность",
        "предлагаем вам работу", "предлагаем вам должность", "предлагаем вам оффер",
        "направляем оффер", "направляем вам оффер", "высылаем вам оффер",
    ],
    "rejection": [
        "not moving forward", "not be moving forward",
        "decided not to move forward", "decided not to proceed", "decided not to continue",
        "won't be proceeding", "will not be proceeding", "regret to inform",
        "unable to move forward", "able to move forward with your",
        "move forward with other candidates", "move forward with another candidate",
        "pursuing other candidates", "selected other candidates",
        "not a good fit for this", "not the right fit for this",
        "application was declined",
        "к сожалению, мы приняли решение", "к сожалению, вынуждены отказать",
        "приняли решение отказать", "вынуждены вам отказать", "вам отказано",
        "не готовы продолжить", "вы не подошли",
        # soft/templated rejections whose exact wording broke the substring matches above
        # (audit 2026-08-31): keep the negation on "selected to move forward" — the bare form
        # appears in benign CONDITIONAL acks ("if you're selected to move forward").
        "not be proceeding with your candidacy", "wasn't selected to move forward",
        "candidates whose experience is a closer match", "the position has been filled",
    ],
    # action_needed = the submit landed but the ATS needs the CANDIDATE to do something to
    # complete/advance it (sign an NDA, verify identity, e-sign) — looks like an ack but the
    # application is gated. Priority ABOVE ack so an "application received + verify identity"
    # email surfaces as action-needed, not a passive ack. Phrases kept specific so a plain
    # ack never matches (NO bare "nda" — matches age**nda**/Ama**nda**; NO "please sign" alone).
    "action_needed": [
        "action required", "identity verification", "verify your identity",
        "complete your submission", "verification link", "confirm your email to complete",
        "non-disclosure agreement", "nda request", "sign and return", "e-signature",
        "background check consent", "please complete the following",
        "требуется действие", "подтвердите вашу личность", "подтвердите личность",
        # ASSESSMENT invites (Maximus & other mass-hiring) — the candidate must go take a
        # test; these were caught by ack's "thank you for applying" before reaching here, so
        # they sit ABOVE ack in KEYWORD_KINDS priority (audit 2026-08-31).
        "complete your assessment", "complete an assessment",
        # statement-form / follow-up asks whose word order broke the interrogative keywords
        "you are open to relocating", "we have a few outstanding questions",
        "share the additional information needed",
    ],
    "ack": [
        "application received", "application has been received",
        "your application has been submitted", "application was submitted",
        "thank you for applying", "thanks for applying", "thanks for your application",
        "we received your application", "we've received your application",
        "we have received your application", "application is under review",
        "application is being reviewed", "got your application", "reviewing all applications", "reviewing applications",
        "received your application", "received your materials", "will review your application",
        "review your application and be in touch",
        "appreciate you taking the time to complete your application",
        "отклик принят", "отклик получен", "отклик отправлен",
        "заявка принята", "заявка получена",
        "ваша заявка на рассмотрении", "ваша заявка рассматривается",
    ],
    "code": [
        "security code for your application", "your security code",
        "copy and paste this code", "verification code", "your verification code",
        "confirmation code", "one-time passcode", "one-time password",
        "your login code", "your access code", "enter the code below",
        "код подтверждения", "проверочный код", "одноразовый код",
    ],
}
_KEYWORDS_CACHE: dict[str, Any] = {"mtime": None, "rules": None}
_KEYWORDS_LOCK = threading.Lock()


# Map smart/typographic punctuation to ASCII so a keyword typed with a straight quote
# still matches a sender's curly one (and vice-versa). Without this, a rejection using a
# curly apostrophe ("we won't be moving forward") slipped past the straight-quote keyword
# and landed in "other" — 4 Pinterest rejections did exactly that (2026-08-30).
_PUNCT_MAP = str.maketrans({
    "’": "'", "‘": "'", "ʼ": "'", "′": "'",   # ’ ‘ ʼ ′ -> '
    "“": '"', "”": '"', "″": '"',                   # “ ” ″ -> "
    "–": "-", "—": "-", "−": "-",                   # – — − -> -
    " ": " ",                                                  # nbsp -> space
})


def _normalise_phrase(value: str) -> str:
    return " ".join((value or "").translate(_PUNCT_MAP).casefold().split())


def _clean_keyword_rules(data: dict | None) -> dict[str, list[str]]:
    data = data if isinstance(data, dict) else {}
    out: dict[str, list[str]] = {}
    for kind in KEYWORD_KINDS:
        raw = data.get(kind, DEFAULT_KEYWORDS[kind])
        if not isinstance(raw, list):
            raw = DEFAULT_KEYWORDS[kind]
        seen, items = set(), []
        for value in raw[:100]:
            phrase = _normalise_phrase(str(value))[:120]
            if phrase and phrase not in seen:
                seen.add(phrase)
                items.append(phrase)
        out[kind] = items
    return out


def keyword_rules() -> dict[str, list[str]]:
    try:
        mtime = KEYWORDS_FILE.stat().st_mtime_ns
    except OSError:
        mtime = -1
    with _KEYWORDS_LOCK:
        if _KEYWORDS_CACHE["rules"] is not None and _KEYWORDS_CACHE["mtime"] == mtime:
            return {k: list(v) for k, v in _KEYWORDS_CACHE["rules"].items()}
        try:
            data = json.loads(KEYWORDS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = DEFAULT_KEYWORDS
        rules = _clean_keyword_rules(data)
        _KEYWORDS_CACHE.update({"mtime": mtime, "rules": rules})
        return {k: list(v) for k, v in rules.items()}


def save_keyword_rules(data: dict[str, list[str]]) -> dict[str, list[str]]:
    rules = _clean_keyword_rules(data)
    KEYWORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = KEYWORDS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rules, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, KEYWORDS_FILE)
    with _KEYWORDS_LOCK:
        _KEYWORDS_CACHE.update({"mtime": KEYWORDS_FILE.stat().st_mtime_ns, "rules": rules})
    return {k: list(v) for k, v in rules.items()}


def classifier_version() -> str:
    payload = json.dumps(keyword_rules(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return f"{CLASSIFIER_VERSION}:{hashlib.sha1(payload).hexdigest()[:12]}"


def classify(subject: str, body: str) -> str:
    text = _normalise_phrase(f"{subject or ''}\n{body or ''}")
    rules = keyword_rules()
    for kind in KEYWORD_KINDS:
        if any(phrase in text for phrase in rules[kind]):
            return kind
    return "other"


# A Maximus «Complete Your Assessment» invite classifies `action_needed`, but once the etalon
# engine has COMPLETED that persona's assessment the invite is handled — the runner records the
# mailbox in shl_assess_done.json, and `build_index_row` re-tags it `assessment_done` (🤖) so the
# CRM stops flagging it as needing action. Cached by mtime (build_index_row runs per-file).
_ASSESS_DONE_PATH = Path(__file__).resolve().parent.parent / "data" / "shl_assess_done.json"
_assess_done_cache = {"mtime": None, "set": frozenset()}


def assessment_done_mailboxes() -> frozenset:
    try:
        m = _ASSESS_DONE_PATH.stat().st_mtime
    except OSError:
        return frozenset()
    if _assess_done_cache["mtime"] != m:
        try:
            _assess_done_cache["set"] = frozenset(json.loads(_ASSESS_DONE_PATH.read_text()))
        except Exception:
            _assess_done_cache["set"] = frozenset()
        _assess_done_cache["mtime"] = m
    return _assess_done_cache["set"]


def _kind_with_done_override(subject: str, body: str, mailbox: str) -> str:
    kind = classify(subject, body)
    if (kind == "action_needed" and "complete your assessment" in (subject or "").lower()
            and mailbox in assessment_done_mailboxes()):
        return "assessment_done"
    return kind


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


DEMO_FILE = ROOT / "backend" / "data" / "demo_personas.json"  # synthetic demo personas (email -> {id,name})


_DEMO_LOCK = threading.Lock()


def register_demo_persona(email: str, name: str, pid: str = "") -> None:
    """Add a synthetic demo persona (synth_persona) to the candidate registry so its mailbox
    is scanned + shown in the CRM inbox — demo personas aren't in profiles.json. Idempotent.

    THREAD-SAFE (locked + atomic write): the parallel bulk lane runs N dashboard threads that
    each call this concurrently; the old bare read-modify-write raced and CLOBBERED entries —
    real leads (gulmira's Salmon HR-interview thread) silently vanished from the registry, so
    their mail stopped surfacing. The lock + tmp-replace keep every registration."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return
    entry = {"id": pid or email.split("@")[0], "name": name or email.split("@")[0]}
    with _DEMO_LOCK:
        reg = _load(DEMO_FILE, {})
        if not isinstance(reg, dict):
            reg = {}
        if reg.get(email) == entry:
            return
        reg[email] = entry
        try:
            tmp = DEMO_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, DEMO_FILE)
        except Exception:
            pass


def _demo_candidates() -> list[dict]:
    out = []
    reg = _load(DEMO_FILE, {})
    for email, info in (reg if isinstance(reg, dict) else {}).items():
        local, _, domain = str(email).lower().partition("@")
        if not domain:
            continue
        out.append({"id": (info or {}).get("id") or local, "email": email.lower(),
                    "name": (info or {}).get("name") or local, "local": local,
                    "domain": domain, "maildir": _maildir(local, domain), "is_demo": True})
    return out


def candidates() -> list[dict]:
    """[{id,email,name,local,domain,maildir}] for every provisioned candidate — the real
    roster (profiles.json + mail_addresses.json) plus synthetic demo personas so their
    mailboxes surface in the CRM inbox too."""
    profs = {p["id"]: p for p in _load(PROFILES, [])}
    out, seen = [], set()
    for pid, email in _load(ADDR_FILE, {}).items():
        p = profs.get(pid)
        if not p or p.get("is_sample"):
            continue
        local, _, domain = email.lower().partition("@")
        if not domain:
            continue
        out.append({"id": pid, "email": email.lower(), "name": p.get("full_name") or pid,
                    "local": local, "domain": domain, "maildir": _maildir(local, domain)})
        seen.add(email.lower())
    for c in _demo_candidates():          # demo personas (real roster wins on any collision)
        if c["email"] not in seen:
            out.append(c)
            seen.add(c["email"])
    return out


def _by_email() -> dict[str, dict]:
    return {c["email"]: c for c in candidates()}


def candidate_groups(stage: str = "", q: str = "", limit: int = 50,
                     offset: int = 0) -> list[dict]:
    """Grouped candidate inbox rows (see mail_db.candidate_groups) enriched with the
    authoritative roster display NAME (real roster > demo registry > stored candidate >
    local-part), a candidate `id` and an `is_demo` flag — the spine of the merged
    Кандидаты screen. Returns [] (the empty state, never a crash) when the index is
    unavailable."""
    try:
        rows = mail_db.candidate_groups(stage=stage or None, q=q or None,
                                        limit=limit, offset=offset)
    except Exception as e:
        _health_fallback("candidate_groups", e)
        return []
    by = _by_email()
    for r in rows:
        c = by.get(r["mailbox"]) or {}
        local = r["mailbox"].split("@")[0]
        r["name"] = c.get("name") or r.get("last_candidate") or local
        r["id"] = c.get("id") or local
        r["is_demo"] = bool(c.get("is_demo")) if c else True
    return rows


# ---- Maildir reading -------------------------------------------------------
def _pid(path: str) -> str:
    """STABLE message id: hash the Maildir file's UNIQUE name (`<time>.<uniq>.<host>`),
    independent of the new/ vs cur/ directory AND the ':2,<flags>' suffix. Maildir uniq
    names are globally unique, so this stays constant when a message is read (new→cur) or
    re-flagged — a full-path hash changed on every move, so an inbox link built while a
    message was unread 404'd ('Письмо не найдено') the moment it moved to cur/."""
    base = os.path.basename(path).split(":2,")[0]
    return hashlib.sha1(base.encode()).hexdigest()


def _display_name(addr: str) -> str:
    name, email = parseaddr(addr)
    return name or (email.split("@")[0] if email else addr)


def _email_only(addr: str) -> str:
    return parseaddr(addr)[1] or addr


_RE_PREFIX = re.compile(r"^(?:\s*(?:re|fwd|fw|аноним|ответ)\s*:\s*)+", re.I)


def _norm_subject(s: str) -> str:
    """Thread key: subject with Re:/Fwd: prefixes stripped, lowercased."""
    return _RE_PREFIX.sub("", (s or "").strip()).strip().lower() or "(без темы)"


def _is_attachment(part) -> bool:
    """True only for a REAL downloadable attachment. NOT: a cid-referenced inline body image
    (e.g. a logo in a multipart/related email — it carries a filename == its Content-Id but
    belongs to the HTML body), a text/plain|text/html body part, or a multipart container.
    The old `filename or disp==attachment` test flagged the inline logo → a bogus '5 КБ'
    attachment chip and a false paperclip flag on ATS security-code emails."""
    if part.get_content_maintype() == "multipart":
        return False
    disp = (part.get_content_disposition() or "")
    if disp == "attachment":
        return True
    fn = part.get_filename()
    if not fn:
        return False
    cid = part.get("Content-Id") or part.get("Content-ID")
    if disp == "inline" and cid:
        return False                       # inline body image, not an attachment
    return part.get_content_type() not in ("text/plain", "text/html")


def _attachments(msg) -> list[dict]:
    out = []
    i = 0
    for part in msg.walk():                # keep i aligned with get_attachment's walk
        if _is_attachment(part):
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:
                payload = b""
            out.append({"i": i, "filename": part.get_filename() or f"attachment-{i}",
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


def _message_text(msg) -> str:
    try:
        body = msg.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body else ""
    except Exception:
        text = ""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_BODY]


def _html_to_text(html: str) -> str:
    """Readable plain-text from an HTML body: drop <style>/<script>, turn block-ends into line
    breaks, strip tags, unescape entities. Keeps line structure so an HTML-only email reads
    sensibly in the plain <div> (URLs are linkified by the UI)."""
    from html import unescape
    if not html:
        return ""
    t = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"(?i)<(br|/p|/div|/tr|/li|/h[1-6]|/table)\s*/?>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = unescape(t)
    t = re.sub(r"[ \t\f\v]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()[:MAX_BODY]


def _snippet(msg) -> str:
    return _message_text(msg)[:280]


def _health_fallback(where: str, exc: Exception | None = None) -> None:
    """Signal (throttled Telegram) that a DB-first read dropped to a live scan, so a
    dead index/indexer can't hide behind the fallback. Best-effort. `exc` is recorded
    in the alert so the cause (pool exhausted vs Postgres down) is diagnosable."""
    if exc is not None:
        try:
            print(f"[mailcrm] index fallback at {where}: "
                  f"{type(exc).__name__}: {str(exc)[:200]}", flush=True)
        except Exception:
            pass
    try:
        from backend.tools import mail_health
        mail_health.record_fallback(where, str(exc)[:120] if exc is not None else "")
    except Exception:
        pass


def build_index_row(path: str, seen: int) -> dict | None:
    """Parse one Maildir file into the mail_index row shape (used by the indexer
    and the retention job). Returns None for a file outside any candidate mailbox."""
    # Never index a message inside a hidden Maildir subfolder (.Trash / .Sent / .Drafts /
    # .Junk) — those hold deleted/sent/archived copies, not inbox mail. delete_thread moves
    # a thread into .Trash/cur, and re-indexing it there would resurrect a just-deleted
    # thread. (The inotify watcher also excludes these dirs; this is a defensive backstop so
    # any path-based caller is safe.)
    if any(p.startswith(".") and len(p) > 1 for p in os.path.dirname(path).split(os.sep)):
        return None
    box = _mailbox_of(path)
    if not box:
        return None
    try:
        with open(path, "rb") as f:
            msg = BytesParser(policy=policy.default).parse(f)
    except Exception:
        return None
    frm = str(msg["From"] or "")
    subj = str(msg["Subject"] or "")
    full_text = _message_text(msg)
    snip = full_text[:280]
    from_email = _email_only(frm)
    return {
        "mailbox": box["email"], "candidate": box["name"], "candidate_id": box["id"],
        "path": path, "path_hash": _pid(path),
        "from_name": _display_name(frm), "from_email": from_email,
        "subject": subj, "snippet": snip,
        "kind": _kind_with_done_override(subj, full_text, box["email"]),
        "thread_key": _norm_subject(subj),
        "has_att": any(_is_attachment(p) for p in msg.walk()),
        "outbound": from_email.lower() == box["email"],
        "date_ts": _date_ts(msg, path), "seen": bool(seen),
    }


def list_messages(mailbox: str | None = None, q: str = "",
                  limit: int = 50, before_ts: int | None = None,
                  before_id: str | None = None, stage: str = "") -> list[dict]:
    """Newest-first message rows. Reads the Postgres index (fast); falls back to a
    live Maildir scan if the index is unavailable."""
    try:
        return mail_db.list_messages(mailbox=mailbox, q=(q or None), limit=limit,
                                     before_ts=before_ts, before_id=before_id,
                                     stage=stage or None)
    except Exception as e:
        _health_fallback("list_messages", e)
        return _scan_messages(mailbox, q, limit, before_ts, before_id, stage)


def _scan_messages(mailbox: str | None = None, q: str = "",
                   limit: int = 50, before_ts: int | None = None,
                   before_id: str | None = None, stage: str = "") -> list[dict]:
    """Fallback: newest-first rows straight off disk (keyset by date_ts,id)."""
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
            full_text = _message_text(msg)
            snip = full_text[:280]
            outbound = _email_only(frm).lower() == c["email"]
            kind = classify(subj, full_text)
            if stage == "sent" and not outbound:
                continue
            if stage in ("ack", "interview", "offer", "rejection", "action_needed", "code") \
                    and (outbound or kind != stage):
                continue
            rows.append({
                "id": _pid(path), "path": path, "mailbox": c["email"],
                "candidate": c["name"], "candidate_id": c["id"],
                "from": frm, "from_name": _display_name(frm), "from_email": _email_only(frm),
                "subject": subj, "snippet": snip, "date_ts": _date_ts(msg, path),
                "seen": seen, "kind": kind, "outbound": outbound,
                "thread": _norm_subject(subj),
                "has_att": any(_is_attachment(p) for p in msg.walk()),
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
    # Match on a PATH-SEGMENT boundary, not a bare substring: a candidate maildir
    # ".../takhet.com/aibek.sarsenov" must NOT match a file under a DIFFERENT mailbox
    # whose name it is merely a prefix of (".../takhet.com/aibek.sarsenov531/cur/..").
    # Demo personas use numeric-suffix locals (first.last<NUM>), so this collision is
    # common and would mis-attribute their mail (wrong mailbox → thread never groups).
    for c in candidates():
        md = c["maildir"]
        if path == md or path.startswith(md + "/"):
            return c
    return None


# Detects real HTML tags embedded in a text/plain body. Some senders (GoFasti/HeyMilo) ship
# "<a href='URL'>URL</a>" INSIDE the text/plain part, which escape()+linkify downstream turns
# into a broken giant href (the closing tag gets swallowed into the URL) + visible ">...</a>"
# garbage. Fixed tag whitelist on purpose, so it never matches a bare "<someone@example.com>"
# address in a genuine plain-text mail.
_PLAIN_HTML_RE = re.compile(
    r"</?(?:a|p|div|br|span|table|tr|td|th|thead|tbody|li|ul|ol|h[1-6]|strong|em|b|i|"
    r"body|html|head|img|font|center|blockquote)\b[^>]*>", re.I)


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
    # HTML-only mail (many ATS transactional emails — e.g. a Greenhouse "Security code" is a
    # multipart/related with only a text/html part) has no plain part, so _msg_card would fall
    # to a sandboxed <iframe srcdoc> that collapses to a BLANK body on mobile Safari. Derive a
    # readable plain-text from the HTML so the sturdy <div> path renders it (links preserved by
    # the UI's linkify). The html is still kept for anyone who wants the rich version.
    if not (plain or "").strip() and (html or "").strip():
        plain = _html_to_text(html)
    # Reverse case: a text/plain part that actually CONTAINS HTML markup (GoFasti/HeyMilo put
    # a raw <a href="URL">URL</a> in text/plain). Flatten it the same way, so the sturdy <div>
    # path renders a clean, correctly-linkified URL instead of a broken href + literal tag text.
    if plain and _PLAIN_HTML_RE.search(plain):
        plain = _html_to_text(plain)
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


def _resolve_path(path_hash: str) -> str | None:
    """Absolute file path for a message id — from the Postgres index first, then a
    live Maildir scan as a fallback."""
    try:
        row = mail_db.get_row(path_hash)
        if row and row.get("path") and os.path.isfile(row["path"]):
            return row["path"]
    except Exception:
        pass
    return _find_by_id(path_hash)


def get_message(path_hash: str, mark: bool = True) -> dict | None:
    path = _resolve_path(path_hash)
    if not path:
        return None
    if mark and (os.sep + "new" + os.sep) in path:
        old_hash = path_hash
        path = _mark_read(path)
        path_hash = _pid(path)
        try:
            mail_db.mark_seen(old_hash, path, path_hash)
        except Exception:
            pass
    return _parse_full(path, path_hash)


def get_thread(path_hash: str, mark: bool = True) -> dict | None:
    """The whole conversation for a message (Gmail-style). Uses the Postgres index;
    falls back to a live Maildir scan if the index is unavailable."""
    try:
        row = mail_db.get_row(path_hash)
    except Exception:
        row = None
    if row and row.get("mailbox") and row.get("thread_key") is not None:
        t = _thread_from_index(row, mark)
        if t is not None:
            return t
    return _scan_thread(path_hash, mark)


def _thread_from_index(row: dict, mark: bool) -> dict | None:
    """Build the conversation from the index: one query for the thread, then read
    each message's file for its full body/attachments."""
    box = _by_email().get(row["mailbox"])
    try:
        rows = mail_db.thread_rows(row["mailbox"], row["thread_key"])
    except Exception:
        return None
    msgs = []
    for r in rows:
        p = r.get("path")
        if not p or not os.path.isfile(p):
            continue
        full = _parse_full(p, r["path_hash"])
        if not full:
            continue
        full["seen"] = bool(r.get("seen"))
        if mark and (os.sep + "new" + os.sep) in p:
            old_hash = r["path_hash"]
            np = _mark_read(p)
            full["path"], full["id"], full["seen"] = np, _pid(np), 1
            try:
                mail_db.mark_seen(old_hash, np, _pid(np))
            except Exception:
                pass
        msgs.append(full)
    if not msgs:
        return None
    msgs.sort(key=lambda m: m["date_ts"])
    subject = next((m["subject"] for m in reversed(msgs) if m["subject"]), "")
    name = box["name"] if box else (msgs[0].get("candidate") or "")
    return {"subject": subject, "candidate": name, "mailbox": row["mailbox"], "messages": msgs}


def _scan_thread(path_hash: str, mark: bool = True) -> dict | None:
    """Fallback: rebuild the thread by scanning the candidate's Maildir."""
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
    path = _resolve_path(path_hash)
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            msg = BytesParser(policy=policy.default).parse(f)
    except Exception:
        return None
    for idx, part in enumerate(msg.walk()):
        if idx == i and _is_attachment(part):
            try:
                data = part.get_payload(decode=True) or b""
            except Exception:
                data = b""
            return (part.get_filename() or f"attachment-{i}",
                    part.get_content_type() or "application/octet-stream", data)
    return None


def delete_thread(path_hash: str) -> dict[str, Any]:
    """Move a whole conversation out of Inbox into the mailbox's .Trash.

    The operation is recoverable on disk and removes the corresponding index rows
    immediately. The inotify indexer also sees the move and independently prunes
    them, so a temporary DB failure cannot make a deleted thread reappear forever.
    """
    thread = get_thread(path_hash, mark=False)
    if not thread or not thread.get("messages"):
        return {"ok": False, "error": "message not found"}
    box = _by_email().get((thread.get("mailbox") or "").lower())
    if not box:
        return {"ok": False, "error": "candidate mailbox not found"}

    maildir = os.path.realpath(box["maildir"])
    trash_root = os.path.join(maildir, ".Trash")
    trash_cur = os.path.join(trash_root, "cur")
    for sub in ("cur", "new", "tmp"):
        os.makedirs(os.path.join(trash_root, sub), exist_ok=True)

    moved_hashes: list[str] = []
    for message in thread["messages"]:
        path = os.path.realpath(message.get("path") or "")
        try:
            if os.path.commonpath((maildir, path)) != maildir or not os.path.isfile(path):
                continue
        except ValueError:
            continue
        name = os.path.basename(path)
        if ":2," not in name:
            name += ":2,ST"
        else:
            base, flags = name.split(":2,", 1)
            name = f"{base}:2,{''.join(sorted(set(flags + 'ST')))}"
        dst = os.path.join(trash_cur, name)
        if os.path.exists(dst):
            dst += "." + os.urandom(4).hex()
        try:
            os.replace(path, dst)
            moved_hashes.append(_pid(path))
        except OSError:
            continue

    if moved_hashes:
        try:
            mail_db.delete_paths(moved_hashes)
        except Exception:
            pass
    return {"ok": bool(moved_hashes), "deleted": len(moved_hashes),
            "recoverable": True, "mailbox": box["email"],
            "error": "could not move messages to Trash" if not moved_hashes else ""}


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
        # mailbox_passwords.json is a best-effort cache that lost ~5k entries to an unlocked
        # write race; the authoritative password lives in virtual_users.password_plain. Fall
        # back to it so a candidate whose cache entry was dropped can still send a reply.
        try:
            from backend.tools.provision_mailboxes import get_submission_password
            pw = get_submission_password(from_email)
        except Exception:
            pw = None
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
    """Nav badge counts — from the index; falls back to a live Maildir scan."""
    try:
        return mail_db.counts()
    except Exception as e:
        _health_fallback("counts", e)
        return _scan_counts()


def _scan_counts() -> dict[str, int]:
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


def stage_counts() -> dict[str, int]:
    try:
        return mail_db.stage_counts()
    except Exception:
        rows = _scan_messages(limit=1_000_000)
        # DISTINCT-CANDIDATE counts (match mail_db.stage_counts / the Candidates funnel).
        buckets: dict[str, set] = {"sent": set(), "ack": set(), "interview": set(),
                                   "offer": set(), "rejection": set()}
        allbx: set = set()
        for row in rows:
            mb = row.get("mailbox")
            allbx.add(mb)
            if row.get("outbound"):
                buckets["sent"].add(mb)
            elif row.get("kind") in buckets:
                buckets[row["kind"]].add(mb)
        out = {k: len(v) for k, v in buckets.items()}
        out["all"] = len(allbx)
        return out


def reclassify_existing() -> int:
    """Apply the currently saved keyword rules to all indexed mail immediately."""
    updates = []
    for path, seen in mail_db.indexed_paths():
        row = build_index_row(path, int(bool(seen)))
        if not row:
            continue
        updates.append((row["kind"], row["path_hash"]))
    updated = mail_db.update_kinds(updates)
    mail_db.set_meta("classifier_version", classifier_version())
    return updated
