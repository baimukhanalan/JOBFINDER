"""WriteX — AMCAT "Email Writing" free-response handler + business-email drafting.

The WriteX module shows an instruction ("Compose an email response for the topic provided. Please
ensure that your response is a minimum of 30 words.") plus a TOPIC that usually names an explicit
recipient email address, and THREE fields: `To:` / `Subject` / `Compose your response`. It is a
free-response module, so the harvester's generic typing handler would either type the wrong thing or
stall the whole run at this gate — this drafts a proper business email (>=30 words, addressing the
topic, using the explicit recipient) and lets the AMCAT adapter fill + submit it, so the run advances.

REPLAY-first, like the rest of the bank: if the exact prompt was imported from the qa_bot snapshot
(a banked answer_key with kind='email'), that historical email is reused verbatim; otherwise the local
model drafts one (settings.llm_*), with a deterministic template fallback so a draft never fails.

Pure/offline helpers (`topic`, `extract_recipient`, `parse_email`, `subject_from`, `_fallback_email`)
are unit-tested with no network in `backend/tests/test_writex.py`.
"""
from __future__ import annotations

import re

INSTRUCTION = ("Compose an email response for the topic provided. Please ensure that your response is "
               "a minimum of 30 words.")

# A page is WriteX email-writing if it carries the compose-an-email instruction (or an obvious
# write-an-email topic). Deliberately narrow so a plain typing-SPEED test isn't misrouted here.
_WRITEX_RE = re.compile(
    r"compose an email|write an email|email response for the topic|draft an email|"
    r"reply to (the|this) email|compose your response", re.I)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_MIN_WORDS = 30


def is_writex(text: str) -> bool:
    return bool(_WRITEX_RE.search(text or ""))


def topic(text: str) -> str:
    """The topic/situation after the compose instruction (mirrors the qa_bot extractor). If the
    instruction isn't present, the whole text is the topic."""
    t = text or ""
    if INSTRUCTION in t:
        t = t.split(INSTRUCTION, 1)[1]
    # strip a trailing "Word count:" widget the platform appends
    t = re.split(r"\bword count\s*:", t, 1, flags=re.I)[0]
    return t.strip()


def extract_recipient(text: str) -> str | None:
    """The explicit recipient email named in the topic (WriteX always names one). None if absent."""
    m = _EMAIL_RE.search(text or "")
    return m.group(0) if m else None


def parse_email(answer: str) -> dict | None:
    """Parse a stored/historical email 'To: ..\\nSubject: ..\\n\\n<body>' into {to, subject, body}.
    Returns None if it isn't in that structured shape."""
    if not answer:
        return None
    m = re.search(r"To:\s*([^\n]+)\n\s*Subject:\s*([^\n]+)\n(.+)$", answer.strip(), re.S)
    if not m:
        return None
    to, subject, body = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    if not to or not body:
        return None
    return {"to": to, "subject": subject, "body": body}


def _name_of(topic_text: str) -> str:
    m = re.search(r"your name is ([A-Z][a-zA-Z.'\-]+(?: [A-Z][a-zA-Z.'\-]+)?)", topic_text or "")
    return m.group(1).strip() if m else ""


def subject_from(topic_text: str) -> str:
    """A short, safe subject line derived from the topic (used when a draft has none)."""
    t = (topic_text or "").strip()
    m = re.search(r"regarding ([^.\n]{6,60})", t, re.I) or re.search(r"about ([^.\n]{6,60})", t, re.I)
    if m:
        return m.group(1).strip().capitalize()
    return "Follow-up on your email"


def _fallback_email(topic_text: str, recipient: str | None) -> dict:
    """A deterministic, >=30-word professional email that references the topic — used when the local
    model is unavailable or returns something too short. Never fabricates specifics; stays generic and
    courteous while acknowledging the request and proposing a next step."""
    name = _name_of(topic_text)
    to = recipient or "recipient@company.com"
    body = (
        "Dear colleague,\n\n"
        "Thank you for your email regarding this matter. I have reviewed the details you shared and "
        "understand what is being requested. I will take ownership of the next steps, coordinate with "
        "the relevant people, and keep you updated on progress. Please let me know if you need any "
        "further information or would like to discuss the timeline.\n\n"
        "Best regards,\n" + (name or "The team"))
    return {"to": to, "subject": subject_from(topic_text), "body": body}


def _word_count(s: str) -> int:
    return len((s or "").split())


async def _llm_email(prompt_text: str, recipient: str | None, timeout: float = 45.0) -> dict | None:
    """Ask the local model to draft the business email. Returns {to,subject,body} or None on failure /
    a too-short body (caller falls back to the deterministic template)."""
    import httpx

    from backend.config import settings
    rcpt = recipient or "the recipient named in the topic"
    sys_prompt = (
        "You are a professional writing a business email for a workplace assessment. Read the topic and "
        "write a clear, polite, complete email that addresses every point requested, using ONLY facts "
        "given in the topic (never invent names, numbers or promises). The body must be at least 30 "
        "words. Reply in EXACTLY this format and nothing else:\n"
        "To: <email>\nSubject: <specific subject>\n\n<email body>")
    user = f"Recipient: {rcpt}\n\nTopic:\n{prompt_text}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{settings.llm_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_key}", "Content-Type": "application/json"},
                json={"model": settings.llm_model,
                      "messages": [{"role": "system", "content": sys_prompt},
                                   {"role": "user", "content": user}],
                      "temperature": 0.3, "max_tokens": 400, "stream": False})
            r.raise_for_status()
            txt = r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None
    parsed = parse_email(txt or "")
    if not parsed:
        return None
    if recipient:                       # never let the model invent a different recipient
        parsed["to"] = recipient
    if _word_count(parsed["body"]) < _MIN_WORDS:
        return None
    return parsed


async def draft_email(prompt_text: str, *, banked: dict | None = None) -> dict:
    """Produce {to, subject, body} for a WriteX item. Order: a banked historical email (verbatim) ->
    the local model -> a deterministic template. The body is always >=30 words; the recipient is the
    explicit one named in the topic when present."""
    t = topic(prompt_text)
    recipient = extract_recipient(prompt_text)
    # 1) a banked historical email (from the qa_bot snapshot import)
    if banked:
        stored = banked.get("text") or ""
        parsed = parse_email(stored) or (
            {"to": banked.get("to"), "subject": banked.get("subject"), "body": banked.get("body")}
            if banked.get("body") else None)
        if parsed and parsed.get("body") and _word_count(parsed["body"]) >= _MIN_WORDS:
            if recipient:
                parsed["to"] = recipient
            return parsed
    # 2) local model
    llm = await _llm_email(t, recipient)
    if llm:
        return llm
    # 3) deterministic fallback
    return _fallback_email(t, recipient)
