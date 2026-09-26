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

from datetime import datetime, timedelta, timezone
from html import escape

from backend.interviews import avail_editor, portal_shell, slots
from backend.tools import mailcrm_ui


def _roles_of(responsible: dict) -> list:
    """The acting user's role set for the shared shell nav (multi-role aware, tolerant of an
    old single-role row)."""
    r = responsible.get("roles")
    if r:
        return list(r)
    return [responsible.get("role")] if responsible.get("role") else ["employee"]


def _shell(responsible: dict, active: str, inner: str, title: str, as_id=None,
           extra_css: str = "") -> str:
    """Wrap a cabinet page body in the shared left-menu portal shell (fixes the old top-nav +
    the hiring-events admin-rail leak). `inner` already includes the admin-view banner where
    needed (callers prepend `portal_shell.admin_banner`)."""
    head = f"<style>{_CAB_CSS}{extra_css}</style>"
    return portal_shell.shell(active=active, roles=_roles_of(responsible),
                              name=responsible.get("name") or responsible.get("login") or "",
                              body=inner, title=title, as_id=as_id, extra_head=head)

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


def _inbox_href(mailbox: str, as_id=None) -> str:
    """«Переписка» → the FULL clickable candidate inbox (the same grouped Gmail-style surface as
    the «Кандидаты» tab), scoped to this candidate and AUTO-EXPANDED to their thread (`open=`),
    instead of the flat non-clickable /cabinet/thread. So переписка = the whole inbox, clickable."""
    from urllib.parse import quote
    mb = quote(mailbox or "", safe="@.")
    return _cab_href(f"/cabinet/candidates?q={mb}&open={mb}", as_id)


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
           # the shared «События найма» board — open to every role (общий раздел)
           f'<a class="{"active" if active=="hiring" else ""}" href="/hiring-events">События найма</a>'
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


# ---- home: week calendar + actionable interview list --------------------------------------
_HOME_CSS = """
.hm-lead{color:var(--ink-soft);font-size:13px;line-height:1.5;margin:0 0 16px;}
/* week calendar */
.wk-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;margin-bottom:16px;}
.wk-h{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 12px;flex-wrap:wrap;}
.wk-h h2{margin:0;font-size:15px;font-weight:700;}
.wk-h .wk-sub{font-size:12px;color:var(--ink-mute);font-weight:600;}
.wk-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:8px;}
@media(max-width:760px){.wk-grid{grid-template-columns:repeat(2,1fr);}}
@media(max-width:420px){.wk-grid{grid-template-columns:1fr;}}
.wk-day{border:1px solid var(--line);border-radius:var(--r-sm);padding:8px 9px;min-height:74px;background:var(--panel-2);}
.wk-day.today{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset;}
.wk-day.empty{opacity:.6;}
.wk-dh{font-size:11px;font-weight:700;color:var(--ink-soft);margin-bottom:6px;display:flex;justify-content:space-between;gap:4px;}
.wk-dh .wk-dow{color:var(--ink-mute);font-weight:600;}
.wk-ev{display:block;font-size:11.5px;line-height:1.3;padding:3px 6px;margin-bottom:4px;border-radius:6px;
  background:var(--accent-soft);color:var(--accent-deep);text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.wk-ev:hover{filter:brightness(.97);text-decoration:none;}
.wk-ev b{font-variant-numeric:tabular-nums;}
.wk-ev.done{background:var(--panel);color:var(--ink-mute);text-decoration:line-through;}
.wk-none{font-size:11px;color:var(--ink-mute);}
/* actionable interview rows (collapse/expand by icon) */
.hv-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:8px;margin-bottom:16px;}
.hv-top{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:6px 10px 10px;flex-wrap:wrap;}
.hv-top h2{margin:0;font-size:15px;font-weight:700;}
.hv-sort{display:inline-flex;gap:2px;padding:3px;background:var(--panel-2);border:1px solid var(--line-strong);border-radius:var(--r-full);}
.hv-sortb{display:inline-flex;align-items:center;height:28px;padding:0 11px;border-radius:var(--r-full);font-size:12px;font-weight:600;color:var(--ink-mute);text-decoration:none;}
.hv-sortb.active{background:var(--panel);color:var(--accent);box-shadow:0 1px 2px rgba(0,0,0,.12);}
.hv-list{display:flex;flex-direction:column;gap:7px;}
.hv-row{border:1px solid var(--line);border-radius:var(--r-sm);overflow:hidden;}
.hv-row.done{opacity:.66;}
.hv-head{display:flex;align-items:center;gap:9px;padding:10px 11px;cursor:pointer;user-select:none;}
.hv-head:hover{background:var(--panel-2);}
.hv-chev{flex:0 0 auto;color:var(--ink-mute);font-size:12px;transition:transform .15s;}
.hv-row.open .hv-chev{transform:rotate(90deg);}
.hv-mid{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;gap:1px;}
.hv-nm{font-size:13.5px;font-weight:700;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.hv-sub{font-size:11.5px;color:var(--ink-soft);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.hv-when{flex:0 0 auto;font-size:12px;font-weight:700;color:var(--accent-deep);font-variant-numeric:tabular-nums;white-space:nowrap;}
.hv-when.none{color:var(--ink-mute);font-weight:600;}
.hv-sal{flex:0 0 auto;font-family:var(--ff-mono);font-size:11.5px;font-weight:700;color:var(--ok);white-space:nowrap;}
.hv-dl{flex:0 0 auto;font-size:11px;font-weight:700;border-radius:var(--r-full);padding:2px 8px;white-space:nowrap;}
.hv-dl-ok{color:var(--ink-soft);background:var(--panel-2);}
.hv-dl-soon{color:var(--warn);background:var(--warn-soft);}
.hv-dl-urgent{color:var(--danger);background:#fce8e6;}
.hv-dl-over{color:#fff;background:var(--danger);}
.hv-done-badge{flex:0 0 auto;font-size:10.5px;font-weight:700;color:#166534;background:#dcfce7;border-radius:var(--r-full);padding:2px 8px;}
.hv-panel{padding:0 11px 12px 30px;display:none;flex-direction:column;gap:10px;}
.hv-row.open .hv-panel{display:flex;}
.hv-acts{display:flex;flex-wrap:wrap;gap:8px;align-items:center;}
.hv-acts a.hbtn,.hv-acts button{white-space:nowrap;}
.hv-sched{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin:0;}
.hv-sched input[type=datetime-local]{padding:8px 10px;border:1px solid var(--line-strong);border-radius:8px;background:var(--panel);color:var(--ink);font-size:13px;min-height:var(--ctl-h);flex:1 1 180px;min-width:0;}
.hv-hint{font-size:11.5px;color:var(--ink-mute);line-height:1.4;margin:0;}
.hv-done-form{margin:0;}
"""

_HOME_JS = ("<script>(function(){document.addEventListener('click',function(e){"
            "var h=e.target.closest('.hv-head');if(!h)return;"
            "if(e.target.closest('a,button,input,form,label'))return;"
            "var row=h.closest('.hv-row');if(row)row.classList.toggle('open');});})();</script>")

_WK_DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_WK_MON = ["", "янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _week_calendar(interviews: list[dict], rtz, as_id) -> str:
    """A Mon–Sun week grid in the interviewer's own zone, each day listing its booked собесы
    (time + company, linking into переписка). The visual «расписание на неделю» the owner wants
    on the home screen. Собесы with no time set don't appear here (they're in the list below with
    «назначить время»)."""
    z = rtz or slots.DEFAULT_TZ
    now_local = slots.to_local(datetime.now(timezone.utc), z)
    monday = now_local.date() - timedelta(days=now_local.weekday())
    today = now_local.date()
    by_day: dict = {}
    for iv in interviews:
        st = iv.get("start_ts")
        if not st:
            continue
        try:
            loc = slots.to_local(st, z)
        except Exception:
            continue
        by_day.setdefault(loc.date(), []).append((loc, iv))
    cols = []
    for i in range(7):
        d = monday + timedelta(days=i)
        evs = sorted(by_day.get(d, []), key=lambda t: t[0])
        cells = []
        for loc, iv in evs:
            company = escape((iv.get("company") or "Собес")[:22])
            mb = iv.get("mailbox") or ""
            href = _inbox_href(mb, as_id) if mb else _cab_href("/cabinet", as_id)
            done = " done" if iv.get("done_at") else ""
            cells.append(f'<a class="wk-ev{done}" href="{escape(href, quote=True)}" '
                         f'title="{company}"><b>{loc.strftime("%H:%M")}</b> {company}</a>')
        body = "".join(cells) if cells else '<span class="wk-none">—</span>'
        cls = "wk-day" + (" today" if d == today else "") + ("" if cells else " empty")
        cols.append(f'<div class="{cls}"><div class="wk-dh"><span>{d.day} {_WK_MON[d.month]}</span>'
                    f'<span class="wk-dow">{_WK_DOW[i]}</span></div>{body}</div>')
    n = sum(len(v) for v in by_day.values())
    return (f'<div class="wk-card"><div class="wk-h"><h2>Календарь недели</h2>'
            f'<span class="wk-sub">запланировано на этой неделе: {n} · время по {escape(slots.tz_label(z))}</span></div>'
            f'<div class="wk-grid">{"".join(cols)}</div></div>')


def _home_row(iv: dict, rtz, as_id) -> str:
    """One actionable собес row: a collapsed one-liner (candidate · company · time · deadline ·
    salary) that expands (chevron) to reveal переписка, the booking/созвон link, a «во сколько я
    записался» time-set form and a «проведено» toggle."""
    from backend.tools import interview_priority as ip
    mb = iv.get("mailbox") or ""
    nm = (iv.get("candidate") or "").strip() or (mb.split("@")[0] if mb else "—")
    company = escape(iv.get("company") or "")
    iid = iv.get("id")
    done = bool(iv.get("done_at"))
    thread_href = _inbox_href(mb, as_id) if mb else ""
    when_txt = _fmt_local(iv.get("start_ts"), rtz)
    when_html = (f'<span class="hv-when">{escape(when_txt)}</span>' if iv.get("start_ts")
                 else '<span class="hv-when none">время не назначено</span>')
    sal = iv.get("salary_label") or ""
    sal_html = f'<span class="hv-sal">{escape(sal)}/год</span>' if sal else ""
    dtext, dlvl = ip.deadline_text(iv)
    dl_html = f'<span class="hv-dl hv-dl-{dlvl}">{escape(dtext)}</span>' if dtext and not done else ""
    done_badge = '<span class="hv-done-badge">✓ проведено</span>' if done else ""
    sub = escape(mb) + (f' · {company}' if company else "")
    head = (f'<div class="hv-head"><span class="hv-chev">▸</span>'
            f'<span class="hv-mid"><span class="hv-nm">{escape(nm)}</span>'
            f'<span class="hv-sub">{sub}</span></span>'
            f'{done_badge}{sal_html}{dl_html}{when_html}</div>')

    as_field = f'<input type="hidden" name="as" value="{as_id}">' if as_id else ""
    acts = []
    if thread_href:
        acts.append(f'<a class="hbtn" href="{escape(thread_href, quote=True)}">Переписка кандидата →</a>')
    bk = iv.get("booking_url") or iv.get("iv_booking_url") or ""
    if bk:
        acts.append(f'<a class="hbtn" href="{escape(bk, quote=True)}" target="_blank" '
                    'rel="noopener noreferrer">📅 Записаться / созвон →</a>')
    acts_html = f'<div class="hv-acts">{"".join(acts)}</div>' if acts else ""
    # «во сколько я записался» — set my OWN slot time (appears in the week calendar + arms reminders)
    sched = (
        f'<form class="hv-sched" method="post" action="/cabinet/self_schedule">'
        f'<input type="hidden" name="iid" value="{iid}">{as_field}'
        '<input type="datetime-local" name="start_local" aria-label="Во сколько собеседование">'
        '<button class="primary" type="submit">Записать время</button></form>'
        '<p class="hv-hint">Перейдите по ссылке записи выше, забронируйте слот у рекрутёра, '
        'затем впишите сюда это время — собес появится в вашем календаре недели, и бот напомнит '
        'заранее.</p>')
    # «проведено» toggle
    done_form = (
        f'<form class="hv-done-form" method="post" action="/cabinet/mark_done">'
        f'<input type="hidden" name="iid" value="{iid}">{as_field}'
        f'<input type="hidden" name="done" value="{"0" if done else "1"}">'
        f'<button class="{"ghost" if done else "hbtn"}" type="submit">'
        f'{"↩︎ Снять отметку «проведено»" if done else "○ Отметить проведённым"}</button></form>')
    panel = f'<div class="hv-panel">{acts_html}{sched}{done_form}</div>'
    return f'<div class="hv-row{" done" if done else ""}">{head}{panel}</div>'


def dashboard_page(responsible: dict, interviews: list[dict], as_id=None,
                   pool_sort: str = "salary", sort_base: str = "/cabinet") -> str:
    """Home screen: the week calendar (schedule) + the actionable «Мои собеседования» list whose
    rows collapse/expand by icon, sorted by urgency/salary/age. Done собесы collapse into a
    «Проведённые» details at the bottom."""
    from backend.tools import interview_priority as ip
    rtz = responsible.get("tz")
    interviews = interviews or []
    done = [iv for iv in interviews if iv.get("done_at")]
    not_done = [iv for iv in interviews if not iv.get("done_at")]
    # non-actual (EXPLICITLY-expired: booking window closed / link dead) sink to a «Пропущенные»
    # block at the very bottom; the actionable list is only the still-bookable ones.
    active = [iv for iv in not_done if not iv.get("expired")]
    expired = [iv for iv in not_done if iv.get("expired")]
    sort = pool_sort if pool_sort in ("salary", "urgency", "age") else "urgency"
    active_sorted = ip.sort_groups(active, sort)

    # sort toggle keeps ?as
    sep = "&" if "?" in sort_base else "?"
    def _sb(k):
        return f'{escape(sort_base, quote=True)}{sep}pool_sort={k}#hv'
    sort_toggle = ('<div class="hv-sort" role="group" aria-label="Сортировка">'
                   + "".join(f'<a class="hv-sortb{" active" if sort==k else ""}" href="{_sb(k)}">{l}</a>'
                             for k, l in (("urgency", "Срочность"), ("salary", "Зарплата"), ("age", "Давность")))
                   + "</div>")

    if active_sorted:
        rows = "".join(_home_row(iv, rtz, as_id) for iv in active_sorted)
        list_html = f'<div class="hv-list">{rows}</div>'
    else:
        list_html = '<div class="empty">Активных предстоящих собеседований нет.</div>'

    def _bucket(items, label):
        if not items:
            return ""
        body = "".join(_home_row(iv, rtz, as_id) for iv in items)
        return ('<details class="ivp-det" style="margin-top:8px;border:1px solid var(--line);'
                'border-radius:var(--r-sm)"><summary style="cursor:pointer;padding:10px 12px;'
                f'font-weight:700;font-size:13px;list-style:none">{escape(label)} ({len(items)})'
                f'</summary><div class="hv-list" style="padding:0 8px 8px">{body}</div></details>')

    # non-actual собесы (link dead / срок истёк) sink to the very bottom, then «Проведённые»
    expired_html = _bucket(expired, "Пропущенные — ссылка не работает или срок истёк")
    done_html = _bucket(done, "Проведённые")

    tg_prompt = "" if responsible.get("telegram_chat_id") else _tg_card(responsible, as_id)
    upcoming_n = sum(1 for iv in active if not (iv.get("start_ts") and iv["start_ts"] < datetime.now(timezone.utc)))

    inner = (
        portal_shell.admin_banner(responsible.get("name") or "", as_id)
        + '<h1 class="cab-h">Главная</h1>'
        + '<p class="hm-lead">Ваше расписание на неделю и актуальные собеседования по приоритету. '
        'Нажмите на кандидата, чтобы раскрыть: перейти в переписку, записаться на слот у рекрутёра, '
        'проставить время и отметить собес проведённым.</p>'
        + tg_prompt
        + _week_calendar(active, rtz, as_id)
        + '<div class="hv-card" id="hv"><div class="hv-top">'
        '<h2>Мои собеседования</h2>' + (sort_toggle if active_sorted else "") + '</div>'
        + list_html + expired_html + done_html + '</div>' + _HOME_JS)
    return _shell(responsible, "home", inner, "Главная", as_id=as_id, extra_css=_HOME_CSS)


def _tg_card(responsible: dict, as_id=None) -> str:
    if as_id:
        # Admin read-through: Telegram linking is a SELF-SERVICE step (it mints a code the
        # person opens in their own Telegram), so it makes no sense for an admin to do it FOR
        # them — show status only, no interactive connect/unlink control.
        _u = (responsible.get("telegram_username") or "").lstrip("@")
        _as = f' как @{escape(_u)}' if _u else ''
        status = (f'✓ Telegram подключён{_as}' if responsible.get("telegram_chat_id")
                  else 'Telegram не подключён')
        return ('<div class="card tg-card"><div class="tg-h">Уведомления в Telegram</div>'
                f'<div class="tg-sub">{status} (привязку делает сам сотрудник).</div></div>')
    if responsible.get("telegram_chat_id"):
        _u = (responsible.get("telegram_username") or "").lstrip("@")
        _as = f' как @{escape(_u)}' if _u else ''
        inner = (f'<span style="color:#166534;font-weight:700;">✓ Telegram подключён{_as}</span>'
                 '<form method="post" action="/cabinet/tg/unlink" style="display:inline;margin-left:12px;">'
                 '<button class="ghost" type="submit">Отвязать</button></form>')
        sub = "Напоминания приходят лично вам — только о ВАШИХ собеседованиях."
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
    inner = (portal_shell.admin_banner(responsible.get("name") or "", as_id) +
             '<h1 class="cab-h">Расписание доступности</h1>' + note +
             '<p style="color:var(--ink-soft);margin:0 0 16px;font-size:13px;line-height:1.5;">'
             f'Время — по вашему устройству (<b>{escape(slots.tz_label(rtz))}</b>). '
             'Можно добавить <b>несколько промежутков</b> в один день (напр. 06:30–14:00 и 18:00–01:00). '
             'День без промежутков — выходной. Конец раньше начала — ночное окно через полночь.</p>'
             + _tg_card(responsible, as_id) +
             '<form method="post" action="/cabinet/availability">'
             + _as_field(as_id)
             + avail_editor.render_days(rows) +
             '<div class="avd-actions">'
             '<button class="primary" type="submit">Сохранить</button>'
             '<button class="ghost" type="button" onclick="avdCopyMon()">Скопировать Пн</button>'
             '</div></form>' + avail_editor.JS + tz_js)
    return _shell(responsible, "schedule", inner, "Расписание", as_id=as_id,
                  extra_css=avail_editor.CSS)


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
    body = (portal_shell.admin_banner(responsible.get("name") or "", as_id) +
            '<h1 class="cab-h">Почта</h1>' + inner)
    return _shell(responsible, "candidates", body, "Почта", as_id=as_id)


# ---- scoped candidate inbox (full Gmail-style inbox of the interviewer's собес candidates) ----
def _cab_inbox_js(as_id, page: int, open_mbx: str = "") -> str:
    """Cabinet-scoped card JS: expand a candidate card → its thread, open a message inline, and
    infinite-scroll — all pointing at the guarded /cabinet/candidates/* routes (never the operator
    ones). The reused message card's reply button is re-routed to the full guarded /cabinet/thread
    view (which owns the reply form). `as_id` is carried on every fetch so an admin read-through
    stays in the viewed user's context. `open_mbx` (from «Переписка кандидата» → ?open=) AUTO-EXPANDS
    that candidate's card on load, so переписка lands right on the thread inside the full inbox."""
    import json as _json
    a = _json.dumps(str(as_id) if as_id else "")
    om = _json.dumps(open_mbx or "")
    return (
        "<script>(function(){\n"
        f"  var AS={a}; var PAGE={int(page)}; var OPEN={om};\n"
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
        "  if(OPEN){ var card=document.querySelector('.cg-card[data-mailbox=\"'+OPEN+'\"]');\n"
        "    if(card && !card.classList.contains('open')){ var h=card.querySelector('.cg-head')||card;\n"
        "      window.cgToggle(h); card.scrollIntoView({block:'start'}); } }\n"
        "})();</script>")


def candidates_page(responsible: dict, groups: list, *, q: str = "", has_more: bool = False,
                    offset: int = 0, as_id=None, iv_count=None, open_mbx: str = "") -> str:
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
    body = (portal_shell.admin_banner(responsible.get("name") or "", as_id)
            + '<h1 class="cab-h">Кандидаты</h1>' + tools + inner + sentinel
            + _cab_inbox_js(as_id, candidates_inbox.PAGE, open_mbx=open_mbx))
    return _shell(responsible, "candidates", body, "Кандидаты", as_id=as_id,
                  extra_css=candidates_inbox._CG_CSS)


def _cal_item(iv: dict, time_lbl: str, past: bool, as_id=None) -> str:
    company = escape(iv.get("company") or "Собеседование")
    mb = iv.get("mailbox") or ""
    mailbox = escape(mb)
    href = _inbox_href(mb, as_id) if mb else _cab_href("/cabinet/candidates", as_id)
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
    body = (portal_shell.admin_banner(responsible.get("name") or "", as_id)
            + '<h1 class="cab-h">Мой календарь</h1>'
            + '<p style="color:var(--ink-soft);margin:0 0 16px;font-size:13px;line-height:1.5;">'
            f'Ваши собеседования по дням — когда и во сколько (время по <b>{escape(slots.tz_label(rtz))}</b>).</p>'
            + tg_prompt + block)
    return _shell(responsible, "home", body, "Календарь", as_id=as_id)


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

    body = (portal_shell.admin_banner(responsible.get("name") or "", as_id) +
            f'<a class="back-link" href="{_cab_href("/cabinet/candidates", as_id)}">← К списку</a>'
            f'<h1 class="tsubj">{escape(subj)}</h1>'
            f'<div class="tbox">Ящик: {box}</div>' + sent_banner + cards + reply_form)
    return _shell(responsible, "candidates", body, subj, as_id=as_id)


# ---- Инструкции (role-specific short user-flow guide) --------------------------------------
_GUIDE_CSS = """
.gd-lead{color:var(--ink-soft);font-size:13.5px;line-height:1.55;margin:0 0 18px;}
.gd-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:18px 20px;margin-bottom:16px;}
.gd-card h2{margin:0 0 4px;font-size:16px;font-weight:800;letter-spacing:-.01em;}
.gd-card .gd-role{font-size:12px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.04em;margin:0 0 12px;}
.gd-steps{list-style:none;counter-reset:s;margin:0;padding:0;display:flex;flex-direction:column;gap:11px;}
.gd-steps li{position:relative;padding-left:38px;font-size:13.5px;line-height:1.5;color:var(--ink);}
.gd-steps li::before{counter-increment:s;content:counter(s);position:absolute;left:0;top:-1px;width:26px;height:26px;
  border-radius:50%;background:var(--accent-soft);color:var(--accent-deep);font-weight:800;font-size:13px;
  display:flex;align-items:center;justify-content:center;}
.gd-steps li b{color:var(--ink);}
.gd-steps li .gd-note{display:block;color:var(--ink-mute);font-size:12px;margin-top:2px;}
.gd-tip{background:var(--accent-soft);border:1px solid var(--accent);border-radius:var(--r-sm);
  padding:11px 14px;font-size:12.5px;color:var(--accent-deep);line-height:1.5;margin-top:12px;}
"""

_GUIDE_INTERVIEWER = """
<div class="gd-card">
  <div class="gd-role">Интервьюер</div>
  <h2>Как проводить собеседования</h2>
  <ol class="gd-steps">
    <li>Откройте <b>«Расписание»</b> и укажите, в какие часы по дням недели вы свободны для собесов.
        <span class="gd-note">Можно несколько промежутков в день; ночное окно — конец раньше начала.</span></li>
    <li>Подключите <b>Telegram</b> (кнопка на «Главной» или в «Расписании») — бот напомнит о каждом собесе за 2 часа, час, 15 и 5 минут.</li>
    <li>На <b>«Главной»</b> в списке «Мои собеседования» разберите кандидатов сверху вниз (сортировка по срочности / зарплате / давности). Нажмите на кандидата, чтобы раскрыть карточку.</li>
    <li>Нажмите <b>«Переписка кандидата»</b> — прочитайте письмо рекрутёра и, если нужно, ответьте прямо оттуда (ответ уходит от имени кандидата).</li>
    <li>Нажмите <b>«Записаться / созвон»</b> — откроется ссылка рекрутёра, где вы бронируете время собеседования.</li>
    <li>Вернитесь и впишите это время в поле <b>«Записать время»</b> — собес появится в вашем «Календаре недели», и придут напоминания.</li>
    <li>После проведения нажмите <b>«○ Отметить проведённым»</b> — собес уйдёт в «Проведённые».</li>
  </ol>
  <div class="gd-tip">«Кандидаты» — вся переписка ваших собесов в одном месте (поиск + ответ). «События найма» — живые Zoom-комнаты Teleperformance, куда можно зайти и получить оффер без теста.</div>
</div>
"""

_GUIDE_MANAGER = """
<div class="gd-card">
  <div class="gd-role">Управляющий</div>
  <h2>Как распределять собеседования на команду</h2>
  <ol class="gd-steps">
    <li>Всё, что выделил вам главный админ, <b>сразу числится за вами</b> — вы видите это в разделе <b>«Команда»</b>.</li>
    <li>Добавьте сотрудников в блоке <b>«Моя команда»</b> (имя + логин; пароль сгенерируется — передайте его сотруднику).</li>
    <li>В блоке <b>«Раздать / забрать»</b> отдайте N собесов сотруднику (по фильтру пол/направление) или распределите всё поровну одной кнопкой.</li>
    <li>В <b>«Приоритет»</b> смотрите, кого раздать первым (по зарплате/срочности). Нажатие на кандидата открывает переписку.</li>
    <li>Что не раздали — <b>проводите сами</b>: эти собесы лежат у вас на «Главной», как у интервьюера.</li>
    <li>Нужно вернуть собес от сотрудника — «← Забрать себе» на карточке или «Забрать» в блоке раздачи.</li>
  </ol>
  <div class="gd-tip">Раздача сотруднику присылает ему уведомление в Telegram. «Актуальные предстоящие» на «Команде» показывают весь ваш поток: свои + розданные, по срочности.</div>
</div>
"""


def guide_page(responsible: dict, as_id=None) -> str:
    """Short, role-specific «как этим пользоваться» guide. Interviewer steps are shown to
    everyone (every role attends собесы); the manager section is added for managers."""
    roles = _roles_of(responsible)
    is_mgr = "manager" in roles
    lead = ("Короткая инструкция по вашему порталу. " +
            ("Вы управляющий и интервьюер: распределяете собесы на команду и проводите свои."
             if is_mgr else "Вы проводите назначенные вам собеседования."))
    blocks = (_GUIDE_MANAGER if is_mgr else "") + _GUIDE_INTERVIEWER
    body = (portal_shell.admin_banner(responsible.get("name") or "", as_id)
            + '<h1 class="cab-h">Инструкции</h1>'
            + f'<p class="gd-lead">{lead}</p>' + blocks)
    return _shell(responsible, "guide", body, "Инструкции", as_id=as_id, extra_css=_GUIDE_CSS)
