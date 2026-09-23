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
/* Telegram-connect prompt */
.mg-tg{display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:var(--accent-soft);
  border:1px solid var(--accent);border-radius:var(--r);padding:13px 15px;margin-bottom:16px;}
.mg-tg-txt{flex:1 1 220px;font-size:13px;color:var(--accent-deep);line-height:1.45;min-width:0;}
.mg-tg form{margin:0;flex:0 0 auto;}
@media(max-width:560px){.mg-tg form,.mg-tg form .btn{flex:1 1 100%;width:100%;}}
.mg-note{margin:0 0 14px;padding:11px 14px;border-radius:var(--r-sm);font-size:13.5px;line-height:1.45;font-weight:600;}
.mg-note code{font-family:var(--ff-mono);font-size:12.5px;background:rgba(0,0,0,.06);padding:1px 6px;border-radius:5px;}
/* summary stat chips */
.mg-stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px;}
.mg-stat{flex:1 1 120px;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:12px 15px;}
.mg-stat b{display:block;font-size:24px;font-weight:800;letter-spacing:-.02em;color:var(--ink);font-family:var(--ff-mono);}
.mg-of{font-size:13px;font-weight:600;color:var(--ink-mute);font-family:var(--ff);letter-spacing:0;}
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
.mg-quick{display:flex;align-items:center;gap:6px;margin:0;flex:0 0 auto;}
.mg-quick input{width:60px;padding:6px 8px;border:1px solid var(--line-strong);border-radius:7px;font-size:13px;}
@media(max-width:560px){.mg-sub .ld{margin-left:auto;}.mg-quick{flex:1 1 100%;margin-top:6px;}.mg-quick input{flex:1 1 auto;width:auto;}}
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
.mg-dist select{flex:1 1 160px;min-width:0;padding:9px 10px;border:1px solid var(--line-strong);border-radius:8px;background:var(--panel);color:var(--ink);font-size:13.5px;}
.mg-dist input{width:84px;padding:9px 10px;border:1px solid var(--line-strong);border-radius:8px;font-size:14px;}
@media(max-width:560px){.mg-dist select{flex:1 1 100%;}.mg-dist input{flex:1 1 100%;width:auto;}.mg-dist .btn{flex:1 1 100%;}}
/* gender/direction chips on interview cards */
.mg-chip{display:inline-flex;align-items:center;height:18px;padding:0 7px;border-radius:var(--r-full);font-size:10.5px;font-weight:700;line-height:1;}
.mg-chip.sx-female{color:#9d174d;background:#fce7f3;}
.mg-chip.sx-male{color:#1e40af;background:#dbeafe;}
.mg-chip.dir{color:#3730a3;background:#e0e7ff;}
/* pool filter row */
.mg-filter{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 12px;}
.mg-filter input,.mg-filter select{flex:1 1 140px;min-width:0;padding:9px 10px;border:1px solid var(--line-strong);border-radius:8px;background:var(--panel);color:var(--ink);font-size:13.5px;}
.mg-filter .hbtn{flex:0 0 auto;}
@media(max-width:560px){.mg-filter input,.mg-filter select{flex:1 1 100%;}.mg-filter .hbtn{flex:1 1 100%;}}
/* my-own queue rows */
.mg-own-foot{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:9px;}
.mg-own-act{margin:0;}
/* team-assigned collapsible */
.mg-details>summary{cursor:pointer;font-size:15px;font-weight:700;list-style:none;user-select:none;display:flex;width:100%;align-items:center;gap:7px;padding:8px 0;}
.mg-details>summary::before{content:'▸';color:var(--ink-mute);font-size:12px;}
.mg-details[open]>summary::before{content:'▾';}
.mg-teamlist{display:flex;flex-direction:column;gap:8px;margin-top:12px;}
.mg-teamrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;border:1px solid var(--line);border-radius:var(--r-sm);padding:9px 12px;}
.mg-tr-cand{font-weight:700;font-size:13.5px;display:inline-flex;align-items:center;gap:6px;}
.mg-tr-who{font-size:12.5px;color:var(--ink-soft);}
.mg-teamrow form{margin-left:auto;}
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
        # a manager also attends interviews assigned to himself → his own cabinet; the shared
        # «События найма» board is open to every role (admin/управляющий/интервьюер).
        nav += ('<a href="/cabinet">Мои собесы</a>'
                '<a href="/hiring-events">События найма</a>'
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


# gender / direction display + filter labels (neutral, no stack names)
_SEX_LBL = {"male": "М", "female": "Ж"}
_DIR_LBL = {"it": "IT", "nonit": "не-IT", "other": "другое"}
_GENDER_OPTS = (("", "Любой пол"), ("male", "Мужчины"), ("female", "Женщины"))
_DIR_OPTS = (("", "Любое направление"), ("it", "IT"), ("nonit", "Не-IT"), ("other", "Другое"))


def _sel(name: str, opts, current: str = "", aria: str = "") -> str:
    o = "".join(f"<option value='{v}'{' selected' if v == (current or '') else ''}>{escape(lbl)}</option>"
                for v, lbl in opts)
    a = f" aria-label='{escape(aria)}'" if aria else ""
    return f"<select name='{name}'{a}>{o}</select>"


def _chips(iv: dict) -> str:
    """Small gender + direction chips for an interview card."""
    out = []
    sx = _SEX_LBL.get(iv.get("sex"))
    if sx:
        out.append(f'<span class="mg-chip sx-{iv.get("sex")}">{sx}</span>')
    di = iv.get("direction")
    if di and di != "other":
        out.append(f'<span class="mg-chip dir">{_DIR_LBL.get(di, di)}</span>')
    return "".join(out)


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


def _own_card(iv: dict, team: list[tuple], as_id=None) -> str:
    """One of the manager's OWN собесы (he still attends it) — not yet handed to the team. He can
    proceed to hold it himself, schedule a time, or hand it DOWN to a subordinate via the select
    (auto-own model: everything allocated to him is his until he redistributes it)."""
    cand = escape(_cand_label(iv))
    company = escape(iv.get("company") or "")
    co = f' · {company}' if company else ""
    subject = escape((iv.get("notes") or "").strip())
    head = (f'<div class="mg-iv-head"><span class="mg-iv-cand">{cand}</span>'
            f'<span class="mg-iv-co">{co}</span>{_chips(iv)}</div>')
    subj = f'<div class="mg-iv-subj">{subject}</div>' if subject else ""
    when = _fmt_local(iv.get("start_ts"))
    when_txt = f' · {escape(when)}' if when else ""
    state = (f'<div class="mg-iv-state assigned"><span class="mg-dot assigned"></span>'
             f'Ваш — проведёте сами{when_txt} или раздайте сотруднику</div>')
    h = iv.get("source_message_hash")
    foot = (f'<div class="mg-own-foot"><a href="/cabinet/thread?'
            f'hash={escape(str(h), quote=True)}">Переписка</a></div>' if h else "")
    # preselect the manager himself → the default is «оставить себе»; picking a subordinate hands
    # it down. Reuses /manage/assign (isolation re-checked server-side).
    assign = _assign_select(iv["id"], team, current_rid=iv.get("responsible_id"), as_id=as_id)
    return f'<div class="mg-iv">{head}{subj}{state}{assign}{foot}</div>'


def _team_card(iv: dict, names: dict, as_id=None) -> str:
    """A row for a собес handed DOWN to a SUBORDINATE, with a per-row «← забрать» (pull it back to
    the manager himself via /manage/unassign)."""
    cand = escape(_cand_label(iv))
    who = escape(names.get(iv.get("responsible_id")) or "—")
    when = _fmt_local(iv.get("start_ts"))
    when_txt = f' · {escape(when)}' if when else ""
    as_field = f'<input type="hidden" name="as" value="{as_id}">' if as_id else ""
    ret = (f'<form method="post" action="/manage/unassign" class="mg-own-act">'
           f'<input type="hidden" name="iid" value="{iv["id"]}">{as_field}'
           f'<button class="ghost btn" type="submit">← Забрать себе</button></form>')
    return (f'<div class="mg-teamrow"><span class="mg-tr-cand">{cand}{_chips(iv)}</span>'
            f'<span class="mg-tr-who">→ {who}{when_txt}</span>{ret}</div>')


def _filter_form(q: str, gender: str, direction: str, as_id=None) -> str:
    """Email-search + gender + direction filter over the managed pool (GET, preserves ?as=)."""
    from backend.interviews import pool as iv_pool
    as_field = f'<input type="hidden" name="as" value="{as_id}">' if as_id else ""
    return (
        '<form class="mg-filter" method="get" action="/manage">'
        f'{as_field}'
        f'<input name="q" value="{escape(q or "", quote=True)}" autocomplete="off" '
        'placeholder="Поиск по e-mail" aria-label="Поиск по e-mail">'
        + _sel("gender", _GENDER_OPTS, gender, "Пол")
        + _sel("direction", _DIR_OPTS, direction, "Направление")
        + iv_pool.direction_legend_html("Что означает направление") +
        '<button class="hbtn" type="submit">Фильтр</button>'
        '</form>')


def _tg_banner(tg_missing: bool) -> str:
    """A prominent prompt to link Telegram (only in a manager's OWN view — see routes). Reuses
    the existing cabinet route POST /cabinet/tg/connect, which mints the deep link + redirects."""
    if not tg_missing:
        return ""
    return (
        '<div class="mg-tg"><div class="mg-tg-txt">'
        '<b>Подключите Telegram</b> — бот заранее напомнит о ваших предстоящих собеседованиях '
        'и пришлёт материалы кандидата.</div>'
        '<form method="post" action="/cabinet/tg/connect">'
        '<button class="primary btn" type="submit">Подключить Telegram</button></form></div>')


def portal_page(manager: dict, subs: list[dict], own_ivs: list[dict],
                team_ivs: list[dict], loads: dict, names: dict, counts: dict,
                q: str = "", gender: str = "", direction: str = "", notice=None,
                is_admin_view: bool = False, pool_all: list[dict] | None = None,
                scope_ivs: list[dict] | None = None, pool_sort: str = "salary",
                sort_base: str = "/manage", tg_missing: bool = False) -> str:
    """The manager portal under the AUTO-OWN model: `own_ivs` = his собесы he still attends
    himself (FILTERED — the distributable set), `team_ivs` = собесы handed down to his team.
    `pool_all` = his FULL own set for the priority card; `scope_ivs` = everything allocated to him."""
    as_id = manager["id"] if is_admin_view else None
    # team options for the assign dropdowns: the manager himself + his ACTIVE subordinates
    team: list[tuple] = [(manager["id"], manager.get("name") or manager.get("login") or "—", True)]
    for s in subs:
        if s.get("active"):
            team.append((s["id"], s.get("name") or s.get("login") or "—", False))
    active_subs = [(s["id"], s.get("name") or s.get("login") or "—") for s in subs if s.get("active")]
    as_field = f'<input type="hidden" name="as" value="{as_id}">' if as_id else ""

    # «Мои — не розданы» shows the FILTERED count («2 из 18») when a filter narrows the list, so the
    # gap between the stat and the visible cards never reads as "where did the rest go?".
    own_total = counts.get("own", 0)
    own_shown = counts.get("own_shown", own_total)
    own_stat = (f'{own_shown} <span class="mg-of">из {own_total}</span>'
                if counts.get("filtered") and own_shown != own_total else str(own_total))
    stats = (
        '<div class="mg-stats">'
        f'<div class="mg-stat"><b>{own_stat}</b><span>Мои — не розданы</span></div>'
        f'<div class="mg-stat"><b>{counts.get("team", 0)}</b><span>Роздано команде</span></div>'
        f'<div class="mg-stat"><b>{counts.get("total", 0)}</b><span>Всего выделено</span></div>'
        f'<div class="mg-stat"><b>{counts.get("team_size", 0)}</b><span>В команде</span></div>'
        '</div>')

    # team card — each active subordinate shows load + a «докинуть» quick action (hand N of my own
    # собесы, honouring the current filter, straight to that person via /manage/distribute_to).
    if subs:
        rows = []
        for s in subs:
            ld = loads.get(s["id"], 0)
            off = "" if s.get("active") else " off"
            quick = ""
            if s.get("active"):
                quick = (
                    f'<form method="post" action="/manage/distribute_to" class="mg-quick">'
                    f'{as_field}<input type="hidden" name="member_id" value="{s["id"]}">'
                    f'<input type="hidden" name="gender" value="{escape(gender or "", quote=True)}">'
                    f'<input type="hidden" name="direction" value="{escape(direction or "", quote=True)}">'
                    '<input type="number" name="count" min="1" step="1" value="1" '
                    'inputmode="numeric" aria-label="Сколько докинуть">'
                    '<button class="hbtn btn" type="submit">Докинуть</button></form>')
            rows.append(
                f'<div class="mg-sub{off}"><span class="nm">{escape(s.get("name") or "—")}</span>'
                f'<span class="lg">@{escape(s.get("login") or "")}</span>'
                f'<span class="ld">собесов: {ld}</span>{quick}</div>')
        team_list = f'<div class="mg-team">{"".join(rows)}</div>'
    else:
        team_list = '<div class="mg-empty">Пока нет сотрудников — добавьте первого ниже.</div>'

    add_form = (
        '<form class="mg-add" method="post" action="/manage/subordinate/add">'
        f'{as_field}'
        '<label>Имя<input name="name" required placeholder="Иван Петров"></label>'
        '<label>Логин<input name="login" required placeholder="ivan" autocomplete="off"></label>'
        '<label>Пароль<input name="password" placeholder="(сгенерируется)" autocomplete="off"></label>'
        '<div class="go"><button class="primary" type="submit">Добавить сотрудника</button></div>'
        '</form>')

    g_hidden = (f'<input type="hidden" name="gender" value="{escape(gender or "", quote=True)}">'
                f'<input type="hidden" name="direction" value="{escape(direction or "", quote=True)}">')
    # bulk hand-down: N of my own собесы (matching the filter) → a chosen member (self or subordinate)
    member_opts = "".join(
        f'<option value="{rid}">{escape(name)}{" · я" if is_self else ""}</option>'
        for rid, name, is_self in team)
    # bulk reclaim: N of a SUBORDINATE's собесы (matching the filter) back to me
    sub_opts = "".join(f'<option value="{rid}">{escape(name)}</option>' for rid, name in active_subs)
    reclaim_form = (
        '<form method="post" action="/manage/reclaim_from" class="mg-dist" style="margin-top:10px">'
        f'{as_field}{g_hidden}'
        f'<select name="member_id" aria-label="У кого забрать">{sub_opts}</select>'
        '<input type="number" name="count" min="1" step="1" value="1" inputmode="numeric" aria-label="Сколько забрать">'
        '<button class="hbtn btn" type="submit">Забрать</button>'
        '</form>') if active_subs else ""
    dist = (
        '<div class="mg-card"><h3>Раздать / забрать у команды</h3>'
        '<p class="mg-hint">Раздайте N своих собесов конкретному сотруднику (по текущему фильтру '
        'пола/направления ниже) — они уйдут ему; или заберите их обратно себе. Либо распределите '
        'все свои нераспределённые собесы поровну между вами и командой одной кнопкой.</p>'
        '<form method="post" action="/manage/distribute_to" class="mg-dist">'
        f'{as_field}{g_hidden}'
        f'<select name="member_id" aria-label="Кому">{member_opts}</select>'
        '<input type="number" name="count" min="1" step="1" value="1" inputmode="numeric" aria-label="Сколько">'
        '<button class="primary btn" type="submit">Раздать</button>'
        '</form>'
        + reclaim_form +
        # one-tap round-robin of the manager's OWN set across himself + his active team (no time set,
        # ignores the gender/direction filter) → POST /manage/distribute.
        '<form method="post" action="/manage/distribute" class="mg-dist" style="margin-top:10px" '
        'onsubmit="return confirm(\'Распределить ваши нераспределённые собесы поровну между вами и сотрудниками?\');">'
        f'{as_field}'
        '<button class="hbtn btn" type="submit">Распределить всё поровну</button>'
        '</form></div>')

    # SECTION A — my собесы, not yet handed to the team (filtered), each with a hand-down control
    if own_ivs:
        own_cards = "".join(_own_card(iv, team, as_id=as_id) for iv in own_ivs)
        own_block = f'<div class="mg-ivs">{own_cards}</div>'
    elif counts.get("own", 0):
        own_block = '<div class="mg-empty">По этому фильтру собесов нет — измените фильтр.</div>'
    else:
        own_block = ('<div class="mg-empty">Пока ничего не выделено. Собеседования выделяет главный '
                     'админ в разделе «Пользователи» — и они сразу становятся вашими.</div>')

    # SECTION B — handed down to the team, each with a per-row «← Забрать себе»
    if team_ivs:
        team_rows = "".join(_team_card(iv, names, as_id) for iv in team_ivs)
        team_block = f'<div class="mg-teamlist">{team_rows}</div>'
    else:
        team_block = ('<div class="mg-empty">Вы ещё ничего не раздали команде. Раздайте собесы '
                      'сотрудникам выше — до тех пор они числятся за вами.</div>')

    admin_bar = ('<div class="mg-adminbar">Просмотр портала управляющего от имени '
                 f'<b>{escape(manager.get("name") or "")}</b> (режим главного админа).</div>'
                 if is_admin_view else "")

    # priority + «актуальные предстоящие» cards (the SAME urgency/priority signal as the «Собес»
    # screen). Priority card = the manager's own not-yet-distributed set, split IT/non-IT + sortable;
    # upcoming card = every live (non-expired) собес in his scope with a status chip.
    from backend.interviews import db as iv_db, priority_ui
    pool_all = pool_all if pool_all is not None else own_ivs
    scope_ivs = scope_ivs if scope_ivs is not None else (own_ivs + team_ivs)
    mid = manager["id"]
    # per-user colour (owner: «пусть у каждого пользователя свой цвет») — the manager himself +
    # his subordinates are the only owners of his scope rows; both carry an explicit/palette colour.
    colors: dict = {}
    try:
        colors[mid] = iv_db.color_for(manager)
        for s in subs:
            colors[s["id"]] = iv_db.color_for(s)
    except Exception:
        colors = {}

    def _mg_status(iv: dict) -> str:
        rid = iv.get("responsible_id")
        if not rid:
            return priority_ui.status_free()
        if rid == mid:
            return priority_ui.status_manager(names.get(mid) or "—", color=colors.get(mid))  # мой — провожу сам
        return priority_ui.status_assigned(names.get(rid) or "—", color=colors.get(rid))      # роздан сотруднику

    priority_card = priority_ui.priority_card(
        pool_all, pool_sort, sort_base, anchor="mg-pri",
        title="Приоритет: что раздать/провести первым",
        blurb=("Ваши нераспределённые собеседования по приоритету — сложные (IT) и простые (не‑IT), "
               "по зарплате, срочности брони слота или давности заявки. Разбирайте сверху вниз."),
        empty="Нераспределённых собесов нет.")
    upcoming_card = priority_ui.upcoming_list(
        scope_ivs, anchor="mg-live", status_of=_mg_status,
        title="Актуальные предстоящие собеседования",
        blurb=("Все ваши живые собесы (свои + розданные команде), у которых срок брони ещё не истёк, "
               "— от самых срочных. Явно просроченные — в «Истёкшие»."),
        empty="Актуальных предстоящих собеседований у вас нет.")

    body = (
        priority_ui.CSS +
        _topbar(manager, "portal", is_admin_view) +
        admin_bar +
        _tg_banner(tg_missing) +
        '<h1 class="mg-h1">Портал управляющего</h1>'
        '<p class="mg-lead">Выделенные вам собеседования сразу числятся за вами. Что не раздадите '
        'команде — проводите сами; остальное распределяйте между сотрудниками.</p>'
        + _note(notice) + stats + upcoming_card +
        '<div class="mg-card"><h3>Моя команда</h3>'
        '<p class="mg-hint">Сотрудники под вашим руководством. Раздавайте им собесы, забирайте '
        'обратно и видьте их загрузку.</p>'
        + team_list + add_form + '</div>'
        + dist +
        # Section A — my own, not yet distributed
        '<div class="mg-card"><h3>Мои собеседования — не розданы команде</h3>'
        '<p class="mg-hint">Собесы, которые пока за вами. Проведёте сами или раздайте сотруднику. '
        'Найдите нужный по e-mail, отфильтруйте по полу/направлению.</p>'
        + _filter_form(q, gender, direction, as_id)
        + own_block + '</div>'
        + priority_card +
        # Section B — handed to the team
        '<div class="mg-card"><h3>Роздано команде</h3>'
        '<p class="mg-hint">Собесы, которые проводят ваши сотрудники. Любой можно забрать себе.</p>'
        + team_block + '</div>')
    return _doc(body)
