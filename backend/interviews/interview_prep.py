"""Interview PREP PACK — the reusable core of the «Interview Cockpit» prep engine.

For one upcoming собес (a persona mailbox assigned to the interviewer), gather everything a human
needs to walk in prepared AND everything the Natively assistant needs loaded:
  * the candidate's résumé PDF path (what gets attached into Natively's settings),
  * the candidate card (name / state / ~age / applied role / company) from the persona + the CRM,
  * the recruiter invite (subject / snippet / booking link / join URL) from `mail_index`,
  * a short, human-readable BRIEF string for the cockpit screen.

Pure-ish + guarded: every external lookup is wrapped so a missing résumé / absent persona never
raises — the pack still renders with whatever resolved. No network, no writes. This module is shared
by the manual P1 playbook, the P2 Mac auto-prep, and the cockpit schedule view.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict

from backend.tools import hiring_events as he
from backend.tools import mail_db

# Candidate prep photo (owner 2026-10-08): a slot on the prep card to attach a candidate photo.
# Stored per-mailbox under gitignored uploads/ (PII) — NOT an identity document, just a headshot the
# interviewer can keep with the brief.
_PREP_PHOTO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                               "uploads", "prep_photos")
_PHOTO_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def _photo_key(mailbox: str) -> str:
    return re.sub(r"[^a-z0-9_.@-]", "_", (mailbox or "").lower())[:120]


def prep_photo_path(mailbox: str) -> str | None:
    """The on-disk path of this candidate's attached photo, or None if none attached."""
    key = _photo_key(mailbox)
    if not key:
        return None
    for ext in ("jpg", "png", "webp"):
        p = os.path.join(_PREP_PHOTO_DIR, f"{key}.{ext}")
        if os.path.exists(p):
            return p
    return None


def save_prep_photo(mailbox: str, data: bytes, content_type: str) -> str | None:
    """Save an attached candidate photo (image only, ≤8MB). Returns the path, or None on reject."""
    ext = _PHOTO_EXT.get((content_type or "").split(";")[0].strip().lower())
    if not ext or not data or len(data) > 8 * 1024 * 1024:
        return None
    key = _photo_key(mailbox)
    if not key:
        return None
    try:
        os.makedirs(_PREP_PHOTO_DIR, exist_ok=True)
        # drop any prior ext so one photo per mailbox
        for e in ("jpg", "png", "webp"):
            old = os.path.join(_PREP_PHOTO_DIR, f"{key}.{e}")
            if os.path.exists(old):
                os.remove(old)
        path = os.path.join(_PREP_PHOTO_DIR, f"{key}.{ext}")
        with open(path, "wb") as f:
            f.write(data)
        return path
    except Exception:
        return None


@dataclass
class PrepPack:
    mailbox: str
    candidate_name: str = ""
    state: str = ""
    age: int | None = None
    company: str = ""
    role: str = ""
    jobid: str = ""
    start_ts: object = None            # tz-aware datetime | None
    resume_path: str | None = None
    resume_filename: str = ""
    has_resume: bool = False
    invite_subject: str = ""
    invite_snippet: str = ""
    invite_from: str = ""
    booking_url: str | None = None     # recruiter self-schedule link (Calendly/ModernLoop/…)
    booking_provider: str | None = None
    join_url: str | None = None        # a Zoom/Teams room (hiring-event / already-scheduled call)
    brief: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _latest_invite(mailbox: str) -> dict:
    """The newest interview-kind inbound for this mailbox from mail_index (subject/snippet/links)."""
    try:
        with mail_db._cur() as cur:
            cur.execute(
                """SELECT subject, snippet, from_email, booking_url, booking_provider, date_ts
                   FROM mail_index
                   WHERE mailbox=%s AND kind='interview' AND COALESCE(outbound,false)=false
                   ORDER BY date_ts DESC NULLS LAST LIMIT 1""",
                (mailbox,))
            row = cur.fetchone()
            return dict(row) if row else {}
    except Exception:
        return {}


def _find_join_url(text: str) -> str | None:
    """A Zoom/Teams/Meet room in free text (for an already-scheduled call / hiring event)."""
    import re
    if not text:
        return None
    m = re.search(r"https?://[^\s<>\"]*(?:zoom\.us/j/|teams\.microsoft\.com/l/meetup|meet\.google\.com/)[^\s<>\")]*",
                  text, re.I)
    return m.group(0) if m else None


def build_pack(mailbox: str, *, iv_row: dict | None = None) -> PrepPack:
    """Assemble the prep pack for one mailbox. `iv_row` (the iv_interviews row) may be passed to
    avoid a re-query; otherwise company/jobid/start come from the newest non-cancelled row."""
    pack = PrepPack(mailbox=mailbox)

    if iv_row is None:
        try:
            with mail_db._cur() as cur:
                cur.execute(
                    """SELECT company, jobid, start_ts FROM iv_interviews
                       WHERE mailbox=%s AND status<>'cancelled'
                       ORDER BY start_ts NULLS LAST LIMIT 1""", (mailbox,))
                iv_row = cur.fetchone() or {}
        except Exception:
            iv_row = {}
    pack.company = (iv_row.get("company") or "") if iv_row else ""
    pack.jobid = str(iv_row.get("jobid") or "") if iv_row else ""
    pack.start_ts = iv_row.get("start_ts") if iv_row else None

    # candidate card from the synthetic persona + its résumé (gitignored PII, resolves on the deploy)
    try:
        persona = he.load_persona(mailbox)
    except Exception:
        persona = None
    detail = he.candidate_detail(persona, fallback_name=mailbox.split("@")[0]) if persona is not None \
        else {"full_name": mailbox.split("@")[0], "state": "", "age": None}
    pack.candidate_name = detail.get("full_name") or mailbox.split("@")[0]
    pack.state = detail.get("state") or ""
    pack.age = detail.get("age")
    try:
        pack.resume_path = he.resume_pdf_path(mailbox)
    except Exception:
        pack.resume_path = None
    pack.has_resume = bool(pack.resume_path)
    try:
        pack.resume_filename = he.resume_filename(mailbox, persona)
    except Exception:
        pack.resume_filename = f"{pack.candidate_name}.pdf"
    pack.role = (persona or {}).get("role") or (persona or {}).get("title") or ""

    inv = _latest_invite(mailbox)
    pack.invite_subject = inv.get("subject") or ""
    pack.invite_snippet = inv.get("snippet") or ""
    pack.invite_from = inv.get("from_email") or ""
    pack.booking_url = inv.get("booking_url")
    pack.booking_provider = inv.get("booking_provider")
    pack.join_url = _find_join_url(f"{pack.invite_subject}\n{pack.invite_snippet}")

    pack.brief = _render_brief(pack)
    return pack


def cheat_sheet(pack: "PrepPack") -> str | None:
    """A concise, role/company-specific interview cheat-sheet for the собес — likely questions +
    the candidate's talking points. Uses the project's existing LLM path (`services.tailor._llm_complete`,
    Sumrak-router → Claude-CLI fallback, honoring its shared circuit-breaker). GUARDED: any failure /
    LLM-down returns None so the caller shows the deterministic card without the sheet (never errors).
    Neutral Russian output; no stack names (the prompt is about the role, not the engine)."""
    try:
        from backend.services.tailor import tailor
    except Exception:
        return None
    role = (pack.role or "").strip() or "данной роли"
    company = (pack.company or "").strip() or "компании"
    where = (pack.state or "").strip() or "США"
    prompt = (
        f"Ты готовишь интервьюера к собеседованию на позицию «{role}» в компании «{company}». "
        f"Кандидат: {pack.candidate_name}, проживает: {where}. "
        f"Контекст приглашения: {(pack.invite_subject or '').strip()[:160]}.\n\n"
        "Дай короткую шпаргалку на РУССКОМ ровно из двух разделов, без вступления и без markdown-заголовков:\n"
        "«Вероятные вопросы» — 6–8 типичных вопросов именно под эту роль и компанию.\n"
        "«Сильные стороны и ответы» — 4–5 кратких тезисов, что подчёркивать кандидату.\n"
        "Только суть, маркированными строками.")
    try:
        out = tailor._llm_complete(prompt)
    except Exception:
        return None
    out = (out or "").strip()
    return out or None


def _render_brief(p: PrepPack) -> str:
    when = p.start_ts.strftime("%d.%m %H:%M") if getattr(p.start_ts, "strftime", None) else "время не назначено"
    age = f", ~{p.age} лет" if p.age else ""
    state = f" · {p.state}" if p.state else ""
    role = f" · {p.role}" if p.role else ""
    lines = [
        f"👤 {p.candidate_name}{age}{state}{role}",
        f"🏢 {p.company or '—'}{(' · job ' + p.jobid) if p.jobid else ''}",
        f"🕒 {when}",
        f"📄 Резюме: {'готово — ' + (p.resume_filename or 'resume.pdf') if p.has_resume else 'НЕ НАЙДЕНО'}",
    ]
    if p.booking_url:
        lines.append(f"📅 Бронь ({p.booking_provider or 'ссылка'}): {p.booking_url}")
    if p.join_url:
        lines.append(f"🔗 Звонок: {p.join_url}")
    if p.invite_subject:
        lines.append(f"✉️ Инвайт: {p.invite_subject[:90]}")
    return "\n".join(lines)
