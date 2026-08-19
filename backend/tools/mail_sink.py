"""Per-profile mailbox generator + reliable, persistent inbox reader.

Two providers, same durable store and same dashboard:

  mailpit  — local throwaway sink (http://localhost:8025). `@mail.kz` is just a
             label; Mailpit catches anything sent into it. Nothing real arrives.
  mailgun  — REAL mailboxes. Every profile gets <slug>@<MAILGUN_DOMAIN>; a
             catch-all inbound route stores every message Mailgun receives for
             that domain, and we poll the events API for it. Mail actually sent
             to those addresses from the outside world lands here.

    python -m backend.tools.mail_sink --setup      # (mailgun) create the catch-all route
    python -m backend.tools.mail_sink --assign     # give every profile an address
    python -m backend.tools.mail_sink --poll       # merge provider -> durable store once
    python -m backend.tools.mail_sink --send TO    # send one probe mail (mailgun)
    python -m backend.tools.mail_sink --seed       # send demo mails into the sink (mailpit)
    python -m backend.tools.mail_sink --list       # print the durable store
    python -m backend.tools.mail_sink --status     # provider/domain/route health

Reliability: every poll MERGES the provider's current messages into
`uploads/inbox/mail_sink.json` keyed by message id. Clearing Mailpit, or Mailgun
expiring stored messages after ~3 days, never drops anything already captured —
the durable store is the source of truth for the dashboard.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx

from backend.config import settings

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "backend" / "data" / "profiles.json"
ADDR_FILE = ROOT / "backend" / "data" / "mail_addresses.json"
STORE = ROOT / "uploads" / "inbox" / "mail_sink.json"


def _env(name: str, fallback: str) -> str:
    """Process env wins over .env (settings), so pm2/CLI can override per run."""
    return os.getenv(name) or fallback


PROVIDER = _env("MAIL_PROVIDER", settings.mail_provider or "mailpit").lower()

# --- mailgun ---------------------------------------------------------------
MG_KEY = _env("MAILGUN_API_KEY", settings.mailgun_api_key)
MG_DOMAIN = _env("MAILGUN_DOMAIN", settings.mailgun_domain)
MG_BASE = _env("MAILGUN_BASE", settings.mailgun_base or "https://api.mailgun.net").rstrip("/")
MG_AUTH = ("api", MG_KEY)

MG_SMTP_HOST = _env("MAILGUN_SMTP_HOST", settings.mailgun_smtp_host or "smtp.mailgun.org")
MG_SMTP_PORT = int(_env("MAILGUN_SMTP_PORT", str(settings.mailgun_smtp_port or 587)))
MG_SMTP_LOGIN = _env("MAILGUN_SMTP_LOGIN", settings.mailgun_smtp_login)
MG_SMTP_PASSWORD = _env("MAILGUN_SMTP_PASSWORD", settings.mailgun_smtp_password)
SEND_TRANSPORT = _env("MAIL_SEND_TRANSPORT", settings.mail_send_transport or "api").lower()

# --- mailpit ---------------------------------------------------------------
MAILPIT_API = _env("MAILPIT_API", "http://localhost:8025")
SMTP_HOST = _env("MAILPIT_SMTP_HOST", "localhost")
SMTP_PORT = int(_env("MAILPIT_SMTP_PORT", "1025"))

# The domain the addresses live on. On mailgun it MUST be the Mailgun domain,
# otherwise mail to those addresses is delivered somewhere we cannot read.
DOMAIN = _env("MAIL_DOMAIN", settings.mail_domain) or (
    MG_DOMAIN if PROVIDER == "mailgun" else "mail.kz")

ROUTE_DESC = "jobfinder catch-all -> store"

# ---- classification -------------------------------------------------------
_INTERVIEW = re.compile(
    r"\b(interview|schedule a call|book a time|calendly|assessment|next steps?|"
    r"screening|meet(ing)? with|phone screen|move forward|shortlist|"
    r"собеседовани|интервью|назначить звонок|следующий этап|тестовое задание)\b", re.I)
_REJECT = re.compile(
    r"\b(unfortunately|not moving forward|decided not to|other candidates|"
    r"won'?t be proceeding|not a (good )?fit|regret to inform|"
    r"we will not|declined|к сожалению|отказ|не сможем предложить|не подошли)\b", re.I)
_OFFER = re.compile(
    r"\b(offer letter|pleased to offer|extend an offer|welcome aboard|"
    r"предлагаем вам|оффер|job offer)\b", re.I)
_ACK = re.compile(
    r"\b(application received|thanks? for applying|we received your|"
    r"has been received|получили ваш|заявка принята|confirm(ing)? (your )?application)\b", re.I)


def classify(subject: str, body: str) -> str:
    text = f"{subject}\n{body}"
    # order matters: offer wins; rejection is checked BEFORE interview because
    # rejections often contain negated interview phrasing ("not to move forward").
    if _OFFER.search(text):
        return "offer"
    if _REJECT.search(text):
        return "rejection"
    if _INTERVIEW.search(text):
        return "interview"
    if _ACK.search(text):
        return "ack"
    return "other"


# ---- address generation ---------------------------------------------------
def _slug(profile: dict) -> str:
    base = (profile.get("full_name") or profile.get("id") or "").lower()
    base = re.sub(r"[^a-z0-9]+", ".", base).strip(".")
    return base or (profile.get("id") or "candidate")


def assign_addresses(write_back: bool = True, domain: str | None = None) -> dict[str, str]:
    """Give every non-sample profile a deterministic <slug>@<domain> address.
    Returns {profile_id: address}. Persists a map + (optionally) sets profile['email']."""
    dom = domain or DOMAIN
    profiles = json.loads(PROFILES.read_text())
    addrs: dict[str, str] = {}
    seen: set[str] = set()
    for p in profiles:
        if p.get("is_sample"):
            continue
        slug = _slug(p)
        addr = f"{slug}@{dom}"
        n = 2
        while addr in seen:
            addr = f"{slug}{n}@{dom}"
            n += 1
        seen.add(addr)
        addrs[p["id"]] = addr
        if write_back:
            p["email"] = addr
            if isinstance(p.get("resume"), dict):
                p["resume"].setdefault("personal_info", {})["email"] = addr
    if write_back:
        PROFILES.write_text(json.dumps(profiles, ensure_ascii=False, indent=2))
    ADDR_FILE.write_text(json.dumps(addrs, ensure_ascii=False, indent=2))
    return addrs


def address_map() -> dict[str, str]:
    if ADDR_FILE.exists():
        return json.loads(ADDR_FILE.read_text())
    return {}


# ---- durable store --------------------------------------------------------
# Full recruiter message text is kept alongside the snippet so the dashboard can
# show the whole email, not just the header. Capped so a stray huge/HTML body can
# never bloat the store (recruiter mail is a few KB; 40k is a generous ceiling).
MAX_BODY = 40_000


def _load_store() -> dict[str, Any]:
    if STORE.exists():
        try:
            data = json.loads(STORE.read_text())
            # Guard the shape: a hand-edit that leaves a list/str or drops "messages"
            # would otherwise KeyError in every consumer (summary/poll/record_inbound).
            if isinstance(data, dict) and isinstance(data.get("messages"), dict):
                return data
        except Exception:
            pass
    return {"messages": {}, "updated": 0}


def _save_store(store: dict[str, Any]) -> None:
    # Not locked across processes: the dashboard's poller thread and a CLI --poll can
    # interleave read-modify-write. Worst case one merge loses a just-captured message,
    # and the next poll re-adds it (Mailgun keeps events ~30 days, Mailpit keeps the
    # message) — self-healing, so this stays lock-free on purpose.
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2))
    tmp.replace(STORE)  # atomic — never leaves a half-written store


def _addr(obj: dict) -> str:
    return (obj or {}).get("Address", "") if isinstance(obj, dict) else str(obj or "")


def _emails(raw: Any) -> list[str]:
    """Pull bare addresses out of a To/Cc header that may be a list or a string."""
    if isinstance(raw, list):
        parts = [str(x) for x in raw]
    else:
        parts = str(raw or "").split(",")
    out: list[str] = []
    for part in parts:
        m = re.search(r"[\w.+-]+@[\w.-]+", part)
        if m:
            out.append(m.group(0).lower())
    return out


def application_address(profile_id: str, jid: str, domain: str | None = None) -> str | None:
    """Deterministic per-APPLICATION reply address `<base-local>+<jid>@<domain>`,
    derived from the profile's assigned base address. A recruiter replying to this
    address tells us BOTH the candidate and the exact position (jid). Returns None
    when the profile has no base address (then the caller keeps the base email)."""
    base = address_map().get(profile_id)
    if not base or not jid:
        return None
    local, _, dom = base.partition("@")
    return f"{local}+{jid}@{dom or (domain or DOMAIN)}"


def _resolve_via_reports(to_addrs: list[str]) -> tuple[str | None, str | None]:
    """Fallback: match a recipient against the `application_email` baked into each
    prefill report.json. Rescues replies whose base address was re-assigned (slug
    changed by a later --assign) after the address was already baked into the form."""
    wanted = {a.lower() for a in to_addrs}
    root = ROOT / "uploads" / "prefill"
    for rep in root.glob("*/*/report.json"):
        try:
            r = json.loads(rep.read_text())
        except Exception:
            continue
        ae = (r.get("application_email") or "").lower()
        if ae and ae in wanted:
            return r.get("profile"), rep.parent.name
    return None, None


def resolve_application(to_addrs: list[str]) -> tuple[str | None, str | None]:
    """Map recipient addresses to (profile_id, jid). Handles per-application
    plus-addresses (`local+jid@dom`) and falls back to the profile-only base
    address (jid=None) so a reply to the bare address still attributes the candidate."""
    base_to_profile = {v.lower(): k for k, v in address_map().items()}
    for a in to_addrs:
        local, _, dom = a.lower().partition("@")
        base_local, plus, jid = local.partition("+")
        if plus and f"{base_local}@{dom}" in base_to_profile:
            return base_to_profile[f"{base_local}@{dom}"], (jid or None)
    for a in to_addrs:  # second pass: exact base-address match (no +jid)
        if a.lower() in base_to_profile:
            return base_to_profile[a.lower()], None
    return _resolve_via_reports(to_addrs)  # last resort: match a baked report address


def _record(store_msgs: dict, mid: str, *, frm: str, to: list[str], subject: str,
            body: str, snippet: str, received: str, ts: int, message_id: str = "") -> bool:
    """Insert one message if unseen. Returns True when it was new."""
    if not mid or mid in store_msgs:
        return False
    profile_id, jid = resolve_application(to)
    store_msgs[mid] = {
        "id": mid,
        "from": frm,
        "to": to,
        "profile": profile_id,
        "jid": jid,
        "subject": subject,
        "snippet": (snippet or body[:200]).strip(),
        "body": (body or "").strip()[:MAX_BODY],
        # RFC Message-ID of the incoming mail — lets a reply thread via In-Reply-To.
        "message_id": (message_id or "").strip(),
        "kind": classify(subject, body),
        "received": received,
        "captured_ts": ts,
    }
    return True


# ---- provider: mailpit ----------------------------------------------------
def _poll_mailpit(msgs: dict, ts: int) -> dict[str, Any]:
    try:
        r = httpx.get(f"{MAILPIT_API}/api/v1/messages", params={"limit": 500}, timeout=8)
        listing = r.json().get("messages", [])
    except Exception:
        return {"new": 0, "total": len(msgs), "error": "mailpit unreachable"}

    new = 0
    for m in listing:
        mid = m.get("ID")
        if not mid or mid in msgs:
            continue
        body = ""
        full: dict = {}
        try:
            full = httpx.get(f"{MAILPIT_API}/api/v1/message/{mid}", timeout=8).json()
            body = full.get("Text") or full.get("HTML") or ""
        except Exception:
            body = m.get("Snippet", "")
        new += _record(msgs, mid, frm=_addr(m.get("From")),
                       to=[_addr(x).lower() for x in (m.get("To") or [])],
                       subject=m.get("Subject", ""), body=body,
                       snippet=m.get("Snippet") or "", received=m.get("Created", ""), ts=ts,
                       message_id=(full.get("MessageID") if isinstance(full, dict) else "") or m.get("MessageID", ""))
    return {"new": new, "total": len(msgs)}


# ---- provider: mailgun ----------------------------------------------------
def mg_ready() -> str:
    """Empty string when Mailgun is configured, else the reason it is not."""
    if not MG_KEY:
        return "MAILGUN_API_KEY is not set"
    if not MG_DOMAIN:
        return "MAILGUN_DOMAIN is not set"
    return ""


def mg_routes() -> list[dict]:
    r = httpx.get(f"{MG_BASE}/v3/routes", auth=MG_AUTH, params={"limit": 100}, timeout=15)
    r.raise_for_status()
    return r.json().get("items", [])


def _catch_all_expression() -> str:
    return f'match_recipient(".*@{MG_DOMAIN}")'


def mg_ensure_route(inbound_url: str | None = None) -> dict[str, Any]:
    """Create/repoint the catch-all route (idempotent).

    Without `inbound_url`: action `store()` — the events poller then fetches bodies,
    but ONLY if message retrieval is enabled (takhet.com's is not → empty bodies).
    With `inbound_url`: action `forward('<url>')` ONLY — Mailgun POSTs the fully-parsed
    message (BODY included) to the webhook, retrieval-independent. `store()` is dropped
    on purpose: it yields no body here and, because it re-emits a `stored` event the
    poller ingests under a different key, it would duplicate every message (empty copy).
    Mailgun retries a failed forward, so no store() backup is needed. An existing
    catch-all route with the wrong actions is updated in place (PUT)."""
    err = mg_ready()
    if err:
        return {"ok": False, "error": err}
    expr = _catch_all_expression()
    actions = [f"forward('{inbound_url}')"] if inbound_url else ["store()"]
    for route in mg_routes():
        if route.get("expression") == expr:
            if route.get("actions") != actions:
                u = httpx.put(f"{MG_BASE}/v3/routes/{route['id']}", auth=MG_AUTH, timeout=15,
                              data={"priority": "0", "description": ROUTE_DESC,
                                    "expression": expr, "action": actions})
                if u.status_code >= 300:
                    return {"ok": False, "error": f"{u.status_code} {u.text[:300]}"}
                return {"ok": True, "updated": True, "id": route.get("id"), "actions": actions}
            return {"ok": True, "created": False, "id": route.get("id"), "actions": actions}
    # Free plan allows exactly one route. If a stale JobFinder route for a previous
    # domain is holding the only slot, reclaim it so the switch isn't wedged.
    stale = [r for r in mg_routes() if r.get("description") == ROUTE_DESC]
    reclaimed = []
    for r in stale:
        httpx.delete(f"{MG_BASE}/v3/routes/{r['id']}", auth=MG_AUTH, timeout=15)
        reclaimed.append(r["id"])
    # no stop(): leave any other route the account may grow later still able to fire.
    r = httpx.post(f"{MG_BASE}/v3/routes", auth=MG_AUTH, timeout=15,
                   data={"priority": "0", "description": ROUTE_DESC,
                         "expression": expr, "action": actions})
    if r.status_code >= 300:
        return {"ok": False, "error": f"{r.status_code} {r.text[:300]}", "reclaimed": reclaimed}
    return {"ok": True, "created": True, "id": r.json().get("route", {}).get("id"),
            "actions": actions, "reclaimed": reclaimed}


def mg_delete_route(all_ours: bool = True) -> dict[str, Any]:
    """Remove our catch-all route(s) — inbound capture stops, nothing else changes.
    all_ours=True also removes a route left over from a previous domain (matched by
    our description), which matters on the free plan where the route quota is 1."""
    expr = _catch_all_expression()
    killed = []
    for route in mg_routes():
        if route.get("expression") == expr or (all_ours and route.get("description") == ROUTE_DESC):
            httpx.delete(f"{MG_BASE}/v3/routes/{route['id']}", auth=MG_AUTH, timeout=15)
            killed.append(route["id"])
    return {"ok": True, "deleted": killed}


def _mg_iso(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _mg_fetch_stored(url: str) -> dict:
    """Full stored message. Mailgun drops these after ~3 days — a miss is not fatal,
    the durable store already has whatever we captured earlier."""
    try:
        r = httpx.get(url, auth=MG_AUTH, timeout=20,
                      headers={"Accept": "application/json"})
        if r.status_code < 300:
            return r.json()
    except Exception:
        pass
    return {}


def _poll_mailgun(msgs: dict, ts: int, pages: int = 5) -> dict[str, Any]:
    err = mg_ready()
    if err:
        return {"new": 0, "total": len(msgs), "error": err}

    url = f"{MG_BASE}/v3/{MG_DOMAIN}/events"
    params: dict[str, Any] = {"event": "stored", "limit": 300}
    new = 0
    try:
        for _ in range(pages):
            r = httpx.get(url, auth=MG_AUTH, params=params, timeout=20)
            if r.status_code >= 300:
                return {"new": new, "total": len(msgs),
                        "error": f"events {r.status_code} {r.text[:200]}"}
            payload = r.json()
            items = payload.get("items", [])
            if not items:
                break
            page_new = 0
            for ev in items:
                storage = ev.get("storage") or {}
                headers = (ev.get("message") or {}).get("headers") or {}
                mid = ("mg:" + (storage.get("key") or headers.get("message-id") or
                                ev.get("id") or ""))
                if mid == "mg:" or mid in msgs:
                    continue
                full = _mg_fetch_stored(storage.get("url", "")) if storage.get("url") else {}
                body = (full.get("stripped-text") or full.get("body-plain")
                        or full.get("body-html") or "")
                subject = full.get("subject") or headers.get("subject") or ""
                frm = (_emails(full.get("From") or headers.get("from")) or [""])[0]
                to = _emails(full.get("To") or headers.get("to")) or \
                    _emails(ev.get("recipient"))
                msg_id = full.get("Message-Id") or full.get("message-id") \
                    or headers.get("message-id") or ""
                page_new += _record(msgs, mid, frm=frm, to=to, subject=subject, body=body,
                                    snippet=body[:200], received=_mg_iso(ev.get("timestamp")),
                                    ts=ts, message_id=msg_id)
            new += page_new
            nxt = (payload.get("paging") or {}).get("next")
            # only walk further back while the whole page was unseen (we're behind)
            if not nxt or page_new < len(items):
                break
            url, params = nxt, {}
    except Exception as exc:
        return {"new": new, "total": len(msgs), "error": f"mailgun unreachable: {exc}"}
    return {"new": new, "total": len(msgs)}


# ---- provider: mailgun INBOUND webhook (the reliable way to get the BODY) ----
# The events poller can only fetch the stored body when the domain has "message
# retrieval" enabled — takhet.com does NOT (Mailgun 404s "Message retrieval disabled
# for domain"), so poller-captured messages have an empty body. A Mailgun route with a
# forward('<url>') action instead POSTs the fully-parsed message (body-plain /
# stripped-text included) to us at delivery, no retrieval needed. record_inbound()
# stores that — with the body. Point a route at POST /mail/inbound to turn it on.
def mg_verify_webhook(token: str, timestamp: str, signature: str) -> bool:
    """Verify a Mailgun webhook/route POST signature (HMAC-SHA256 of timestamp+token).
    Uses the HTTP webhook signing key if set, else the API key. False if anything is
    missing or the digest mismatches — the endpoint must reject on False."""
    import hashlib
    import hmac
    key = (getattr(settings, "mailgun_webhook_key", "") or MG_KEY or "").strip()
    if not (key and token and timestamp and signature):
        return False
    mac = hmac.new(key.encode(), f"{timestamp}{token}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, signature)


def record_inbound(fields: dict) -> dict[str, Any]:
    """Record one inbound message delivered by a Mailgun route's forward() POST. The
    parsed fields carry the FULL body, so this works even when message retrieval is
    disabled. Runs apply_statuses like the poller. Returns {ok, new, id}."""
    frm = (fields.get("from") or fields.get("From") or fields.get("sender") or "").strip()
    to = _emails(fields.get("recipient") or fields.get("To") or fields.get("to"))
    subject = fields.get("subject") or fields.get("Subject") or ""
    body = (fields.get("stripped-text") or fields.get("body-plain")
            or fields.get("body-html") or "")
    msg_id = (fields.get("Message-Id") or fields.get("message-id") or "").strip()
    mid = "mg:" + (msg_id.strip("<>") or (fields.get("token") or "").strip())
    if mid == "mg:":
        return {"ok": False, "error": "no message id"}
    try:
        ts_hdr = float(fields.get("timestamp") or 0)
        received = datetime.fromtimestamp(ts_hdr, tz=timezone.utc).isoformat() if ts_hdr \
            else datetime.now(timezone.utc).isoformat()
    except Exception:
        received = datetime.now(timezone.utc).isoformat()
    ts = int(_now())
    store = _load_store()
    new = _record(store["messages"], mid, frm=frm, to=to, subject=subject, body=body,
                  snippet=(fields.get("stripped-text") or body)[:200], received=received,
                  ts=ts, message_id=msg_id)
    if new:
        store["updated"] = ts
        apply_statuses(store)
        _save_store(store)
    return {"ok": True, "new": bool(new), "id": mid}


def _sender(frm: str = "") -> str:
    return frm or f"JobFinder <postmaster@{MG_DOMAIN}>"


def mg_send_api(to: str, subject: str, text: str, frm: str = "",
                headers: dict | None = None) -> dict[str, Any]:
    """Send one message through Mailgun's HTTP API (uses MAILGUN_API_KEY).
    On a SANDBOX domain the recipient must be an Authorized Recipient or this 400s;
    on a verified custom domain (e.g. takhet.com) any recipient is fine.
    `headers` become Mailgun `h:<name>` fields (e.g. In-Reply-To for threading)."""
    err = mg_ready()
    if err:
        return {"ok": False, "error": err}
    data = {"from": _sender(frm), "to": to, "subject": subject, "text": text}
    for k, v in (headers or {}).items():
        if v:
            data[f"h:{k}"] = v
    r = httpx.post(f"{MG_BASE}/v3/{MG_DOMAIN}/messages", auth=MG_AUTH, timeout=20, data=data)
    return {"ok": r.status_code < 300, "via": "api", "status": r.status_code,
            "body": r.text[:400]}


def mg_send_smtp(to: str, subject: str, text: str, frm: str = "") -> dict[str, Any]:
    """Send one message through Mailgun SMTP (uses the per-domain SMTP credential).
    STARTTLS on 587. Same deliverability as the API — just a different transport."""
    import ssl
    if not (MG_SMTP_LOGIN and MG_SMTP_PASSWORD):
        return {"ok": False, "error": "MAILGUN_SMTP_LOGIN/PASSWORD not set"}
    msg = EmailMessage()
    msg["From"] = _sender(frm)
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    try:
        with smtplib.SMTP(MG_SMTP_HOST, MG_SMTP_PORT, timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(MG_SMTP_LOGIN, MG_SMTP_PASSWORD)
            s.send_message(msg)
        return {"ok": True, "via": "smtp", "host": f"{MG_SMTP_HOST}:{MG_SMTP_PORT}"}
    except Exception as exc:
        return {"ok": False, "via": "smtp", "error": str(exc)}


def mg_send(to: str, subject: str, text: str, frm: str = "",
            transport: str | None = None) -> dict[str, Any]:
    """Send via the configured transport (MAIL_SEND_TRANSPORT: 'api' default, or 'smtp')."""
    t = (transport or SEND_TRANSPORT).lower()
    return mg_send_smtp(to, subject, text, frm) if t == "smtp" \
        else mg_send_api(to, subject, text, frm)


# ---- reply -> application status (record only; never submits) -------------
def _profile_name(profile_id: str) -> str:
    try:
        profs = json.loads(PROFILES.read_text())
        for p in profs:
            if p.get("id") == profile_id:
                return p.get("full_name") or profile_id
    except Exception:
        pass
    return profile_id


def _tg_notify(text: str) -> bool:
    """Send one Telegram message to the configured chat. Soft no-op if unconfigured."""
    tok, chat = settings.telegram_bot_token, settings.telegram_chat_id
    if not tok or not chat:
        return False
    try:
        r = httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15,
                       data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                             "disable_web_page_preview": "true"})
        return r.status_code < 300
    except Exception:
        return False


def _notify_interview(m: dict) -> None:
    """Push a Telegram alert for an INTERVIEW invitation only — naming which candidate
    account received it and for which position. Rejections/acks/offers do NOT notify."""
    name = _profile_name(m.get("profile") or "")
    acct = (m.get("to") or [""])[0]
    pos = m.get("jid") or "—"
    _tg_notify(
        "📞 <b>Приглашение на собеседование</b>\n"
        f"Кандидат: <b>{name}</b>\n"
        f"Аккаунт: <code>{acct}</code>\n"
        f"Вакансия: <code>{pos}</code>\n"
        f"От: {m.get('from','')}\n"
        f"Тема: {m.get('subject','')}")


_TERMINAL = ("submitted", "interview", "rejected")


def _advance(kind: str, current: str) -> str | None:
    """Forward-only application-status transition from a classified reply. Mirrors
    inbox_index.advance_decision but is inlined so we don't import that module (it
    hard-loads a Maildir reader absent on this deploy). Never demotes a status.

    interview/assessment/offer -> 'interview' (offer means accepted -> Принято);
    rejection -> 'rejected'; ack ('application received') -> 'submitted' as a backstop
    when the human's submit-watch didn't already record the submit."""
    if kind in ("interview", "assessment", "offer"):
        return "interview" if current not in ("interview", "rejected") else None
    if kind == "rejection":
        return "rejected" if current != "rejected" else None
    if kind == "ack":
        return "submitted" if current not in _TERMINAL else None
    return None


def _jid_for_profile_reply(profile_id: str, text: str) -> str | None:
    """A reply to a candidate's plain address resolves to the profile but not the
    position. Pick the position by matching the role title named in the reply text
    (e.g. "Interview invitation — Credit Scoring Data Scientist") against this
    profile's non-gated applications. Returns a jid, or None if none/ambiguous."""
    root = ROOT / "uploads" / "prefill" / profile_id
    if not root.exists():
        return None
    cands: list[tuple[str, str]] = []
    for rp in root.glob("*/report.json"):
        try:
            r = json.loads(rp.read_text())
        except Exception:
            continue
        if r.get("gated_out"):
            continue
        cands.append((rp.parent.name, r.get("job_title") or ""))
    if len(cands) == 1:  # only one application -> unambiguous
        return cands[0][0]
    tl = (text or "").lower()
    hits = [jid for jid, title in cands if title and title.lower() in tl]
    return hits[0] if len(hits) == 1 else None


def apply_statuses(store: dict[str, Any] | None = None) -> int:
    """Forward-advance each application's status from resolved recruiter mail, and
    fire an interview alert the first time a message advances to 'interview'.
    RECORD ONLY — never sends or submits. Returns the number of status changes."""
    from backend import status_store
    owns_store = store is None
    store = store or _load_store()
    changed = 0
    for m in store["messages"].values():
        if m.get("advanced"):
            continue
        pid, jid = m.get("profile"), m.get("jid")
        if pid and not jid:  # plain-address reply -> resolve the position from the text
            jid = _jid_for_profile_reply(pid, f"{m.get('subject','')} {m.get('snippet','')}")
            if jid:
                m["jid"] = jid  # persist so the dashboard shows the resolved position
        if not pid or not jid:
            continue
        kind = m.get("kind", "other")
        current = status_store.load(pid).get(jid, {}).get("status", "")
        new = _advance(kind, current)
        if new:
            status_store.mark(pid, jid, new)
            _touch_report(pid, jid)  # beat the /roles reset-epoch mtime filter
            # Alert ONLY on a genuine interview invitation (not an offer/assessment
            # that also advances to the 'interview' status) — per the product ask.
            if kind == "interview" and current != "interview":
                _notify_interview(m)
            m["advanced"] = True
            changed += 1
    if changed and owns_store:
        _save_store(store)
    return changed


def _touch_report(profile_id: str, jid: str) -> None:
    """Bump the report.json mtime so counts_by_url (which ignores reports older than
    the /roles reset epoch) surfaces a genuine mail-driven status change."""
    import os
    import time
    rep = ROOT / "uploads" / "prefill" / profile_id / jid / "report.json"
    try:
        if rep.exists():
            os.utime(rep, (time.time(), time.time()))
    except Exception:
        pass


# ---- polling --------------------------------------------------------------
def poll_once(ts: int = 0) -> dict[str, Any]:
    """Fetch the provider's messages and MERGE new ones into the durable store.
    Returns {"new": n, "total": N}. An unreachable provider is a soft no-op."""
    store = _load_store()
    msgs = store["messages"]
    res = _poll_mailgun(msgs, ts) if PROVIDER == "mailgun" else _poll_mailpit(msgs, ts)
    if res.get("new"):
        store["updated"] = ts
        apply_statuses(store)   # advance app statuses + interview alerts before save
        _save_store(store)
    return res


def summary(profile: str | None = None) -> dict[str, Any]:
    """Everything the dashboard needs: counts by kind + messages newest-first,
    the address book (profile -> address), and per-profile message counts.
    `profile` filters the message list (counts stay global for the header chips)."""
    store = _load_store()
    all_items = sorted(store["messages"].values(),
                       key=lambda m: (m.get("received") or ""), reverse=True)
    counts: dict[str, int] = {"interview": 0, "offer": 0, "rejection": 0, "ack": 0, "other": 0}
    per_profile: dict[str, int] = {}
    for m in all_items:
        counts[m.get("kind", "other")] = counts.get(m.get("kind", "other"), 0) + 1
        pid = m.get("profile")
        if pid:
            per_profile[pid] = per_profile.get(pid, 0) + 1
    items = [m for m in all_items if m.get("profile") == profile] if profile else all_items
    # Keep the list (and the 1 Hz SSE payload) light: ship the snippet + a has_body
    # flag, not every full body. The dashboard lazy-loads a full message via
    # message()/`/mail/message` only when a row is expanded.
    listed = [{**{k: v for k, v in m.items() if k not in ("body", "replies")},
               "has_body": bool(m.get("body") or m.get("snippet"))}
              for m in items]
    return {"counts": counts, "total": len(all_items), "shown": len(listed),
            "messages": listed, "filter": profile or "",
            "addresses": address_map(), "per_profile": per_profile,
            "updated": store.get("updated", 0),
            "provider": PROVIDER, "domain": DOMAIN}


def message(mid: str) -> dict[str, Any]:
    """One full stored message by id, including the body — backs `/mail/message`,
    which the inbox calls to show a recruiter's email in full. {} if unknown."""
    m = _load_store()["messages"].get(mid)
    return dict(m) if m else {}


# ---- SEND a reply to the recruiter (human-triggered from the inbox) --------
# Unlike the job-application flow (which never auto-submits), the inbox DOES send:
# the human writes the reply and clicks "Отправить" — this posts it. There is no
# auto-reply: send_reply() runs only from the POST /mail/send endpoint behind that
# button. It sends FROM the candidate address the recruiter wrote to (so the reply
# stays in identity + thread) and never lets the caller spoof a different From.
def reply_target(mid: str) -> dict[str, Any]:
    """The From/To/Subject the inbox should show before the human writes a reply.
    From = the candidate address the recruiter emailed (authoritative, server-side —
    the client cannot override it). {ok: False} for an unknown id."""
    m = _load_store()["messages"].get(mid)
    if not m:
        return {"ok": False, "error": "not found"}
    frm = _reply_from(m)
    to = (_emails(m.get("from")) or [""])[0]
    subject = m.get("subject") or ""
    re_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()
    return {"ok": True, "id": mid, "from": frm, "to": to, "subject": re_subject,
            "candidate": _profile_name(m.get("profile") or ""),
            "replied": bool(m.get("replied"))}


def _reply_from(m: dict) -> str:
    """The address to send the reply FROM. Prefer the recipient that belongs to the
    RESOLVED profile (so From follows the same candidate resolve_application picked,
    even if the mail listed two candidate addresses) — matched by base local-part,
    ignoring any +tag; then any @DOMAIN recipient; then the profile's base address;
    else the first recipient."""
    tos = m.get("to") or []
    prof = m.get("profile") or ""
    base = (address_map().get(prof) or "").lower()
    if base and "@" in base:
        blocal, bdom = base.split("@", 1)
        bbase = blocal.split("+", 1)[0]
        for a in tos:  # the exact address the recruiter used for THIS profile
            al = (a or "").lower()
            if "@" in al:
                loc, dom = al.split("@", 1)
                if dom == bdom and loc.split("+", 1)[0] == bbase:
                    return a
        return address_map().get(prof)  # fall back to the profile's own base address
    for a in tos:
        if a and a.lower().endswith("@" + DOMAIN.lower()):
            return a
    return tos[0] if tos else ""


def send_reply(mid: str, text: str, subject: str | None = None) -> dict[str, Any]:
    """SEND a reply to one recruiter message, on behalf of the candidate. This DOES
    email the recruiter (Mailgun API), so it must run only from the human's click.
    From is derived server-side (never from the caller); the reply is threaded via
    In-Reply-To/References when the original Message-ID was captured. On success the
    message is marked `replied` in the store. Returns {ok, ...} / {ok: False, error}."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty message"}
    store = _load_store()
    m = store["messages"].get(mid)
    if not m:
        return {"ok": False, "error": "not found"}
    if PROVIDER != "mailgun":
        return {"ok": False, "error": f"sending needs the mailgun provider (now: {PROVIDER})"}
    frm = _reply_from(m)
    to = (_emails(m.get("from")) or [""])[0]
    if not to:
        return {"ok": False, "error": "no recruiter address on the message"}
    subj = (subject or m.get("subject") or "").strip()
    if not subj.lower().startswith("re:"):
        subj = f"Re: {subj}".strip()
    headers = {}
    orig = m.get("message_id")
    if orig:
        headers["In-Reply-To"] = orig
        headers["References"] = orig
    res = mg_send_api(to, subj, text, frm=frm, headers=headers)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or res.get("body") or "send failed",
                "to": to, "from": frm}
    # Record the reply IN FULL so the inbox can show the thread (message → our reply),
    # mark "отвечено", and let the human avoid doubles. Lock-free like the rest of the
    # store (a lost write self-heals on the next send).
    ts = int(_now())
    fresh = _load_store()
    if mid in fresh["messages"]:
        fresh["messages"][mid]["replied"] = True
        fresh["messages"][mid]["replied_ts"] = ts
        # Append to a list so multiple replies to one thread are all kept.
        thread = fresh["messages"][mid].setdefault("replies", [])
        thread.append({"text": text, "subject": subj, "from": frm, "to": to, "ts": ts})
        _save_store(fresh)
    return {"ok": True, "id": mid, "to": to, "from": frm, "subject": subj, "sent": True}


def _now() -> float:
    import time as _t
    return _t.time()


# ---- reply DRAFT suggestion (text only — pre-fills the box; NEVER auto-sends) --
# Separate from send_reply: this only *suggests* text. The human edits it and clicks
# Отправить (send_reply) — the draft path itself contains no mg_send call.
def _job_context(profile_id: str, jid: str) -> tuple[str, str]:
    """(job_title, company) from the application's report.json — grounds the draft
    so it names the real role. Best-effort; empty strings when unknown."""
    if not (profile_id and jid):
        return "", ""
    rp = ROOT / "uploads" / "prefill" / profile_id / jid / "report.json"
    try:
        r = json.loads(rp.read_text())
        return (r.get("job_title") or ""), (r.get("company") or "")
    except Exception:
        return "", ""


def _clean_draft(text: str) -> str:
    """Strip the odds and ends a small model tends to wrap around the reply body."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
    lines = t.splitlines()
    while lines and re.match(r"^\s*(subject|тема|body|draft|ответ)\s*[:：]", lines[0], re.I):
        lines.pop(0)
    t = "\n".join(lines).strip()
    if len(t) >= 2 and t[0] in "\"'«" and t[-1] in "\"'»":
        t = t[1:-1].strip()
    return t


_DRAFT_PROMPT = """Today's date is {today}. You are writing a reply email on behalf of \
{name}, a job candidate.{applied} Below is a message the candidate received from a recruiter.

Write ONLY the body of {name}'s reply, in the SAME language as the recruiter's message BODY \
below (decide the language from the body text, NOT from the subject line — the subject may be in \
a different language). Rules:
- If the recruiter proposes interview dates/times, ACCEPT and confirm the NEAREST upcoming one \
(relative to today's date above), quoting it exactly as the recruiter wrote it. If several are \
offered, choose the earliest future option and add that you can adjust if needed.
- If the recruiter invites to an interview but gives NO specific time, say you are glad to attend \
and ask them to propose a time — do not invent a date.
- Do NOT invent facts: no dates, times, salary, or details not present in the recruiter's message.
- Professional, warm, concise (3-6 sentences). Finish with a sign-off using the name {name}.
- Output the email body only — no "Subject:" line, no preamble, no surrounding quotes.

Recruiter's message:
From: {frm}
Subject: {subject}

{body}
"""


def _fallback_draft(name: str, body: str) -> str:
    """Deterministic reply when the LLM is unavailable. Language is decided ONLY by
    Cyrillic actually present in the recruiter's body — with no body there is no
    reliable signal, so it defaults to English (recruiters on file write in English)
    and the UI flags a no-body draft as approximate. Never guesses a date."""
    if re.search(r"[а-яё]", body or "", re.I):
        return (f"Здравствуйте!\n\nСпасибо за приглашение — мне интересна позиция, и я готов(а) "
                "пройти собеседование. Подскажите, пожалуйста, удобное время из предложенных "
                "(или предложите слот), и я подтвержу.\n\n"
                f"С уважением,\n{name}")
    return (f"Hello,\n\nThank you for the invitation — I'm very interested and glad to attend the "
            "interview. Could you please confirm which of the proposed times works, or suggest a "
            f"slot, and I'll confirm.\n\nBest regards,\n{name}")


def draft_reply(mid: str) -> dict[str, Any]:
    """SUGGEST reply text for one recruiter message, in the SAME language, tuned for
    interview invitations (confirm the nearest date the recruiter proposed). TEXT ONLY —
    this never sends; the human edits it and clicks Отправить. {ok: False} if unknown."""
    m = _load_store()["messages"].get(mid)
    if not m:
        return {"ok": False, "error": "not found"}
    name = _profile_name(m.get("profile") or "") or "—"
    title, company = _job_context(m.get("profile") or "", m.get("jid") or "")
    body = (m.get("body") or m.get("snippet") or "").strip()[:6000]
    subject = m.get("subject") or ""
    frm = m.get("from") or ""
    applied = ""
    if title:
        applied = f' They applied for the "{title}" position' + (f" at {company}." if company else ".")
    has_body = bool(m.get("body") or m.get("snippet"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d (%A)")
    prompt = _DRAFT_PROMPT.format(today=today, name=name, applied=applied, frm=frm,
                                  subject=subject, body=body)
    draft = ""
    # With NO captured body there is no reliable language/date signal — an English
    # subject does not imply an English body — so don't let the LLM guess from the
    # subject. Use the honest fallback; the UI flags a no-body draft as approximate.
    if has_body and (settings.llm_url or settings.anthropic_api_key):
        try:
            from backend.services.tailor.tailor import _llm_complete
            draft = _clean_draft(_llm_complete(prompt))
        except Exception:
            draft = ""
    if not draft:
        draft = _fallback_draft(name, body)
    return {"ok": True, "id": mid, "draft": draft, "candidate": name,
            "has_body": has_body, "sent": False}


def status() -> dict[str, Any]:
    """Health of whichever provider is configured — printed by --status and
    shown in the dashboard header so a dead route is visible, not silent."""
    out: dict[str, Any] = {"provider": PROVIDER, "domain": DOMAIN,
                           "addresses": len(address_map())}
    if PROVIDER == "mailgun":
        err = mg_ready()
        out["configured"] = not err
        if err:
            out["error"] = err
            return out
        out["mailgun_domain"] = MG_DOMAIN
        out["send_transport"] = SEND_TRANSPORT
        out["smtp_login_set"] = bool(MG_SMTP_LOGIN and MG_SMTP_PASSWORD)
        try:
            expr = _catch_all_expression()
            routes = mg_routes()
            out["route"] = next((r for r in routes if r.get("expression") == expr), None)
            out["catch_all"] = out["route"] is not None
        except Exception as exc:
            out["error"] = str(exc)
        out["address_domain_matches"] = (DOMAIN == MG_DOMAIN)
    else:
        out["mailpit_api"] = MAILPIT_API
        try:
            out["reachable"] = httpx.get(f"{MAILPIT_API}/api/v1/info", timeout=5).status_code < 300
        except Exception:
            out["reachable"] = False
    return out


# ---- demo seeding (so you can watch classification work) ------------------
_DEMO = [
    ("recruiting@acme-demo.test", "interview",
     "Interview invitation — Customer Support",
     "Hi, we'd love to schedule a call. Please pick a time on our Calendly for the next steps."),
    ("talent@globex-demo.test", "rejection",
     "Update on your application",
     "Thank you for applying. Unfortunately we have decided not to move forward with other candidates."),
    ("hr@initech-demo.test", "offer",
     "We are pleased to offer you the role",
     "Congratulations! Please find your offer letter attached. Welcome aboard."),
    ("no-reply@umbrella-demo.test", "ack",
     "We received your application",
     "Thanks for applying — your application has been received and is under review."),
]


def seed(ts: int = 0) -> int:
    addrs = list(address_map().values()) or [f"demo.candidate@{DOMAIN}"]
    sent = 0
    for i, (frm, _kind, subj, body) in enumerate(_DEMO):
        msg = EmailMessage()
        msg["From"] = frm
        msg["To"] = addrs[i % len(addrs)]
        msg["Subject"] = subj
        msg.set_content(body)
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=8) as s:
                s.send_message(msg)
            sent += 1
        except Exception as exc:
            print(f"  ! could not reach Mailpit SMTP {SMTP_HOST}:{SMTP_PORT}: {exc}")
            break
    return sent


def main() -> None:
    import time
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setup", action="store_true",
                    help="(mailgun) create the catch-all inbound route")
    ap.add_argument("--webhook", metavar="URL",
                    help="(mailgun) repoint the catch-all route to forward(URL) + store() so the "
                         "message BODY reaches POST /mail/inbound (needed: retrieval is disabled)")
    ap.add_argument("--teardown", action="store_true",
                    help="(mailgun) delete the catch-all inbound route")
    ap.add_argument("--assign", action="store_true",
                    help=f"assign <slug>@{DOMAIN} to every profile")
    ap.add_argument("--poll", action="store_true", help="merge provider -> durable store once")
    ap.add_argument("--send", metavar="TO", help="(mailgun) send one probe message")
    ap.add_argument("--smtp", action="store_true",
                    help="force the SMTP transport for --send (default follows MAIL_SEND_TRANSPORT)")
    ap.add_argument("--seed", action="store_true", help="(mailpit) send demo mails into the sink")
    ap.add_argument("--list", action="store_true", help="print the durable store summary")
    ap.add_argument("--status", action="store_true", help="provider / domain / route health")
    a = ap.parse_args()
    now = int(time.time())

    if a.setup:
        print("Route:", json.dumps(mg_ensure_route(), ensure_ascii=False))
    if a.webhook:
        print("Route:", json.dumps(mg_ensure_route(inbound_url=a.webhook), ensure_ascii=False))
    if a.teardown:
        print("Route:", json.dumps(mg_delete_route(), ensure_ascii=False))
    if a.assign:
        addrs = assign_addresses()
        print(f"Assigned {len(addrs)} @{DOMAIN} addresses:")
        for pid, addr in list(addrs.items())[:8]:
            print(f"  {pid:34} -> {addr}")
        if len(addrs) > 8:
            print(f"  … +{len(addrs) - 8} more (full map in {ADDR_FILE.relative_to(ROOT)})")
    if a.send:
        res = mg_send(a.send, "JobFinder send probe",
                      "Probe message from JobFinder. If it reached you, outbound on "
                      "this domain works. Reply to it to test inbound capture.",
                      transport="smtp" if a.smtp else None)
        print("Send:", json.dumps(res, ensure_ascii=False))
    if a.seed:
        n = seed(now)
        print(f"Seeded {n} demo message(s) into Mailpit ({SMTP_HOST}:{SMTP_PORT}).")
    if a.poll or a.seed or a.send:
        res = poll_once(now)
        print(f"Poll [{PROVIDER}]: +{res.get('new', 0)} new, {res.get('total', 0)} total"
              + (f"  ({res['error']})" if res.get("error") else ""))
    if a.list:
        s = summary()
        print(f"\nStore: {s['total']} messages  {s['counts']}")
        for m in s["messages"][:20]:
            who = m.get("profile") or "?"
            print(f"  [{m['kind']:9}] {who:28} {m['subject'][:44]}")
    if a.status:  # last, so it reflects anything the other flags just changed
        print(json.dumps(status(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
