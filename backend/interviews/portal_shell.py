"""Shared LEFT-MENU shell for the NON-ADMIN user portals — the manager «Команда» portal,
the interviewer cabinet (Главная / Кандидаты / Расписание) and the shared «События найма»
board. One consistent chrome for every role, mirroring the admin rail's LOOK by reusing
`mailcrm_ui._CSS` classes (`.sidebar`/`.nav`/`.side-foot`/`.gm-topbar`/`.gm-drawer`/`.layout`),
but the nav is the USER's OWN role-scoped set — an interviewer or manager NEVER sees the admin
sections (Кандидаты-CRM / Вакансии / Статистика / Пользователи / Health). This is the fix for
the leak where /hiring-events rendered the admin rail to everyone.

Admin read-through: pass `as_id` and every nav link (except the global «События найма» + logout)
carries `?as=<id>` so the admin stays scoped to the viewed user; an amber «режим администратора»
strip + a «← Пользователи» exit are shown.

Neutral Russian throughout; no stack names, no decorative emoji beyond quiet nav icons.
"""
from __future__ import annotations

from html import escape

from backend.tools import mailcrm_ui

# --- icons (reuse the admin set where it fits; a few new ones for the user nav) -------------
_IC_HOME = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
            'stroke-linecap="round" stroke-linejoin="round"><path d="M3 9.5 12 3l9 6.5"/>'
            '<path d="M5 10v10h14V10"/><path d="M9 20v-6h6v6"/></svg>')
_IC_CAL = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
           'stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/>'
           '<line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/>'
           '<line x1="3" y1="10" x2="21" y2="10"/></svg>')
_IC_GUIDE = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
             'stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>'
             '<path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>')
_IC_ADMIN = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
             'stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/>'
             '<rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>'
             '<rect x="3" y="14" width="7" height="7" rx="1"/></svg>')


def _role_label(roles: list[str]) -> str:
    is_mgr = "manager" in (roles or [])
    is_emp = "employee" in (roles or [])
    if is_mgr and is_emp:
        return "Управляющий · интервьюер"
    if is_mgr:
        return "Управляющий"
    return "Интервьюер"


def _nav_items(roles: list[str]):
    """(key, label, icon, path, global?) — role-scoped. `global` links (События найма) are NOT
    ?as-threaded. «Команда» only for managers."""
    # «Кандидаты» is MERGED into «Главная» → «Мои собеседования» (search + colours + the full
    # clickable inbox via «Переписка»), so it is no longer a separate nav item (owner 2026-09-26).
    items = [
        ("home", "Главная", _IC_HOME, "/cabinet", False),
    ]
    if "manager" in (roles or []):
        items.append(("team", "Команда", mailcrm_ui._IC_USERS, "/manage", False))
    items += [
        ("schedule", "Расписание", _IC_CAL, "/cabinet/availability", False),
        # ?ctx=user → the shared «События найма» page KEEPS this user menu (never flips to the
        # admin rail) even for a multi-role admin who is navigating the USER portal.
        ("hiring", "События найма", mailcrm_ui._IC_HIRING, "/hiring-events?ctx=user", True),
        ("guide", "Инструкции", _IC_GUIDE, "/cabinet/guide", False),
    ]
    # a multi-role ADMIN who lives in the user portal needs a way back to the full admin
    # dashboard (the user menu deliberately shows NO other admin sections).
    if "admin" in (roles or []):
        items.append(("adminhome", "Админ-панель", _IC_ADMIN, "/", True))
    return items


def _href(path: str, as_id, is_global: bool) -> str:
    if not as_id or is_global:
        return path
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}as={as_id}"


def _nav_links(active: str, roles: list[str], as_id) -> str:
    out = []
    for key, label, icon, path, is_global in _nav_items(roles):
        cls = "active" if active == key else ""
        out.append(f'<a class="{cls}" href="{escape(_href(path, as_id, is_global), quote=True)}">'
                   f'{icon}<span>{escape(label)}</span></a>')
    return "".join(out)


def _sidebar(active: str, roles: list[str], role_label: str, as_id, name: str) -> str:
    who = (f'<span class="side-who" title="{escape(name)}">{escape(name)}</span>'
           if name else "")
    return (f'<aside class="sidebar"><div class="brand">{mailcrm_ui._LOGO_IMG}</div>'
            f'<div class="nav">{_nav_links(active, roles, as_id)}</div>'
            f'<div class="side-foot">{who}<span class="side-role">{escape(role_label)}</span>'
            f'<a class="side-logout" href="/logout" title="Выйти">{mailcrm_ui._IC_LOGOUT}'
            '<span>Выход</span></a></div></aside>')


def _topbar(active: str, title: str) -> str:
    burger = ('<button type="button" class="gm-burger" aria-label="Меню" onclick="psDrawer(true)">'
              '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
              'stroke-linecap="round"><path d="M3 6h18M3 12h18M3 18h18"/></svg></button>')
    return (f'<div class="gm-topbar"><div class="gm-pill">{burger}'
            f'<span class="gm-title">{escape(title)}</span>'
            f'<span class="gm-ava">{mailcrm_ui._LOGO_IMG}</span></div></div>')


def _drawer(active: str, roles: list[str], role_label: str, as_id, name: str) -> str:
    who = f'<span class="gm-drawer-role">{escape(name)}</span>' if name else ""
    return ('<div class="gm-scrim" onclick="psDrawer(false)"></div>'
            '<aside class="gm-drawer"><div class="gm-drawer-head">'
            f'<span class="brand">{mailcrm_ui._LOGO_IMG}</span><b>{escape(name) or "JobFinder"}</b>'
            f'<span class="gm-drawer-role">{escape(role_label)}</span></div>'
            f'<nav class="gm-drawer-nav">{_nav_links(active, roles, as_id)}</nav>'
            '<div class="gm-drawer-foot">'
            f'<a class="gm-logout" href="/logout">{mailcrm_ui._IC_LOGOUT}<span>Выйти</span></a>'
            '</div></aside>')


# a self-contained drawer toggle (we deliberately do NOT pull in mailcrm_ui._JS — that is the
# admin jfSwap engine tied to admin routes). Idempotent + safe on every user page.
_DRAWER_JS = ("<script>window.psDrawer=function(o){"
              "var d=document.querySelector('.gm-drawer'),s=document.querySelector('.gm-scrim');"
              "if(!d||!s)return;if(o){d.classList.add('open');s.classList.add('open');"
              "document.body.style.overflow='hidden';}else{d.classList.remove('open');"
              "s.classList.remove('open');document.body.style.overflow='';}};"
              "document.addEventListener('keydown',function(e){if(e.key==='Escape')psDrawer(false);});"
              "</script>")

# small shell-only CSS: the admin-view banner + a comfortable content column inside the rail.
_SHELL_CSS = """
.ps-adminbar{background:#fef3c7;color:#92400e;border:1px solid #fde68a;border-radius:var(--r-sm);
  padding:9px 14px;font-size:13px;font-weight:600;margin:0 0 16px;display:flex;align-items:center;
  gap:10px;flex-wrap:wrap;}
.ps-adminbar a{margin-left:auto;}
.ps-main{max-width:960px;}
@media(max-width:760px){.ps-main{max-width:100%;}}
.side-who{font-size:9.5px;font-weight:700;color:var(--ink);text-align:center;line-height:1.15;
  max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:0 3px;}
"""


def admin_banner(name: str, as_id) -> str:
    if not as_id:
        return ""
    return ('<div class="ps-adminbar"><span>Просмотр портала пользователя: '
            f'<b>{escape(name or "")}</b> (режим администратора)</span>'
            '<a class="hbtn" href="/users">← К пользователям</a></div>')


def shell(*, active: str, roles: list[str], name: str, body: str, title: str,
          as_id=None, extra_head: str = "") -> str:
    """Full user-portal document: left rail (desktop) + ☰ drawer (mobile) + <main>.
    `active` ∈ {home,candidates,team,schedule,hiring,guide}; `roles` = the acting user's role set;
    `body` is the page's inner HTML (the caller supplies its page-specific CSS via `extra_head`)."""
    role_label = _role_label(roles)
    if as_id:
        role_label = "Режим администратора"
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        + mailcrm_ui._HEAD_PWA +
        f"<title>{escape(title)}</title>" + mailcrm_ui._FONTS +
        f"<style>{mailcrm_ui._CSS}{_SHELL_CSS}</style>{extra_head}</head><body>"
        f"{_topbar(active, title)}{_drawer(active, roles, role_label, as_id, name)}"
        f"<div class='layout'>{_sidebar(active, roles, role_label, as_id, name)}"
        f"<main class='ps-main'>{body}</main></div>"
        + _DRAWER_JS + mailcrm_ui._SW_REG + "</body></html>")
