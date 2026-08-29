"""Operator «Пользователи» tab — manage interview responsibles (the accounts we can
assign «Собес» to). ADMIN-ONLY: every /users route is non-allowlisted, so the
dashboard AdminAuthMiddleware already gates it. Renders inside the shared dashboard
shell (mailcrm_ui._page, active='users'). All times GMT/UTC.
"""
from __future__ import annotations

from html import escape

from backend.tools import mailcrm_ui

_DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

_CSS = """
<style>
.u-wrap{max-width:1040px;margin:16px auto;padding:0 14px}
.u-card{background:var(--card,#fff);border:1px solid rgba(0,0,0,.08);border-radius:12px;padding:18px 20px;margin-bottom:16px}
.u-card h2{margin:0 0 4px;font-size:20px}
.u-sub{color:#6b7280;font-size:13px;margin:0 0 16px}
.u-tbl{width:100%;border-collapse:collapse;font-size:14px}
.u-tbl th{text-align:left;color:#6b7280;font-weight:600;font-size:12px;padding:8px 10px;border-bottom:1px solid rgba(0,0,0,.08)}
.u-tbl td{padding:10px;border-bottom:1px solid rgba(0,0,0,.05);vertical-align:middle}
.u-tag{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600}
.u-form{display:flex;gap:10px;flex-wrap:wrap;align-items:end;padding:14px;background:rgba(0,0,0,.03);border-radius:10px;margin-bottom:18px}
.u-form label{font-size:12px;color:#374151;display:flex;flex-direction:column;gap:4px}
.u-form input,.u-form select{padding:7px 9px;border:1px solid rgba(0,0,0,.15);border-radius:8px;font-size:14px;min-width:150px}
.u-btn{display:inline-block;padding:7px 13px;border-radius:8px;border:1px solid rgba(0,0,0,.15);background:#fff;color:#111;font-size:13px;cursor:pointer;text-decoration:none;font-weight:600}
.u-btn.primary{background:#2563eb;border-color:#2563eb;color:#fff}
.u-btn.danger{background:#fef2f2;border-color:#fecaca;color:#b91c1c}
.u-btn.ghost{background:transparent}
.u-note{margin:0 0 14px;padding:10px 14px;border-radius:8px;font-size:14px}
.u-av{display:grid;grid-template-columns:46px 1fr 1fr;gap:8px 12px;align-items:center;max-width:420px}
.u-av input[type=time]{padding:6px 8px;border:1px solid rgba(0,0,0,.15);border-radius:7px}
.u-row{display:flex;gap:22px;flex-wrap:wrap}
.u-blk{flex:1;min-width:260px;padding:16px 0;border-top:1px solid rgba(0,0,0,.06)}
.u-blk h3{font-size:15px;margin:0 0 10px}
.u-back{color:#2563eb;text-decoration:none;font-size:14px}
</style>
"""


def _min_to_hhmm(m) -> str:
    m = int(m or 0)
    return f"{m // 60:02d}:{m % 60:02d}"


def _role_tag(role: str) -> str:
    if role == "admin":
        return '<span class="u-tag" style="color:#b45309;background:#fef3c7">админ</span>'
    return '<span class="u-tag" style="color:#3730a3;background:#e0e7ff">интервьюер</span>'


def _fmt_window(r: dict) -> str:
    """A weekday window as text. start==end is a full 24h window; end<start is an
    overnight window that crosses midnight (both are valid, so neither is hidden)."""
    s, e = int(r.get("start_min") or 0), int(r.get("end_min") or 0)
    if s == e:
        return "24 ч"
    label = f"{_min_to_hhmm(s)}–{_min_to_hhmm(e)}"
    return label + " (ночн.)" if e < s else label


def _avail_summary(av: list[dict]) -> str:
    days = [f"{_DOW[r['dow']]} {_fmt_window(r)}" for r in av if r.get("enabled")]
    if not days:
        return "<span style='color:#dc2626'>нет окон → нельзя назначить</span>"
    return " · ".join(escape(d) for d in days)


def _note(notice) -> str:
    if not notice:
        return ""
    kind, text = notice
    style = {"ok": "color:#065f46;background:#d1fae5",
             "err": "color:#991b1b;background:#fee2e2",
             "pw": "color:#1e3a8a;background:#dbeafe"}.get(kind, "color:#374151;background:#f3f4f6")
    return f'<div class="u-note" style="{style}">{text}</div>'


def list_page(users: list[dict], avail_by_id: dict, notice=None) -> str:
    rows = []
    for u in users:
        av = _avail_summary(avail_by_id.get(u["id"], []))
        active = u.get("active")
        st = "активен" if active else "<span style='color:#9ca3af'>отключён</span>"
        tg = "✓" if u.get("telegram_chat_id") else "—"
        rows.append(
            "<tr>"
            f"<td>{u['id']}</td>"
            f"<td><b>{escape(u.get('name') or '')}</b><br>"
            f"<span style='color:#6b7280'>{escape(u.get('login') or '')}</span></td>"
            f"<td>{_role_tag(u.get('role'))}</td>"
            f"<td style='font-size:13px'>{av}</td>"
            f"<td style='text-align:center'>{tg}</td>"
            f"<td>{st}</td>"
            f"<td><a class='u-btn' href='/users/{u['id']}'>Настроить</a></td>"
            "</tr>")
    tbody = "".join(rows) or ("<tr><td colspan='7' style='padding:22px;color:#9ca3af'>"
                              "Пока нет пользователей</td></tr>")
    body = (
        _CSS +
        "<div class='u-wrap'><div class='u-card'>"
        "<h2>Пользователи</h2>"
        "<p class='u-sub'>Ответственные, которым можно назначать интервью по кнопке «Собес». "
        "Они входят в кабинет и видят почту персоны только после назначения. "
        "Чтобы человека можно было назначить — задай ему доступность.</p>"
        + _note(notice) +
        "<form class='u-form' method='post' action='/users/add'>"
        "<label>Имя<input name='name' required placeholder='Иван Петров'></label>"
        "<label>Логин<input name='login' required placeholder='ivan' autocomplete='off'></label>"
        "<label>Пароль<input name='password' placeholder='(сгенерировать)' autocomplete='off'></label>"
        "<label>Роль<select name='role'>"
        "<option value='employee'>интервьюер</option><option value='admin'>админ</option>"
        "</select></label>"
        "<button class='u-btn primary' type='submit'>Добавить</button>"
        "</form>"
        "<table class='u-tbl'><thead><tr>"
        "<th>#</th><th>Пользователь</th><th>Роль</th><th>Доступность (GMT)</th>"
        "<th>TG</th><th>Статус</th><th></th></tr></thead>"
        f"<tbody>{tbody}</tbody></table>"
        "</div></div>")
    return mailcrm_ui._page("users", body)


def edit_page(u: dict, availability: list[dict], notice=None) -> str:
    rid = u["id"]
    role = u.get("role")
    active = u.get("active")

    # availability editor rows (7 weekdays)
    av_by_dow = {r["dow"]: r for r in availability}
    av_rows = []
    for d in range(7):
        r = av_by_dow.get(d, {"enabled": False, "start_min": 540, "end_min": 1080})
        en = "checked" if r.get("enabled") else ""
        start = _min_to_hhmm(r.get("start_min") or 540)
        end = _min_to_hhmm(r.get("end_min") or 1080)
        av_rows.append(
            f"<label style='justify-self:start'><input type='checkbox' name='en_{d}' {en}> {_DOW[d]}</label>"
            f"<input type='time' name='start_{d}' value='{start}'>"
            f"<input type='time' name='end_{d}' value='{end}'>")

    role_other = "employee" if role == "admin" else "admin"
    role_other_lbl = "интервьюер" if role == "admin" else "админ"
    toggle_lbl = "Отключить" if active else "Включить"
    toggle_val = "0" if active else "1"
    toggle_cls = "danger" if active else "primary"

    body = (
        _CSS +
        "<div class='u-wrap'>"
        "<p><a class='u-back' href='/users'>← Пользователи</a></p>"
        "<div class='u-card'>"
        f"<h2>{escape(u.get('name') or '')} &nbsp;{_role_tag(role)}</h2>"
        f"<p class='u-sub'>Логин <b>{escape(u.get('login') or '')}</b> · id {rid} · "
        f"{'активен' if active else 'отключён'} · вход в кабинет: cabinet.systeam.kz</p>"
        + _note(notice) +

        "<div class='u-row'>"
        # availability
        "<div class='u-blk' style='flex-basis:100%'>"
        "<h3>Доступность (GMT) — когда его можно назначить</h3>"
        f"<form method='post' action='/users/{rid}/availability'>"
        f"<div class='u-av'>{''.join(av_rows)}</div>"
        "<div style='margin-top:12px'><button class='u-btn primary' type='submit'>Сохранить доступность</button></div>"
        "</form></div>"

        # password
        "<div class='u-blk'>"
        "<h3>Пароль</h3>"
        f"<form method='post' action='/users/{rid}/passwd' style='display:flex;gap:8px;align-items:end'>"
        "<label style='font-size:12px;color:#374151'>Новый (пусто = сгенерировать)<br>"
        "<input name='password' placeholder='(сгенерировать)' autocomplete='off' "
        "style='padding:7px 9px;border:1px solid rgba(0,0,0,.15);border-radius:8px'></label>"
        "<button class='u-btn' type='submit'>Сбросить пароль</button></form></div>"

        # telegram
        "<div class='u-blk'>"
        "<h3>Telegram для напоминаний</h3>"
        f"<form method='post' action='/users/{rid}/telegram' style='display:flex;gap:8px;align-items:end'>"
        "<label style='font-size:12px;color:#374151'>chat_id (пусто = отвязать)<br>"
        f"<input name='chat_id' value='{u.get('telegram_chat_id') or ''}' autocomplete='off' "
        "style='padding:7px 9px;border:1px solid rgba(0,0,0,.15);border-radius:8px'></label>"
        "<button class='u-btn' type='submit'>Сохранить</button></form>"
        "<p class='u-sub' style='margin-top:6px'>Сначала он должен написать боту, иначе ЛС не дойдёт.</p></div>"

        # role + active
        "<div class='u-blk'>"
        "<h3>Роль и доступ</h3>"
        "<div style='display:flex;gap:8px;flex-wrap:wrap'>"
        f"<form method='post' action='/users/{rid}/role'>"
        f"<input type='hidden' name='role' value='{role_other}'>"
        f"<button class='u-btn ghost' type='submit'>Сделать: {role_other_lbl}</button></form>"
        f"<form method='post' action='/users/{rid}/active'>"
        f"<input type='hidden' name='active' value='{toggle_val}'>"
        f"<button class='u-btn {toggle_cls}' type='submit'>{toggle_lbl}</button></form>"
        "</div>"
        "<p class='u-sub' style='margin-top:6px'>Отключение мгновенно отзывает сессию в кабинете.</p></div>"

        "</div></div></div>")
    return mailcrm_ui._page("users", body)
