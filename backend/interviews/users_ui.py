"""Operator «Пользователи» tab — manage interview responsibles (the accounts we can
assign «Собес» to). ADMIN-ONLY: every /users route is non-allowlisted, so the
dashboard AdminAuthMiddleware already gates it. Renders inside the shared dashboard
shell (mailcrm_ui._page, active='users') and uses its design tokens (var(--panel)/
--line/--accent/--ink*/--r) so it matches the other tabs and works on a phone
(cards instead of a wide table, stacked forms, full-width inputs). Each member's availability is
shown/labelled in THAT member's own timezone (iv_responsibles.tz).
"""
from __future__ import annotations

from html import escape

from backend.interviews import slots
from backend.tools import mailcrm_ui

_DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_DOW_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

_CSS = """
<style>
.u-wrap{max-width:940px;margin:0 auto}
.u-top{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap;margin:2px 0 4px}
.u-h1{font-size:26px;font-weight:800;letter-spacing:-.02em;margin:0;line-height:1.1}
.u-h1 b{font-family:var(--ff-mono);font-size:13px;font-weight:400;color:var(--ink-mute);margin-left:9px}
.u-lead{color:var(--ink-soft);font-size:13.5px;line-height:1.5;margin:6px 0 16px;max-width:660px}
.u-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;margin-bottom:14px}
.u-card>h3{margin:0 0 4px;font-size:15px;font-weight:700}
.u-card>.u-chint{margin:0 0 14px;font-size:12.5px;color:var(--ink-mute)}
.u-note{margin:0 0 14px;padding:11px 14px;border-radius:var(--r-sm);font-size:13.5px;line-height:1.45}
.u-note code{font-family:var(--ff-mono);font-size:12.5px;background:rgba(0,0,0,.06);padding:1px 6px;border-radius:5px}
/* add-user form */
.u-add{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:11px 12px;align-items:end}
.u-add label{display:flex;flex-direction:column;gap:5px;font-size:12px;font-weight:600;color:var(--ink-soft);margin:0}
.u-add input,.u-add select{width:100%}
.u-add .u-go{grid-column:1/-1;justify-self:start}
/* user cards */
.u-list{display:flex;flex-direction:column;gap:10px}
.u-user{border:1px solid var(--line);border-radius:var(--r);padding:13px 15px;background:var(--panel);transition:border-color .15s}
.u-user:hover{border-color:var(--line-strong)}
.u-user.off{opacity:.6}
.u-utop{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.u-name{font-size:15.5px;font-weight:700}
.u-login{font-family:var(--ff-mono);font-size:12px;color:var(--ink-mute)}
.u-utop .u-spacer{margin-left:auto}
.u-tag{display:inline-flex;align-items:center;padding:2px 9px;border-radius:var(--r-full);font-size:11.5px;font-weight:600;white-space:nowrap}
.u-av{margin-top:9px;font-size:13px;color:var(--ink-soft);line-height:1.55}
.u-av .k{color:var(--ink-mute);font-weight:600;margin-right:5px}
.u-av .none{color:var(--danger);font-weight:600}
.u-empty{padding:26px 8px;text-align:center;color:var(--ink-mute)}
/* edit page */
.u-back{display:inline-flex;align-items:center;gap:6px;color:var(--accent);font-size:13.5px;font-weight:600;margin:0 0 12px}
.u-eh{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 3px}
.u-eh h2{font-size:20px;margin:0}
.u-sub{color:var(--ink-soft);font-size:13px;margin:0 0 16px}
.u-sub code{font-family:var(--ff-mono)}
.u-grid{display:grid;grid-template-columns:1fr;gap:14px}
@media(min-width:720px){.u-grid{grid-template-columns:1fr 1fr}.u-grid .u-span{grid-column:1/-1}}
/* availability editor */
.u-days{display:flex;flex-direction:column;gap:9px}
.u-day{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
.u-daychk{display:flex;align-items:center;gap:8px;min-width:100px;font-weight:600;font-size:14px;margin:0;cursor:pointer;user-select:none}
.u-daychk input{width:18px;height:18px;flex:0 0 auto}
.u-times{display:flex;align-items:center;gap:8px;flex:1 1 220px;min-width:0}
.u-times input[type=time]{flex:1;min-width:0;text-align:center}
.u-times .sep{color:var(--ink-mute);flex:0 0 auto}
.u-hint{font-size:12px;color:var(--ink-mute);margin:12px 0 0;line-height:1.55}
.u-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;align-items:center}
/* settings blocks */
.u-set label{font-size:12px;font-weight:600;color:var(--ink-soft);display:block;margin:0 0 6px}
.u-set input{width:100%;margin-bottom:9px}
.u-rolebtns{display:flex;gap:8px;flex-wrap:wrap}
@media(max-width:760px){.u-h1{font-size:23px}}
</style>
"""


def _min_to_hhmm(m) -> str:
    m = int(m or 0)
    return f"{m // 60:02d}:{m % 60:02d}"


def _role_tag(role: str) -> str:
    if role == "admin":
        return '<span class="u-tag" style="color:#b45309;background:#fef3c7">админ</span>'
    return '<span class="u-tag" style="color:#3730a3;background:#e0e7ff">интервьюер</span>'


def _status_tag(active) -> str:
    if active:
        return '<span class="u-tag" style="color:#166534;background:#dcfce7">активен</span>'
    return '<span class="u-tag" style="color:#6b7280;background:#f1f3f4">отключён</span>'


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
        return "<span class='none'>нет окон → нельзя назначить</span>"
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
    cards = []
    for u in users:
        av = _avail_summary(avail_by_id.get(u["id"], []))
        tg = ('<span class="u-tag" style="color:#1e40af;background:#dbeafe">TG ✓</span>'
              if u.get("telegram_chat_id") else "")
        cards.append(
            f"<div class='u-user{'' if u.get('active') else ' off'}'>"
            "<div class='u-utop'>"
            f"<span class='u-name'>{escape(u.get('name') or '—')}</span>"
            f"<span class='u-login'>@{escape(u.get('login') or '')}</span>"
            f"{_role_tag(u.get('role'))}{_status_tag(u.get('active'))}{tg}"
            f"<span class='u-spacer'></span>"
            f"<a class='hbtn' href='/users/{u['id']}'>Настроить</a>"
            "</div>"
            f"<div class='u-av'><span class='k'>Доступность ({escape(slots.tz_label(u.get('tz')))}):</span>{av}</div>"
            "</div>")
    listing = ("<div class='u-list'>" + "".join(cards) + "</div>") if cards else (
        "<div class='u-empty'>Пока нет пользователей — добавьте первого выше.</div>")

    body = (
        _CSS +
        "<div class='u-wrap'>"
        "<div class='u-top'><h1 class='u-h1'>Пользователи"
        f"<b>{len(users)}</b></h1></div>"
        "<p class='u-lead'>Ответственные, которым можно назначать интервью по кнопке «Собес». "
        "Они входят в кабинет и видят почту персоны только после назначения. "
        "Чтобы человека можно было назначить — задайте ему доступность.</p>"
        + _note(notice) +

        "<div class='u-card'><h3>Добавить пользователя</h3>"
        "<form class='u-add' method='post' action='/users/add'>"
        "<label>Имя<input name='name' required placeholder='Иван Петров'></label>"
        "<label>Логин<input name='login' required placeholder='ivan' autocomplete='off'></label>"
        "<label>Пароль<input name='password' placeholder='(сгенерируется)' autocomplete='off'></label>"
        "<label>Роль<select name='role'>"
        "<option value='employee'>интервьюер</option><option value='admin'>админ</option>"
        "</select></label>"
        "<div class='u-go'><button class='primary' type='submit'>Добавить</button></div>"
        "</form></div>"

        + listing +
        "</div>")
    return mailcrm_ui._page("users", body)


def edit_page(u: dict, availability: list[dict], notice=None) -> str:
    rid = u["id"]
    role = u.get("role")
    active = u.get("active")

    av_by_dow = {r["dow"]: r for r in availability}
    day_rows = []
    for d in range(7):
        r = av_by_dow.get(d, {"enabled": False, "start_min": 540, "end_min": 1080})
        en = "checked" if r.get("enabled") else ""
        start = _min_to_hhmm(r.get("start_min") if r.get("start_min") is not None else 540)
        end = _min_to_hhmm(r.get("end_min") if r.get("end_min") is not None else 1080)
        day_rows.append(
            "<div class='u-day'>"
            f"<label class='u-daychk' title='{_DOW_FULL[d]}'>"
            f"<input type='checkbox' name='en_{d}' {en}> {_DOW[d]}</label>"
            "<span class='u-times'>"
            f"<input type='time' name='start_{d}' value='{start}' aria-label='{_DOW_FULL[d]} начало'>"
            "<span class='sep'>–</span>"
            f"<input type='time' name='end_{d}' value='{end}' aria-label='{_DOW_FULL[d]} конец'>"
            "</span></div>")

    role_other = "employee" if role == "admin" else "admin"
    role_other_lbl = "интервьюер" if role == "admin" else "админ"
    toggle_lbl = "Отключить" if active else "Включить"
    toggle_val = "0" if active else "1"
    toggle_cls = "hbtn danger" if active else "primary"

    body = (
        _CSS +
        "<div class='u-wrap'>"
        "<a class='u-back' href='/users'>← Все пользователи</a>"
        "<div class='u-card'>"
        f"<div class='u-eh'><h2>{escape(u.get('name') or '—')}</h2>"
        f"{_role_tag(role)}{_status_tag(active)}</div>"
        f"<p class='u-sub'>Логин <code>{escape(u.get('login') or '')}</code> · id {rid} · "
        "вход в кабинет — на том же адресе через <code>/login</code> (роль ведёт в «/cabinet»).</p>"
        + _note(notice) +
        "</div>"

        # availability — full width, its own card
        "<div class='u-card'>"
        f"<h3>Доступность ({escape(slots.tz_label(u.get('tz')))}) — когда его можно назначить</h3>"
        f"<p class='u-chint'>Время местное, по его поясу (<b>{escape(slots.tz_label(u.get('tz')))}</b>). "
        "Определяется автоматически, когда он заходит в кабинет со своего устройства.</p>"
        f"<form method='post' action='/users/{rid}/availability'>"
        f"<div class='u-days'>{''.join(day_rows)}</div>"
        "<p class='u-hint'>Конец <b>раньше</b> начала — ночное окно через полночь "
        "(напр. 19:00–01:30 для часов США). Одинаковое время начала и конца — доступен <b>24 ч</b>.</p>"
        "<div class='u-actions'>"
        "<button class='primary' type='submit'>Сохранить доступность</button>"
        "<button class='ghost' type='button' onclick='uCopyMon(this)'>Скопировать Пн на все дни</button>"
        "</div>"
        "</form></div>"

        "<div class='u-grid'>"
        # password
        "<div class='u-card u-set'><h3>Пароль</h3>"
        f"<form method='post' action='/users/{rid}/passwd'>"
        "<label>Новый пароль (пусто — сгенерируется)</label>"
        "<input name='password' placeholder='(сгенерируется)' autocomplete='off'>"
        "<button class='hbtn' type='submit'>Сбросить пароль</button></form></div>"

        # telegram
        "<div class='u-card u-set'><h3>Telegram для напоминаний</h3>"
        f"<form method='post' action='/users/{rid}/telegram'>"
        "<label>chat_id (пусто — отвязать)</label>"
        f"<input name='chat_id' value='{u.get('telegram_chat_id') or ''}' autocomplete='off' inputmode='numeric'>"
        "<button class='hbtn' type='submit'>Сохранить</button></form>"
        "<p class='u-chint' style='margin-top:8px'>Сначала он должен написать боту, иначе ЛС не дойдёт.</p></div>"

        # role + active
        "<div class='u-card u-set u-span'><h3>Роль и доступ</h3>"
        "<div class='u-rolebtns'>"
        f"<form method='post' action='/users/{rid}/role'>"
        f"<input type='hidden' name='role' value='{role_other}'>"
        f"<button class='hbtn' type='submit'>Сделать: {role_other_lbl}</button></form>"
        f"<form method='post' action='/users/{rid}/active'>"
        f"<input type='hidden' name='active' value='{toggle_val}'>"
        f"<button class='{toggle_cls}' type='submit'>{toggle_lbl}</button></form>"
        "</div>"
        "<p class='u-chint' style='margin-top:8px'>Отключение мгновенно отзывает сессию в кабинете.</p></div>"
        "</div>"
        "</div>"

        "<script>function uCopyMon(b){var f=b.closest('form');"
        "function v(n){var e=f.querySelector('[name=\"'+n+'\"]');return e?e:null;}"
        "var s=v('start_0'),e=v('end_0'),c=v('en_0');if(!s)return;"
        "for(var d=1;d<7;d++){var sd=v('start_'+d),ed=v('end_'+d),cd=v('en_'+d);"
        "if(sd)sd.value=s.value;if(ed)ed.value=e.value;if(cd&&c)cd.checked=c.checked;}}</script>")
    return mailcrm_ui._page("users", body)
