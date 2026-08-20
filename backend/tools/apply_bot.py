"""JobFinder control bot — the convenient Telegram front-end for the prefill engine.

    python -m backend.tools.apply_bot

Flow (all from inside Telegram):
  ➕ New application  -> generates a candidate, tailors a résumé, pre-fills a real
                        Salmon (Ashby) form headlessly, and sends you the screenshot.
  🖥 Open in browser  -> opens that SAME form, pre-filled, in a visible browser ON
                        THIS MAC so you can review and click Submit yourself.
  ✅ Submitted / 🗑    -> records the status the dashboard + tracker read.
  📋 Queue / 🌐 Dash   -> list pre-filled items / open the web dashboard.

The engine NEVER clicks Submit — the bot only opens and fills; the human submits.
Only the chat id in backend/.env (TELEGRAM_CHAT_ID) may drive the bot.

NOTE on "browser transition": the visible browser opens on the machine running this
bot (this Mac). Operating from a phone away from the Mac, use the co-pilot/noVNC
server surface instead — that's the remote-submit deployment.
"""
from __future__ import annotations

import asyncio
import html
import itertools
import os
import re
import shutil
import socket
import time
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from backend.applier.batch import (_load_assignments, _record_assignment, at_application_cap,
                                    country_allowed)
from backend.applier.runner import ATS_GATE_MIN, MATCH_GATE_MIN, prefill_application
from backend.applier.strategies.base import strip_review
from backend.config import settings
from backend.profiles.facts import load_facts
from backend.profiles.store import PROJECT_ROOT, Profile, get_profile
from backend.services.tailor.answers import deterministic_answers
from backend import status_store
from backend.tools.gen_profiles import generate
from backend.tools.salmon_autofill import (ASHBY_BOARD, _CS_KEYWORDS,
                                           _upsert_profile)

PREFILL_ROOT = PROJECT_ROOT / "uploads" / "prefill"
DASH_PORT = 8089

# token -> {profile, url, title, company, jid, name, description, resume_pdf, reply_addr}
SESSIONS: dict[str, dict] = {}
# batch_id -> [token, ...]  (a group of prefills opened together as N tabs)
BATCHES: dict[str, list[str]] = {}
# token / "batch:<id>" -> live "open browser" subprocess
OPEN_PROCS: dict[str, asyncio.subprocess.Process] = {}
BATCH_SIZE = 5  # how many applications the "➕ N заявок" button prepares at once

# Résumé-parser-only (anti-spam) mode: when ON, applications are pre-filled by uploading
# the résumé to the ATS's OWN parser + attaching it, and NOTHING else is typed — the bot
# then posts the remaining required fields to the chat as a checklist and the human fills
# them in. This is the intended flow ("push the résumé once, hand the rest to the human")
# AND it avoids the machine-gun field-fill that trips ATS spam detection, so it is
# ON BY DEFAULT. Set RESUME_PARSER_ONLY=0 (or flip the 📎 menu button) to get the old
# full auto-fill instead. In-memory dict so handlers mutate it live without `global`;
# note a bot restart re-reads this default, so the env is the durable setting.
_PARSER_ONLY = {"on": os.getenv("RESUME_PARSER_ONLY", "1").strip().lower()
                in ("1", "true", "yes", "on")}
# Optional HARD cap on applications to ONE posting, shared with the cron batch via
# uploads/prefill/_assignments.json. Default 0 = no numeric cap. Regardless of the cap,
# the ledger guarantees NO CANDIDATE REPEATS A POSITION: distinct family-matched
# candidates MAY apply to the same posting (up to the family pool), but the same person
# never applies to it twice. Set >0 to also cap how many distinct candidates per posting.
PER_POSITION = int(os.getenv("PER_POSITION", "0"))
# How many candidates to try per role before giving up, so a batch slot is never lost to
# one sub-threshold score: keep picking/generating until fit≥MATCH_GATE_MIN & ATS≥ATS_GATE_MIN.
MAX_CANDIDATE_TRIES = int(os.getenv("MAX_CANDIDATE_TRIES", "8"))
# Auto-open the pre-filled browser on the Mac right after prefill (no manual "open" tap).
# Off → the old manual flow. Set AUTO_OPEN_BROWSER=0 to restore the button-driven open.
AUTO_OPEN = os.getenv("AUTO_OPEN_BROWSER", "1").strip().lower() in ("1", "true", "yes", "on")
_ids = itertools.count(1)
_jobs_cache: list[dict] = []
_jobs_ts: float = 0.0  # when the role list was last fetched (TTL below)
_JOBS_TTL = 600        # refresh remote-role list every 10 min so closed roles drop out
_role_rr = itertools.count(0)
_cand_rr = itertools.count(0)  # round-robin over the fixed candidate pool


def _pool_ids() -> list[str]:
    from backend.profiles.store import load_profiles
    return sorted(pid for pid in load_profiles() if pid.startswith("gen_kz_"))


def _profile_family(pid: str) -> str | None:
    import json
    try:
        f = PROJECT_ROOT / "backend" / "data" / "facts" / f"{pid}.json"
        return json.loads(f.read_text()).get("role_family")
    except Exception:
        return None


def _profile_families(pid: str) -> list[str]:
    """Every role family a persona honestly covers (multi-family model): facts
    `role_families`, falling back to the single `role_family`."""
    import json
    try:
        f = PROJECT_ROOT / "backend" / "data" / "facts" / f"{pid}.json"
        data = json.loads(f.read_text())
    except Exception:
        return []
    fams = data.get("role_families")
    if isinstance(fams, list) and fams:
        return [x for x in fams if x]
    single = data.get("role_family")
    return [single] if single else []


def _family_pool(role_title: str) -> list[str]:
    """Pool profile ids whose archetype cluster COVERS this role (résumé genuinely fits
    -> clears the match gate). Multi-family: a persona qualifies if the role's family is
    among the several it covers, so one identity is offered roles across its cluster."""
    from backend.tools.gen_profiles import family_for_role
    fam = family_for_role(role_title)
    return [pid for pid in _pool_ids() if fam in _profile_families(pid)] if fam else []


def _untried(pool: list[str], apply_url: str) -> list[str]:
    """Of `pool`, the profiles that have NOT already been sent to this posting."""
    if not apply_url:
        return pool
    tried = set(_load_assignments().get(apply_url.split("?")[0], {}).get("tried", []))
    return [pid for pid in pool if pid not in tried]


def _matched_candidate(role_title: str, apply_url: str = "") -> Profile:
    """A family-matched pool candidate that has NOT already applied to THIS posting, so
    the same person never repeats a position (distinct candidates on one posting are
    fine). Round-robin within the family; if the family pool is empty OR every member
    already applied here, generate a NEW matched identity (still not a repeat)."""
    ids = _untried(_family_pool(role_title), apply_url)
    if ids:
        return get_profile(ids[next(_cand_rr) % len(ids)])
    pd, facts = generate(next(_ids), role_title=role_title, use_llm=False, country="kz")
    _upsert_profile(pd, facts)
    return Profile.from_dict(pd)


async def _online_roles_cached() -> list[dict]:
    """All strictly-REMOTE roles across ENABLED targets (family+company-tagged), cached ~10 min."""
    global _jobs_cache, _jobs_ts
    if _jobs_cache and (time.time() - _jobs_ts) < _JOBS_TTL:
        return _jobs_cache
    from backend.applier.batch import _online_roles
    _jobs_cache = await asyncio.get_running_loop().run_in_executor(None, _online_roles)
    _jobs_ts = time.time()
    return _jobs_cache


def _pick_roles(n: int, roles: list[dict]) -> list[dict]:
    """Up to `n` DISTINCT roles that still have a candidate who can apply WITHOUT
    repeating, round-robin so successive batches keep moving through the board. A
    position is skipped when every family-matched candidate has already applied to it
    (so no person repeats a posting), or when an optional PER_POSITION numeric cap is
    reached. Distinct candidates on one posting are allowed."""
    from backend.tools.gen_profiles import family_for_role
    led = _load_assignments()
    fams_of = {pid: set(_profile_families(pid)) for pid in _pool_ids()}
    out, seen = [], set()
    for _ in range(len(roles)):
        if len(out) >= n:
            break
        r = roles[next(_role_rr) % len(roles)]
        url = (r.get("apply_url") or r.get("applyUrl") or "").split("?")[0]
        if not url or url in seen:
            continue
        seen.add(url)
        entry = led.get(url, {})
        if PER_POSITION and len(entry.get("owners", [])) >= PER_POSITION:
            continue  # optional hard cap on distinct candidates reached
        fam = family_for_role(r.get("title", ""))
        pool = [pid for pid, fs in fams_of.items() if fam in fs]
        tried = set(entry.get("tried", []))
        if pool and all(pid in tried for pid in pool):
            continue  # every family candidate already applied here — would be a repeat
        out.append(r)
    return out


def _tok() -> str:
    return str(next(_ids))


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _dash_url(profile: str = "") -> str:
    # Admin operates from this Mac; the dashboard binds to localhost (no LAN
    # exposure, no auth needed). _lan_ip() stays available if you ever expose it.
    # Inbox/roles have their own helpers: _mail_url() / _roles_url().
    base = f"http://127.0.0.1:{DASH_PORT}/"
    return base + (f"?profile={profile}" if profile else "")


def _mail_url(profile: str = "") -> str:
    """Inbox dashboard (/mail), optionally scoped to one candidate's replies."""
    base = f"http://127.0.0.1:{DASH_PORT}/mail"
    return base + (f"?profile={profile}" if profile else "")


def _roles_url() -> str:
    """Positions table dashboard (/roles)."""
    return f"http://127.0.0.1:{DASH_PORT}/roles"


async def _salmon_cs_jobs() -> list[dict]:
    """CS/support roles that are strictly REMOTE (no hybrid) — the bot only applies
    to fully-online positions."""
    global _jobs_cache, _jobs_ts
    if _jobs_cache and (time.time() - _jobs_ts) < _JOBS_TTL:
        return _jobs_cache
    from backend.tools.roles_dashboard import _is_remote
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(ASHBY_BOARD)
        r.raise_for_status()
    jobs = [j for j in r.json().get("jobs", [])
            if any(k in j.get("title", "").lower() for k in _CS_KEYWORDS)
            and _is_remote(j)]
    jobs.sort(key=lambda j: j.get("title", ""))
    _jobs_cache, _jobs_ts = jobs, time.time()
    return jobs


async def _retry(fn, attempts: int = 3, delay: float = 1.5):
    """Retry a Telegram call across intermittent connection resets — KZ networks
    throttle api.telegram.org, so most calls pass but some reset mid-flight."""
    last = None
    for i in range(attempts):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001 — any network/telegram error is retryable
            last = e
            if i < attempts - 1:
                await asyncio.sleep(delay * (i + 1))
    raise last


def _authorized(chat_id: int) -> bool:
    want = str(settings.telegram_chat_id or "").strip()
    return want != "" and str(chat_id) == want


async def _guard(c: CallbackQuery) -> bool:
    """Reject any callback from a user other than the configured owner. Every
    callback handler calls this first — cb_open spawns a subprocess and the mark
    handlers mutate state, so none may run for an unauthorized sender."""
    if c.from_user and _authorized(c.from_user.id):
        return True
    await c.answer("Not authorized", show_alert=True)
    return False


def _enabled_companies() -> str:
    """Short 'Salmon, Vanta, 1Password' label of currently-active targets for the menu."""
    from backend.applier.batch import load_targets
    names = [t.get("company", t.get("key", "")) for t in load_targets()]
    if not names:
        return "нет"
    shown = ", ".join(names[:3])
    return shown + (f" +{len(names) - 3}" if len(names) > 3 else "")


def _menu() -> InlineKeyboardMarkup:
    po = "вкл ✅" if _PARSER_ONLY["on"] else "выкл"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новая заявка", callback_data="new"),
         InlineKeyboardButton(text=f"➕ {BATCH_SIZE} заявок", callback_data="new5")],
        [InlineKeyboardButton(text="📋 Очередь", callback_data="queue"),
         InlineKeyboardButton(text="🌐 Дашборд", callback_data="dash")],
        [InlineKeyboardButton(text="📥 Инбокс", callback_data="inbox"),
         InlineKeyboardButton(text="📊 Таблица", callback_data="table")],
        [InlineKeyboardButton(text=f"🎯 Компании: {_enabled_companies()}",
                              callback_data="targets")],
        [InlineKeyboardButton(text=f"📎 Только résumé-парсер: {po}",
                              callback_data="toggle_parser")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
    ])


def _targets_kb() -> InlineKeyboardMarkup:
    """Multi-select of target companies. ✅ = active (feeds the queue), ⬜ = off,
    ⏳ = present but not fetchable yet (Greenhouse/dead board — toggling it warns)."""
    from backend.applier.batch import load_targets, online_ats_supported
    supported = online_ats_supported()
    rows = []
    for t in load_targets(enabled_only=False):
        ready = t.get("ats") in supported
        mark = ("✅" if t.get("enabled") else "⬜") if ready else "⏳"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {t.get('company', t.get('key', ''))}",
            callback_data=f"tt:{t.get('key', '')}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _item_kb(token: str) -> InlineKeyboardMarkup:
    # The pre-filled browser auto-opens after prefill (AUTO_OPEN), so no manual "open"
    # button here — just mark the outcome or start another.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправлено", callback_data=f"sub:{token}"),
         InlineKeyboardButton(text="🗑 Убрать", callback_data=f"discard:{token}")],
        [InlineKeyboardButton(text="➕ Ещё одна", callback_data="new"),
         InlineKeyboardButton(text="🌐 Дашборд", callback_data="dash")],
    ])


def _status_kb(token: str) -> InlineKeyboardMarkup:
    """Per-item keyboard for BATCH cards: all tabs are opened together via 'open all',
    so each card only needs its own submit/discard mark."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправлено", callback_data=f"sub:{token}"),
        InlineKeyboardButton(text="🗑 Убрать", callback_data=f"discard:{token}")]])


bot = Bot(settings.telegram_bot_token)
dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: Message) -> None:
    if not _authorized(m.chat.id):
        await m.answer("Not authorized.")
        return
    await m.answer(
        "🐟 <b>JobFinder</b> — пульт заявок.\n\n"
        "Генерирую кандидата (🇰🇿 Казахстан), подгоняю резюме и полностью предзаполняю "
        "реальную форму Salmon (Ashby) — все поля, включая cover letter, локацию и "
        "согласия. Ты смотришь скриншот тут, жмёшь «Открыть в браузере» — форма "
        "открывается заполненной в анти-детект браузере, и ты сам жмёшь Submit.\n\n"
        "Движок Submit не нажимает никогда.",
        parse_mode="HTML", reply_markup=_menu())


@dp.message(Command("menu"))
async def menu_cmd(m: Message) -> None:
    if _authorized(m.chat.id):
        await m.answer("Меню:", reply_markup=_menu())


@dp.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery) -> None:
    if not await _guard(c):
        return
    await c.answer()
    await c.message.answer(
        "▸ <b>Новая заявка</b> — сгенерить кандидата + предзаполнить форму (скриншот).\n"
        "▸ <b>Открыть в браузере</b> — та же форма, заполненная, откроется на этом Маке; "
        "проверь и жми Submit, потом «Готово» чтобы закрыть окно.\n"
        "▸ <b>Отправлено/Убрать</b> — статус для дашборда.\n"
        "▸ <b>Дашборд</b> — веб-очередь со всеми заявками.\n\n"
        "⚠️ Профили — сгенерированные фейковые личности; реальный Submit создаёт "
        "ложную заявку. Для настоящих откликов заведи реальный профиль в /setup.",
        parse_mode="HTML")


@dp.callback_query(F.data == "dash")
async def cb_dash(c: CallbackQuery) -> None:
    if not await _guard(c):
        return
    await c.answer()
    await c.message.answer(f"🌐 Дашборд (открой на этом Маке): {_dash_url()}")


@dp.callback_query(F.data == "inbox")
async def cb_inbox(c: CallbackQuery) -> None:
    if not await _guard(c):
        return
    await c.answer()
    await c.message.answer(f"📥 Инбокс кандидатов (открой на этом Маке): {_mail_url()}")


@dp.callback_query(F.data == "table")
async def cb_table(c: CallbackQuery) -> None:
    if not await _guard(c):
        return
    await c.answer()
    await c.message.answer(f"📊 Таблица позиций (открой на этом Маке): {_roles_url()}")


@dp.callback_query(F.data == "toggle_parser")
async def cb_toggle_parser(c: CallbackQuery) -> None:
    """Flip résumé-parser-only (anti-spam) mode on/off for subsequent pre-fills."""
    if not await _guard(c):
        return
    _PARSER_ONLY["on"] = not _PARSER_ONLY["on"]
    if _PARSER_ONLY["on"]:
        await c.answer("Режим «только résumé-парсер» включён: заявки будут только "
                       "подгружать résumé в парсер ATS; остальное заполняешь сам.",
                       show_alert=True)
    else:
        await c.answer("Режим «только résumé-парсер» выключен: полное авто-заполнение.")
    try:  # reflect the new state in the menu button label
        await c.message.edit_reply_markup(reply_markup=_menu())
    except Exception:
        pass


@dp.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery) -> None:
    """'⬅️ Назад' from a submenu: restore the main menu on the same message."""
    if not await _guard(c):
        return
    await c.answer()
    try:
        await c.message.edit_text("Меню:", reply_markup=_menu())
    except Exception:
        await c.message.answer("Меню:", reply_markup=_menu())


@dp.callback_query(F.data == "targets")
async def cb_targets(c: CallbackQuery) -> None:
    """Open the company multi-select. ✅ active · ⬜ off · ⏳ not fetchable yet."""
    if not await _guard(c):
        return
    await c.answer()
    txt = ("🎯 <b>Компании для заявок</b>\n"
           "Нажми, чтобы вкл/выкл. Заявки (➕) распределяются round-robin по "
           "<b>включённым</b>.\n⏳ — борд пока не поддержан (Greenhouse — Фаза 2 / Deel мёртв).")
    try:
        await c.message.edit_text(txt, parse_mode="HTML", reply_markup=_targets_kb())
    except Exception:
        await c.message.answer(txt, parse_mode="HTML", reply_markup=_targets_kb())


@dp.callback_query(F.data.startswith("tt:"))
async def cb_toggle_target(c: CallbackQuery) -> None:
    """Toggle one target's enabled flag, persist to targets.json, refresh the role cache."""
    if not await _guard(c):
        return
    global _jobs_ts
    from backend.applier.batch import load_targets, online_ats_supported, set_target_enabled
    key = c.data.split(":", 1)[1]
    reg = {t.get("key"): t for t in load_targets(enabled_only=False)}
    tgt = reg.get(key)
    if tgt is None:
        await c.answer("Неизвестный таргет", show_alert=True)
        return
    if tgt.get("ats") not in online_ats_supported():
        await c.answer(f"{tgt.get('company', key)} пока не поддержан "
                       f"(ats={tgt.get('ats')}). Фаза 2 / нужен рабочий борд.",
                       show_alert=True)
        return
    now_on = not tgt.get("enabled", False)
    set_target_enabled(key, now_on)
    _jobs_ts = 0.0  # invalidate the cached role list so the next ➕ reflects the change
    await c.answer(f"{tgt.get('company', key)}: {'включён ✅' if now_on else 'выключен ⬜'}")
    try:
        await c.message.edit_reply_markup(reply_markup=_targets_kb())
    except Exception:
        pass


def _resume_filename(name: str, title: str) -> str:
    """Human-readable, collision-resistant résumé filename for Downloads AND the
    Telegram document — mirrors the extension's `Resume - <company> - <role>.pdf`
    scheme (here `Resume - <candidate> - <role>.pdf`, since the bot generates a new
    candidate per application) so both surfaces drop consistently named files instead
    of a single `resume_<profile>.pdf` that overwrites across jobs."""
    raw = " - ".join(p for p in ("Resume", (name or "").strip(), (title or "").strip()) if p)
    safe = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]+', " ", raw)).strip()[:120]
    return (safe or "resume") + ".pdf"


def _fmt_fields(items: list[str], cap: int = 20) -> str:
    shown = [f"• {html.escape((x or '')[:60])}" for x in items[:cap]]
    if len(items) > cap:
        shown.append(f"…и ещё {len(items) - cap}")
    return "\n".join(shown)


def _leftovers_text(rep: dict) -> str | None:
    """Human checklist of fields still empty after pre-fill — required AND optional —
    so the reviewer knows exactly what to type before Submit. Built from the DOM
    completeness scan (report['completeness']). Returns None if no scan data."""
    comp = rep.get("completeness") or {}
    if not comp:
        return None
    er = comp.get("empty_required", [])
    eo = comp.get("empty_optional", [])
    if not er and not eo:
        return "✅ <b>Все поля заполнены</b> — проверь и жми Submit."
    parts = ["📝 <b>Осталось вставить вручную</b>"]
    parts.append(f"\n<b>Обязательные ({len(er)}):</b>\n{_fmt_fields(er)}" if er
                 else "\n<b>Обязательные:</b> нет ✅")
    if eo:
        parts.append(f"\n<b>Необязательные ({len(eo)}):</b>\n{_fmt_fields(eo)}")
    return "\n".join(parts)


# Field labels that are click-targets, never paste values. ("start typing…" is NOT
# here — on Ashby that placeholder is the Location autocomplete, handled as a value.)
_NOT_PASTE = ("select...", "please select", "choose",
              "i agree", "i consent", "i accept")
# Location-field detector. \b-anchored so it matches "Location"/"City"/"Current
# location" but NOT "reLOCATION" or "capaCITY" (substring collisions).
_LOC_RE = re.compile(r"(?i)\b(location|city|town|region|current location|based in|"
                     r"where are you|where do you live)\b")


def _paste_lines(rep: dict, profile: Profile, job: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """For each still-empty field, resolve the VALUE the human should paste (so they
    copy instead of retype). Identity from the profile / per-application email, factual
    open fields (hear-about, notice, salary) from the deterministic answer engine,
    telegram fixed @none. The cover letter is sent as its own message; résumé is the
    attached file. Choice/consent options and typeahead placeholders aren't paste
    targets — they come back as `picks` (clicks the human makes on the form)."""
    comp = rep.get("completeness") or {}
    labels = list(comp.get("empty_required") or []) + list(comp.get("empty_optional") or [])
    form = profile.to_form_dict()
    try:
        facts = load_facts(profile.id)
    except Exception:
        facts = {}
    app_email = rep.get("application_email") or form.get("email") or ""
    loc = (form.get("location")
           or ", ".join(p for p in (form.get("city"), form.get("country")) if p)
           or None)
    paste: list[tuple[str, str]] = []
    picks: list[str] = []
    seen: set[str] = set()
    for raw in labels:
        label = (raw or "").strip()
        key = label.lower()
        if not label or key in seen:
            continue
        seen.add(key)
        if "resume" in key or key == "cv" or "cover letter" in key:
            continue  # résumé is the attached file; cover letter is its own message
        disp, val = label, None  # disp = the label shown next to the value
        if "telegram" in key:
            val = "@none"
        elif ("full name" in key or key == "name" or "your name" in key
              or "legal name" in key or "applicant name" in key or "candidate name" in key):
            val = form.get("full_name") or None
        elif "email" in key or "e-mail" in key:
            val = app_email or None
        elif "phone" in key or "mobile" in key:
            val = form.get("phone") or None
        elif "linkedin" in key:
            val = form.get("linkedin_url") or "N/A"
        elif "country" in key:
            val = form.get("country") or None
        elif _LOC_RE.search(key):  # \b-anchored: skips "relocation"/"capacity"
            val = loc
        elif key.startswith("start typing"):
            # Ashby's Location autocomplete exposes only this placeholder as its label —
            # it's the location field: relabel it clearly and hand over the value.
            val = loc
            if val:
                disp = "Location"
        elif any(p in key for p in _NOT_PASTE):
            val = None  # click target, not a paste value
        else:
            try:  # LLM-free factual resolver (hear-about, notice period, salary, …)
                det = deterministic_answers([label], form, job, facts)
                if det:
                    val = (strip_review(next(iter(det.values())))[0] or "").strip() or None
            except Exception:
                val = None
        if val:
            paste.append((disp, val))
        else:
            picks.append(label)
    return paste, picks


def _values_text(rep: dict, profile: Profile, job: dict) -> str | None:
    """Copy-paste-ready values for the still-empty fields — parser-only mode types
    nothing into the form, so the human pastes these. Each value sits in <code> so
    Telegram makes it tap-to-copy. Returns None when there's no completeness scan
    (caller falls back to the label-only checklist)."""
    comp = rep.get("completeness") or {}
    if not comp:
        return None
    if not (comp.get("empty_required") or comp.get("empty_optional")):
        return "✅ <b>Все поля заполнены</b> — проверь и жми Submit."
    paste, picks = _paste_lines(rep, profile, job)
    parts = ["📋 <b>Значения для вставки</b> — копируй и вставляй:"]
    for label, val in paste:
        parts.append(f"\n<b>{html.escape(label[:60])}:</b>\n<code>{html.escape(val)}</code>")
    if not paste:
        parts.append("\n<i>(текстовые поля, похоже, уже подтянул résumé-парсер)</i>")
    if picks:
        parts.append("\n\n📝 <b>Заполнить/отметить вручную</b> "
                     "(галочки, согласия, свободные ответы):\n" + _fmt_fields(picks))
    return "\n".join(parts)


def _base_fit(profile: Profile, job: dict) -> float:
    """Cheap, LLM-free honest fit of the candidate's BASE résumé vs the JD — the same
    metric the runner's gate uses. Lets the batch skip sub-threshold candidates WITHOUT
    paying for a tailor+prefill on each reject. Fails OPEN (returns the threshold) so a
    scoring gap never silently drops every candidate."""
    try:
        from backend.services.tailor.ats_score import ats_score as _ats
        r = dict(profile.resume or {})
        r["_jd_title"] = job.get("title", "")
        return float(_ats(job.get("description", ""), r).get("score", 0.0))
    except Exception:
        return float(MATCH_GATE_MIN)  # fail-open: don't over-skip on a scoring bug


async def _prefill_and_send(dest: Message, role: dict, kb_mode: str = "full") -> str | None:
    """Prefill one (family-matched candidate, role) headlessly and send the screenshot
    + résumé to the chat. Returns the SESSIONS token, or None if no candidate could clear
    the gate after MAX_CANDIDATE_TRIES or an error occurred. kb_mode: "full" (single:
    submit+discard) or "status" (batch item: submit/discard only; opened via 'open all')."""
    title = role.get("title", "")
    company = role.get("company", "Salmon")
    apply_url = role.get("apply_url") or role.get("applyUrl")
    job = {"title": title, "company": company,
           "description": (role.get("description") or role.get("descriptionPlain") or "")[:8000],
           "apply_url": apply_url}
    # Find a candidate that clears the gate (fit≥MATCH_GATE_MIN & tailored ATS≥ATS_GATE_MIN)
    # so a batch slot is NEVER lost to one low score. The cheap base-fit pre-screen skips
    # weak candidates without paying for a tailor+prefill; only screened-in ones prefill.
    profile = None
    rep: dict | None = None
    for _ in range(MAX_CANDIDATE_TRIES):
        cand = _matched_candidate(title, apply_url or "")
        # Eligibility: nationality rule (KZ personas -> Salmon only; US/CA elsewhere) and
        # the company's submitted-application cap. Either fail -> mark tried so the next
        # pick advances, and skip (never prepare an ineligible / over-limit application).
        if not country_allowed(cand.id, company) or at_application_cap(cand.id, company):
            if apply_url:
                try:
                    _record_assignment(apply_url, cand.id, ok=False)
                except Exception:
                    pass
            continue
        if _base_fit(cand, job) < MATCH_GATE_MIN:  # sub-threshold — skip cheaply, mark tried
            if apply_url:
                try:
                    _record_assignment(apply_url, cand.id, ok=False)
                except Exception:
                    pass
            continue
        r = await prefill_application(job, cand, headless=True, use_ai=True,
                                      draft_answers=True, use_variants=False,
                                      resume_parser_only=_PARSER_ONLY["on"])
        # Ledger: passed → owns the slot; gated (e.g. tailored ATS below bar) → tried only.
        if apply_url:
            try:
                _record_assignment(apply_url, cand.id, ok=not r.get("gated_out"))
            except Exception:
                pass
        if r.get("gated_out"):
            continue  # try another candidate rather than skip the slot
        profile, rep = cand, r
        break
    if rep is None or profile is None:
        await _retry(lambda: dest.answer(
            f"🚫 <b>{title}</b>: не нашёл кандидата ≥ порога "
            f"(fit {int(MATCH_GATE_MIN)} / ATS {int(ATS_GATE_MIN)}%) за "
            f"{MAX_CANDIDATE_TRIES} попыток — пропущено.", parse_mode="HTML"))
        return None
    jid = Path(rep["screenshot"]).parent.name
    token = _tok()
    SESSIONS[token] = {"profile": profile.id, "url": apply_url, "title": title,
                       "company": company, "jid": jid, "name": profile.full_name,
                       "description": job["description"], "resume_pdf": rep.get("resume_pdf", ""),
                       "reply_addr": rep.get("application_email", "")}
    comp = rep.get("completeness", {})
    er = comp.get("empty_required", [])
    eo = comp.get("empty_optional", [])
    status_line = ("✅ <b>Готово к отправке</b>" if comp.get("ready_to_submit")
                   else f"⚠️ <b>Осталось обязательных: {len(er)}</b>"
                        + (f" · необяз.: {len(eo)}" if eo else ""))
    caption = (
        f"🐟 <b>{profile.full_name}</b> · {profile.country}\n"
        f"↳ <b>{html.escape(company)}</b> · {title}\n\n{status_line}\n\n"
        f"Заполнено: <b>{rep.get('filled')}</b> · fit <b>{rep.get('fit_score')}</b> · "
        f"ATS <b>{rep.get('ats_score')}%</b> · на ревью: {len(rep.get('review_items', []))}\n\n"
        f"📥 <a href=\"{_mail_url(profile.id)}\">Инбокс кандидата</a> · "
        f"📊 <a href=\"{_roles_url()}\">Таблица</a> <i>(на Маке)</i>")
    kb = _status_kb(token) if kb_mode == "status" else _item_kb(token)
    try:
        await _retry(lambda: dest.answer_photo(FSInputFile(rep["screenshot"]),
                     caption=caption, parse_mode="HTML", reply_markup=kb))
    except Exception:
        await _retry(lambda: dest.answer(caption, parse_mode="HTML", reply_markup=kb))
    # Explicit checklist of what the parser/pre-fill did NOT fill — required AND
    # optional — so you know exactly what to type before hitting Submit.
    # Copy-paste-ready VALUES for the empty fields (parser-only mode fills nothing in
    # the form), falling back to the label-only checklist if there's no completeness scan.
    values = _values_text(rep, profile, job) or _leftovers_text(rep)
    if values:
        await _retry(lambda: dest.answer(values, parse_mode="HTML"))
    # Cover letter is long — its own tap-to-copy message, only when the form has the field.
    cover = (rep.get("cover_letter") or "").strip()
    _comp = rep.get("completeness") or {}
    _labels_l = " ".join((_comp.get("empty_required") or [])
                         + (_comp.get("empty_optional") or [])).lower()
    if cover and "cover letter" in _labels_l:
        await _retry(lambda: dest.answer(
            "✍️ <b>Cover Letter</b> — вставь целиком:\n<code>"
            + html.escape(cover[:3500]) + "</code>", parse_mode="HTML"))
    resume_pdf = rep.get("resume_pdf")
    if resume_pdf and Path(resume_pdf).exists():
        fname = _resume_filename(profile.full_name, title)
        try:
            shutil.copyfile(resume_pdf, Path.home() / "Downloads" / fname)
        except Exception:
            pass
        try:
            await _retry(lambda: dest.answer_document(
                FSInputFile(resume_pdf, filename=fname),
                caption=f"📄 <b>{profile.full_name}</b> · {title}", parse_mode="HTML"))
        except Exception:
            pass
    return token


@dp.callback_query(F.data == "new")
async def cb_new(c: CallbackQuery) -> None:
    if not await _guard(c):
        return
    await c.answer()
    status = await c.message.answer("⏳ Генерирую кандидата и заполняю форму… (~40 c)")
    try:
        roles = _pick_roles(1, await _online_roles_cached())
        if not roles:
            await _retry(lambda: status.edit_text("Нет доступных online-вакансий."))
            return
        token = await _prefill_and_send(c.message, roles[0])
        try:
            await _retry(lambda: status.delete())
        except Exception:
            pass
        if token and AUTO_OPEN:  # open the pre-filled browser on the Mac automatically
            await _open_single(c.message, token)
    except Exception as exc:
        try:
            await _retry(lambda: status.edit_text(f"⚠️ Ошибка: {exc}"))
        except Exception:
            pass


@dp.callback_query(F.data == "new5")
async def cb_new5(c: CallbackQuery) -> None:
    """Prepare a batch of BATCH_SIZE applications and offer to open them as N tabs."""
    if not await _guard(c):
        return
    await c.answer()
    status = await c.message.answer(
        f"⏳ Готовлю {BATCH_SIZE} заявок (по одной, ~40 c каждая)…")
    try:
        roles = _pick_roles(BATCH_SIZE, await _online_roles_cached())
        tokens: list[str] = []
        for i, role in enumerate(roles, 1):
            try:
                await _retry(lambda: status.edit_text(
                    f"⏳ Заявка {i}/{len(roles)}: {role.get('title','')[:40]}…"))
            except Exception:
                pass
            tok = await _prefill_and_send(c.message, role, kb_mode="status")
            if tok:
                tokens.append(tok)
        try:
            await _retry(lambda: status.delete())
        except Exception:
            pass
        if not tokens:
            await c.message.answer(
                "Ни одна заявка не прошла гейт даже после подбора. Попробуй ещё раз.")
            return
        bid = _tok()
        BATCHES[bid] = tokens
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Ещё пачка", callback_data="new5"),
             InlineKeyboardButton(text="🌐 Дашборд", callback_data="dash")]])
        await c.message.answer(
            f"✅ Готово <b>{len(tokens)}</b> заявок — открываю все вкладки в браузере на "
            "Маке. Проверь каждую, жми Submit сам, потом «Готово» и отметь статусы.",
            parse_mode="HTML", reply_markup=kb)
        if AUTO_OPEN:  # open all N tabs on the Mac automatically
            await _open_batch(c.message, bid)
    except Exception as exc:
        try:
            await _retry(lambda: status.edit_text(f"⚠️ Ошибка: {exc}"))
        except Exception:
            pass


async def _open_batch(dest: Message, bid: str) -> None:
    """Spawn ONE visible browser on the Mac with N pre-filled tabs (open_batch), held
    open until the human closes it. Shared by auto-open (after a batch prefill) and the
    manual button. No-op with a note if the batch session expired / it's already open."""
    ctxs = [SESSIONS[t] for t in BATCHES.get(bid, []) if t in SESSIONS]
    if not ctxs:
        await _retry(lambda: dest.answer("Сессия пачки истекла — собери заново."))
        return
    key = f"batch:{bid}"
    if OPEN_PROCS.get(key) and OPEN_PROCS[key].returncode is None:
        return  # already open
    import json
    import os
    spec = [{"profile": x["profile"], "url": x["url"], "title": x["title"],
             "company": x["company"], "description": x.get("description", ""),
             "resume_pdf": x.get("resume_pdf", ""), "reply_addr": x.get("reply_addr", "")}
            for x in ctxs]
    specfile = PREFILL_ROOT / f"_batch_{bid}.json"
    specfile.write_text(json.dumps(spec), encoding="utf-8")
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = "."
    if _PARSER_ONLY["on"]:  # keep the tabs résumé-parser-only too (anti-spam)
        child_env["RESUME_PARSER_ONLY"] = "1"
    proc = await asyncio.create_subprocess_exec(
        ".venv/bin/python", "-m", "backend.tools.open_batch", "--spec", str(specfile), "--draft",
        cwd=str(PROJECT_ROOT), env=child_env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    OPEN_PROCS[key] = proc
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Готово (закрыть все вкладки)",
                             callback_data=f"close:{key}")]])
    await _retry(lambda: dest.answer(
        f"🖥 Открываю <b>{len(spec)}</b> вкладок — все заполнены. Проверь каждую, жми "
        "Submit сам, потом «Готово». Затем отметь статусы под карточками.",
        parse_mode="HTML", reply_markup=kb))


@dp.callback_query(F.data.startswith("openall:"))
async def cb_open_all(c: CallbackQuery) -> None:
    if not await _guard(c):
        return
    await c.answer("Открываю вкладки на Маке…")
    await _open_batch(c.message, c.data.split(":", 1)[1])


async def _open_single(dest: Message, token: str) -> None:
    """Spawn a visible, pre-filled browser for ONE application on the Mac
    (open_for_submit), held open until the human closes it. Shared by auto-open (after
    prefill) and the manual reopen path. No-op with a note if the session expired /
    it's already open."""
    ctx = SESSIONS.get(token)
    if not ctx:
        await _retry(lambda: dest.answer("Сессия истекла — сгенерь заново."))
        return
    if OPEN_PROCS.get(token) and OPEN_PROCS[token].returncode is None:
        return  # already open
    import os
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = "."  # HOME/PATH/etc inherited so Playwright finds Chromium
    if _PARSER_ONLY["on"]:  # keep the form résumé-parser-only too (anti-spam)
        child_env["RESUME_PARSER_ONLY"] = "1"
    proc = await asyncio.create_subprocess_exec(
        ".venv/bin/python", "-m", "backend.tools.open_for_submit",
        "--profile", ctx["profile"], "--url", ctx["url"],
        "--title", ctx["title"], "--company", ctx["company"],
        "--description", (ctx.get("description") or "")[:8000], "--ai", "--draft",
        cwd=str(PROJECT_ROOT), env=child_env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    OPEN_PROCS[token] = proc
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Готово (закрыть браузер)",
                             callback_data=f"close:{token}")]])
    await _retry(lambda: dest.answer(
        f"🖥 Открываю форму «{ctx['title']}» для <b>{ctx['name']}</b> на Маке — "
        "заполненную. Проверь, при желании жми <b>Submit</b>, потом «Готово».",
        parse_mode="HTML", reply_markup=kb))


@dp.callback_query(F.data.startswith("open:"))
async def cb_open(c: CallbackQuery) -> None:
    if not await _guard(c):
        return
    await c.answer("Открываю браузер на Маке…")
    await _open_single(c.message, c.data.split(":", 1)[1])


@dp.callback_query(F.data.startswith("close:"))
async def cb_close(c: CallbackQuery) -> None:
    if not await _guard(c):
        return
    token = c.data.split(":", 1)[1]
    await c.answer("Закрываю…")
    proc = OPEN_PROCS.pop(token, None)
    if proc and proc.returncode is None:
        try:
            proc.stdin.write(b"\n")
            await proc.stdin.drain()
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=8)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
    await c.message.answer("Браузер закрыт. Не забудь отметить статус (✅ Отправлено).")


@dp.callback_query(F.data.startswith("sub:"))
async def cb_sub(c: CallbackQuery) -> None:
    if not await _guard(c):
        return
    token = c.data.split(":", 1)[1]
    ctx = SESSIONS.get(token)
    await c.answer()
    if not ctx:
        await c.message.answer("Сессия истекла.")
        return
    status_store.mark(ctx["profile"], ctx["jid"], "submitted")
    await c.message.answer(f"✅ Отмечено как <b>submitted</b>: {ctx['name']} → "
                           f"{ctx['title']}", parse_mode="HTML")


@dp.callback_query(F.data.startswith("discard:"))
async def cb_discard(c: CallbackQuery) -> None:
    if not await _guard(c):
        return
    token = c.data.split(":", 1)[1]
    ctx = SESSIONS.pop(token, None)
    await c.answer("Убрано")
    if ctx:
        status_store.mark(ctx["profile"], ctx["jid"], "rejected")
        await c.message.answer(f"🗑 Убрано: {ctx['name']} → {ctx['title']}")


@dp.callback_query(F.data == "queue")
async def cb_queue(c: CallbackQuery) -> None:
    if not await _guard(c):
        return
    await c.answer()
    items = []
    for rep_path in sorted(PREFILL_ROOT.glob("*/*/report.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:8]:
        import json
        try:
            rep = json.loads(rep_path.read_text())
        except Exception:
            continue
        profile = rep_path.parents[1].name
        jid = rep_path.parent.name
        st = status_store.load(profile).get(jid, {}).get("status", "pending")
        token = _tok()
        SESSIONS[token] = {"profile": profile, "url": rep.get("apply_url", ""),
                           "title": rep.get("job_title", ""),
                           "company": rep.get("company", "Salmon"),
                           "jid": jid, "name": profile}
        items.append((token, rep, st))
    if not items:
        await c.message.answer("Очередь пуста. Нажми «➕ Новая заявка».")
        return
    for token, rep, st in items:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🖥 Открыть", callback_data=f"open:{token}"),
            InlineKeyboardButton(text="✅", callback_data=f"sub:{token}"),
            InlineKeyboardButton(text="🗑", callback_data=f"discard:{token}")]])
        await c.message.answer(
            f"• <b>{rep.get('company')}</b> — {rep.get('job_title')}\n"
            f"  заполнено {rep.get('filled')} · статус: {st}",
            parse_mode="HTML", reply_markup=kb)


async def _amain() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN empty in backend/.env")
    if not settings.telegram_chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID empty — run: python -m backend.tools.tg_resolve")
    me = await bot.get_me()
    print(f"Bot @{me.username} polling. Authorized chat: {settings.telegram_chat_id}")
    await bot.send_message(settings.telegram_chat_id,
                           "🐟 JobFinder бот запущен. Жми /start.")
    await dp.start_polling(bot)


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
