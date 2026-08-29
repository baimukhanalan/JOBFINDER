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

from backend.interviews import avail_editor, slots
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
/* weekly load calendar */
.u-cal-head{margin-top:11px;font-size:12.5px;color:var(--ink-soft);font-weight:600}
.u-cal-head b{font-family:var(--ff-mono);color:var(--ink)}
.u-cal-tog{cursor:pointer;user-select:none;display:inline-flex;align-items:center;gap:7px}
.u-cal-tog:hover{color:var(--ink)}
.u-cal-chev{transition:transform .15s;color:var(--ink-mute);font-size:11px}
.u-cal-collapsed .u-cal-chev{transform:rotate(-90deg)}
.u-logout{flex:0 0 auto}
.u-cal-empty{margin-top:4px;font-size:12.5px;color:var(--ink-mute)}
.u-cal{margin-top:7px;display:grid;grid-template-columns:repeat(7,1fr);gap:6px}
.u-cal-day{min-width:0;display:flex;flex-direction:column;gap:4px;padding:6px 5px;border-radius:var(--r-sm);background:var(--panel-2);min-height:54px}
.u-cal-day.has{background:var(--accent-soft)}
.u-cal-dn{font-size:11px;font-weight:700;color:var(--ink-mute);text-align:center}
.u-slot{display:block;font-family:var(--ff-mono);font-size:11px;font-weight:700;color:var(--accent-deep,var(--accent));line-height:1.2;text-align:center;overflow:hidden}
.u-slot i{display:block;font-style:normal;font-family:var(--ff);font-weight:500;font-size:10.5px;color:var(--ink-soft);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.u-slot-none{text-align:center;color:var(--ink-mute);font-size:12px}
@media(max-width:760px){.u-cal{grid-auto-flow:column;grid-auto-columns:minmax(58px,1fr);grid-template-columns:none;overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:4px}}
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
    # a weekday can have SEVERAL windows now — group them so a day reads
    # "Пн 06:30–14:00, 18:00–01:00" rather than repeating the day name.
    by_dow: dict[int, list[str]] = {}
    for r in av:
        if r.get("enabled", True):
            by_dow.setdefault(int(r["dow"]), []).append(_fmt_window(r))
    if not by_dow:
        return "<span class='none'>нет окон → нельзя назначить</span>"
    parts = [f"{_DOW[d]} {', '.join(by_dow[d])}" for d in sorted(by_dow)]
    return " · ".join(escape(p) for p in parts)


def _note(notice) -> str:
    if not notice:
        return ""
    kind, text = notice
    style = {"ok": "color:#065f46;background:#d1fae5",
             "err": "color:#991b1b;background:#fee2e2",
             "pw": "color:#1e3a8a;background:#dbeafe"}.get(kind, "color:#374151;background:#f3f4f6")
    return f'<div class="u-note" style="{style}">{text}</div>'


def _week_calendar(interviews: list[dict], tz, monday) -> str:
    """A compact 7-day (Пн–Вс) mini-calendar of THIS week's booked собесы for one
    interviewer, in THEIR timezone — the weekly-load view for balancing assignments.
    Empty week → a muted note."""
    by_day: dict[int, list] = {d: [] for d in range(7)}
    for iv in interviews:
        st = iv.get("start_ts")
        if not st:
            continue
        try:
            loc = slots.to_local(st, tz)
        except Exception:
            continue
        who = (iv.get("company") or "").strip() or (iv.get("mailbox") or "").split("@")[0] or "собес"
        by_day[loc.weekday()].append((loc.strftime("%H:%M"), who))
    total = sum(len(v) for v in by_day.values())
    if not total:
        return ("<div class='u-cal-head'>Собесы на неделе: <b>0</b></div>"
                "<div class='u-cal-empty'>на этой неделе собесов нет</div>")
    cols = []
    for d in range(7):
        items = sorted(by_day[d])
        inner = ("".join(f"<span class='u-slot'>{escape(t)} <i>{escape(w[:16])}</i></span>"
                         for t, w in items)
                 if items else "<span class='u-slot-none'>—</span>")
        cols.append(f"<div class='u-cal-day{' has' if items else ''}'>"
                    f"<span class='u-cal-dn'>{_DOW[d]}</span>{inner}</div>")
    # the head toggles the grid open/closed (expand to see every собес, collapse for tidiness)
    return (f"<div class='u-cal-head u-cal-tog' onclick='uCalToggle(this)' role='button' tabindex='0'>"
            f"Собесы на неделе: <b>{total}</b><span class='u-cal-chev' aria-hidden='true'>▾</span></div>"
            f"<div class='u-cal'>{''.join(cols)}</div>")


def list_page(users: list[dict], avail_by_id: dict, notice=None,
              week_by_id: dict | None = None, monday=None, week_sig: str = "") -> str:
    week_by_id = week_by_id or {}
    cards = []
    for u in users:
        av = _avail_summary(avail_by_id.get(u["id"], []))
        tg = ('<span class="u-tag" style="color:#1e40af;background:#dbeafe">TG ✓</span>'
              if u.get("telegram_chat_id") else "")
        week_html = _week_calendar(week_by_id.get(u["id"], []), u.get("tz"), monday)
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
            f"{week_html}"
            "</div>")
    listing = ("<div class='u-list' id='u-list'>" + "".join(cards) + "</div>") if cards else (
        "<div class='u-empty' id='u-list'>Пока нет пользователей — добавьте первого выше.</div>")

    body = (
        _CSS +
        "<div class='u-wrap'>"
        "<div class='u-top'><h1 class='u-h1'>Пользователи"
        f"<b>{len(users)}</b></h1>"
        "<a class='hbtn u-logout' href='/logout'>Выход</a></div>"
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
        "</div>"
        + _USERS_JS.replace("__SIG__", escape(week_sig, quote=True)))
    return mailcrm_ui._page("users", body)


_USERS_JS = """
<script>
// collapse/expand an interviewer's weekly calendar (click the «Собесы на неделе» head)
function uCalToggle(head){
  var cal=head.nextElementSibling;
  if(!cal||!cal.classList.contains('u-cal')) return;
  if(cal.hasAttribute('hidden')){cal.removeAttribute('hidden');head.classList.remove('u-cal-collapsed');}
  else{cal.setAttribute('hidden','');head.classList.add('u-cal-collapsed');}
}
document.addEventListener('keydown',function(e){
  if((e.key==='Enter'||e.key===' ')&&e.target&&e.target.classList&&e.target.classList.contains('u-cal-tog')){
    e.preventDefault(); uCalToggle(e.target);
  }
});
// Auto-refresh the interviewer cards (+ their weekly calendars) when a собес is assigned/
// reassigned/cancelled elsewhere — so a second admin tab checking load updates itself.
// Poll a cheap signature; on change, fetch /users and swap just the #u-list cards.
(function(){
  var sig="__SIG__", busy=false;
  setInterval(function(){
    if(busy||document.hidden) return; busy=true;
    fetch('/users/signature').then(function(r){return r.ok?r.json():null;}).then(function(j){
      if(!j||!j.sig||j.sig===sig){ busy=false; return; }
      return fetch('/users').then(function(r){return r.text();}).then(function(html){
        var doc=new DOMParser().parseFromString(html,'text/html');
        var fresh=doc.getElementById('u-list'), cur=document.getElementById('u-list');
        if(fresh&&cur) cur.innerHTML=fresh.innerHTML;
        sig=j.sig; busy=false;
      });
    }).catch(function(){busy=false;});
  }, 20000);
})();
</script>
"""


def edit_page(u: dict, availability: list[dict], notice=None, interview_count: int = 0) -> str:
    rid = u["id"]
    role = u.get("role")
    active = u.get("active")

    role_other = "employee" if role == "admin" else "admin"
    role_other_lbl = "интервьюер" if role == "admin" else "админ"
    toggle_lbl = "Отключить" if active else "Включить"
    toggle_val = "0" if active else "1"
    toggle_cls = "hbtn danger" if active else "primary"

    # Danger zone: hard-delete. A user with any interview can't be hard-deleted (FK keeps the
    # history) — show why + point to «Отключить» instead. Otherwise a confirmed delete button.
    if interview_count:
        del_block = (
            "<div class='u-card u-set u-span'><h3>Удаление</h3>"
            "<p class='u-chint'>Нельзя удалить: за пользователем закреплено интервью — "
            f"<b>{interview_count}</b>. Чтобы сохранить историю, используйте «Отключить». "
            "Удаление доступно только для пользователя без интервью.</p></div>")
    else:
        del_block = (
            "<div class='u-card u-set u-span'><h3>Удаление</h3>"
            "<p class='u-chint'>Полностью удаляет учётную запись и её доступность. "
            "Действие необратимо.</p>"
            f"<form method='post' action='/users/{rid}/delete' "
            "onsubmit=\"return confirm('Удалить пользователя безвозвратно?');\">"
            "<button class='hbtn danger' type='submit'>Удалить пользователя</button></form></div>")

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
        f"<style>{avail_editor.CSS}</style>"
        f"<form method='post' action='/users/{rid}/availability'>"
        + avail_editor.render_days(availability) +
        "<p class='u-hint'>Можно добавить <b>несколько промежутков</b> в день (напр. 06:30–14:00 и "
        "18:00–01:00). День без промежутков — выходной. Конец раньше начала — ночное окно через "
        "полночь; одинаковое время начала и конца — доступен <b>24 ч</b>.</p>"
        "<div class='avd-actions'>"
        "<button class='primary' type='submit'>Сохранить доступность</button>"
        "<button class='ghost' type='button' onclick='avdCopyMon()'>Скопировать Пн на все дни</button>"
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
        + del_block +
        "</div>"

        + avail_editor.JS)
    return mailcrm_ui._page("users", body)
