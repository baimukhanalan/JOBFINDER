"""Server-rendered HTML for the manager («Управляющий») portal at /manage.

A middle tier between admin and employee: a manager sees the interviews the admin
allocated to him, his subordinates and their load, can add subordinates, and can assign
each allocated interview to himself or a subordinate (from his pool ONLY). Its own minimal
shell (like the interviewer cabinet) — it deliberately does NOT use mailcrm_ui._page (that
carries the admin rail + «Админ» label). Borrows mailcrm_ui._CSS/_FONTS for base styling.

Neutral Russian throughout; no stack names, no decorative emoji beyond the quiet status
dots. Fully responsive: cards stack, controls go full-width, the assign row wraps on a
phone (owner is phone-first).
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from backend.interviews import slots
from backend.tools import mailcrm_ui

_MG_CSS = """
main{max-width:940px;margin:0 auto;padding:20px 18px;}
@media(max-width:600px){main{padding:14px 12px;}}
.mg-top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:18px;
  padding-bottom:13px;border-bottom:1px solid var(--line);}
.mg-top .brand{width:34px;height:34px;border-radius:9px;background:var(--accent);overflow:hidden;padding:0;flex:0 0 auto;}
.mg-top .who{font-weight:700;color:var(--ink);font-size:15px;min-width:0;overflow:hidden;text-overflow:ellipsis;}
.mg-top .who small{display:block;font-weight:600;color:var(--ink-mute);font-size:11.5px;}
.mg-nav{display:flex;gap:7px;margin-left:auto;flex-wrap:wrap;}
.mg-nav a{padding:8px 13px;border-radius:var(--r-full);font-weight:600;font-size:13px;
  color:var(--ink-soft);border:1px solid var(--line-strong);background:var(--panel);white-space:nowrap;}
.mg-nav a:hover{background:var(--panel-2);color:var(--ink);text-decoration:none;}
.mg-nav a.active{background:var(--accent-soft);color:var(--accent-deep);border-color:var(--accent);}
.mg-h1{font-size:23px;font-weight:800;letter-spacing:-.02em;margin:0 0 3px;line-height:1.15;}
.mg-lead{color:var(--ink-soft);font-size:13px;line-height:1.5;margin:0 0 16px;max-width:640px;}
.mg-adminbar{background:#fef3c7;color:#92400e;border:1px solid #fde68a;border-radius:var(--r-sm);
  padding:9px 13px;font-size:13px;font-weight:600;margin-bottom:14px;}
.mg-note{margin:0 0 14px;padding:11px 14px;border-radius:var(--r-sm);font-size:13.5px;line-height:1.45;font-weight:600;}
.mg-note code{font-family:var(--ff-mono);font-size:12.5px;background:rgba(0,0,0,.06);padding:1px 6px;border-radius:5px;}
/* summary stat chips */
.mg-stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px;}
.mg-stat{flex:1 1 120px;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:12px 15px;}
.mg-stat b{display:block;font-size:24px;font-weight:800;letter-spacing:-.02em;color:var(--ink);font-family:var(--ff-mono);}
.mg-stat span{font-size:12px;color:var(--ink-mute);font-weight:600;}
.mg-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:16px 17px;margin-bottom:14px;}
.mg-card>h3{margin:0 0 3px;font-size:15px;font-weight:700;}
.mg-card>.mg-hint{margin:0 0 13px;font-size:12.5px;color:var(--ink-mute);line-height:1.45;}
/* team */
.mg-team{display:flex;flex-direction:column;gap:9px;}
.mg-sub{display:flex;align-items:center;gap:10px;flex-wrap:wrap;border:1px solid var(--line);border-radius:var(--r-sm);padding:11px 13px;}
.mg-sub.off{opacity:.55;}
.mg-sub .nm{font-weight:700;font-size:14.5px;}
.mg-sub .lg{font-family:var(--ff-mono);font-size:11.5px;color:var(--ink-mute);}
.mg-sub .ld{margin-left:auto;font-size:12.5px;color:var(--ink-soft);font-weight:600;background:var(--panel-2);
  border-radius:var(--r-full);padding:3px 11px;white-space:nowrap;}
.mg-empty{color:var(--ink-mute);font-size:13px;padding:6px 0;}
/* add-subordinate form */
.mg-add{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px 11px;align-items:end;margin-top:13px;}
.mg-add label{display:flex;flex-direction:column;gap:5px;font-size:12px;font-weight:600;color:var(--ink-soft);margin:0;}
.mg-add input{width:100%;}
.mg-add .go{grid-column:1/-1;justify-self:start;}
/* interview rows */
.mg-ivs{display:flex;flex-direction:column;gap:10px;}
.mg-iv{border:1px solid var(--line);border-radius:var(--r-sm);padding:12px 14px;}
.mg-iv-head{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;}
.mg-iv-cand{font-weight:700;font-size:14.5px;color:var(--ink);}
.mg-iv-co{font-size:12.5px;color:var(--ink-soft);}
.mg-iv-subj{margin:5px 0 0;font-size:12.5px;color:var(--ink-mute);line-height:1.45;
  overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;}
.mg-dot{display:inline-block;width:8px;height:8px;border-radius:50%;flex:0 0 auto;margin-right:2px;vertical-align:middle;}
.mg-dot.pool{background:#f59e0b;}
.mg-dot.assigned{background:#188038;}
.mg-iv-state{font-size:12px;font-weight:700;margin-top:8px;}
.mg-iv-state.assigned{color:#188038;}
.mg-iv-state.pool{color:#b45309;}
/* assign row */
.mg-assign{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px;padding-top:10px;border-top:1px solid var(--line);}
.mg-assign select,.mg-assign input[type=datetime-local]{padding:8px 10px;border:1px solid var(--line-strong);
  border-radius:8px;background:var(--panel);color:var(--ink);font-size:13.5px;min-height:var(--ctl-h);}
.mg-assign select{min-width:150px;flex:1 1 150px;}
.mg-assign input[type=datetime-local]{flex:1 1 170px;min-width:0;}
.mg-assign .btn{flex:0 0 auto;}
.mg-when{font-size:12.5px;color:var(--ink-soft);}
.mg-when b{color:var(--ink);}
@media(max-width:560px){.mg-assign select,.mg-assign input[type=datetime-local]{flex:1 1 100%;}
  .mg-assign .btn{flex:1 1 100%;}}
/* distribute tool */
.mg-dist{display:flex;align-items:center;gap:9px;flex-wrap:wrap;}
.mg-dist input{width:90px;padding:9px 10px;border:1px solid var(--line-strong);border-radius:8px;font-size:14px;}
"""


def _doc(body: str, title: str = "Портал управляющего") -> str:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        + mailcrm_ui._HEAD_PWA +
        f"<title>{escape(title)}</title>" + mailcrm_ui._FONTS +
        f"<style>{mailcrm_ui._CSS}{_MG_CSS}</style></head>"
        f"<body><main>{body}</main>" + mailcrm_ui._SW_REG + "</body></html>")


def _topbar(manager: dict, active: str = "portal", is_admin_view: bool = False) -> str:
    name = escape(manager.get("name") or manager.get("login") or "")
    role_lbl = "Просмотр как управляющий" if is_admin_view else "Управляющий"
    # admin read-through keeps the ?as= on the nav links so it stays scoped to this manager
    q = f"?as={manager['id']}" if is_admin_view else ""
    back = ('<a href="/users">← Пользователи</a>' if is_admin_view else "")
    nav = (back +
           f'<a class="{"active" if active=="portal" else ""}" href="/manage{q}">Портал</a>')
    if not is_admin_view:
        # a manager also attends interviews assigned to himself → his own cabinet
        nav += ('<a href="/cabinet">Мои собесы</a>'
                '<a href="/cabinet/availability">Расписание</a>'
                '<a href="/logout">Выход</a>')
    return (f'<div class="mg-top"><div class="brand">{mailcrm_ui._LOGO_IMG}</div>'
            f'<span class="who">{name}<small>{role_lbl}</small></span>'
            f'<nav class="mg-nav">{nav}</nav></div>')


def _note(notice) -> str:
    if not notice:
        return ""
    kind, text = notice
    style = {"ok": "color:#065f46;background:#d1fae5",
             "err": "color:#991b1b;background:#fee2e2",
             "pw": "color:#1e3a8a;background:#dbeafe"}.get(kind, "color:#374151;background:#f3f4f6")
    return f'<div class="mg-note" style="{style}">{text}</div>'


def _cand_label(iv: dict) -> str:
    """A human label for the interview's persona — the mailbox local-part."""
    mb = iv.get("mailbox") or ""
    return mb.split("@", 1)[0] if mb else "—"


def _fmt_local(dt, tz=None) -> str:
    if not dt:
        return ""
    try:
        z = tz or slots.DEFAULT_TZ
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return slots.to_local(dt, z).strftime("%d.%m %H:%M")
    except Exception:
        return ""


def _assign_select(iid: int, team: list[tuple], current_rid=None, as_id=None) -> str:
    """The interviewer <select> for one interview: the manager himself first (marked «я»),
    then his subordinates. `current_rid` preselects the assigned person on a reassign."""
    opts = []
    for rid, name, is_self in team:
        sel = " selected" if current_rid == rid else ""
        suffix = " · я" if is_self else ""
        opts.append(f'<option value="{rid}"{sel}>{escape(name)}{escape(suffix)}</option>')
    as_field = f'<input type="hidden" name="as" value="{as_id}">' if as_id else ""
    return (
        f'<form method="post" action="/manage/assign" class="mg-assign">'
        f'<input type="hidden" name="iid" value="{iid}">{as_field}'
        f'<select name="responsible_id" aria-label="Кому назначить">{"".join(opts)}</select>'
        '<input type="datetime-local" name="start_local" aria-label="Время (необязательно)">'
        '<button class="primary btn" type="submit">Назначить</button>'
        '</form>')


def _iv_card(iv: dict, team: list[tuple], names: dict, as_id=None) -> str:
    cand = escape(_cand_label(iv))
    company = escape(iv.get("company") or "")
    subject = escape((iv.get("notes") or "").strip())
    co = f' · {company}' if company else ""
    head = (f'<div class="mg-iv-head"><span class="mg-iv-cand">{cand}</span>'
            f'<span class="mg-iv-co">{co}</span></div>')
    subj = f'<div class="mg-iv-subj">{subject}</div>' if subject else ""
    assigned = iv.get("responsible_id")
    if assigned:
        who = escape(names.get(assigned) or "—")
        when = _fmt_local(iv.get("start_ts"))
        when_txt = f' · {escape(when)}' if when else ' · время не указано'
        state = (f'<div class="mg-iv-state assigned"><span class="mg-dot assigned"></span>'
                 f'Назначено: {who}{when_txt}</div>')
        # reassign (preselect current) — a separate «Вернуть в пул» form sits as a SIBLING
        # (never nested; nested <form> is invalid HTML).
        as_field = f'<input type="hidden" name="as" value="{as_id}">' if as_id else ""
        unassign = (f'<form method="post" action="/manage/unassign" class="mg-assign" '
                    'style="border-top:0;padding-top:0;margin-top:6px">'
                    f'<input type="hidden" name="iid" value="{iv["id"]}">{as_field}'
                    f'<button class="ghost btn" type="submit">Вернуть в пул</button></form>')
        assign = _assign_select(iv["id"], team, current_rid=assigned, as_id=as_id)
        return f'<div class="mg-iv">{head}{subj}{state}{assign}{unassign}</div>'
    state = ('<div class="mg-iv-state pool"><span class="mg-dot pool"></span>'
             'В пуле — не назначено</div>')
    assign = _assign_select(iv["id"], team, as_id=as_id)
    return f'<div class="mg-iv">{head}{subj}{state}{assign}</div>'


def portal_page(manager: dict, subs: list[dict], interviews: list[dict],
                loads: dict, names: dict, notice=None,
                is_admin_view: bool = False) -> str:
    as_id = manager["id"] if is_admin_view else None
    # team options for the assign dropdowns: the manager himself + his ACTIVE subordinates
    team: list[tuple] = [(manager["id"], manager.get("name") or manager.get("login") or "—", True)]
    for s in subs:
        if s.get("active"):
            team.append((s["id"], s.get("name") or s.get("login") or "—", False))

    n_assigned = sum(1 for iv in interviews if iv.get("responsible_id"))
    n_pool = len(interviews) - n_assigned

    # summary stats
    stats = (
        '<div class="mg-stats">'
        f'<div class="mg-stat"><b>{len(interviews)}</b><span>Всего собесов</span></div>'
        f'<div class="mg-stat"><b>{n_pool}</b><span>В пуле</span></div>'
        f'<div class="mg-stat"><b>{n_assigned}</b><span>Назначено</span></div>'
        f'<div class="mg-stat"><b>{len([s for s in subs if s.get("active")])}</b><span>В команде</span></div>'
        '</div>')

    # team card
    if subs:
        rows = []
        for s in subs:
            ld = loads.get(s["id"], 0)
            off = "" if s.get("active") else " off"
            rows.append(
                f'<div class="mg-sub{off}"><span class="nm">{escape(s.get("name") or "—")}</span>'
                f'<span class="lg">@{escape(s.get("login") or "")}</span>'
                f'<span class="ld">собесов: {ld}</span></div>')
        team_list = f'<div class="mg-team">{"".join(rows)}</div>'
    else:
        team_list = '<div class="mg-empty">Пока нет сотрудников — добавьте первого ниже.</div>'

    as_field = f'<input type="hidden" name="as" value="{as_id}">' if as_id else ""
    add_form = (
        '<form class="mg-add" method="post" action="/manage/subordinate/add">'
        f'{as_field}'
        '<label>Имя<input name="name" required placeholder="Иван Петров"></label>'
        '<label>Логин<input name="login" required placeholder="ivan" autocomplete="off"></label>'
        '<label>Пароль<input name="password" placeholder="(сгенерируется)" autocomplete="off"></label>'
        '<div class="go"><button class="primary" type="submit">Добавить сотрудника</button></div>'
        '</form>')

    # interviews
    if interviews:
        # unassigned (pool) first, then assigned
        pool_ivs = [iv for iv in interviews if not iv.get("responsible_id")]
        asgn_ivs = [iv for iv in interviews if iv.get("responsible_id")]
        cards = "".join(_iv_card(iv, team, names, as_id=as_id) for iv in pool_ivs + asgn_ivs)
        ivs_block = f'<div class="mg-ivs">{cards}</div>'
    else:
        ivs_block = ('<div class="mg-empty">Пока нет выделенных собеседований. '
                     'Их выделяет главный админ в разделе «Пользователи».</div>')

    # distribute tool (only if there IS a pool + a team to spread across)
    dist = ""
    if n_pool and len(team) > 1:
        dist = (
            '<div class="mg-card"><h3>Быстрое распределение</h3>'
            '<p class="mg-hint">Разложить все непризначенные собесы из пула поровну между вами и '
            'сотрудниками (по кругу). Время можно назначить позже.</p>'
            '<form method="post" action="/manage/distribute" class="mg-dist">'
            f'{as_field}'
            '<button class="primary" type="submit">Распределить поровну</button>'
            '</form></div>')

    admin_bar = ('<div class="mg-adminbar">Просмотр портала управляющего от имени '
                 f'<b>{escape(manager.get("name") or "")}</b> (режим главного админа).</div>'
                 if is_admin_view else "")

    body = (
        _topbar(manager, "portal", is_admin_view) +
        admin_bar +
        '<h1 class="mg-h1">Портал управляющего</h1>'
        '<p class="mg-lead">Выделенные вам собеседования, ваша команда и её загрузка. '
        'Назначайте собесы себе или сотрудникам — только из выделенного вам пула.</p>'
        + _note(notice) + stats +
        f'<div class="mg-card"><h3>Моя команда</h3>'
        '<p class="mg-hint">Сотрудники под вашим руководством. Вы можете назначать им '
        'собеседования и видеть их загрузку.</p>'
        + team_list + add_form + '</div>'
        + dist +
        '<div class="mg-card"><h3>Собеседования</h3>'
        '<p class="mg-hint">Пул выделенных вам собесов. Выберите исполнителя (себя или '
        'сотрудника) и, при желании, время.</p>'
        + ivs_block + '</div>')
    return _doc(body)
