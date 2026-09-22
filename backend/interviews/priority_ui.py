"""Shared interview PRIORITY + upcoming-list cards, reused by the admin (/users),
manager (/manage) and interviewer (/cabinet) surfaces so all three read the SAME
urgency/priority signal (the one the «Собес» screen introduced) without duplicating markup.

Two cards:
  * `priority_card` — the interviews assigned to THIS person, split into complex (IT) vs
    simple (non-IT), sortable by potential salary / booking urgency / application age. This
    is the «такой же фильтр по срочности и приоритету» the manager pool + interviewer queue get.
  * `upcoming_list` — a flat «Актуальные предстоящие собеседования» list: every still-LIVE
    interview (an EXPLICITLY-expired booking window is dropped into a collapsed «Истёкшие»
    bucket), urgency-first, with an optional per-row status chip (в пуле / у сотрудника / …).

Both cards are SELF-CONTAINED (they carry their own `CSS`, once per page, and use only the
shared design tokens `var(--panel)`/`--line`/`--ink*`/`--ok`/… that every shell already
defines) so a portal with its own minimal shell (manage/cabinet) can drop them in unchanged.

Rows must already be enriched by `interview_priority.enrich_interview_groups` (deadline_ts /
deadline_days / deadline_estimated / direction / salary_label / has_booking / expired). Neutral
Russian throughout — no stack names.
"""
from __future__ import annotations

from html import escape

from backend.tools import interview_priority as ip

_DIR_LBL = {"it": "IT", "nonit": "не‑IT", "other": "Другое"}

# One <style> block, dropped once per page (a second copy is harmless but wasteful). Mirrors
# the users_ui `.u-pri-*` look under an `ivp-` prefix + a self-contained `.ivp-card` wrapper so
# it renders natively inside ANY shell (users/manage/cabinet), which all include mailcrm_ui._CSS.
CSS = """
<style>
.ivp-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;margin-bottom:14px;}
.ivp-top{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin:0 0 2px;}
.ivp-top h3{margin:0;font-size:15px;font-weight:700;}
.ivp-hint{margin:0 0 12px;font-size:12.5px;color:var(--ink-mute);line-height:1.45;}
.ivp-sort{display:inline-flex;gap:2px;padding:3px;background:var(--panel-2);border:1px solid var(--line-strong);border-radius:var(--r-full);}
.ivp-sortb{display:inline-flex;align-items:center;height:30px;padding:0 12px;border-radius:var(--r-full);font-size:12px;font-weight:600;color:var(--ink-mute);text-decoration:none;white-space:nowrap;}
.ivp-sortb:hover{color:var(--ink-soft);text-decoration:none;}
.ivp-sortb.active{background:var(--panel);color:var(--accent);box-shadow:0 1px 2px rgba(0,0,0,.12);}
.ivp-sec{display:flex;align-items:center;gap:9px;margin:14px 0 8px;}
.ivp-sect{font-size:11.5px;font-weight:800;letter-spacing:.03em;text-transform:uppercase;color:var(--ink-soft);}
.ivp-n{font-family:var(--ff-mono);font-size:11px;font-weight:700;color:#fff;background:var(--ink-mute);border-radius:var(--r-full);padding:1px 8px;}
.ivp-empty{padding:11px;color:var(--ink-mute);font-size:12.5px;text-align:center;border:1px dashed var(--line-strong);border-radius:var(--r-sm);}
.ivp-list{display:flex;flex-direction:column;gap:7px;}
.ivp-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:9px 11px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--panel);}
.ivp-row.past{opacity:.6;}
.ivp-row.past:hover{opacity:1;}
.ivp-main{flex:1 1 190px;min-width:0;display:flex;flex-direction:column;gap:1px;}
.ivp-nm{font-size:13.5px;font-weight:700;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.ivp-em{font-family:var(--ff-mono);font-size:11px;color:var(--ink-mute);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.ivp-badge{flex:0 0 auto;font-size:10.5px;font-weight:700;border-radius:var(--r-full);padding:2px 8px;white-space:nowrap;}
.ivp-st-free{color:#b45309;background:#fef3c7;}
.ivp-st-mgr{color:#6d28d9;background:#ede9fe;}
.ivp-st-set{color:#166534;background:#dcfce7;}
.ivp-dir{flex:0 0 auto;font-size:10.5px;font-weight:700;color:var(--ink-soft);background:var(--panel-2);border-radius:var(--r-full);padding:2px 8px;}
.ivp-sal{flex:0 0 auto;font-family:var(--ff-mono);font-size:12px;font-weight:700;color:var(--ok);}
.ivp-bk{flex:0 0 auto;font-size:10.5px;font-weight:700;color:var(--accent);background:var(--accent-soft,#e8f0fe);border-radius:var(--r-full);padding:2px 8px;white-space:nowrap;}
.ivp-dl{flex:0 0 auto;font-size:11.5px;font-weight:700;border-radius:var(--r-full);padding:3px 9px;white-space:nowrap;}
.ivp-dl-ok{color:var(--ink-soft);background:var(--panel-2);}
.ivp-dl-soon{color:var(--warn);background:var(--warn-soft);}
.ivp-dl-urgent{color:var(--danger);background:#fce8e6;}
.ivp-dl-over{color:#fff;background:var(--danger);}
.ivp-exp{margin-top:8px;border-top:1px dashed var(--line);padding-top:8px;}
.ivp-exp>summary{cursor:pointer;list-style:none;font-size:12px;font-weight:700;color:var(--ink-mute);display:flex;align-items:center;gap:6px;padding:6px 0;user-select:none;}
.ivp-exp>summary::before{content:'▸';color:var(--ink-mute);font-size:11px;}
.ivp-exp[open]>summary::before{content:'▾';}
.ivp-exp>summary:hover{color:var(--ink-soft);}
.ivp-exp .ivp-list{margin-top:8px;}
</style>
"""

_SORTS = (("salary", "Зарплата"), ("urgency", "Срочность"), ("age", "По давности"))


def _cand_name(r: dict) -> str:
    mb = r.get("mailbox") or ""
    return (r.get("candidate") or "").strip() or (mb.split("@")[0] if mb else "—")


def _row(r: dict, status_html: str = "") -> str:
    """One interview row: candidate + email, optional status chip, direction, salary, a booking
    marker and the deadline/urgency chip (the SAME wording as the «Собес» card via
    `interview_priority.deadline_text`)."""
    mb = r.get("mailbox") or ""
    nm = _cand_name(r)
    sal = r.get("salary_label") or ""
    dtext, dlvl = ip.deadline_text(r)
    dir_lbl = _DIR_LBL.get(r.get("direction"), "")
    dir_html = f"<span class='ivp-dir'>{escape(dir_lbl)}</span>" if dir_lbl else ""
    sal_html = f"<span class='ivp-sal'>{escape(sal)}/год</span>" if sal else ""
    dl_html = f"<span class='ivp-dl ivp-dl-{dlvl}'>{escape(dtext)}</span>" if dtext else ""
    bk_html = ("<span class='ivp-bk' title='есть ссылка записи — можно бронировать'>📅 запись</span>"
               if r.get("has_booking") else "")
    row_cls = "ivp-row past" if dlvl == "over" else "ivp-row"
    return (f"<div class='{row_cls}'>"
            f"<div class='ivp-main'><span class='ivp-nm'>{escape(nm)}</span>"
            f"<span class='ivp-em'>{escape(mb)}</span></div>"
            f"{status_html}{bk_html}{dir_html}{sal_html}{dl_html}</div>")


def _sort_toggle(sort: str, sort_base: str, anchor: str) -> str:
    sep = "&" if "?" in sort_base else "?"
    out = []
    for k, l in _SORTS:
        cls = "ivp-sortb active" if sort == k else "ivp-sortb"
        out.append(f"<a class='{cls}' href='{escape(sort_base, quote=True)}{sep}pool_sort={k}#{anchor}'>{escape(l)}</a>")
    return "<div class='ivp-sort' role='group' aria-label='Сортировка'>" + "".join(out) + "</div>"


def _section(title: str, rows: list[dict], status_of=None) -> str:
    """A titled section: still-bookable rows shown, EXPLICITLY-expired ones collapsed into a
    «Истёкшие» details (the header count is the actionable/bookable count)."""
    bookable = [r for r in rows if not r.get("expired")]
    expired = [r for r in rows if r.get("expired")]

    def _sh(r):
        return status_of(r) if status_of else ""
    head = (f"<div class='ivp-sec'><span class='ivp-sect'>{escape(title)}</span>"
            f"<span class='ivp-n'>{len(bookable)}</span></div>")
    body = ("<div class='ivp-list'>" + "".join(_row(r, _sh(r)) for r in bookable) + "</div>"
            if bookable else "<div class='ivp-empty'>Нет доступных собеседований</div>")
    if expired:
        body += ("<details class='ivp-exp'><summary>Истёкшие ("
                 f"{len(expired)})</summary><div class='ivp-list'>"
                 + "".join(_row(r, _sh(r)) for r in expired) + "</div></details>")
    return head + body


def priority_card(rows: list[dict], sort: str, sort_base: str, *,
                  title: str = "Приоритет собеседований",
                  blurb: str = ("Собеседования по приоритету — сложные (IT) и простые (не‑IT), "
                                "по зарплате, срочности брони слота или давности заявки."),
                  anchor: str = "ivp-pri", empty: str = "Пока ничего не назначено.") -> str:
    """The IT/non-IT split priority card (part 1). `sort_base` is the surface URL WITH its
    current query (minus pool_sort) so the sort links keep the portal's context (?as/?q/…)."""
    sort = sort if sort in ("salary", "urgency", "age") else "salary"
    rows = rows or []
    if not rows:
        return (f"<div class='ivp-card' id='{anchor}'><div class='ivp-top'><h3>{escape(title)}</h3></div>"
                f"<p class='ivp-hint'>{escape(empty)}</p></div>")
    it, simple = ip.partition(rows)
    it = ip.sort_groups(it, sort)
    simple = ip.sort_groups(simple, sort)
    return (f"<div class='ivp-card' id='{anchor}'>"
            f"<div class='ivp-top'><h3>{escape(title)}</h3>{_sort_toggle(sort, sort_base, anchor)}</div>"
            f"<p class='ivp-hint'>{escape(blurb)}</p>"
            + _section("IT‑специальности", it)
            + _section("Простые (не‑IT)", simple)
            + "</div>")


def upcoming_list(rows: list[dict], *, title: str = "Актуальные предстоящие собеседования",
                  blurb: str = ("Собеседования, у которых срок брони ещё не истёк, — от самых "
                                "срочных. Явно просроченные собраны в «Истёкшие» ниже."),
                  anchor: str = "ivp-live", status_of=None,
                  empty: str = "Актуальных предстоящих собеседований нет.") -> str:
    """A flat «actual upcoming» list (part 2): urgency-first, EXPLICITLY-expired collapsed, an
    optional per-row status chip via `status_of(row) -> html`. NOT split by direction (an
    at-a-glance live pipeline, not a delegation-priority view)."""
    rows = rows or []
    if not rows:
        return (f"<div class='ivp-card' id='{anchor}'><div class='ivp-top'><h3>{escape(title)}</h3></div>"
                f"<p class='ivp-hint'>{escape(empty)}</p></div>")
    ordered = ip.sort_groups(rows, "urgency")
    return (f"<div class='ivp-card' id='{anchor}'>"
            f"<div class='ivp-top'><h3>{escape(title)}</h3></div>"
            f"<p class='ivp-hint'>{escape(blurb)}</p>"
            + _section("Актуальные", ordered, status_of=status_of)
            + "</div>")


# ---- status chips (part-2 «all upcoming» rows) --------------------------------------------
def status_free() -> str:
    return "<span class='ivp-badge ivp-st-free'>в пуле</span>"


def status_manager(name: str) -> str:
    return f"<span class='ivp-badge ivp-st-mgr'>управл.: {escape(name or '—')}</span>"


def status_assigned(name: str) -> str:
    return f"<span class='ivp-badge ivp-st-set'>назначен: {escape(name or '—')}</span>"
