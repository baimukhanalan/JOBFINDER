"""Merged, Gmail-style «Кандидаты» screen for the JobFinder dashboard.

Server-rendered HTML, no framework. The list is GROUPED BY CANDIDATE (one card per
persona mailbox), newest-activity first. A card header shows the candidate + the latest
message preview + badges + (when the candidate has an interview mail) a «Собес» control;
clicking a header EXPANDS the card inline to that candidate's messages, and clicking a
message opens its body inline. Everything expands / loads in place through the small
fragment endpoints below — minimal page navigation.

Routes (already wired in dashboard_app.py) call the four public functions here:
    GET /mail/candidates                       -> render_page(...)
    GET /mail/candidates/more?tab&stage&q&offset -> render_groups(groups)
    GET /mail/candidates/thread?mailbox        -> render_thread_fragment(mailbox, msgs)
    GET /mail/candidates/message?id            -> render_message_fragment(message)

All rendering reuses the existing shell (sidebar / topbar / drawer / modals + global JS)
via mailcrm_ui._page, and its shared components (avatar, kind tags, message card, the
interview-assign «Собес» control + modal, the reply compose modal). This module only
adds the grouped-card markup, its scoped CSS (all classes prefixed `cg-`) and its scoped
JS (all functions prefixed `cg`). No stack names appear in any user-facing text.
"""
from __future__ import annotations

import re
from html import escape, unescape
from urllib.parse import urlencode

from backend.tools import candidate_apps, mailcrm
from backend.tools.mailcrm_ui import (
    _page, _initial, _avatar_color, maildate, _kind_tag, _msg_card,
    _iv_sobes, _iv_modal, _COMPOSE_MODAL,
)

# One page of candidate groups. The routes pass this as candidate_groups(limit=PAGE),
# and the infinite-scroll JS advances the offset by PAGE per fetch.
PAGE = 40

# Funnel chips: (stage key, label). The empty key is «Все» (its count lives under 'all'
# in stage_counts). Order mirrors the existing mail funnel. Each single-kind chip counts
# candidates whose FURTHEST inbound stage is that kind (mail_db.stage_counts, furthest-based),
# so a progressed candidate is counted only once under its latest stage.
_FUNNEL = [
    ("", "Все"),
    ("sent", "Отправленные"),
    ("ack", "Принято"),
    ("action_needed", "Действие"),
    ("interview", "Собеседование"),
    ("offer", "Оффер"),
    ("rejection", "Отказ"),
    ("code", "Коды"),
]


# A stored mail_index.snippet occasionally carries raw CSS/HTML that a sender put in the
# text/plain part (e.g. "body, table { font-family: Verdana… }", "img border:0;height:auto;…").
# Strip it at render so the preview shows real message text, never markup. Render-time only —
# no DB/index change. (Kept from the Direction-B pass; the card look itself was reverted.)
_CSS_RULE_RE = re.compile(r"[^{}<>]*\{[^{}]*\}")
_TAG_RE = re.compile(r"<[^>]+>")
_ATRULE_RE = re.compile(r"@[a-zA-Z-]+[^;{}]*[;{]")
_CSS_DECL_RE = re.compile(
    r"(?i)(?:(?:img|td|tr|table|tbody|thead|div|span|body|font|center|a|p|h[1-6]|ul|ol|li)\s+|"
    r"[.#][\w-]+\s+)?"
    r"(?:border(?:-[\w]+)?|margin|padding|height|width|max-width|min-width|line-height|"
    r"font(?:-[\w]+)?|color|background(?:-[\w]+)?|display|text-[\w-]+|vertical-align|mso-[\w-]+|"
    r"-webkit-[\w-]+|-ms-[\w-]+|outline|border-collapse|table-layout)\s*:\s*[^;]+;?")
_LEADING_TAG_RE = re.compile(
    r"^(?:img|td|tr|table|div|span|body|p|a|ul|ol|li|h[1-6]|tbody|thead|font|center|br)\b[\s,]*",
    re.I)


def _clean_snippet(s: str) -> str:
    """Best-effort clean preview text: drop <style>/<script> blocks, bare CSS rules, at-rules,
    bare CSS declarations, HTML tags, then unescape + collapse whitespace. Returns '' if nothing
    readable is left (a snippet that was pure CSS)."""
    if not s:
        return ""
    t = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", s)
    t = _ATRULE_RE.sub(" ", t)
    for _ in range(4):
        nt = _CSS_RULE_RE.sub(" ", t)
        if nt == t:
            break
        t = nt
    for _ in range(3):
        nt = _CSS_DECL_RE.sub(" ", t)
        if nt == t:
            break
        t = nt
    t = _TAG_RE.sub(" ", t)
    t = unescape(t)
    t = t.replace("{", " ").replace("}", " ")
    t = re.sub(r"(?i)[\s;,]*(?:-ms-|-webkit-|-moz-|mso-)[\w-]*[:;]?[^;]*$", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    for _ in range(3):
        nt = _LEADING_TAG_RE.sub("", t)
        if nt == t:
            break
        t = nt.strip()
    return t if len(t) >= 2 else ""


def _clink(tab: str, stage: str, q: str) -> str:
    """A /mail/candidates URL carrying the current tab (always), plus stage/q when set."""
    params = {"tab": tab or "all"}
    if stage:
        params["stage"] = stage
    if q:
        params["q"] = q
    return "/mail/candidates?" + urlencode(params)


def _apps_chip(mailbox: str) -> str:
    """«📄 N» chip → the candidate's applications page (/candidates/<id>): where they applied
    + each tailored résumé PDF (downloadable). Restored 2026-09-03 — the 2026-08-29 merge to
    this grouped inbox dropped the old roster's chip, so résumés were no longer clickable.
    Resolves the mailbox → candidate id (roster or synthetic demo). stopPropagation so the
    click opens the apps page, not the card. Returns "" for a candidate with no résumé/apps
    or on any failure (never breaks the card)."""
    try:
        cid = candidate_apps.id_for_email(mailbox)
        if not cid:
            return ""
        na = candidate_apps.app_count(cid)
        if not (na or cid in candidate_apps.resume_profile_ids()):
            return ""
        label = f"📄 {na}" if na else "📄"
        return (f'<span class="cg-apps" title="Резюме + куда подавались" '
                f'onclick="event.stopPropagation();'
                f"location.href='/candidates/{escape(cid)}'\">{label}</span>")
    except Exception:
        return ""


def assessment_inner(mailbox: str, done: bool) -> str:
    """Inner HTML (status chip + toggle button) of the assessment control — reused by the card
    and returned by the /mail/assessment/mark|unmark routes so the control swaps in place."""
    mb = escape(mailbox, quote=True)
    if done:
        return ('<span class="cg-asmt done" title="Ассессмент пройден">✓ Пройдено</span>'
                f'<button type="button" class="cg-asmt-btn" data-mailbox="{mb}" data-mark="0" '
                'onclick="event.stopPropagation();cgMarkAsmt(this)" '
                'title="Вернуть в «Осталось»">↺</button>')
    return ('<span class="cg-asmt pending" title="Ассессмент не пройден">⏳ Осталось</span>'
            f'<button type="button" class="cg-asmt-btn primary" data-mailbox="{mb}" data-mark="1" '
            'onclick="event.stopPropagation();cgMarkAsmt(this)">✓ Отметить пройденным</button>')


def _assessment_control(g: dict) -> str:
    """«✓ Пройдено» / «⏳ Осталось» chip + a one-click mark/un-mark button, shown ONLY for a
    persona that actually has an assessment invite. Passed = an assessment_done row OR the
    mailbox is in shl_assess_done.json (the override that survives re-index); else pending if an
    open «complete your assessment» invite exists. Returns "" (nothing) for everyone else."""
    mailbox = g.get("mailbox", "") or ""
    if not mailbox:
        return ""
    try:
        done = (g.get("n_assessment_done") or 0) > 0 or mailbox in mailcrm.assessment_done_mailboxes()
    except Exception:
        done = (g.get("n_assessment_done") or 0) > 0
    pending = (g.get("n_asmt_pending") or 0) > 0
    if not (done or pending):
        return ""
    return (f'<span class="cg-asmt-wrap" data-mailbox="{escape(mailbox, quote=True)}">'
            f'{assessment_inner(mailbox, done)}</span>')


def _iv_assigned(mailbox: str, thread: str, name: str) -> str:
    """Lazy «Назначено · <name>» control (operator_ui.assigned_button). Returns "" on any
    import failure so a degraded interviews package never breaks the card."""
    try:
        from backend.interviews import operator_ui
        return operator_ui.assigned_button(mailbox, thread, "", name, as_span=True)
    except Exception:
        return ""


# --------------------------------------------------------------- group cards
def _group_card(g: dict) -> str:
    """One candidate card (collapsed). The header toggles the card open (cgToggle);
    its body is filled lazily from /mail/candidates/thread on first open."""
    mailbox = g.get("mailbox", "") or ""
    name = g.get("name") or (mailbox.split("@")[0] if mailbox else "?")
    avatar = (f'<span class="avatar cg-ava" style="background:{_avatar_color(name)}">'
              f'{escape(_initial(name))}</span>')

    subject = g.get("last_subject") or "(без темы)"
    # An outbound last message reads as "Вы: …" (Gmail-style), so the operator can tell at a
    # glance whether the candidate is waiting on us.
    snip_prefix = "Вы: " if g.get("last_outbound") else ""
    snippet = _clean_snippet(g.get("last_snippet") or "")

    clip = '<span class="cg-clip" title="есть вложение">📎</span>' if g.get("has_att") else ""
    date = maildate(g.get("last_ts", 0))

    # Stage tag (furthest inbound kind). _kind_tag returns "" for the neutral «other» bucket.
    stage_tag = _kind_tag(g.get("stage", "other"))

    n_msg = g.get("msg_count", 0)
    count_chip = f'<span class="cg-count" title="писем в переписке">{n_msg}</span>' if n_msg else ""

    # Interview control (stops its own click propagation → opens the modal, not the card):
    # «Назначено · <name>» once a booking exists (edit / reassign / cancel via the modal),
    # else «Собес» when the candidate has an interview mail, else nothing.
    sobes = ""
    asg = g.get("assigned")
    if asg:
        sobes = _iv_assigned(mailbox, asg.get("thread_key", "") or "",
                             asg.get("responsible_name") or "")
    elif g.get("iv_hash"):
        sobes = _iv_sobes(mailbox, g.get("iv_thread", "") or "", g.get("iv_hash", "") or "",
                          as_span=True)

    unread = g.get("unread", 0)
    unread_badge = f'<span class="cg-cnt" title="непрочитанных">{unread}</span>' if unread else ""
    card_cls = "cg-card unread" if unread else "cg-card"

    # ONE clean preview line: subject lead, then «· snippet» only when there's real snippet text.
    snip_txt = snip_prefix + snippet
    preview = f'<span class="cg-subj">{escape(subject)}</span>'
    if snip_txt.strip():
        preview += (f'<span class="cg-psep">·</span>'
                    f'<span class="cg-snip">{escape(snip_txt)}</span>')

    return (
        f'<div class="{card_cls}" data-mailbox="{escape(mailbox, quote=True)}" data-loaded="0">'
        f'<div class="cg-head" onclick="cgToggle(this)">'
        f'{avatar}'
        f'<div class="cg-mid">'
        f'<div class="cg-top"><span class="cg-name">{escape(name)}</span>'
        f'{clip}<span class="cg-date">{escape(date)}</span></div>'
        f'<div class="cg-preview">{preview}</div>'
        f'<div class="cg-metaline">{stage_tag}{count_chip}{_apps_chip(mailbox)}'
        f'{_assessment_control(g)}{sobes}</div>'
        f'</div>'
        f'<div class="cg-right">{unread_badge}<span class="cg-chev">›</span></div>'
        f'</div>'
        f'<div class="cg-body" hidden></div>'
        f'</div>'
    )


def render_groups(groups) -> str:
    """Fragment: just the group cards. Used for the first page (inside #grouplist),
    the /mail/candidates/more page fetches, and any AJAX list swap."""
    return "".join(_group_card(g) for g in (groups or []))


# ------------------------------------------------------- expanded message rows
def _msg_row(m: dict) -> str:
    """One collapsed message row inside an expanded candidate card. Clicking it opens the
    message body inline (cgOpen → /mail/candidates/message)."""
    hid = m.get("id", "") or ""
    outbound = bool(m.get("outbound"))
    who = "Вы" if outbound else (m.get("from_name") or m.get("from_email") or "?")
    unread = "" if m.get("seen") else " unread"
    kind_tag = _kind_tag(m.get("kind", "other")) if m.get("kind") else ""
    clip = '<span class="cg-msg-clip" title="есть вложение">📎</span>' if m.get("has_att") else ""
    subject = m.get("subject") or "(без темы)"
    snippet = _clean_snippet(m.get("snippet", "") or "")
    date = maildate(m.get("date_ts", 0))
    return (
        f'<div class="cg-msg{unread}" data-id="{escape(hid, quote=True)}" data-loaded="0" '
        f'onclick="cgOpen(this)">'
        f'<div class="cg-msg-top"><span class="cg-msg-from">{escape(who)}</span>'
        f'{kind_tag}{clip}<span class="cg-msg-date">{escape(date)}</span></div>'
        f'<div class="cg-msg-line"><span class="cg-msg-subj">{escape(subject)}</span>'
        f'<span class="cg-msg-snip">{escape(snippet)}</span></div>'
        f'<div class="cg-msg-body" hidden></div>'
        f'</div>'
    )


def render_thread_fragment(mailbox, messages) -> str:
    """Fragment: a candidate's message rows, newest first (list_messages is already
    newest-first). Each row is inline-openable. Empty → a small placeholder, never a crash."""
    rows = messages or []
    if not rows:
        return '<div class="cg-thread-empty">Писем нет</div>'
    return "".join(_msg_row(m) for m in rows)


def render_message_fragment(message) -> str:
    """Fragment: one message body — the shared full-message card. Wrapped defensively so an
    unexpected message dict shape degrades to a small error, never breaks the page."""
    try:
        return _msg_card(message)
    except Exception:
        return '<div class="cg-msg-err">Не удалось показать письмо.</div>'


# ------------------------------------------------------------------- full page
def _title(count=None) -> str:
    # Clean page title: «Кандидаты» + a mono total count. Replaces the old lone underlined
    # «Все письма» pseudo-tab (leftover of the removed «Приоритетные» tab).
    c = f'<span class="cg-h-count">{count}</span>' if count else ""
    return f'<div class="cg-htitle"><span class="cg-h">Кандидаты</span>{c}</div>'


def _filter_btn(stage: str) -> str:
    """Compact «Фильтры» control (shows the active stage). Desktop uses the inline funnel;
    on mobile (funnel hidden) this button opens the same stages as a modal — the pre-merge
    behaviour. Reuses the shared `.filter-btn` (mobile-only) styling."""
    active = next((label for key, label in _FUNNEL if key == stage), "Все")
    return ('<button type="button" class="filter-btn" onclick="cgFilters(true)" '
            'aria-haspopup="dialog"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
            '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>'
            f'<span>{escape(active)}</span></button>')


def _filter_modal(tab: str, stage: str, q: str, stage_counts: dict | None) -> str:
    """The mobile filter sheet — the same stage options as the desktop funnel, in the shared
    modal chrome (`.modal`/`.modal-card`/`.fm-stage`). Stage chips are plain navigation links."""
    sc = stage_counts or {}
    links = []
    for key, label in _FUNNEL:
        ck = "all" if not key else key
        cnt = sc.get(ck, "")
        cls = "fm-stage active" if stage == key else "fm-stage"
        href = escape(_clink(tab, key, q), quote=True)
        links.append(f'<a class="{cls}" href="{href}"><span class="fm-stage-lbl">{label}</span>'
                     f'<span class="fm-stage-n">{cnt}</span></a>')
    return ('<div class="modal" id="cgFilterModal" '
            'onclick="if(event.target===this)cgFilters(false)">'
            '<div class="modal-card"><div class="modal-head"><b>Фильтры</b>'
            '<button type="button" class="iconbtn" onclick="cgFilters(false)" '
            'aria-label="Закрыть">✕</button></div>'
            '<div class="fm-stages">' + "".join(links) + '</div></div></div>')


# Compose affordances: a Gmail-style floating button on mobile (shared `.fab-compose`, hidden
# on desktop) + a header button on desktop (`.cg-compose-desk`, hidden on mobile). Both open
# the shared compose modal (`openCompose` / `#composeModal`, shipped by _page + _COMPOSE_MODAL).
_FAB_COMPOSE = ('<button class="fab-compose" onclick="openCompose()" aria-label="Написать">'
                '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
                'stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/>'
                '<path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>'
                '<span>Написать</span></button>')
_COMPOSE_BTN = ('<button class="hbtn hbtn-compose cg-compose-desk" onclick="openCompose()">'
                '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
                'stroke-linecap="round"><path d="M12 20h9"/>'
                '<path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>'
                '<span class="hbtn-lbl">Написать</span></button>')


def _funnel(tab: str, stage: str, q: str, stage_counts: dict | None) -> str:
    sc = stage_counts or {}
    chips = []
    for key, label in _FUNNEL:
        cls = "cg-fbtn active" if stage == key else "cg-fbtn"
        ck = "all" if not key else key
        # Show the count only when stage_counts actually carries this bucket.
        cnt = f' <b>{sc[ck]}</b>' if ck in sc else ""
        href = escape(_clink(tab, key, q), quote=True)
        chips.append(f'<a class="{cls}" href="{href}">{label}{cnt}</a>')
    return '<div class="cg-funnel">' + "".join(chips) + '</div>'


def _search(tab: str, stage: str, q: str) -> str:
    # Desktop search. On mobile the shared Gmail top pill (name="q" → /mail/candidates)
    # already provides search, so .cg-search is hidden ≤760px.
    hidden = f'<input type="hidden" name="tab" value="{escape(tab or "all", quote=True)}">'
    if stage:
        hidden += f'<input type="hidden" name="stage" value="{escape(stage, quote=True)}">'
    return ('<form class="cg-search" method="get" action="/mail/candidates" role="search">'
            + hidden
            + f'<input type="search" name="q" value="{escape(q or "", quote=True)}" '
            'placeholder="Поиск кандидата" autocomplete="off"></form>')


def render_page(groups, *, tab: str = "all", stage: str = "", q: str = "",
                stage_counts: dict | None = None, has_more: bool = False,
                offset: int = 0) -> str:
    groups = groups or []
    tab = tab or "all"

    total = (stage_counts or {}).get("all")
    toolbar = ('<div class="cg-toolbar">' + _title(total)
               + '<div class="cg-actions">' + _filter_btn(stage) + _COMPOSE_BTN
               + _search(tab, stage, q) + '</div></div>')
    funnel = _funnel(tab, stage, q, stage_counts)

    empty = '' if groups else '<div class="cg-empty">Кандидатов пока нет</div>'
    # The sentinel keeps the paging state the infinite-scroll JS reads. It is inert (hidden)
    # when there is no next page; when there IS one it stays observable (1px, empty).
    next_off = offset + len(groups)
    sentinel = (
        f'<div id="grpmore" data-offset="{next_off}" data-tab="{escape(tab, quote=True)}" '
        f'data-stage="{escape(stage or "", quote=True)}" data-q="{escape(q or "", quote=True)}"'
        f'{"" if has_more else " hidden"}></div>'
    )

    body = (
        f'<style>{_CG_CSS}</style>'
        + toolbar + funnel
        + f'<div id="grouplist">{render_groups(groups)}</div>'
        + empty
        + sentinel
        + f'<script>window.CG_PAGE={PAGE};</script>'
        + _FAB_COMPOSE
        + _CG_JS
    )
    # _COMPOSE_MODAL powers compose + the reply button inside an opened message; _iv_modal()
    # the «Собес» assign flow; the candidates filter modal holds the mobile stage picker.
    modal = _COMPOSE_MODAL + _iv_modal() + _filter_modal(tab, stage, q, stage_counts)
    return _page("candidates", body, modal)


# ------------------------------------------------------------------------ CSS
# Scoped, all classes prefixed `cg-`, reusing the shell design tokens so the screen matches
# the rest of the app in both padding and palette. Mobile-first with a single 760px break.
_CG_CSS = """
.cg-toolbar{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:0 0 14px;}
.cg-actions{display:flex;align-items:center;gap:10px;flex:0 0 auto;}
.cg-htitle{display:flex;align-items:baseline;gap:9px;}
.cg-h{font-size:21px;font-weight:800;color:var(--ink);letter-spacing:-.02em;}
.cg-h-count{font-family:var(--ff-mono);font-size:14px;font-weight:700;color:var(--ink-mute);font-variant-numeric:tabular-nums;}
.cg-search{margin:0;flex:0 0 auto;}
.cg-search input[type=search]{min-width:260px;}
.cg-funnel{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 16px;}
.cg-fbtn{display:inline-flex;align-items:center;gap:6px;height:var(--ctl-h);padding:0 var(--ctl-px);border-radius:var(--r-full);border:1px solid var(--line);background:var(--panel);color:var(--ink-soft);font-size:var(--ctl-fs);font-weight:600;text-decoration:none;white-space:nowrap;}
.cg-fbtn b{font-family:var(--ff-mono);font-size:12.5px;color:var(--ink);}
.cg-fbtn:hover{border-color:var(--accent);text-decoration:none;}
.cg-fbtn.active{background:var(--accent);border-color:var(--accent);color:#fff;}
.cg-fbtn.active b{color:#fff;}
/* card list */
#grouplist{display:flex;flex-direction:column;gap:11px;}
.cg-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;box-shadow:0 1px 2px rgba(16,24,40,.05);transition:border-color .16s,box-shadow .16s,transform .16s;}
.cg-card:hover{border-color:var(--line-strong);transform:translateY(-1px);box-shadow:0 4px 14px rgba(16,24,40,.08);}
.cg-card.open{border-color:var(--accent);box-shadow:0 2px 16px -8px rgba(26,115,232,.4);transform:none;}
.cg-head{display:flex;align-items:flex-start;gap:13px;padding:13px 16px;cursor:pointer;}
.cg-head:hover{background:#f8fafd;}
.cg-ava{width:38px;height:38px;font-size:15px;margin-top:1px;}
.cg-mid{min-width:0;flex:1;display:flex;flex-direction:column;gap:3px;}
.cg-top{display:flex;align-items:baseline;gap:8px;}
.cg-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600;color:var(--ink);font-size:15px;letter-spacing:-.012em;}
.cg-card.unread .cg-name{font-weight:700;}
.cg-clip{flex:0 0 auto;font-size:12px;color:var(--ink-mute);}
.cg-date{flex:0 0 auto;margin-left:auto;font-family:var(--ff-mono);font-size:12px;color:var(--ink-mute);}
.cg-card.unread .cg-date{color:var(--accent);font-weight:600;}
/* Строгая классика: ONE clean ellipsized preview line — subject lead (·) snippet — so a
   short subject never squishes to a 6-char stub and the whole line truncates as a unit. */
.cg-preview{display:block;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13.5px;line-height:1.45;}
.cg-subj{color:var(--ink-soft);font-weight:500;}
.cg-psep{margin:0 5px;color:var(--ink-mute);opacity:.6;}
.cg-snip{color:var(--ink-mute);}
.cg-card.unread .cg-subj{color:var(--ink);font-weight:600;}
.cg-card.unread .cg-snip{color:var(--ink-soft);}
.cg-metaline{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:1px;}
.cg-metaline:empty{display:none;}
.cg-count{font-family:var(--ff-mono);font-size:10.5px;color:var(--ink-mute);background:var(--panel-2);border-radius:var(--r-full);padding:1px 8px;}
.cg-apps{flex:0 0 auto;display:inline-flex;align-items:center;height:var(--chip-h);font-size:var(--chip-fs);font-weight:700;color:var(--accent);background:var(--accent-soft);border-radius:var(--r-full);padding:0 var(--chip-px);cursor:pointer;white-space:nowrap;}
.cg-apps:hover{background:#d7e6fd;}
.cg-asmt-wrap{display:inline-flex;align-items:center;gap:6px;}
.cg-asmt{flex:0 0 auto;display:inline-flex;align-items:center;height:var(--chip-h);font-size:var(--chip-fs);font-weight:700;border-radius:var(--r-full);padding:0 var(--chip-px);white-space:nowrap;}
.cg-asmt.done{color:#0a7d33;background:#e4f6ea;}
.cg-asmt.pending{color:#8a5a00;background:#fbeecd;}
.cg-asmt-btn{flex:0 0 auto;display:inline-flex;align-items:center;height:var(--chip-h);font-size:var(--chip-fs);font-weight:700;border:1px solid var(--line);background:var(--panel);color:var(--ink-soft);border-radius:var(--r-full);padding:0 var(--chip-px);cursor:pointer;white-space:nowrap;}
.cg-asmt-btn:hover{background:#f0f4fb;}
.cg-asmt-btn.primary{border-color:#0a7d33;color:#0a7d33;background:#eafaef;}
.cg-asmt-btn.primary:hover{background:#d6f2df;}
.cg-asmt-btn:disabled{opacity:.5;cursor:default;}
.cg-right{display:flex;align-items:center;gap:10px;flex:0 0 auto;align-self:center;}
.cg-cnt{font-family:var(--ff-mono);font-size:11px;color:#fff;background:var(--accent);border-radius:var(--r-full);padding:1px 8px;min-width:20px;text-align:center;}
.cg-chev{flex:0 0 auto;font-size:22px;line-height:1;color:var(--ink-mute);transition:transform .18s;}
.cg-card.open .cg-chev{transform:rotate(90deg);color:var(--accent);}
.cg-body{background:#f8fafd;border-top:1px solid var(--line);padding:4px 12px 8px;}
.cg-load,.cg-thread-empty{padding:16px;text-align:center;color:var(--ink-mute);font-size:13px;}
.cg-empty{text-align:center;padding:48px;color:var(--ink-mute);}
#grpmore{min-height:1px;}
/* message rows inside an expanded card */
.cg-msg{border-bottom:1px solid var(--line);padding:10px 6px;cursor:pointer;}
.cg-msg:last-child{border-bottom:0;}
.cg-msg:hover{background:#fff;}
.cg-msg-top{display:flex;align-items:baseline;gap:8px;}
.cg-msg-from{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600;color:var(--ink-soft);font-size:13px;}
.cg-msg-clip{flex:0 0 auto;font-size:11px;color:var(--ink-mute);}
.cg-msg-date{flex:0 0 auto;margin-left:auto;font-family:var(--ff-mono);font-size:11px;color:var(--ink-mute);}
.cg-msg-line{display:flex;gap:6px;min-width:0;margin-top:2px;}
.cg-msg-subj{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink-soft);font-size:12.5px;}
.cg-msg-snip{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink-mute);font-size:12px;}
.cg-msg.unread .cg-msg-from,.cg-msg.unread .cg-msg-subj{color:var(--ink);font-weight:700;}
.cg-msg.unread .cg-msg-date{color:var(--accent);}
.cg-msg-body{margin-top:8px;background:var(--panel);border:1px solid var(--line);border-radius:var(--r-sm);padding:2px 14px;}
/* When a message is open, hide its collapsed preview line so the full message card doesn't
   read as a duplicate of the row above it (Gmail-style: preview is replaced by the message). */
.cg-msg.open .cg-msg-top,.cg-msg.open .cg-msg-line{display:none;}
.cg-msg.open .cg-msg-body{margin-top:0;}
.cg-msg.open{background:transparent;}
.cg-msg-err{padding:14px;color:var(--danger);font-size:13px;}
@media(max-width:760px){
  .cg-toolbar{margin-bottom:12px;}
  .cg-h{font-size:19px;}
  .cg-search{display:none;}
  .cg-compose-desk{display:none;}
  .cg-funnel{display:none;}          /* mobile uses the «Фильтры» button + modal instead */
  .cg-head{padding:14px;gap:12px;min-height:44px;}
  .cg-name{font-size:15px;}
  .cg-msg{padding:12px 6px;}
}
"""

# ------------------------------------------------------------------------- JS
# Scoped behaviour, all functions prefixed `cg`. Toggling expands a card and lazy-loads its
# thread; opening a row lazy-loads a message body; an IntersectionObserver on #grpmore drives
# infinite scroll. Reply/forward buttons inside a freshly injected message body are re-wired
# to the global reply()/forward() helpers that ship with _page.
_CG_JS = """
<script>
(function(){
  var PAGE = window.CG_PAGE || 40;

  // «Фильтры» modal (mobile): open/close the stage sheet. Backdrop closes it inline; Esc here.
  window.cgFilters = function(open){
    var m = document.getElementById('cgFilterModal'); if(!m) return;
    m.classList.toggle('open', open);
    document.body.style.overflow = open ? 'hidden' : '';
  };
  document.addEventListener('keydown', function(e){ if(e.key === 'Escape') window.cgFilters(false); });

  // Assessment «Отметить пройденным» / «↺ Вернуть»: POST the mailbox, swap the chip+button in
  // place with the returned fragment (its inline onclick re-binds automatically). stopPropagation
  // in the inline handler already kept the click off the card toggle.
  window.cgMarkAsmt = function(btn){
    var wrap = btn.closest('.cg-asmt-wrap'); if(!wrap) return;
    var mailbox = btn.dataset.mailbox || wrap.dataset.mailbox || '';
    var mark = btn.dataset.mark === '1';
    btn.disabled = true;
    var fd = new FormData(); fd.append('mailbox', mailbox);
    fetch(mark ? '/mail/assessment/mark' : '/mail/assessment/unmark', {method:'POST', body:fd})
      .then(function(r){ if(!r.ok) throw 0; return r.text(); })
      .then(function(h){ wrap.innerHTML = h; })
      .catch(function(){ btn.disabled = false; });
  };

  // Collapse the «Написать» FAB to its icon (and hide the mobile top pill) on scroll-down,
  // restore on scroll-up — the Gmail behaviour the shared inbox handler wires only for the
  // #maillist page, so this screen (own #grouplist) needs its own null-safe copy.
  (function(){
    var fab = document.querySelector('.fab-compose');
    var pill = document.querySelector('.gm-topbar');
    var lastY = window.scrollY;
    window.addEventListener('scroll', function(){
      var y = window.scrollY, dy = y - lastY;
      if(Math.abs(dy) <= 6) return;      // ignore momentum-scroll jitter (±1px)
      lastY = y;
      if(dy > 0 && y > 90){ if(fab) fab.classList.add('collapsed'); if(pill) pill.classList.add('hide'); }
      else if(dy < 0){ if(fab) fab.classList.remove('collapsed'); if(pill) pill.classList.remove('hide'); }
    }, {passive:true});
  })();

  // Re-bind reply/forward controls inside a just-injected message body. The page-level
  // wiring only ran over markup present at parse time, so dynamically loaded cards need this.
  function cgWireReply(root){
    if(!root) return;
    root.querySelectorAll('.reply-action').forEach(function(b){
      if(b._cgw) return; b._cgw=1;
      b.addEventListener('click',function(){ if(window.reply) reply(b.dataset.from,b.dataset.to,b.dataset.subject,b.dataset.mid); });
    });
    root.querySelectorAll('.fwd-action').forEach(function(b){
      if(b._cgw) return; b._cgw=1;
      b.addEventListener('click',function(){ if(window.forward) forward(b.dataset.from,b.dataset.subject,b.dataset.body); });
    });
  }

  // Expand / collapse a candidate card; lazy-load its thread on first open.
  window.cgToggle = function(head){
    // Ignore clicks that landed on the «Собес» control or any link/button in the header
    // (the sobes span already stops propagation; this is belt-and-suspenders).
    if(window.event && window.event.target && window.event.target.closest('.iv-sobes, a, button')) return;
    var card = head.closest('.cg-card'); if(!card) return;
    var body = card.querySelector('.cg-body');
    var open = card.classList.toggle('open');
    if(body) body.hidden = !open;
    if(open && card.dataset.loaded === '0' && body){
      card.dataset.loaded = '1';
      body.innerHTML = '<div class="cg-load">Загрузка…</div>';
      fetch('/mail/candidates/thread?mailbox=' + encodeURIComponent(card.dataset.mailbox || ''))
        .then(function(r){ return r.text(); })
        .then(function(h){ body.innerHTML = h; })
        .catch(function(){ body.innerHTML = '<div class="cg-load">Не удалось загрузить письма.</div>'; card.dataset.loaded = '0'; });
    }
  };

  // Open / close a single message body inline; lazy-load it on first open.
  window.cgOpen = function(row){
    if(window.event && window.event.target && window.event.target.closest('a, button')) return;
    var body = row.querySelector('.cg-msg-body'); if(!body) return;
    var open = row.classList.toggle('open');
    body.hidden = !open;
    if(open && row.dataset.loaded !== '1'){
      row.dataset.loaded = '1';
      body.innerHTML = '<div class="cg-load">Загрузка…</div>';
      fetch('/mail/candidates/message?id=' + encodeURIComponent(row.dataset.id || ''))
        .then(function(r){ return r.text(); })
        .then(function(h){ body.innerHTML = h; cgWireReply(body); })
        .catch(function(){ body.innerHTML = '<div class="cg-msg-err">Не удалось загрузить письмо.</div>'; row.dataset.loaded = ''; });
    }
  };

  // Infinite scroll: append the next page of cards when the sentinel scrolls into view.
  var sentinel = document.getElementById('grpmore');
  var list = document.getElementById('grouplist');
  if(sentinel && list && 'IntersectionObserver' in window){
    var loading = false, done = false;
    function cgMore(){
      if(loading || done || sentinel.hidden) return;
      loading = true;
      var off = parseInt(sentinel.dataset.offset || '0', 10) || 0;
      var qs = new URLSearchParams({
        tab: sentinel.dataset.tab || 'all',
        stage: sentinel.dataset.stage || '',
        q: sentinel.dataset.q || '',
        offset: String(off)
      });
      fetch('/mail/candidates/more?' + qs.toString())
        .then(function(r){ return r.ok ? r.text() : ''; })
        .then(function(html){
          html = (html || '').trim();
          if(html){
            list.insertAdjacentHTML('beforeend', html);
            sentinel.dataset.offset = String(off + PAGE);
          }
          var added = (html.match(/class="cg-card"/g) || []).length;
          if(added < PAGE){ done = true; sentinel.hidden = true; }
          loading = false;
        })
        .catch(function(){ loading = false; });
    }
    var io = new IntersectionObserver(function(entries){
      entries.forEach(function(en){ if(en.isIntersecting) cgMore(); });
    }, {rootMargin: '400px'});
    io.observe(sentinel);
  }
})();
</script>
"""
