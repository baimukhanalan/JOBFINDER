"""Server-rendered HTML for the responsible cabinet (a SEPARATE surface from the
operator dashboard). Deliberately does NOT reuse `mailcrm_ui._page`/`_sidebar`/`_NAV`
(those carry the operator nav); it has its own minimal shell. It borrows only
`mailcrm_ui._CSS`/`_FONTS` for base styling and `mailcrm_ui.render_rows` (with
`show_sobes=False`) so the scoped inbox rows look native without the operator «Собес»
control.

All text is neutral Russian — no stack names, no decorative emoji. Times are shown in the
responsible's OWN timezone (auto-detected from their device; see routes_cabinet POST /cabinet/tz).
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from backend.interviews import avail_editor, slots
from backend.tools import mailcrm_ui

_WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг",
             "Пятница", "Суббота", "Воскресенье"]

# Cabinet-specific styling layered on top of the shared base CSS.
_CAB_CSS = """
main{max-width:900px;margin:0 auto;padding:22px 18px;}
@media(max-width:600px){main{padding:14px 12px;}}
.cab-top{display:flex;align-items:center;gap:18px;flex-wrap:wrap;margin-bottom:22px;
  padding-bottom:14px;border-bottom:1px solid var(--line);}
.cab-top .brand{width:36px;height:36px;border-radius:9px;background:var(--accent);overflow:hidden;padding:0;}
.cab-top .who{font-weight:700;color:var(--ink);font-size:15px;}
.cab-nav{display:flex;gap:8px;margin-left:auto;flex-wrap:wrap;}
.cab-nav a{padding:8px 14px;border-radius:var(--r-full);font-weight:600;font-size:13px;
  color:var(--ink-soft);border:1px solid var(--line-strong);background:var(--panel);}
.cab-nav a:hover{background:var(--panel-2);color:var(--ink);text-decoration:none;}
.cab-nav a.active{background:var(--accent-soft);color:var(--accent-deep);border-color:var(--accent);}
.cab-badge{display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:18px;
  padding:0 5px;margin-left:6px;border-radius:9px;background:var(--accent);color:#fff;font-size:11px;
  font-weight:700;font-family:var(--ff-mono);vertical-align:middle;}
.cab-nav a.active .cab-badge{background:var(--accent-deep);}
/* the scoped candidate inbox reuses the operator `cg-` card CSS; only the search wrapper is local */
.cab-cand-tools{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:0 0 14px;}
.cab-cand-search{margin:0;flex:1 1 240px;max-width:360px;}
.cab-cand-search input[type=search]{width:100%;}
.cg-load{padding:14px 16px;color:var(--ink-mute);font-size:13px;}
/* personal calendar: day-grouped agenda */
.cal-day{margin:0 0 18px;}
.cal-day-h{font-weight:700;color:var(--ink);font-size:14px;margin:0 0 8px;padding-bottom:6px;
  border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:8px;}
.cal-day-h .cal-dow{color:var(--ink-mute);font-weight:600;font-size:12px;}
.cal-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px;}
.cal-iv{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;}
.cal-iv.past{opacity:.6;}
.cal-iv-link{display:flex;gap:12px;align-items:flex-start;padding:12px 14px;color:inherit;text-decoration:none;}
.cal-iv-link:hover{background:var(--panel-2);text-decoration:none;}
.cal-time{flex:0 0 auto;font-weight:800;color:var(--accent-deep);font-size:15px;font-variant-numeric:tabular-nums;min-width:54px;}
.cal-time.none{color:var(--ink-mute);font-weight:600;font-size:12px;min-width:54px;}
.cal-mid{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;gap:3px;}
.cal-co{font-weight:700;color:var(--ink);font-size:14px;}
.cal-sub{color:var(--ink-soft);font-size:12.5px;word-break:break-word;}
.cal-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px;}
.cal-chip{display:inline-flex;align-items:center;gap:4px;font-size:12px;font-weight:600;padding:3px 9px;
  border-radius:var(--r-full);background:var(--panel-2);color:var(--ink-soft);border:1px solid var(--line);}
.cal-chip.sal{color:#166534;border-color:#bbf7d0;background:#f0fdf4;}
.cal-chip.book{color:var(--accent-deep);border-color:var(--accent);background:var(--accent-soft);}
.cal-open{flex:0 0 auto;color:var(--accent);font-weight:600;font-size:12px;align-self:center;}
.cal-book{padding:0 14px 12px 80px;}
@media(max-width:560px){.cal-book{padding-left:14px;}}
h1.cab-h{font-size:22px;font-weight:600;letter-spacing:-.02em;margin:0 0 16px;}
.note{background:var(--accent-soft);color:var(--accent-deep);border-radius:var(--r-sm);
  padding:9px 14px;margin-bottom:16px;font-weight:600;font-size:13px;}
.err{background:#fce8e6;color:var(--danger);border-radius:var(--r-sm);padding:9px 14px;
  margin-bottom:16px;font-weight:600;font-size:13px;}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:18px 20px;}
.login-wrap{max-width:360px;margin:8vh auto 0;}
.login-wrap .brand{width:56px;height:56px;border-radius:14px;background:var(--accent);overflow:hidden;padding:0;margin:0 auto 18px;}
.login-wrap .card{padding:24px;}
.login-wrap label{margin-top:14px;}
.login-wrap input{width:100%;}
.login-wrap button{width:100%;margin-top:18px;}
.iv-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px;}
.iv-list li{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
  padding:0;overflow:hidden;}
/* the whole card is one tap target: the <a> fills the card incl. its padding */
.iv-card-link{display:flex;flex-direction:column;gap:4px;padding:14px 16px;color:inherit;
  text-decoration:none;min-height:44px;box-sizing:border-box;}
.iv-card-link:hover{background:var(--panel-2);text-decoration:none;}
.iv-open{color:var(--accent);font-weight:600;font-size:13px;margin-top:2px;}
.iv-list .iv-when{font-weight:700;color:var(--ink);font-size:14px;}
.iv-list .iv-meta{color:var(--ink-soft);font-size:13px;}
.iv-list .iv-meta b{color:var(--ink);}
.av-grid{display:flex;flex-direction:column;gap:10px;max-width:560px;}
.av-day{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;background:var(--panel);
  border:1px solid var(--line);border-radius:var(--r-sm);padding:12px 14px;}
.av-day .dow{flex:0 0 auto;min-width:96px;font-weight:600;color:var(--ink);}
.av-day .tog{display:flex;align-items:center;gap:7px;color:var(--ink-soft);font-weight:600;font-size:13px;cursor:pointer;user-select:none;}
.av-day .tog input{width:17px;height:17px;flex:0 0 auto;}
.av-day .times{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--ink-mute);font-size:13px;}
.av-day .times input[type=time]{padding:9px 10px;}
.av-day.off{opacity:.55;}
/* phones: the day label + toggle on one line, the two time inputs full-width below */
@media(max-width:560px){
  .av-day{gap:10px;}
  .av-day .dow{flex:1 1 auto;min-width:0;font-size:15px;}
  .av-day .times{margin-left:0;flex:1 1 100%;gap:8px;}
  .av-day .times input[type=time]{flex:1;min-width:0;text-align:center;}
}
.empty{color:var(--ink-mute);padding:18px 0;}
.tg-card{margin-bottom:18px;}
.tg-h{font-weight:700;margin-bottom:6px;}
.tg-sub{color:var(--ink-soft);font-size:13px;line-height:1.5;margin-bottom:12px;}
.tg-act{display:flex;align-items:center;flex-wrap:wrap;gap:8px;}
/* read-only avatar wrapper in the reused mail rows: not a select toggle */
.msel-ro{cursor:default;}
.msel-ro:hover::after{display:none;}
.tcard{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
  padding:16px 18px;margin-bottom:12px;}
.tcard .tmeta{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:baseline;
  margin-bottom:10px;padding-bottom:9px;border-bottom:1px solid var(--line);}
.tcard .tmeta b{color:var(--ink);font-size:14px;}
.tcard .tmeta .addr{color:var(--ink-mute);font-size:12px;font-family:var(--ff-mono);}
.tcard .tmeta .date{color:var(--ink-mute);font-size:12px;margin-left:auto;}
.tcard .body{white-space:pre-wrap;word-break:break-word;color:var(--ink);font-size:13.5px;line-height:1.6;}
.back-link{display:inline-flex;align-items:center;padding:10px 8px;margin:0 0 8px -8px;
  color:var(--ink-soft);font-weight:600;min-height:40px;box-sizing:border-box;}
.back-link:hover{color:var(--ink);text-decoration:none;}
.tsubj{font-size:20px;font-weight:600;letter-spacing:-.02em;margin:0 0 4px;}
.tbox{color:var(--ink-mute);font-size:12px;margin-bottom:16px;}
.cab-reply{margin-top:16px;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
  padding:16px 18px;display:flex;flex-direction:column;gap:10px;}
.cab-reply-h{font-weight:700;color:var(--ink);font-size:14px;}
.cab-reply textarea{width:100%;resize:vertical;min-height:96px;font:inherit;padding:10px 12px;
  border:1px solid var(--line-strong);border-radius:var(--r-sm);}
.cab-reply button{align-self:flex-start;}
"""


def _doc(body: str, title: str = "Кабинет") -> str:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        + mailcrm_ui._HEAD_PWA +
        f"<title>{escape(title)}</title>" + mailcrm_ui._FONTS +
        f"<style>{mailcrm_ui._CSS}{_CAB_CSS}</style></head>"
        f"<body><main>{body}</main>" + mailcrm_ui._SW_REG + "</body></html>")


# ---- admin read-through (?as=<id>) helpers ----------------------------------------
# When an ADMIN opens a user's cabinet via /cabinet?as=<id> (routes_cabinet._acting_cabinet),
# every in-cabinet link + form must carry the same ?as so navigation stays in that user's
# context. A normal self-view passes as_id=None → these are all no-ops.
def _cab_href(path: str, as_id=None) -> str:
    if not as_id:
        return path
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}as={as_id}"


def _as_field(as_id=None) -> str:
    """A hidden `as` form field so an admin's POST (save availability / reply) targets the
    user being viewed, not the admin. Empty for a self-view."""
    return f'<input type="hidden" name="as" value="{escape(str(as_id))}">' if as_id else ""


def _asview_banner(responsible: dict, as_id=None) -> str:
    if not as_id:
        return ""
    who = escape(responsible.get("name") or responsible.get("login") or "")
    return ('<div class="cab-asview" style="background:#fef3c7;border:1px solid #f59e0b;'
            'border-radius:10px;padding:8px 14px;margin:0 0 14px;font-size:13px;font-weight:600;'
            'color:#92400e;display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
            f'<span>Просмотр кабинета: {who} (режим администратора)</span>'
            '<a class="hbtn" href="/users" style="margin-left:auto">← К пользователям</a></div>')


def _topbar(responsible: dict, active: str, as_id=None, iv_count=None) -> str:
    name = escape(responsible.get("name") or responsible.get("login") or "")
    # a manager attends interviews here too; give them a way back to their portal (multi-role
    # aware — an admin+manager or manager+employee still gets the link)
    _roles = responsible.get("roles") or ([responsible.get("role")] if responsible.get("role") else [])
    portal = (f'<a href="{_cab_href("/manage", as_id)}">← Портал</a>' if "manager" in _roles else "")
    # a small count badge on «Собесы» so the interviewer sees at a glance how many upcoming
    # собеседования await (only rendered when a positive count is passed by the page).
    badge = (f'<span class="cab-badge">{iv_count}</span>' if iv_count else "")
    nav = (portal +
           f'<a class="{"active" if active=="home" else ""}" href="{_cab_href("/cabinet", as_id)}">Собесы{badge}</a>'
           f'<a class="{"active" if active=="calendar" else ""}" href="{_cab_href("/cabinet/calendar", as_id)}">Календарь</a>'
           f'<a class="{"active" if active=="candidates" else ""}" href="{_cab_href("/cabinet/candidates", as_id)}">Кандидаты</a>'
           f'<a class="{"active" if active=="availability" else ""}" href="{_cab_href("/cabinet/availability", as_id)}">Расписание</a>'
           f'<a href="/logout">Выход</a>')
    return (f'<div class="cab-top"><div class="brand">{mailcrm_ui._LOGO_IMG}</div>'
            f'<span class="who">{name}</span>'
            f'<nav class="cab-nav">{nav}</nav></div>')


# ---- pages ------------------------------------------------------------------------
def login_page(error: str = "") -> str:
    err = f'<div class="err">{escape(error)}</div>' if error else ""
    body = (
        '<div class="login-wrap"><div class="brand">' + mailcrm_ui._LOGO_IMG + '</div>'
        f'{err}'
        '<div class="card"><form method="post" action="/login">'
        '<label>Логин</label>'
        '<input name="login" autocomplete="username" autofocus required>'
        '<label>Пароль</label>'
        '<input name="password" type="password" autocomplete="current-password" required>'
        '<button class="primary" type="submit">Войти</button>'
        '</form></div></div>')
    return _doc(body, "Вход в кабинет")


def _fmt_local(dt, tz=None) -> str:
    if not dt:
        return "время не указано"     # match the manager portal's convention (not a bare «—»)
    try:
        z = tz or slots.DEFAULT_TZ
        return slots.to_local(dt, z).strftime("%d.%m.%Y %H:%M") + f" ({slots.tz_label(z)})"
    except Exception:
        return str(dt)


def dashboard_page(responsible: dict, interviews: list[dict], as_id=None,
                   pool_sort: str = "salary", sort_base: str = "/cabinet") -> str:
    rtz = responsible.get("tz")

    def _item(iv: dict, past: bool = False) -> str:
        mailbox = escape(iv.get("mailbox") or "")
        company = escape(iv.get("company") or "")
        when = escape(_fmt_local(iv.get("start_ts"), rtz))
        h = iv.get("source_message_hash")
        # the WHOLE card is the tap target (a wrapping <a>), not just the small text link
        href = (_cab_href(f"/cabinet/thread?hash={escape(str(h), quote=True)}", as_id) if h
                else _cab_href("/cabinet/inbox", as_id))
        open_lbl = "Переписка →" if h else "Почта →"
        meta = mailbox + (f' · <b>{company}</b>' if company else "")
        li_open = '<li style="opacity:.62;">' if past else '<li>'
        tag = ('<span style="color:var(--ink-mute);font-weight:600;font-size:12px;">'
               ' · прошло</span>' if past else "")
        return (f'{li_open}<a class="iv-card-link" href="{href}">'
                f'<span class="iv-when">{when}{tag}</span>'
                f'<span class="iv-meta">{meta}</span>'
                f'<span class="iv-open">{open_lbl}</span></a></li>')

    # An assigned собес whose slot time has already passed (but that was never cancelled)
    # is STILL shown — the operator week grid can book an already-passed day of the current
    # week — so a just-assigned собес never silently vanishes. Upcoming ones (soonest first,
    # from the DB's ascending order) sit on top; past-but-still-assigned ones follow (most
    # recent first), clearly marked «прошло».
    now = datetime.now(timezone.utc)
    upcoming, past = [], []
    for iv in interviews:
        st = iv.get("start_ts")
        (past if (st is not None and st < now) else upcoming).append(iv)

    if interviews:
        items = [_item(iv) for iv in upcoming] + [_item(iv, past=True) for iv in reversed(past)]
        block = f'<ul class="iv-list">{"".join(items)}</ul>'
    else:
        block = '<div class="empty">Предстоящих собеседований нет.</div>'
    # a «Подключить Telegram» prompt right on the dashboard when the bot isn't linked yet — the
    # notifier pings a linked interviewer an hour + 5 min before each собес (Task 3). Hidden once
    # connected so it never nags.
    tg_prompt = "" if responsible.get("telegram_chat_id") else _tg_card(responsible, as_id)
    # priority card: the SAME urgency/priority filter as the «Собес» screen over the interviews
    # assigned to this interviewer — «с чего начать» (по срочности брони / зарплате / давности).
    from backend.interviews import priority_ui
    pri = priority_ui.priority_card(
        interviews, pool_sort, sort_base, anchor="cab-pri",
        title="Приоритет: с чего начать",
        blurb=("Ваши собеседования по приоритету — по зарплате, срочности брони слота или "
               "давности заявки. Явно просроченные — в «Истёкшие»."),
        empty="Назначенных собеседований пока нет.") if interviews else ""
    body = (priority_ui.CSS + _topbar(responsible, "home", as_id, iv_count=len(upcoming)) +
            _asview_banner(responsible, as_id) +
            '<h1 class="cab-h">Мои собеседования</h1>' + tg_prompt + block + pri)
    return _doc(body, "Мои собеседования")


def _tg_card(responsible: dict, as_id=None) -> str:
    if as_id:
        # Admin read-through: Telegram linking is a SELF-SERVICE step (it mints a code the
        # person opens in their own Telegram), so it makes no sense for an admin to do it FOR
        # them — show status only, no interactive connect/unlink control.
        status = ('✓ Telegram подключён' if responsible.get("telegram_chat_id")
                  else 'Telegram не подключён')
        return ('<div class="card tg-card"><div class="tg-h">Уведомления в Telegram</div>'
                f'<div class="tg-sub">{status} (привязку делает сам сотрудник).</div></div>')
    if responsible.get("telegram_chat_id"):
        inner = ('<span style="color:#166534;font-weight:700;">✓ Telegram подключён</span>'
                 '<form method="post" action="/cabinet/tg/unlink" style="display:inline;margin-left:12px;">'
                 '<button class="ghost" type="submit">Отвязать</button></form>')
        sub = "Напоминания о собеседованиях приходят в ваш личный Telegram."
    else:
        # the @username as plain text too — a fallback if the button is missed, and so the person
        # can find the bot manually (it's the reminder bot for THEIR собеседования, not the admin one).
        uname = ""
        try:
            from backend.interviews import notify
            uname = notify.bot_username() or ""
        except Exception:
            uname = ""
        uname_txt = (f' Бот: <b>@{escape(uname)}</b>.' if uname else "")
        inner = ('<form method="post" action="/cabinet/tg/connect" style="margin:0;">'
                 '<button class="primary" type="submit">Подключить Telegram</button></form>')
        sub = ("Нажмите — откроется бот, который напоминает о ваших предстоящих собеседованиях; "
               "нажмите в нём «Старт». После этого за час и за 5 минут до собеседования сюда придёт "
               "напоминание со ссылкой на созвон, вакансией, профилем кандидата и его резюме." + uname_txt)
    return ('<div class="card tg-card">'
            '<div class="tg-h">Уведомления в Telegram</div>'
            f'<div class="tg-sub">{sub}</div>'
            f'<div class="tg-act">{inner}</div></div>')


def availability_page(responsible: dict, rows: list[dict], saved: bool = False, as_id=None) -> str:
    note = '<div class="note">Расписание сохранено.</div>' if saved else ""
    import json as _json
    rtz = responsible.get("tz") or slots.DEFAULT_TZ
    # auto-adopt the device timezone: if the browser's zone differs from the stored one,
    # update it and reload so the schedule is shown/anchored to where the person is now.
    # SKIP this in the admin read-through (as_id set) — the admin's OWN device zone must NOT
    # overwrite the viewed user's stored tz.
    tz_js = "" if as_id else (
        "<script>(function(){var b;try{b=Intl.DateTimeFormat().resolvedOptions().timeZone;}"
        "catch(e){return;}var cur=" + _json.dumps(rtz) + ";if(b&&b!==cur){var f=new FormData();"
        "f.append('tz',b);fetch('/cabinet/tz',{method:'POST',body:f}).then(function(){"
        "location.reload();}).catch(function(){});}})();</script>")
    body = (_topbar(responsible, "availability", as_id) + _asview_banner(responsible, as_id) +
            '<h1 class="cab-h">Расписание доступности</h1>' + note +
            '<p style="color:var(--ink-soft);margin:0 0 16px;font-size:13px;line-height:1.5;">'
            f'Время — по вашему устройству (<b>{escape(slots.tz_label(rtz))}</b>). '
            'Можно добавить <b>несколько промежутков</b> в один день (напр. 06:30–14:00 и 18:00–01:00). '
            'День без промежутков — выходной. Конец раньше начала — ночное окно через полночь.</p>'
            + _tg_card(responsible, as_id) +
            f'<style>{avail_editor.CSS}</style>'
            '<form method="post" action="/cabinet/availability">'
            + _as_field(as_id)
            + avail_editor.render_days(rows) +
            '<div class="avd-actions">'
            '<button class="primary" type="submit">Сохранить</button>'
            '<button class="ghost" type="button" onclick="avdCopyMon()">Скопировать Пн</button>'
            '</div></form>' + avail_editor.JS + tz_js)
    return _doc(body, "Расписание")


def inbox_page(responsible: dict, rows: list[dict], as_id=None) -> str:
    # Reuse the operator's row renderer in READ-ONLY mode: no «Собес» control, plain
    # non-interactive avatar (the operator `toggleSel` JS isn't in this shell), no
    # decorative 📎. Row links point to the operator route /mail/message; rewrite them to
    # the cabinet's own guarded /thread so navigation stays inside this app.
    listing = mailcrm_ui.render_rows(rows, show_mailbox=True, read_only=True)
    # In the admin read-through put `as` BEFORE the hash so the query splices cleanly
    # (`/cabinet/thread?as=5&hash=...`) — the click keeps the admin in the user's context.
    thread_link = f"/cabinet/thread?as={as_id}&hash=" if as_id else "/cabinet/thread?hash="
    listing = listing.replace("/mail/message?id=", thread_link)
    inner = (f'<div class="maillist">{listing}</div>' if rows
             else '<div class="empty">Писем пока нет.</div>')
    body = (_topbar(responsible, "inbox", as_id) + _asview_banner(responsible, as_id) +
            '<h1 class="cab-h">Почта</h1>' + inner)
    return _doc(body, "Почта")


# ---- scoped candidate inbox (full Gmail-style inbox of the interviewer's собес candidates) ----
def _cab_inbox_js(as_id, page: int) -> str:
    """Cabinet-scoped card JS: expand a candidate card → its thread, open a message inline, and
    infinite-scroll — all pointing at the guarded /cabinet/candidates/* routes (never the operator
    ones). The reused message card's reply button is re-routed to the full guarded /cabinet/thread
    view (which owns the reply form). `as_id` is carried on every fetch so an admin read-through
    stays in the viewed user's context."""
    import json as _json
    a = _json.dumps(str(as_id) if as_id else "")
    return (
        "<script>(function(){\n"
        f"  var AS={a}; var PAGE={int(page)};\n"
        "  function asq(){ return AS ? ('&as=' + encodeURIComponent(AS)) : ''; }\n"
        "  window.cgToggle = function(head){\n"
        "    if(window.event && window.event.target && window.event.target.closest('a, button')) return;\n"
        "    var card = head.closest('.cg-card'); if(!card) return;\n"
        "    var body = card.querySelector('.cg-body');\n"
        "    var open = card.classList.toggle('open'); if(body) body.hidden = !open;\n"
        "    if(open && card.dataset.loaded === '0' && body){\n"
        "      card.dataset.loaded = '1';\n"
        "      body.innerHTML = '<div class=\"cg-load\">Загрузка…</div>';\n"
        "      fetch('/cabinet/candidates/thread?mailbox=' + encodeURIComponent(card.dataset.mailbox || '') + asq())\n"
        "        .then(function(r){ return r.text(); })\n"
        "        .then(function(h){ body.innerHTML = h; })\n"
        "        .catch(function(){ body.innerHTML = '<div class=\"cg-load\">Не удалось загрузить</div>'; card.dataset.loaded = '0'; });\n"
        "    }\n"
        "  };\n"
        "  window.cgOpen = function(row){\n"
        "    if(window.event && window.event.target && window.event.target.closest('a, button')) return;\n"
        "    var body = row.querySelector('.cg-msg-body'); if(!body) return;\n"
        "    var open = row.classList.toggle('open'); body.hidden = !open;\n"
        "    if(open && row.dataset.loaded !== '1'){\n"
        "      row.dataset.loaded = '1';\n"
        "      body.innerHTML = '<div class=\"cg-load\">Загрузка…</div>';\n"
        "      fetch('/cabinet/candidates/message?id=' + encodeURIComponent(row.dataset.id || '') + asq())\n"
        "        .then(function(r){ return r.text(); })\n"
        "        .then(function(h){ body.innerHTML = h; wireReply(body, row.dataset.id || ''); })\n"
        "        .catch(function(){ body.innerHTML = '<div class=\"cg-msg-err\">Не удалось загрузить</div>'; row.dataset.loaded = ''; });\n"
        "    }\n"
        "  };\n"
        "  function wireReply(rootEl, hash){\n"
        "    var qa = AS ? ('&as=' + encodeURIComponent(AS)) : '';\n"
        "    rootEl.querySelectorAll('.mf-reply, .reply-action').forEach(function(b){\n"
        "      var nb = b.cloneNode(true); if(b.parentNode) b.parentNode.replaceChild(nb, b);\n"
        "      nb.addEventListener('click', function(e){ e.stopPropagation();\n"
        "        location.href = '/cabinet/thread?hash=' + encodeURIComponent(hash) + qa; });\n"
        "    });\n"
        "  }\n"
        "  var sentinel = document.getElementById('grpmore');\n"
        "  var list = document.getElementById('grouplist');\n"
        "  if(sentinel && list && 'IntersectionObserver' in window){\n"
        "    var loading = false, done = false;\n"
        "    function more(){\n"
        "      if(loading || done || sentinel.hidden) return;\n"
        "      loading = true;\n"
        "      var off = parseInt(sentinel.dataset.offset || '0', 10) || 0;\n"
        "      var qs = new URLSearchParams({ q: sentinel.dataset.q || '', offset: String(off) });\n"
        "      if(AS) qs.set('as', AS);\n"
        "      fetch('/cabinet/candidates/more?' + qs.toString())\n"
        "        .then(function(r){ return r.ok ? r.text() : ''; })\n"
        "        .then(function(html){\n"
        "          html = (html || '').trim();\n"
        "          if(html){ list.insertAdjacentHTML('beforeend', html); sentinel.dataset.offset = String(off + PAGE); }\n"
        "          var added = (html.match(/class=\"cg-card[ \"]/g) || []).length;\n"
        "          if(added < PAGE){ done = true; sentinel.hidden = true; }\n"
        "          loading = false;\n"
        "        })\n"
        "        .catch(function(){ loading = false; });\n"
        "    }\n"
        "    var io = new IntersectionObserver(function(entries){\n"
        "      entries.forEach(function(en){ if(en.isIntersecting) more(); });\n"
        "    }, {rootMargin: '400px'});\n"
        "    io.observe(sentinel);\n"
        "  }\n"
        "})();</script>")


def candidates_page(responsible: dict, groups: list, *, q: str = "", has_more: bool = False,
                    offset: int = 0, as_id=None, iv_count=None) -> str:
    """The interviewer/manager's FULL candidate inbox, scoped to their собес candidates — the
    same Gmail-style grouped cards as the admin «Кандидаты» tab (via candidates_inbox.render_groups
    in `plain` mode: no operator assign/assessment controls), the same search, expand-a-card-to-its-
    thread and open-a-message inline, but every route guarded to this user's assigned mailboxes."""
    from backend.tools import candidates_inbox
    groups = groups or []
    listing = candidates_inbox.render_groups(groups, plain=True)
    next_off = offset + len(groups)
    hidden_as = _as_field(as_id)
    search = ('<form class="cab-cand-search" method="get" action="/cabinet/candidates" role="search">'
              + hidden_as
              + f'<input type="search" name="q" value="{escape(q or "", quote=True)}" '
              'placeholder="Поиск кандидата" autocomplete="off"></form>')
    tools = ('<div class="cab-cand-tools">'
             '<span style="color:var(--ink-soft);font-size:13px;line-height:1.5;">'
             'Все кандидаты ваших собеседований — вся переписка, поиск и ответ рекрутёру.</span>'
             + search + '</div>')
    inner = (f'<div id="grouplist">{listing}</div>' if groups
             else '<div class="empty">Кандидатов пока нет.</div>')
    sentinel = (f'<div id="grpmore" data-offset="{next_off}" '
                f'data-q="{escape(q or "", quote=True)}"{"" if has_more else " hidden"}></div>')
    body = (f'<style>{candidates_inbox._CG_CSS}</style>'
            + _topbar(responsible, "candidates", as_id, iv_count=iv_count)
            + _asview_banner(responsible, as_id)
            + '<h1 class="cab-h">Кандидаты</h1>' + tools + inner + sentinel
            + _cab_inbox_js(as_id, candidates_inbox.PAGE))
    return _doc(body, "Кандидаты")


def _cal_item(iv: dict, time_lbl: str, past: bool, as_id=None) -> str:
    company = escape(iv.get("company") or "Собеседование")
    mailbox = escape(iv.get("mailbox") or "")
    h = iv.get("source_message_hash") or iv.get("source_hash") or ""
    href = (_cab_href(f"/cabinet/thread?hash={escape(str(h), quote=True)}", as_id) if h
            else _cab_href("/cabinet/candidates", as_id))
    time_html = (f'<span class="cal-time">{escape(time_lbl)}</span>' if time_lbl
                 else '<span class="cal-time none">—</span>')
    chips = ""
    sal = iv.get("salary_label")
    if sal:
        chips += f'<span class="cal-chip sal">{escape(sal)}/год</span>'
    # booking / созвон link rendered as a SIBLING of the card link (never a nested <a>): its own
    # row below the card so it opens the scheduler / Zoom room in a new tab.
    bk = iv.get("iv_booking_url") or iv.get("booking_url") or ""
    booking_row = ""
    if bk:
        booking_row = (f'<div class="cal-book"><a class="cal-chip book" '
                       f'href="{escape(bk, quote=True)}" target="_blank" rel="noopener noreferrer">'
                       '📅 ссылка записи / созвон</a></div>')
    past_cls = " past" if past else ""
    return (f'<li class="cal-iv{past_cls}"><a class="cal-iv-link" href="{href}">'
            f'{time_html}<span class="cal-mid"><span class="cal-co">{company}</span>'
            f'<span class="cal-sub">{mailbox}</span>'
            f'<span class="cal-chips">{chips}</span></span>'
            f'<span class="cal-open">Переписка →</span></a>{booking_row}</li>')


def calendar_page(responsible: dict, interviews: list, as_id=None) -> str:
    """Personal calendar «Мои собеседования»: the interviewer's upcoming собеседования grouped by
    DAY (in their own timezone), each showing когда и во сколько + company/candidate + the booking/
    созвон link + a link into that candidate's переписка. Interviews with no set time land in a
    «Без даты» group at the end; already-passed-but-active ones stay visible, dimmed."""
    rtz = responsible.get("tz") or slots.DEFAULT_TZ
    now = datetime.now(timezone.utc)
    interviews = interviews or []
    upcoming = [iv for iv in interviews if not (iv.get("start_ts") and iv["start_ts"] < now)]

    order: list[str] = []
    buckets: dict[str, dict] = {}
    for iv in interviews:
        st = iv.get("start_ts")
        label, dow, time_lbl, key = "Без даты", "", "", "zzz-none"
        if st:
            try:
                loc = slots.to_local(st, rtz)
                key = loc.strftime("%Y-%m-%d")
                label = loc.strftime("%d.%m.%Y")
                dow = _WEEKDAYS[loc.weekday()]
                time_lbl = loc.strftime("%H:%M")
            except Exception:
                label, dow, time_lbl, key = "Без даты", "", "", "zzz-none"
        past = bool(st and st < now)
        if key not in buckets:
            buckets[key] = {"label": label, "dow": dow, "items": []}
            order.append(key)
        buckets[key]["items"].append((iv, time_lbl, past))

    if interviews:
        days = []
        for key in order:
            b = buckets[key]
            head = (f'<div class="cal-day-h">{escape(b["label"])}'
                    + (f'<span class="cal-dow">{escape(b["dow"])}</span>' if b["dow"] else "")
                    + '</div>')
            items = "".join(_cal_item(iv, t, p, as_id) for (iv, t, p) in b["items"])
            days.append(f'<div class="cal-day">{head}<ul class="cal-list">{items}</ul></div>')
        block = "".join(days)
    else:
        block = '<div class="empty">Предстоящих собеседований нет.</div>'

    tg_prompt = "" if responsible.get("telegram_chat_id") else _tg_card(responsible, as_id)
    body = (_topbar(responsible, "calendar", as_id, iv_count=len(upcoming))
            + _asview_banner(responsible, as_id)
            + '<h1 class="cab-h">Мой календарь</h1>'
            + '<p style="color:var(--ink-soft);margin:0 0 16px;font-size:13px;line-height:1.5;">'
            f'Ваши собеседования по дням — когда и во сколько (время по <b>{escape(slots.tz_label(rtz))}</b>).</p>'
            + tg_prompt + block)
    return _doc(body, "Календарь")


def _thread_card(m: dict) -> str:
    sender = m.get("from_name") or m.get("from_email") or "?"
    addr = m.get("from_email") or ""
    date = m.get("date") or ""
    plain = (m.get("plain") or "").strip()
    body = escape(plain) if plain else '<span style="color:var(--ink-mute)">(пустое письмо)</span>'
    return (
        '<div class="tcard"><div class="tmeta">'
        f'<b>{escape(sender)}</b>'
        f'<span class="addr">{escape(addr)}</span>'
        f'<span class="date">{escape(str(date))}</span></div>'
        f'<div class="body">{body}</div></div>')


def thread_page(responsible: dict, thread: dict, hash: str = "", sent=None, links=None, as_id=None) -> str:
    subj = thread.get("subject") or "(без темы)"
    mailbox = thread.get("mailbox") or ""
    candidate = thread.get("candidate") or ""
    msgs = thread.get("messages") or []
    cards = "".join(_thread_card(m) for m in msgs) or '<div class="empty">Пусто</div>'
    box = escape(candidate) + (f' &lt;{escape(mailbox)}&gt;' if mailbox else "")

    sent_banner = ""
    if sent == "ok":
        sent_banner = '<div class="note">Ответ отправлен рекрутёру.</div>'
    elif sent == "noreply":
        # The thread's sender is a no-reply/automated notification (e.g. Greenhouse) — a reply by
        # email bounces and never reaches the recruiter. Point the interviewer at the links instead.
        lk = ""
        if links:
            items = "".join(
                f'<li><a href="{escape(u, quote=True)}" target="_blank" rel="noopener noreferrer">'
                f'{escape(u[:90])}</a></li>' for u in links)
            lk = f'<div class="tbox">Ссылки из письма (расписание / портал):<ul>{items}</ul></div>'
        sent_banner = (
            '<div class="err">Это автоматическое уведомление (no-reply) — ответить по почте нельзя, '
            'рекрутёр его не получит. Откройте ссылку из письма (назначение времени / портал) ниже '
            'или свяжитесь по реальному адресу рекрутёра.</div>' + lk)
    elif sent == "err":
        sent_banner = '<div class="err">Не удалось отправить ответ. Попробуйте ещё раз.</div>'

    # Reply is sent server-side FROM the profile mailbox to the recruiter (derived from the
    # thread) — the interviewer only types the body; they can't spoof the from/to. The
    # /cabinet/reply route re-checks that this thread belongs to one of their assigned personas.
    reply_form = ""
    if hash:
        reply_form = (
            '<form method="post" action="/cabinet/reply" class="cab-reply">'
            f'<input type="hidden" name="hash" value="{escape(hash, quote=True)}">'
            + _as_field(as_id) +
            '<div class="cab-reply-h">Ответить рекрутёру</div>'
            '<textarea name="body" rows="4" placeholder="Ваш ответ…" required></textarea>'
            '<button class="primary" type="submit">Отправить ответ</button>'
            '</form>')

    body = (_topbar(responsible, "candidates", as_id) + _asview_banner(responsible, as_id) +
            f'<a class="back-link" href="{_cab_href("/cabinet/candidates", as_id)}">← К списку</a>'
            f'<h1 class="tsubj">{escape(subj)}</h1>'
            f'<div class="tbox">Ящик: {box}</div>' + sent_banner + cards + reply_form)
    return _doc(body, subj)
