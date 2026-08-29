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

from datetime import timezone
from html import escape

from backend.interviews import slots
from backend.tools import mailcrm_ui

_WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг",
             "Пятница", "Суббота", "Воскресенье"]

# Cabinet-specific styling layered on top of the shared base CSS.
_CAB_CSS = """
main{max-width:900px;margin:0 auto;padding:22px 18px;}
@media(max-width:600px){main{padding:14px 12px;}}
.cab-top{display:flex;align-items:center;gap:18px;flex-wrap:wrap;margin-bottom:22px;
  padding-bottom:14px;border-bottom:1px solid var(--line);}
.cab-top .brand{width:34px;height:34px;border-radius:9px;background:var(--accent);
  color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:15px;}
.cab-top .who{font-weight:700;color:var(--ink);font-size:15px;}
.cab-nav{display:flex;gap:8px;margin-left:auto;flex-wrap:wrap;}
.cab-nav a{padding:8px 14px;border-radius:var(--r-full);font-weight:600;font-size:13px;
  color:var(--ink-soft);border:1px solid var(--line-strong);background:var(--panel);}
.cab-nav a:hover{background:var(--panel-2);color:var(--ink);text-decoration:none;}
.cab-nav a.active{background:var(--accent-soft);color:var(--accent-deep);border-color:var(--accent);}
h1.cab-h{font-size:22px;font-weight:600;letter-spacing:-.02em;margin:0 0 16px;}
.note{background:var(--accent-soft);color:var(--accent-deep);border-radius:var(--r-sm);
  padding:9px 14px;margin-bottom:16px;font-weight:600;font-size:13px;}
.err{background:#fce8e6;color:var(--danger);border-radius:var(--r-sm);padding:9px 14px;
  margin-bottom:16px;font-weight:600;font-size:13px;}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:18px 20px;}
.login-wrap{max-width:360px;margin:8vh auto 0;}
.login-wrap .brand{width:44px;height:44px;border-radius:11px;background:var(--accent);
  color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800;
  font-size:19px;margin:0 auto 18px;}
.login-wrap .card{padding:24px;}
.login-wrap label{margin-top:14px;}
.login-wrap input{width:100%;}
.login-wrap button{width:100%;margin-top:18px;}
.iv-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px;}
.iv-list li{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
  padding:14px 16px;display:flex;flex-direction:column;gap:4px;}
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
.back-link{display:inline-block;margin-bottom:14px;color:var(--ink-soft);font-weight:600;}
.tsubj{font-size:20px;font-weight:600;letter-spacing:-.02em;margin:0 0 4px;}
.tbox{color:var(--ink-mute);font-size:12px;margin-bottom:16px;}
"""


def _doc(body: str, title: str = "Кабинет") -> str:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)}</title>" + mailcrm_ui._FONTS +
        f"<style>{mailcrm_ui._CSS}{_CAB_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>")


def _topbar(responsible: dict, active: str) -> str:
    name = escape(responsible.get("name") or responsible.get("login") or "")
    nav = (f'<a class="{"active" if active=="home" else ""}" href="/cabinet">Мои собеседования</a>'
           f'<a class="{"active" if active=="availability" else ""}" href="/cabinet/availability">Расписание</a>'
           f'<a class="{"active" if active=="inbox" else ""}" href="/cabinet/inbox">Почта</a>'
           f'<a href="/logout">Выход</a>')
    return (f'<div class="cab-top"><div class="brand">JF</div>'
            f'<span class="who">{name}</span>'
            f'<nav class="cab-nav">{nav}</nav></div>')


# ---- pages ------------------------------------------------------------------------
def login_page(error: str = "") -> str:
    err = f'<div class="err">{escape(error)}</div>' if error else ""
    body = (
        '<div class="login-wrap"><div class="brand">JF</div>'
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
        return "—"
    try:
        z = tz or slots.DEFAULT_TZ
        return slots.to_local(dt, z).strftime("%d.%m.%Y %H:%M") + f" ({slots.tz_label(z)})"
    except Exception:
        return str(dt)


def dashboard_page(responsible: dict, interviews: list[dict]) -> str:
    rtz = responsible.get("tz")
    if interviews:
        items = []
        for iv in interviews:
            mailbox = escape(iv.get("mailbox") or "")
            company = escape(iv.get("company") or "")
            when = escape(_fmt_local(iv.get("start_ts"), rtz))
            h = iv.get("source_message_hash")
            link = (f'<a href="/cabinet/thread?hash={escape(str(h))}">Открыть переписку</a>'
                    if h else '<a href="/cabinet/inbox">Почта</a>')
            meta = mailbox + (f' · <b>{company}</b>' if company else "")
            items.append(
                f'<li><span class="iv-when">{when}</span>'
                f'<span class="iv-meta">{meta}</span>'
                f'<span>{link}</span></li>')
        block = f'<ul class="iv-list">{"".join(items)}</ul>'
    else:
        block = '<div class="empty">Предстоящих собеседований нет.</div>'
    body = (_topbar(responsible, "home") +
            '<h1 class="cab-h">Мои собеседования</h1>' + block)
    return _doc(body, "Мои собеседования")


def availability_page(responsible: dict, rows: list[dict], saved: bool = False) -> str:
    note = '<div class="note">Расписание сохранено.</div>' if saved else ""
    by_dow = {r["dow"]: r for r in rows}
    day_html = []
    for d in range(7):
        r = by_dow.get(d, {"dow": d, "start_min": 0, "end_min": 0, "enabled": False})
        enabled = bool(r.get("enabled"))
        smin = r.get("start_min") or 540      # display default 09:00 for a never-set day
        emin = r.get("end_min") or 1020       # display default 17:00
        st = f"{smin // 60:02d}:{smin % 60:02d}"
        en = f"{emin // 60:02d}:{emin % 60:02d}"
        chk = " checked" if enabled else ""
        off = "" if enabled else " off"
        day_html.append(
            f'<div class="av-day{off}" data-dow="{d}">'
            f'<span class="dow">{_WEEKDAYS[d]}</span>'
            f'<label class="tog"><input type="checkbox" name="enabled_{d}"{chk}> рабочий</label>'
            f'<span class="times">с <input type="time" name="start_{d}" value="{st}"> '
            f'до <input type="time" name="end_{d}" value="{en}"></span></div>')
    import json as _json
    rtz = responsible.get("tz") or slots.DEFAULT_TZ
    # auto-adopt the device timezone: if the browser's zone differs from the stored one,
    # update it and reload so the schedule is shown/anchored to where the person is now.
    tz_js = (
        "<script>(function(){var b;try{b=Intl.DateTimeFormat().resolvedOptions().timeZone;}"
        "catch(e){return;}var cur=" + _json.dumps(rtz) + ";if(b&&b!==cur){var f=new FormData();"
        "f.append('tz',b);fetch('/cabinet/tz',{method:'POST',body:f}).then(function(){"
        "location.reload();}).catch(function(){});}})();</script>")
    body = (_topbar(responsible, "availability") +
            '<h1 class="cab-h">Расписание доступности</h1>' + note +
            '<p style="color:var(--ink-soft);margin:0 0 16px;font-size:13px;">'
            f'Время — по вашему устройству (<b>{escape(slots.tz_label(rtz))}</b>).</p>'
            '<form method="post" action="/cabinet/availability">'
            f'<div class="av-grid">{"".join(day_html)}</div>'
            '<button class="primary" type="submit" style="margin-top:18px;">Сохранить</button>'
            '</form>' + tz_js)
    return _doc(body, "Расписание")


def inbox_page(responsible: dict, rows: list[dict]) -> str:
    # Reuse the operator's row renderer in READ-ONLY mode: no «Собес» control, plain
    # non-interactive avatar (the operator `toggleSel` JS isn't in this shell), no
    # decorative 📎. Row links point to the operator route /mail/message; rewrite them to
    # the cabinet's own guarded /thread so navigation stays inside this app.
    listing = mailcrm_ui.render_rows(rows, show_mailbox=True, read_only=True)
    listing = listing.replace("/mail/message?id=", "/cabinet/thread?hash=")
    inner = (f'<div class="maillist">{listing}</div>' if rows
             else '<div class="empty">Писем пока нет.</div>')
    body = (_topbar(responsible, "inbox") +
            '<h1 class="cab-h">Почта</h1>' + inner)
    return _doc(body, "Почта")


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


def thread_page(responsible: dict, thread: dict) -> str:
    subj = thread.get("subject") or "(без темы)"
    mailbox = thread.get("mailbox") or ""
    candidate = thread.get("candidate") or ""
    msgs = thread.get("messages") or []
    cards = "".join(_thread_card(m) for m in msgs) or '<div class="empty">Пусто</div>'
    box = escape(candidate) + (f' &lt;{escape(mailbox)}&gt;' if mailbox else "")
    body = (_topbar(responsible, "inbox") +
            '<a class="back-link" href="/cabinet/inbox">← К списку</a>'
            f'<h1 class="tsubj">{escape(subj)}</h1>'
            f'<div class="tbox">Ящик: {box}</div>' + cards)
    return _doc(body, subj)
