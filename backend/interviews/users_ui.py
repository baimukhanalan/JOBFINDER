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
/* explicit input size — else `font:inherit` shrinks the field to the 12px label caption above it */
.u-add input,.u-add select{width:100%;font-size:14px}
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
.u-set input,.u-set select{width:100%;margin-bottom:9px}
.u-rolebtns{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end}
.u-rolebtns form{margin:0}
.u-roleform{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.u-roleform select{min-width:150px}
/* multi-role checkboxes — `label.u-rolechk` (0,1,1) beats `.u-add label` (0,1,1) by source
   order so the checkbox+label stay inline «☑ label», not stacked, inside the add form. */
.u-rolechecks{display:flex;gap:6px 14px;flex-wrap:wrap;align-items:center}
label.u-rolechk{display:inline-flex;flex-direction:row;align-items:center;gap:6px;font-size:13.5px;font-weight:600;color:var(--ink);margin:0;cursor:pointer;white-space:nowrap;min-height:40px;padding:4px 2px}
label.u-rolechk input{width:17px;height:17px;flex:0 0 auto;margin:0}
.u-add-roles{grid-column:1/-1}
/* inline per-user role editor + delete in the list */
.u-rolebox{margin-top:11px;border-top:1px solid var(--line);padding-top:9px}
.u-rolebox>summary{cursor:pointer;font-size:12.5px;font-weight:700;color:var(--ink-soft);list-style:none;user-select:none;display:flex;width:100%;align-items:center;gap:6px;padding:8px 0}
.u-rolebox>summary::before{content:'▸';color:var(--ink-mute);font-size:11px}
.u-rolebox[open]>summary::before{content:'▾'}
.u-rolebox>summary:hover{color:var(--ink)}
.u-roleedit{display:flex;gap:10px 14px;flex-wrap:wrap;align-items:center;margin-top:11px}
.u-inline-del{margin-top:10px}
/* delegation / allocation card */
.u-alloc{display:flex;flex-direction:column;gap:12px}
.u-alloc-filters{display:flex;gap:8px;flex-wrap:wrap}
.u-alloc-filters select{flex:1 1 170px;min-width:0;padding:9px 10px;border:1px solid var(--line-strong);border-radius:8px;background:var(--panel);color:var(--ink);font-size:13.5px}
/* full-width on a narrow phone so «Любое направление» isn't truncated in the closed select */
@media(max-width:560px){.u-alloc-filters select{flex:1 1 100%}}
.u-facet-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 0 4px}
.u-facet{border-collapse:collapse;font-size:12.5px;min-width:300px;width:100%}
.u-facet th,.u-facet td{border:1px solid var(--line);padding:5px 9px;text-align:center;white-space:nowrap}
.u-facet th{background:var(--panel-2);color:var(--ink-soft);font-weight:700}
.u-facet td{font-family:var(--ff-mono);color:var(--ink)}
.u-facet tr th:first-child{text-align:left}
.u-alloc-list{display:flex;flex-direction:column;gap:8px}
.u-alloc-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:0;font-weight:600}
.u-alloc-nm{flex:1 1 200px;font-size:13.5px;color:var(--ink)}
.u-alloc-row input{width:110px;padding:9px 10px;text-align:center}
.u-alloc button{align-self:flex-start}
.u-alloc-sub{margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
.u-alloc-sub h4{margin:0 0 10px;font-size:13.5px;font-weight:700}
.u-alloc-send{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.u-alloc-send select,.u-alloc-send input{flex:1 1 200px;min-width:0;padding:9px 10px;border:1px solid var(--line-strong);border-radius:8px;background:var(--panel);color:var(--ink);font-size:13.5px}
@media(max-width:560px){.u-alloc-send select,.u-alloc-send input{flex:1 1 100%}.u-alloc-send button{flex:1 1 100%}}
/* header actions (drawer toggle + logout) */
.u-top-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.u-top-actions .hbtn svg{width:16px;height:16px}
/* right slide-out drawer: the user list + add-user form live here now */
.u-drawer-scrim{position:fixed;inset:0;background:rgba(32,33,36,.5);z-index:60;opacity:0;visibility:hidden;transition:opacity .2s}
.u-drawer-scrim.open{opacity:1;visibility:visible}
.u-drawer{position:fixed;top:0;right:0;bottom:0;width:min(480px,94vw);background:var(--bg-app);z-index:61;transform:translateX(102%);transition:transform .24s ease;box-shadow:0 0 40px -8px rgba(32,33,36,.45);display:flex;flex-direction:column}
.u-drawer.open{transform:translateX(0)}
.u-drawer-head{flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px;border-bottom:1px solid var(--line);background:var(--panel)}
.u-drawer-head b{font-size:17px}
.u-drawer-body{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:16px 18px}
.u-drawer-body .u-card:first-child{margin-top:0}
@media(prefers-reduced-motion:reduce){.u-drawer{transition:none}.u-drawer-scrim{transition:none}}
/* interview-priority card (free pool, split IT/non-IT, sorted by salary/urgency) */
.u-pri-top{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin:0 0 2px}
.u-pri-top h3{margin:0}
/* right-anchor the direction-legend ⓘ popover when the ⓘ sits at the far right of a card
   header (the delegation card), else the shared left-anchored `.ph-pop` overflows off the
   right screen edge. Desktop-only (min-width:761px) so it never overrides the mobile
   viewport-pinned `.ph-pop` from mailcrm_ui._CSS (which handles the phone case at ≤760px). */
@media(min-width:761px){.ph-pop-right{left:auto;right:0}}
.u-pri-sort{display:inline-flex;gap:2px;padding:3px;background:var(--panel-2);border:1px solid var(--line-strong);border-radius:var(--r-full)}
.u-pri-sortb{display:inline-flex;align-items:center;height:32px;padding:0 13px;border-radius:var(--r-full);font-size:12.5px;font-weight:600;color:var(--ink-mute);text-decoration:none;white-space:nowrap}
.u-pri-sortb:hover{color:var(--ink-soft);text-decoration:none}
.u-pri-sortb.active{background:var(--panel);color:var(--accent);box-shadow:0 1px 2px rgba(0,0,0,.12)}
.u-pri-sec{display:flex;align-items:center;gap:9px;margin:16px 0 9px}
.u-pri-sect{font-size:12px;font-weight:800;letter-spacing:.03em;text-transform:uppercase;color:var(--ink-soft)}
.u-pri-n{font-family:var(--ff-mono);font-size:11px;font-weight:700;color:#fff;background:var(--ink-mute);border-radius:var(--r-full);padding:1px 8px}
.u-pri-empty{padding:11px;color:var(--ink-mute);font-size:12.5px;text-align:center;border:1px dashed var(--line-strong);border-radius:var(--r-sm)}
.u-pri-list{display:flex;flex-direction:column;gap:7px}
.u-pri-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:9px 11px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--panel)}
.u-pri-row.past{opacity:.6}
.u-pri-row.past:hover{opacity:1}
.u-pri-main{flex:1 1 200px;min-width:0;display:flex;flex-direction:column;gap:1px}
.u-pri-nm{font-size:13.5px;font-weight:700;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.u-pri-em{font-family:var(--ff-mono);font-size:11px;color:var(--ink-mute);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.u-pri-dir{flex:0 0 auto;font-size:10.5px;font-weight:700;color:var(--ink-soft);background:var(--panel-2);border-radius:var(--r-full);padding:2px 8px}
.u-pri-sal{flex:0 0 auto;font-family:var(--ff-mono);font-size:12px;font-weight:700;color:var(--ok)}
.u-pri-dl{flex:0 0 auto;font-size:11.5px;font-weight:700;border-radius:var(--r-full);padding:3px 9px;white-space:nowrap}
.u-pri-bk{flex:0 0 auto;font-size:10.5px;font-weight:700;color:var(--accent);background:var(--accent-soft,#e8f0fe);border-radius:var(--r-full);padding:2px 8px;white-space:nowrap}
.u-pri-dl-ok{color:var(--ink-soft);background:var(--panel-2)}
.u-pri-dl-soon{color:var(--warn);background:var(--warn-soft)}
.u-pri-dl-urgent{color:var(--danger);background:#fce8e6}
.u-pri-dl-over{color:#fff;background:var(--danger)}
/* collapsible «Истёкшие» bucket inside a priority section (keeps the actionable list short) */
.u-pri-exp{margin-top:8px;border-top:1px dashed var(--line);padding-top:8px}
.u-pri-exp>summary{cursor:pointer;list-style:none;font-size:12px;font-weight:700;color:var(--ink-mute);display:flex;align-items:center;gap:6px;padding:6px 0;user-select:none}
.u-pri-exp>summary::before{content:'▸';color:var(--ink-mute);font-size:11px}
.u-pri-exp[open]>summary::before{content:'▾'}
.u-pri-exp>summary:hover{color:var(--ink-soft)}
.u-pri-exp .u-pri-list{margin-top:8px}
/* «Забрать интервью» reuses the .u-alloc-* delegation styles; only a danger-tinted quick list */
.u-reclaim-quick .u-alloc-row{gap:10px}
.u-reclaim-quick .u-alloc-nm{flex:1 1 200px}
/* live-pipeline summary + owner filter above the «Актуальные предстоящие» card */
.u-live-sum{display:flex;gap:7px 14px;flex-wrap:wrap;align-items:center;margin:0 0 10px;font-size:12.5px;color:var(--ink-soft)}
.u-live-sum .t{font-weight:700;color:var(--ink)}
.u-live-sum b{font-family:var(--ff-mono);color:var(--ink)}
.u-live-sum .s{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.u-live-owner{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 12px}
.u-live-owner label{font-size:12.5px;font-weight:600;color:var(--ink-soft)}
.u-live-owner select{flex:0 1 260px;min-width:0;padding:8px 10px;border:1px solid var(--line-strong);border-radius:8px;background:var(--panel);color:var(--ink);font-size:13.5px}
@media(max-width:560px){.u-live-owner select{flex:1 1 100%}}
/* the drawer keeps a tappable scrim edge on a phone (94vw, not full-bleed) so tap-outside closes it */
@media(max-width:760px){.u-h1{font-size:23px}}
</style>
"""


def _min_to_hhmm(m) -> str:
    m = int(m or 0)
    return f"{m // 60:02d}:{m % 60:02d}"


_ROLE_META = {
    "admin": ("админ", "color:#b45309;background:#fef3c7"),
    "manager": ("управляющий", "color:#6d28d9;background:#ede9fe"),
    "employee": ("интервьюер", "color:#3730a3;background:#e0e7ff"),
}
# fixed display order (high→low) so a multi-role user's tags read consistently
_ROLE_ORDER = ("admin", "manager", "employee")
_ROLE_CHECK_LABELS = (("admin", "админ"), ("manager", "управляющий"), ("employee", "интервьюер"))


def _roles_of(u: dict) -> list[str]:
    """A user row's role SET — the `roles` array, else the legacy single `role`."""
    rs = u.get("roles")
    if rs:
        return list(rs)
    r = u.get("role")
    return [r] if r else []


def _role_tag(role: str) -> str:
    lbl, style = _ROLE_META.get(role, (role, "color:#374151;background:#f3f4f6"))
    return f'<span class="u-tag" style="{style}">{escape(lbl)}</span>'


def _role_tags(roles) -> str:
    """One coloured tag per role a user holds, in high→low order."""
    have = set(roles or [])
    return "".join(_role_tag(r) for r in _ROLE_ORDER if r in have)


def _role_checks(roles, name: str = "role", lock_admin: bool = False) -> str:
    """The admin/manager/interviewer checkbox trio, pre-ticked from `roles` — the multi-role
    editor reused by the add form, the inline list editor, and the edit page.

    `lock_admin=True` (used on the ACTING admin's OWN card) renders the «админ» box checked +
    DISABLED and submits it via a hidden field, so the admin can still toggle their other roles
    but can never strip their own admin role (a self-lockout the server also refuses)."""
    have = set(roles or [])
    out = []
    for val, lbl in _ROLE_CHECK_LABELS:
        if lock_admin and val == "admin":
            # a disabled checkbox is NOT posted → back it with a hidden field so «admin» persists
            out.append(f"<label class='u-rolechk' title='Свою роль «админ» снять нельзя'>"
                       f"<input type='checkbox' checked disabled> {escape(lbl)}</label>"
                       f"<input type='hidden' name='{name}' value='admin'>")
            continue
        chk = " checked" if val in have else ""
        out.append(f"<label class='u-rolechk'><input type='checkbox' name='{name}' "
                   f"value='{val}'{chk}> {escape(lbl)}</label>")
    return "".join(out)


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


def _manager_options(managers: list[dict], selected=None, blank_label: str = "— не выбран —") -> str:
    opts = [f"<option value=''>{escape(blank_label)}</option>"]
    for m in managers:
        sel = " selected" if selected is not None and m["id"] == selected else ""
        opts.append(f"<option value='{m['id']}'{sel}>{escape(m.get('name') or '')} "
                    f"(@{escape(m.get('login') or '')})</option>")
    return "".join(opts)


# shared filter-select option groups (М/Ж + IT/не-IT/другое), neutral labels
_GENDER_OPTS = (("", "Любой пол"), ("male", "Мужчины"), ("female", "Женщины"))
_DIR_OPTS = (("", "Любое направление"), ("it", "IT"), ("nonit", "Не‑IT"), ("other", "Другое"))


def _sel(name: str, opts, aria: str = "") -> str:
    o = "".join(f"<option value='{v}'>{escape(lbl)}</option>" for v, lbl in opts)
    a = f" aria-label='{escape(aria)}'" if aria else ""
    return f"<select name='{name}'{a}>{o}</select>"


def _facet_table(f: dict) -> str:
    """Compact availability cross-tab (gender × direction) so the admin sees how many e.g.
    IT female interviews are free before splitting. Horizontally scrollable on a phone."""
    if not f:
        return ""
    cross = f.get("cross", {})
    dcols = [("it", "IT"), ("nonit", "Не‑IT"), ("other", "Другое")]
    grows = [("female", "Женщины"), ("male", "Мужчины"), ("unknown", "Не указан")]
    head = "<tr><th></th>" + "".join(f"<th>{escape(l)}</th>" for _k, l in dcols) + "<th>Всего</th></tr>"
    body = []
    for gk, gl in grows:
        cells = "".join(f"<td>{cross.get((gk, dk), 0)}</td>" for dk, _dl in dcols)
        tot = f.get("gender", {}).get(gk, 0)
        body.append(f"<tr><th>{escape(gl)}</th>{cells}<td><b>{tot}</b></td></tr>")
    dtot = f.get("direction", {})
    foot = ("<tr><th>Всего</th>"
            + "".join(f"<td><b>{dtot.get(dk, 0)}</b></td>" for dk, _dl in dcols)
            + f"<td><b>{f.get('total', 0)}</b></td></tr>")
    return (f"<div class='u-facet-wrap'><table class='u-facet'>{head}{''.join(body)}{foot}"
            "</table></div>")


def _allocate_card(managers: list[dict], pool_count: int, pool_rows: list[dict],
                   mgr_alloc: dict, pool_facets: dict | None = None) -> str:
    """The admin delegation tools: split the free interview pool across managers (filtered by
    gender + direction, N per manager), and send a specific interview (email search) to a
    specific manager. Rendered only when at least one manager exists."""
    from backend.interviews import pool as iv_pool
    if not managers:
        return ("<div class='u-card'><h3>Делегирование интервью</h3>"
                "<p class='u-chint'>Чтобы делить интервью, сначала добавьте хотя бы одного "
                "пользователя с ролью «управляющий» (в форме выше).</p></div>")
    # split rows: one count input per manager
    split_rows = []
    for m in managers:
        a = mgr_alloc.get(m["id"], {})
        got = f" · выделено: {a.get('total', 0)}" if a else ""
        split_rows.append(
            "<label class='u-alloc-row'>"
            f"<span class='u-alloc-nm'>{escape(m.get('name') or '')} "
            f"<span class='u-login'>@{escape(m.get('login') or '')}</span>{escape(got)}</span>"
            f"<input type='number' name='count_{m['id']}' min='0' step='1' placeholder='0' inputmode='numeric'>"
            "</label>")
    manager_opts = _manager_options(managers, blank_label="— выберите управляющего —")
    # send-one: email SEARCH (email is the unique key) via a native datalist + a manager picker
    dl_opts = []
    for r in pool_rows:
        if r.get("expired"):        # expired interviews are not delegatable — keep them out of the picker
            continue
        mb = r.get("mailbox") or ""
        nm = (r.get("candidate") or "").strip()
        sx = {"male": "М", "female": "Ж"}.get(r.get("sex"), "")
        di = {"it": "IT", "nonit": "не‑IT"}.get(r.get("direction"), "")
        hint = " · ".join(x for x in (nm, sx, di) if x)
        dl_opts.append(f"<option value='{escape(mb, quote=True)}'>{escape(hint)}</option>")
    send_block = (
        "<div class='u-alloc-sub'><h4>Отправить конкретное интервью</h4>"
        "<p class='u-chint' style='margin-top:0'>Поиск по e-mail персоны (уникальный ключ).</p>"
        "<form class='u-alloc-send' method='post' action='/users/allocate/send'>"
        "<input name='mailbox' list='u-pool-emails' required autocomplete='off' "
        "placeholder='e-mail персоны' aria-label='E-mail интервью'>"
        f"<datalist id='u-pool-emails'>{''.join(dl_opts)}</datalist>"
        f"<select name='manager_id' aria-label='Управляющий' required>{manager_opts}</select>"
        "<button class='hbtn' type='submit'>Отправить</button></form></div>")
    # the ⓘ sits at the far right of the card header (justify-content:space-between), so its
    # popover must be RIGHT-anchored — else the left-anchored default overflows off the right
    # screen edge (clipped by overflow-x:hidden) at desktop widths. `.ph-pop-right` is in _CSS.
    legend = iv_pool.direction_legend_html("Что означает IT / Не-IT / Другое", align="right")
    return (
        "<div class='u-card'>"
        f"<div class='u-pri-top'><h3>Делегирование интервью</h3>{legend}</div>"
        f"<p class='u-chint'>В свободном пуле сейчас <b>{pool_count}</b> интервью "
        "(персоны с приглашением на собеседование, ещё не переданные никому). Разделите их "
        "между управляющими: выберите пол и направление, укажите сколько кому.</p>"
        + _facet_table(pool_facets or {}) +
        "<form class='u-alloc' method='post' action='/users/allocate/split'>"
        "<div class='u-alloc-filters'>"
        + _sel("split_gender", _GENDER_OPTS, "Пол")
        + _sel("split_direction", _DIR_OPTS, "Направление") +
        "</div>"
        f"<div class='u-alloc-list'>{''.join(split_rows)}</div>"
        "<button class='primary' type='submit'>Разделить интервью</button>"
        "</form>"
        + send_block +
        "</div>")


# ---- RECLAIM: pull interviews back from a user into the free pool (inverse of delegation) ----
def _holdings(users: list[dict], allocated_rows: list[dict]) -> dict:
    """{user_id: {"count": n, "by": "manager"|"responsible"}} — how many live interviews each
    user currently holds, and HOW we'd reclaim them. A user holding the «управляющий» role owns
    his WHOLE allocation (his own + his team's, counted by `manager_id`); everyone else owns only
    their personal attendee queue (`responsible_id`). Only users holding ≥1 appear."""
    mgr_ids = {u["id"] for u in users if "manager" in _roles_of(u)}
    held: dict = {}
    for u in users:
        uid = u["id"]
        if uid in mgr_ids:
            n = sum(1 for r in allocated_rows if r.get("manager_id") == uid)
            by = "manager"
        else:
            n = sum(1 for r in allocated_rows if r.get("responsible_id") == uid)
            by = "responsible"
        if n:
            held[uid] = {"count": n, "by": by}
    return held


def _reclaim_user_options(users: list[dict], held: dict) -> str:
    opts = ["<option value=''>— выберите пользователя —</option>"]
    for u in users:
        h = held.get(u["id"])
        if not h:
            continue
        team = " · весь пул (свой + команда)" if h["by"] == "manager" else ""
        opts.append(
            f"<option value='{u['id']}'>{escape(u.get('name') or '')} "
            f"(@{escape(u.get('login') or '')}) · держит: {h['count']}{escape(team)}</option>")
    return "".join(opts)


def _owner_name(r: dict, names_by_id: dict) -> str:
    """The visible OWNER of an allocated row: the attending interviewer if set, else the
    delegated manager, else «в пуле» — matches the status chip in the live-pipeline card."""
    rid = r.get("responsible_id")
    if rid:
        return names_by_id.get(rid) or "—"
    mid = r.get("manager_id")
    if mid:
        return names_by_id.get(mid) or "—"
    return "в пуле"


def _reclaim_card(users: list[dict], allocated_rows: list[dict], names_by_id: dict) -> str:
    """The admin pull-back tools, SYMMETRIC to `_allocate_card`: return a user's held interviews
    to the free pool — by COUNT (with the same gender/direction filter as split), a SPECIFIC one
    (email search), or ALL of one user in one click. Rendered only when someone actually holds
    interviews (else there is nothing to reclaim)."""
    from backend.interviews import pool as iv_pool
    held = _holdings(users, allocated_rows)
    if not held:
        return ("<div class='u-card'><h3>Забрать интервью</h3>"
                "<p class='u-chint'>Сейчас ни за кем не закреплены интервью — забирать нечего.</p></div>")
    user_opts = _reclaim_user_options(users, held)
    # per-interview: email SEARCH over the currently-held interviews (email is the unique key)
    dl_opts = []
    seen = set()
    for r in allocated_rows:
        mb = r.get("mailbox") or ""
        if not mb or mb in seen:
            continue
        seen.add(mb)
        nm = (r.get("candidate") or "").strip()
        who = _owner_name(r, names_by_id)
        hint = " · ".join(x for x in (nm, f"у: {who}") if x)
        dl_opts.append(f"<option value='{escape(mb, quote=True)}'>{escape(hint)}</option>")
    send_block = (
        "<div class='u-alloc-sub'><h4>Забрать конкретное интервью</h4>"
        "<p class='u-chint' style='margin-top:0'>Поиск по e-mail персоны (уникальный ключ) — "
        "вернётся в свободный пул.</p>"
        "<form class='u-alloc-send' method='post' action='/users/reclaim/one'>"
        "<input name='mailbox' list='u-held-emails' required autocomplete='off' "
        "placeholder='e-mail персоны' aria-label='E-mail интервью'>"
        f"<datalist id='u-held-emails'>{''.join(dl_opts)}</datalist>"
        "<button class='hbtn danger' type='submit'>Забрать в пул</button></form></div>")
    # one-click «забрать всё» per holder (improvement: no need to type a count)
    quick_rows = []
    for u in users:
        h = held.get(u["id"])
        if not h:
            continue
        team = " · свой + команда" if h["by"] == "manager" else ""
        quick_rows.append(
            f"<form class='u-alloc-row' method='post' action='/users/reclaim/count' "
            f"onsubmit=\"return confirm('Вернуть в пул все интервью пользователя? {h['count']} шт.');\">"
            f"<input type='hidden' name='reclaim_user_id' value='{u['id']}'>"
            f"<span class='u-alloc-nm'>{escape(u.get('name') or '')} "
            f"<span class='u-login'>@{escape(u.get('login') or '')}</span> · держит: {h['count']}"
            f"{escape(team)}</span>"
            "<button class='hbtn danger' type='submit'>Забрать всё</button></form>")
    quick_block = (
        "<div class='u-alloc-sub u-reclaim-quick'><h4>Забрать всё у пользователя</h4>"
        f"<div class='u-alloc-list'>{''.join(quick_rows)}</div></div>")
    legend = iv_pool.direction_legend_html("Что означает IT / Не-IT / Другое", align="right")
    return (
        "<div class='u-card'>"
        f"<div class='u-pri-top'><h3>Забрать интервью</h3>{legend}</div>"
        "<p class='u-chint'>Вернуть выданные интервью обратно в свободный пул. У управляющего "
        "забирается весь его пул (свой и команды), у интервьюера — его очередь. Можно по "
        "количеству с фильтром (пол/направление), конкретное интервью, или всё сразу.</p>"
        "<form class='u-alloc' method='post' action='/users/reclaim/count'>"
        "<div class='u-alloc-filters'>"
        f"<select name='reclaim_user_id' required aria-label='Пользователь'>{user_opts}</select>"
        "</div>"
        "<div class='u-alloc-filters'>"
        + _sel("reclaim_gender", _GENDER_OPTS, "Пол")
        + _sel("reclaim_direction", _DIR_OPTS, "Направление") +
        "</div>"
        "<div class='u-alloc-list'><label class='u-alloc-row'>"
        "<span class='u-alloc-nm'>Сколько забрать (пусто — все по фильтру)</span>"
        "<input type='number' name='reclaim_count' min='1' step='1' placeholder='все' inputmode='numeric'>"
        "</label></div>"
        "<button class='primary' type='submit'>Забрать в пул</button>"
        "</form>"
        + send_block
        + quick_block +
        "</div>")


# ---- interview priority (same signal as the Собес surface: urgency + salary, IT/non-IT) ----
# every direction gets a tag (incl. 'other' → «Другое») so a priority row is never left tag-less
_DIR_LBL = {"it": "IT", "nonit": "не‑IT", "other": "Другое"}


def _pool_sort_toggle(sort: str) -> str:
    out = []
    for k, l in (("salary", "Зарплата"), ("urgency", "Срочность"), ("age", "По давности")):
        cls = "u-pri-sortb active" if sort == k else "u-pri-sortb"
        out.append(f"<a class='{cls}' href='/users?pool_sort={k}#u-pri'>{escape(l)}</a>")
    return "<div class='u-pri-sort' role='group' aria-label='Сортировка'>" + "".join(out) + "</div>"


def _pool_row(r: dict) -> str:
    from backend.tools import interview_priority as ip
    mb = r.get("mailbox") or ""
    nm = (r.get("candidate") or "").strip() or (mb.split("@")[0] if mb else "—")
    sal = r.get("salary_label") or ""
    dtext, dlvl = ip.deadline_text(r)
    dir_lbl = _DIR_LBL.get(r.get("direction"), "")
    dir_html = f"<span class='u-pri-dir'>{escape(dir_lbl)}</span>" if dir_lbl else ""
    sal_html = f"<span class='u-pri-sal'>{escape(sal)}/год</span>" if sal else ""
    dl_html = f"<span class='u-pri-dl u-pri-dl-{dlvl}'>{escape(dtext)}</span>" if dtext else ""
    bk_html = ("<span class='u-pri-bk' title='есть ссылка записи — можно бронировать'>📅 запись</span>"
               if r.get("has_booking") else "")
    row_cls = "u-pri-row past" if dlvl == "over" else "u-pri-row"   # expired = dimmed + sorted last
    return (f"<div class='{row_cls}'>"
            f"<div class='u-pri-main'><span class='u-pri-nm'>{escape(nm)}</span>"
            f"<span class='u-pri-em'>{escape(mb)}</span></div>"
            f"{bk_html}{dir_html}{sal_html}{dl_html}</div>")


def _pool_section(title: str, rows: list[dict]) -> str:
    # still-bookable interviews are shown; EXPIRED ones (can't be delegated) collapse into a
    # «Истёкшие» details at the bottom so the actionable list stays short. The header count is
    # the delegatable (bookable) count.
    bookable = [r for r in rows if not r.get("expired")]
    expired = [r for r in rows if r.get("expired")]
    head = (f"<div class='u-pri-sec'><span class='u-pri-sect'>{escape(title)}</span>"
            f"<span class='u-pri-n'>{len(bookable)}</span></div>")
    body = ("<div class='u-pri-list'>" + "".join(_pool_row(r) for r in bookable) + "</div>"
            if bookable else "<div class='u-pri-empty'>Нет доступных интервью</div>")
    if expired:
        body += ("<details class='u-pri-exp'><summary>Истёкшие — делегировать нельзя ("
                 f"{len(expired)})</summary><div class='u-pri-list'>"
                 + "".join(_pool_row(r) for r in expired) + "</div></details>")
    return head + body


def _pool_priority_card(pool_rows: list[dict], sort: str) -> str:
    """Prioritised view of the free interview pool — the SAME filter as the Собес surface:
    split into complex (IT) vs simple (non-IT) sections, each sorted by potential salary
    (default) or urgency (soonest booking deadline). Sits directly under the delegation card so
    the admin can see what to delegate first. Read-only view; delegation itself is the card above.
    `pool_rows` must already be enriched by interview_priority.enrich_interview_groups."""
    from backend.tools import interview_priority as ip
    sort = sort if sort in ("salary", "urgency", "age") else "salary"
    rows = pool_rows or []
    if not rows:
        return ("<div class='u-card' id='u-pri'><h3>Приоритет интервью</h3>"
                "<p class='u-chint'>В свободном пуле сейчас нет интервью.</p></div>")
    it, simple = ip.partition(rows)
    it = ip.sort_groups(it, sort)
    simple = ip.sort_groups(simple, sort)
    return ("<div class='u-card' id='u-pri'>"
            "<div class='u-pri-top'><h3>Приоритет интервью</h3>" + _pool_sort_toggle(sort) + "</div>"
            "<p class='u-chint'>Свободные интервью по приоритету — сложные (IT) и простые (не‑IT), "
            "по зарплате, срочности брони слота или давности заявки.</p>"
            + _pool_section("IT-специальности", it)
            + _pool_section("Простые вакансии (не‑IT)", simple)
            + "</div>")


def _live_owner_filter(allocated_rows: list[dict], names_by_id: dict, live_owner: str) -> str:
    """A «показать по владельцу» select above the live-pipeline card — the admin can narrow the
    whole upcoming list to ONE manager/interviewer (or «Все»). Server-side (`?live_owner=<id>`),
    so it survives a reload; the JS just navigates. Rendered only when ≥1 owner exists."""
    owner_ids = set()
    for r in allocated_rows or []:
        if r.get("responsible_id"):
            owner_ids.add(r["responsible_id"])
        if r.get("manager_id"):
            owner_ids.add(r["manager_id"])
    if not owner_ids:
        return ""
    opts = ["<option value=''>Все</option>"]
    for uid in sorted(owner_ids, key=lambda i: (names_by_id.get(i) or "").lower()):
        sel = " selected" if str(uid) == str(live_owner) else ""
        opts.append(f"<option value='{uid}'{sel}>{escape(names_by_id.get(uid) or '—')}</option>")
    return ("<div class='u-live-owner'><label for='u-live-owner-sel'>Показать по владельцу:</label>"
            "<select id='u-live-owner-sel' aria-label='Фильтр по владельцу' onchange='uLiveOwner(this)'>"
            + "".join(opts) + "</select></div>")


def _live_summary(rows: list[dict]) -> str:
    """«N предстоящих: X в пуле · Y у управляющих · Z назначено» over the NON-expired rows —
    the counts agree exactly with the status chips the card shows (free / manager / assigned)."""
    live = [r for r in rows if not r.get("expired")]
    n_free = sum(1 for r in live if not r.get("responsible_id") and not r.get("manager_id"))
    n_mgr = sum(1 for r in live if not r.get("responsible_id") and r.get("manager_id"))
    n_set = sum(1 for r in live if r.get("responsible_id"))
    return (f"<div class='u-live-sum'><span class='t'>{len(live)} предстоящих:</span>"
            f"<span class='s'><b>{n_free}</b> в пуле</span>"
            f"<span class='s'><b>{n_mgr}</b> у управляющих</span>"
            f"<span class='s'><b>{n_set}</b> назначено</span></div>")


def _all_live_card(pool_rows: list[dict], allocated_rows: list[dict], names_by_id: dict,
                   users: list[dict] | None = None, live_owner: str = "") -> str:
    """«Актуальный список ВСЕХ предстоящих собеседований» (part 2, admin): the FREE pool plus
    every already-delegated/assigned interview, non-expired first, each with a status chip
    (в пуле / у управляющего / назначен интервьюеру). Complements the free-pool priority card
    above — here the admin sees the WHOLE live pipeline, not just the undelegated slice. A
    summary header + an owner filter (`live_owner`) sit on top so the admin can control
    urgency/priority across every portal at once."""
    from backend.interviews import db as iv_db, priority_ui
    pool_part = list(pool_rows or [])
    alloc_part = list(allocated_rows or [])
    # per-user colour so managers/interviewers are told apart at a glance (owner: «управляющие все
    # одного цвета — глаза путаются»). db.color_for honours an explicit override, else a stable pick.
    colors_by_id: dict = {}
    for u in (users or []):
        try:
            colors_by_id[u["id"]] = iv_db.color_for(u)
        except Exception:
            pass
    # optional owner narrowing (a manager sees his whole allocation, an interviewer his queue).
    owner_id = None
    if live_owner:
        try:
            owner_id = int(live_owner)
        except (ValueError, TypeError):
            owner_id = None
    if owner_id is not None:
        alloc_part = [r for r in alloc_part
                      if r.get("responsible_id") == owner_id or r.get("manager_id") == owner_id]
        pool_part = []      # the free pool has no owner — hidden when filtering to one
    rows = pool_part + alloc_part

    def _status(r: dict) -> str:
        rid = r.get("responsible_id")
        if rid:
            return priority_ui.status_assigned(names_by_id.get(rid) or "—", color=colors_by_id.get(rid))
        mid = r.get("manager_id")
        if mid:
            return priority_ui.status_manager(names_by_id.get(mid) or "—", color=colors_by_id.get(mid))
        return priority_ui.status_free()

    # a small legend mapping every owner shown → their colour, so name↔colour is unambiguous
    legend_ids: list = []
    seen: set = set()
    for r in rows:
        uid = r.get("responsible_id") or r.get("manager_id")
        if uid and uid in colors_by_id and uid not in seen:
            seen.add(uid)
            legend_ids.append(uid)
    legend = ""
    if legend_ids:
        legend = ("<div class='ivp-legend'>" + "".join(
            f"<span class='lg'><span class='d' style='background:{colors_by_id[uid]}'></span>"
            f"{escape(names_by_id.get(uid) or '—')}</span>" for uid in legend_ids) + "</div>")

    # the owner filter + summary are built over the FULL allocated set (so «Все» is always offered
    # and the counts describe the whole pipeline), then the card renders the (possibly) filtered rows.
    owner_filter = _live_owner_filter(allocated_rows or [], names_by_id, live_owner)
    summary = _live_summary(rows)
    return (priority_ui.CSS + owner_filter + summary + legend + priority_ui.upcoming_list(
        rows, anchor="u-live", status_of=_status,
        title="Актуальные предстоящие собеседования",
        blurb=("Весь живой поток: свободные в пуле, переданные управляющим и назначенные "
               "интервьюерам — у которых срок брони ещё не истёк, от самых срочных. Явно "
               "просроченные собраны в «Истёкшие»."),
        empty="Актуальных предстоящих собеседований нет."))


_LIST_ICON = ("<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' "
              "stroke-linecap='round'><line x1='8' y1='6' x2='21' y2='6'/>"
              "<line x1='8' y1='12' x2='21' y2='12'/><line x1='8' y1='18' x2='21' y2='18'/>"
              "<circle cx='3.5' cy='6' r='.8'/><circle cx='3.5' cy='12' r='.8'/>"
              "<circle cx='3.5' cy='18' r='.8'/></svg>")


def list_page(users: list[dict], avail_by_id: dict, notice=None,
              week_by_id: dict | None = None, monday=None, week_sig: str = "",
              managers: list[dict] | None = None, pool_count: int = 0,
              pool_rows: list[dict] | None = None, mgr_alloc: dict | None = None,
              pool_facets: dict | None = None, me_id: int | None = None,
              pool_sort: str = "salary", allocated_rows: list[dict] | None = None,
              names_by_id: dict | None = None, live_owner: str = "") -> str:
    week_by_id = week_by_id or {}
    managers = managers or []
    pool_rows = pool_rows or []
    mgr_alloc = mgr_alloc or {}
    pool_facets = pool_facets or {}
    allocated_rows = allocated_rows or []
    names_by_id = names_by_id or {}
    cards = []
    for u in users:
        rid = u["id"]
        roles = _roles_of(u)
        av = _avail_summary(avail_by_id.get(rid, []))
        tg = ('<span class="u-tag" style="color:#1e40af;background:#dbeafe">TG ✓</span>'
              if u.get("telegram_chat_id") else "")
        week_html = _week_calendar(week_by_id.get(rid, []), u.get("tz"), monday)
        # Admin read-through into ANY user's OWN portal by role: a manager → their delegation
        # portal (/manage?as, + an allocation count badge); an employee-only user → their
        # cabinet (/cabinet?as). Only the admin sees /users, and only the admin's ?as is
        # honoured server-side, so these links can't let a non-admin peek at someone else.
        extra = ""
        if "manager" in roles:
            a = mgr_alloc.get(rid, {})
            badge = (f"<span class='u-tag' style='color:#6d28d9;background:#ede9fe'>"
                     f"собесов: {a.get('total', 0)}</span>" if a else "")
            extra = (f"{badge}<a class='hbtn' href='/manage?as={rid}'>Портал →</a>")
        elif "employee" in roles:
            extra = (f"<a class='hbtn' href='/cabinet?as={rid}'>Кабинет →</a>")
        # inline MULTI-ROLE editor + delete. NO delete button for the protected logins 1/2/3
        # NOR the acting admin's OWN card (self-delete is blocked server-side; don't offer it).
        protected = (u.get("login") or "") in ("1", "2", "3") or rid == me_id
        del_form = ("" if protected else
                    f"<form method='post' action='/users/{rid}/delete' class='u-inline-del' "
                    "onsubmit=\"return confirm('Удалить пользователя безвозвратно? Его собесы вернутся в пул.');\">"
                    "<button class='hbtn danger' type='submit'>Удалить</button></form>")
        manage = (
            "<details class='u-rolebox'><summary>Роли и доступ</summary>"
            f"<form method='post' action='/users/{rid}/roles' class='u-roleedit'>"
            "<input type='hidden' name='from_list' value='1'>"
            f"<div class='u-rolechecks'>{_role_checks(roles, lock_admin=(rid == me_id))}</div>"
            "<button class='hbtn' type='submit'>Сохранить роли</button></form>"
            f"{del_form}</details>")
        cards.append(
            f"<div class='u-user{'' if u.get('active') else ' off'}'>"
            "<div class='u-utop'>"
            f"<span class='u-name'>{escape(u.get('name') or '—')}</span>"
            f"<span class='u-login'>@{escape(u.get('login') or '')}</span>"
            f"{_role_tags(roles)}{_status_tag(u.get('active'))}{tg}"
            f"<span class='u-spacer'></span>"
            f"{extra}"
            f"<a class='hbtn' href='/users/{rid}'>Настроить</a>"
            "</div>"
            f"<div class='u-av'><span class='k'>Доступность ({escape(slots.tz_label(u.get('tz')))}):</span>{av}</div>"
            f"{week_html}"
            f"{manage}"
            "</div>")
    listing = ("<div class='u-list' id='u-list'>" + "".join(cards) + "</div>") if cards else (
        "<div class='u-empty' id='u-list'>Пока нет пользователей — добавьте первого выше.</div>")

    # The add-user form + the whole user list now live in a right-side slide-out DRAWER (opened
    # from the «Список» toggle in the header), so the main column is the delegation + priority
    # workflow. #u-list stays the swap target the auto-refresh JS updates in place.
    add_card = (
        "<div class='u-card'><h3>Добавить пользователя</h3>"
        "<form class='u-add' method='post' action='/users/add'>"
        "<label>Имя<input name='name' required placeholder='Иван Петров'></label>"
        "<label>Логин<input name='login' required placeholder='ivan' autocomplete='off'></label>"
        "<label>Пароль<input name='password' placeholder='(сгенерируется)' autocomplete='off'></label>"
        "<label class='u-add-roles'>Роли (можно несколько)"
        f"<div class='u-rolechecks' id='u-add-roles' onchange='uAddRole()'>{_role_checks(['employee'])}</div></label>"
        "<label id='u-add-mgr-wrap'>Управляющий (для интервьюера)"
        f"<select name='manager_id'>{_manager_options(managers)}</select></label>"
        "<div class='u-go'><button class='primary' type='submit'>Добавить</button></div>"
        "</form></div>")

    drawer = (
        "<div class='u-drawer-scrim' id='u-drawer-scrim' onclick='uDrawer(false)'></div>"
        "<aside class='u-drawer' id='u-drawer' aria-hidden='true' aria-label='Пользователи'>"
        "<div class='u-drawer-head'><b>Пользователи</b>"
        "<button type='button' class='iconbtn' onclick='uDrawer(false)' aria-label='Закрыть'>"
        "&#10005;</button></div>"
        "<div class='u-drawer-body'>" + add_card + listing + "</div></aside>")

    body = (
        _CSS +
        "<div class='u-wrap'>"
        "<div class='u-top'><h1 class='u-h1'>Пользователи"
        f"<b>{len(users)}</b></h1>"
        "<div class='u-top-actions'>"
        "<button type='button' class='hbtn' onclick='uDrawer(true)' aria-haspopup='dialog'>"
        f"{_LIST_ICON}<span>Список <b>{len(users)}</b></span></button>"
        "<a class='hbtn u-logout' href='/logout'>Выход</a></div></div>"
        "<p class='u-lead'>Ответственные, которым можно назначать интервью по кнопке «Собес». "
        "Они входят в кабинет и видят почту персоны только после назначения. "
        "Роли: <b>админ</b> (всё) · <b>управляющий</b> (свой пул интервью + сотрудники) · "
        "<b>интервьюер</b> (свой кабинет). Список пользователей и добавление — в правой панели «Список».</p>"
        + _note(notice)
        + _allocate_card(managers, pool_count, pool_rows, mgr_alloc, pool_facets)
        + _reclaim_card(users, allocated_rows, names_by_id)
        + _pool_priority_card(pool_rows, pool_sort)
        + _all_live_card(pool_rows, allocated_rows, names_by_id, users=users, live_owner=live_owner)
        + "</div>"
        + drawer
        + _USERS_JS.replace("__SIG__", escape(week_sig, quote=True)))
    return mailcrm_ui._page("users", body)


_USERS_JS = """
<script>
// show the «Управляющий» picker in the add-user form only when the «интервьюер» role is ticked
function uAddRole(){
  var box=document.getElementById('u-add-roles'), w=document.getElementById('u-add-mgr-wrap');
  if(!box||!w) return;
  var emp=box.querySelector("input[value='employee']");
  w.style.display=(emp&&emp.checked)?'':'none';
}
document.addEventListener('DOMContentLoaded', uAddRole);
uAddRole();
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
// right slide-out drawer holding the add-user form + the whole user list
function uDrawer(open){
  var d=document.getElementById('u-drawer'), s=document.getElementById('u-drawer-scrim');
  if(!d) return;
  d.classList.toggle('open', open); if(s) s.classList.toggle('open', open);
  d.setAttribute('aria-hidden', open?'false':'true');
  document.body.style.overflow = open?'hidden':'';
}
document.addEventListener('keydown',function(e){ if(e.key==='Escape') uDrawer(false); });
// «Актуальные предстоящие» owner filter — navigate to the same page scoped to one owner
function uLiveOwner(sel){
  if(!sel) return;
  var v=sel.value||'';
  window.location.href = '/users?live_owner='+encodeURIComponent(v)+'#u-live';
}
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


def edit_page(u: dict, availability: list[dict], notice=None, interview_count: int = 0,
              managers: list[dict] | None = None, me_id: int | None = None) -> str:
    rid = u["id"]
    roles = _roles_of(u)
    active = u.get("active")
    managers = managers or []
    protected = (u.get("login") or "") in ("1", "2", "3")
    is_self = rid == me_id

    toggle_lbl = "Отключить" if active else "Включить"
    toggle_val = "0" if active else "1"
    toggle_cls = "hbtn danger" if active else "primary"

    # MULTI-ROLE checkboxes (admin AND/OR управляющий AND/OR интервьюер) — capabilities are
    # the union; persisted immediately by /users/{rid}/roles.
    role_form = (
        f"<form class='u-roleform' method='post' action='/users/{rid}/roles'>"
        f"<div class='u-rolechecks'>{_role_checks(roles, lock_admin=is_self)}</div>"
        "<button class='hbtn' type='submit'>Сохранить роли</button></form>")

    # manager assignment — only meaningful when they hold the interviewer role. Which
    # управляющий supervises them (their собесы then show up in that manager's portal).
    mgr_block = ""
    if "employee" in roles:
        mgr_block = (
            "<div class='u-card u-set u-span'><h3>Управляющий</h3>"
            "<p class='u-chint'>Кто из управляющих руководит этим интервьюером. "
            "Тогда его собесы попадают в портал этого управляющего, и тот может их назначать.</p>"
            f"<form class='u-roleform' method='post' action='/users/{rid}/manager'>"
            f"<label style='margin:0'>Управляющий<select name='manager_id'>"
            f"{_manager_options(managers, selected=u.get('manager_id'))}</select></label>"
            "<button class='hbtn' type='submit'>Сохранить</button></form></div>")

    # Danger zone: hard-delete ANY user (incl. deactivated / with interview history). Their
    # собесы are returned to the pool by the cascade. The three штатных interviewers (1/2/3)
    # are protected. A count warning is informational only, never a block.
    warn = (f"<p class='u-chint'>За пользователем закреплено интервью — <b>{interview_count}</b>; "
            "при удалении они вернутся в пул.</p>" if interview_count else "")
    if is_self:
        del_block = (
            "<div class='u-card u-set u-span'><h3>Удаление</h3>"
            "<p class='u-chint'>Нельзя удалить собственную учётную запись — вы под ней вошли.</p></div>")
    elif protected:
        del_block = (
            "<div class='u-card u-set u-span'><h3>Удаление</h3>"
            "<p class='u-chint'>Штатного интервьюера удалять нельзя — можно только отключить.</p></div>")
    else:
        del_block = (
            "<div class='u-card u-set u-span'><h3>Удаление</h3>"
            "<p class='u-chint'>Полностью удаляет учётную запись и её доступность. "
            "Действие необратимо.</p>" + warn +
            f"<form method='post' action='/users/{rid}/delete' "
            "onsubmit=\"return confirm('Удалить безвозвратно? Его собесы вернутся в пул.');\">"
            "<button class='hbtn danger' type='submit'>Удалить</button></form></div>")

    body = (
        _CSS +
        "<div class='u-wrap'>"
        "<a class='u-back' href='/users'>← Пользователи</a>"
        "<div class='u-card'>"
        f"<div class='u-eh'><h2>{escape(u.get('name') or '—')}</h2>"
        f"{_role_tags(roles)}{_status_tag(active)}</div>"
        f"<p class='u-sub'>Логин <code>{escape(u.get('login') or '')}</code> · id {rid} · "
        "вход в кабинет — на том же адресе через <code>/login</code> (роль ведёт в «/cabinet»).</p>"
        + _note(notice) +
        "</div>"

        # availability — full width, its own card
        "<div class='u-card'>"
        f"<h3>Доступность ({escape(slots.tz_label(u.get('tz')))})</h3>"
        f"<p class='u-chint'>Время местное, по его поясу (<b>{escape(slots.tz_label(u.get('tz')))}</b>). "
        "Определяется автоматически, когда он заходит в кабинет со своего устройства.</p>"
        f"<style>{avail_editor.CSS}</style>"
        f"<form method='post' action='/users/{rid}/availability'>"
        + avail_editor.render_days(availability) +
        "<p class='u-hint'>Можно добавить <b>несколько промежутков</b> в день (напр. 06:30–14:00 и "
        "18:00–01:00). День без промежутков — выходной. Конец раньше начала — ночное окно через "
        "полночь; одинаковое время начала и конца — доступен <b>24 ч</b>.</p>"
        "<div class='avd-actions'>"
        "<button class='primary' type='submit'>Сохранить</button>"
        "<button class='ghost' type='button' onclick='avdCopyMon()'>Скопировать Пн</button>"
        "</div>"
        "</form></div>"

        "<div class='u-grid'>"
        # password
        "<div class='u-card u-set'><h3>Пароль</h3>"
        f"<form method='post' action='/users/{rid}/passwd'>"
        "<label>Новый пароль (пусто — сгенерируется)</label>"
        "<input name='password' placeholder='(сгенерируется)' autocomplete='off'>"
        "<button class='hbtn' type='submit'>Сбросить</button></form></div>"

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
        f"{role_form}"
        f"<form method='post' action='/users/{rid}/active'>"
        f"<input type='hidden' name='active' value='{toggle_val}'>"
        f"<button class='{toggle_cls}' type='submit'>{toggle_lbl}</button></form>"
        "</div>"
        "<p class='u-chint' style='margin-top:8px'>Управляющий видит свой пул интервью и "
        "сотрудников; интервьюер — только свой кабинет. Отключение мгновенно отзывает сессию.</p></div>"
        + mgr_block +
        "</div>"
        + del_block +
        "</div>"

        + avail_editor.JS)
    return mailcrm_ui._page("users", body)
