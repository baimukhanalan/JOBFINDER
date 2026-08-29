"""Pure render helpers for the operator's «Собес» (interview-assign) modal, shown
INSIDE the existing /mail inbox (no new nav tab).

Three surfaces:
  * ``modal_shell()``   — the one-time ``#ivModal`` container + its <style>/<script>,
    injected once per inbox/thread page (mirrors catalog_ui's ``.cat-modal`` chrome).
  * ``sobes_button(...)`` — the small «Собес» control placed inside a row/thread; its
    onclick calls ``event.stopPropagation()`` so it never triggers the row navigation
    (the same technique as mailcrm_ui's «📄 N» apps-chip).
  * ``grid_fragment(...)`` — the server-rendered week grid (orta look): a CSS grid of
    an hour column + 7 day columns; a FREE cell is a pale-green button carrying
    ``data-free="id:name,..."`` + its free-count, a none-free/out-of-window cell is a
    gray disabled button; prev/next-week buttons carry the target Monday; hidden
    company/jobid are prefilled from ``service.mailbox_context``.

Renders only — no DB writes here; booking goes through ``routes_operator`` → ``service``.
Neutral Russian text, GMT/UTC throughout, no decorative emoji, no stack names.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from html import escape
from urllib.parse import quote

from backend.interviews import service, slots

_DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def sobes_button(mailbox: str, thread: str, hash: str, label: str = "Собес",
                 as_span: bool = False) -> str:
    """A small «Собес» control. Opens the assign modal for this (mailbox, thread,
    message-hash); the onclick stops propagation/default so it never navigates.

    `as_span=True` renders a `<span role="button" tabindex="0">` (for placement INSIDE
    a row `<a>`, where a nested `<button>` is invalid interactive-in-interactive HTML —
    matches the `render_candidate_rows` 📄-chip pattern); default renders a real
    `<button>` (for the thread action bar, which sits in a `<div>`).

    Args are embedded as JS string literals via json.dumps (handles quotes in the
    normalized-subject thread key) and the whole attribute is HTML-escaped, so a
    subject with quotes/angle-brackets can neither break the JS nor inject markup."""
    args = ",".join(json.dumps(x or "") for x in (mailbox, thread, hash))
    onclick = escape(
        f"event.stopPropagation();event.preventDefault();openSobes({args})",
        quote=True,
    )
    if as_span:
        return (f'<span class="iv-sobes" role="button" tabindex="0" onclick="{onclick}" '
                f'title="Назначить собеседование">{escape(label)}</span>')
    return (f'<button type="button" class="iv-sobes" onclick="{onclick}" '
            f'title="Назначить собеседование">{escape(label)}</button>')


def grid_fragment(mailbox: str, monday: date,
                  company: str | None = None, jobid: str | None = None) -> str:
    """Server-rendered week grid starting at `monday` (a date). Free cells are green
    buttons carrying `data-free` (a JSON `[{"id","name"},...]` array) + `data-start`
    (the hour's UTC ISO start); none-free cells are gray+disabled. Includes prev/next-
    week buttons and prefilled hidden company/jobid.

    `company`/`jobid` are resolved ONCE (via `service.mailbox_context`) only when not
    already supplied by the caller, then threaded through the prev/next-week fetch URLs
    and the hidden assign-form fields — so subsequent week nav never re-globs the ~19k
    prefill persona.json files (they're invariant across weeks)."""
    grid = service.grid_for_week(monday)
    cells = grid["cells"]
    hours = grid["hours"]
    dates = grid["dates"]  # 7 iso date strings, Mon..Sun
    if company is None and jobid is None:
        ctx = service.mailbox_context(mailbox)
        company = ctx.get("company", "") or ""
        jobid = ctx.get("jobid", "") or ""
    else:
        company = company or ""
        jobid = jobid or ""

    # thread the resolved context back into the week-nav fetch URLs so a prev/next click
    # re-renders WITHOUT re-globbing the prefill dir.
    ctx_qs = f"&company={quote(company)}&jobid={quote(jobid)}"
    prev_m = (monday - timedelta(days=7)).isoformat()
    next_m = (monday + timedelta(days=7)).isoformat()
    sunday = monday + timedelta(days=6)
    rng = (f"{monday.day:02d}.{monday.month:02d} — "
           f"{sunday.day:02d}.{sunday.month:02d}.{sunday.year}")

    # header row: empty corner + Пн..Вс with the day-of-month
    header = ['<div class="iv-hcell iv-corner"></div>']
    for i, diso in enumerate(dates):
        d = date.fromisoformat(diso)
        header.append(
            f'<div class="iv-hcell"><span class="iv-dn">{_DAY_NAMES[i]}</span>'
            f'<span class="iv-dd">{d.day:02d}.{d.month:02d}</span></div>')

    body = []
    for hour in hours:
        body.append(f'<div class="iv-hourcell">{hour:02d}:00</div>')
        for diso in dates:
            free = cells.get(f"{diso}:{hour:02d}", [])
            if free:
                d = date.fromisoformat(diso)
                # the grid axis is LOCAL (Almaty) date+hour; the booking start_ts is
                # the corresponding UTC instant (whole-hour +5 offset).
                start_iso = slots.cell_start_utc(d, hour).isoformat()
                # JSON payload (not "id:name,..."): a responsible name may contain a comma
                # or colon ("Ivanov, A."), which a delimiter-split would corrupt.
                data_free = escape(
                    json.dumps([{"id": r["id"], "name": r["name"]} for r in free]),
                    quote=True)
                lbl = f"{d.day:02d}.{d.month:02d} {hour:02d}:00 по Алматы"
                body.append(
                    '<button type="button" class="iv-cell iv-free" '
                    f'data-start="{escape(start_iso, quote=True)}" '
                    f'data-free="{data_free}" '
                    f'data-label="{escape(lbl, quote=True)}" '
                    f'title="Свободно: {len(free)}">{len(free)}</button>')
            else:
                body.append('<button type="button" class="iv-cell iv-none" '
                            'disabled aria-hidden="true"></button>')

    ctx_line = ""
    if company or jobid:
        ctx_line = ('<div class="iv-ctx">'
                    + escape(company)
                    + (f' · {escape(jobid)}' if jobid else '')
                    + '</div>')

    return (
        f'<div class="iv-weeknav" data-cur-monday="{escape(monday.isoformat(), quote=True)}" '
        f'data-ctx="{escape(ctx_qs, quote=True)}">'
        f'<button type="button" class="iv-wk" data-monday="{escape(prev_m, quote=True)}">← Пред.</button>'
        f'<span class="iv-wk-label">{escape(rng)}</span>'
        f'<button type="button" class="iv-wk" data-monday="{escape(next_m, quote=True)}">След. →</button>'
        '</div>'
        f'<input type="hidden" id="ivCompany" value="{escape(company, quote=True)}">'
        f'<input type="hidden" id="ivJobid" value="{escape(jobid, quote=True)}">'
        + ctx_line
        + '<div class="iv-grid">' + "".join(header) + "".join(body) + '</div>'
        + '<p class="iv-note">Время по Алматы. Зелёная ячейка — свободный час; '
          'число — сколько ответственных свободно.</p>'
    )


_IV_STYLE = """<style>
.iv-modal{position:fixed;inset:0;z-index:1200;display:flex;align-items:center;justify-content:center;padding:20px}
.iv-modal[hidden]{display:none}
.iv-modal-backdrop{position:absolute;inset:0;background:rgba(15,23,42,.55)}
.iv-modal-panel{position:relative;display:flex;flex-direction:column;width:min(720px,100%);max-height:90vh;background:var(--panel);border:1px solid var(--line);border-radius:16px;box-shadow:0 24px 64px rgba(15,23,42,.30);overflow:hidden}
.iv-modal-head{flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--line)}
.iv-modal-title{font-size:16px;font-weight:700;color:var(--ink)}
.iv-modal-x{width:34px;height:34px;border:none;background:var(--bg-app);border-radius:50%;font-size:13px;color:var(--ink-soft);cursor:pointer;display:flex;align-items:center;justify-content:center}
.iv-modal-x:hover{background:var(--line);color:var(--ink)}
.iv-modal-body{overflow:auto;padding:14px 20px 20px}
.iv-weeknav{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
.iv-wk{background:var(--panel);border:1px solid var(--line-strong);border-radius:var(--r-full);padding:7px 14px;font-size:13px;font-weight:600;color:var(--ink-soft);cursor:pointer}
.iv-wk:hover{border-color:var(--accent);color:var(--ink)}
.iv-wk-label{font-size:13px;font-weight:700;color:var(--ink)}
.iv-ctx{font-size:12px;color:var(--ink-mute);margin:0 0 10px}
.iv-grid{display:grid;grid-template-columns:44px repeat(7,1fr);gap:3px;min-width:520px}
.iv-hcell{text-align:center;font-size:11px;font-weight:700;color:var(--ink-mute);padding:4px 0;display:flex;flex-direction:column;gap:1px;align-items:center}
.iv-hcell .iv-dd{font-family:var(--ff-mono,monospace);font-weight:500;font-size:10px;color:var(--ink-mute)}
.iv-hourcell{font-family:var(--ff-mono,monospace);font-size:10px;color:var(--ink-mute);display:flex;align-items:center;justify-content:flex-end;padding-right:5px}
.iv-cell{border:1px solid var(--line);border-radius:6px;min-height:30px;font-size:11px;font-weight:700;cursor:pointer;padding:0;line-height:1}
.iv-cell.iv-free{background:#e7f6ec;border-color:#bcdfc4;color:#188038}
.iv-cell.iv-free:hover{background:#d3efdc;border-color:#188038}
.iv-cell.iv-sel{background:var(--accent);border-color:var(--accent);color:#fff}
.iv-cell.iv-none{background:var(--bg-app);border-color:var(--line);color:transparent;cursor:default}
.iv-note{font-size:11px;color:var(--ink-mute);margin:12px 0 0}
.iv-grid-scroll{overflow-x:auto}
.iv-assign{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
.iv-assign[hidden]{display:none}
.iv-when{font-size:13px;font-weight:700;color:var(--ink)}
.iv-assign select{padding:8px 10px;border:1px solid var(--line-strong);border-radius:8px;background:var(--panel);color:var(--ink);font-size:14px;min-width:150px}
.iv-assign-btn{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:9px 18px;font-size:14px;font-weight:700;cursor:pointer}
.iv-assign-btn:disabled{opacity:.6;cursor:default}
.iv-toast{font-size:13px;font-weight:600;color:var(--accent-deep,var(--accent));margin-top:10px;min-height:18px}
.iv-loading{font-size:13px;color:var(--ink-mute);padding:16px 0}
.iv-sobes{display:inline-flex;align-items:center;gap:5px;background:var(--accent-soft);color:var(--accent);border:1px solid transparent;border-radius:var(--r-full);padding:2px 10px;font-size:12px;font-weight:700;cursor:pointer;line-height:1.6}
.iv-sobes:hover{border-color:var(--accent)}
@media(max-width:760px){
  .iv-modal{padding:0;align-items:flex-end}
  .iv-modal-panel{width:100%;max-height:94vh;border-radius:18px 18px 0 0;border-bottom:0}
  .iv-modal-body{padding:12px 14px 22px}
}
</style>"""


_IV_SCRIPT = """<script>
(function(){
  window._ivState={mailbox:'',thread:'',hash:''};
  window._ivStart='';
  window._ivCurMonday='';
  window._ivCtx='';   // "&company=…&jobid=…" — resolved once, threaded through week nav
  function el(id){return document.getElementById(id);}
  function ivToast(m){var t=el('ivToast'); if(t) t.textContent=m||'';}
  function ivHideAssign(){var a=el('ivAssign'); if(a) a.setAttribute('hidden',''); window._ivStart='';
    var s=document.querySelector('.iv-cell.iv-sel'); if(s) s.classList.remove('iv-sel');}
  function ivLoadWeek(monday, ctx){
    var g=el('ivGrid'); if(!g) return;
    g.innerHTML='<div class="iv-loading">Загрузка…</div>'; ivHideAssign();
    var url='/mail/interview/grid?mailbox='+encodeURIComponent(window._ivState.mailbox||'')
      +'&monday='+encodeURIComponent(monday||'')+(ctx||'');
    fetch(url).then(function(r){return r.text();}).then(function(html){
      g.innerHTML='<div class="iv-grid-scroll">'+html+'</div>';
      var nav=g.querySelector('[data-cur-monday]');
      window._ivCurMonday=nav?nav.getAttribute('data-cur-monday'):'';
      window._ivCtx=nav?(nav.getAttribute('data-ctx')||''):'';
    }).catch(function(){ g.innerHTML='<div class="iv-loading">Не удалось загрузить сетку.</div>'; });
  }
  window.openSobes=function(mailbox,thread,hash){
    window._ivState={mailbox:mailbox||'',thread:thread||'',hash:hash||''};
    var m=el('ivModal'); if(!m) return;
    m.removeAttribute('hidden'); document.body.style.overflow='hidden';
    ivToast(''); ivLoadWeek('');
  };
  window.closeSobes=function(){
    var m=el('ivModal'); if(!m) return;
    m.setAttribute('hidden',''); document.body.style.overflow='';
  };
  function ivPickCell(cell){
    var prev=document.querySelector('.iv-cell.iv-sel'); if(prev) prev.classList.remove('iv-sel');
    cell.classList.add('iv-sel');
    window._ivStart=cell.getAttribute('data-start')||'';
    // data-free is a JSON [{"id","name"},…] array — a name may contain commas/colons.
    var free=[]; try{ free=JSON.parse(cell.getAttribute('data-free')||'[]'); }catch(e){ free=[]; }
    var sel=el('ivResp'); if(!sel) return; sel.innerHTML='';
    free.forEach(function(r){ if(!r) return;
      var o=document.createElement('option'); o.value=r.id; o.textContent=r.name; sel.appendChild(o); });
    var w=el('ivWhen'); if(w) w.textContent=cell.getAttribute('data-label')||window._ivStart;
    var a=el('ivAssign'); if(a) a.removeAttribute('hidden'); ivToast('');
  }
  window.ivDoAssign=function(){
    var sel=el('ivResp');
    if(!sel||!sel.value){ ivToast('Выберите ответственного'); return; }
    var fd=new FormData();
    fd.append('mailbox', window._ivState.mailbox||'');
    fd.append('responsible_id', sel.value);
    fd.append('start_iso', window._ivStart||'');
    fd.append('company', (el('ivCompany')||{}).value||'');
    fd.append('jobid', (el('ivJobid')||{}).value||'');
    fd.append('thread_key', window._ivState.thread||'');
    fd.append('source_message_hash', window._ivState.hash||'');
    var btn=el('ivAssignBtn'); if(btn) btn.disabled=true;
    fetch('/mail/interview/assign',{method:'POST',body:fd}).then(function(r){
      if(r.ok){ ivToast('Собеседование назначено ✓'); ivHideAssign(); ivLoadWeek(window._ivCurMonday||'', window._ivCtx); }
      else if(r.status===409){ ivToast('Этот слот уже занят — выберите другой'); ivLoadWeek(window._ivCurMonday||'', window._ivCtx); }
      else { ivToast('Не удалось назначить'); }
    }).catch(function(){ ivToast('Ошибка сети'); })
      .then(function(){ if(btn) btn.disabled=false; });
  };
  document.addEventListener('click',function(e){
    if(!e.target||!e.target.closest) return;
    var g=el('ivGrid');
    var wk=e.target.closest('.iv-wk');
    if(wk && g && g.contains(wk)){ e.preventDefault();
      var nav=g.querySelector('[data-ctx]');
      ivLoadWeek(wk.getAttribute('data-monday'), nav?(nav.getAttribute('data-ctx')||''):window._ivCtx);
      return; }
    var cell=e.target.closest('.iv-cell.iv-free');
    if(cell && g && g.contains(cell)){ e.preventDefault(); ivPickCell(cell); return; }
  });
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape'){ var m=el('ivModal'); if(m && !m.hasAttribute('hidden')) window.closeSobes(); }
  });
})();
</script>"""


def modal_shell() -> str:
    """The one-time #ivModal container (backdrop, ✕, week-grid slot, assign row, toast)
    plus its <style>/<script>. Inject ONCE per inbox/thread page."""
    return (
        '<div class="iv-modal" id="ivModal" hidden>'
        '<div class="iv-modal-backdrop" onclick="closeSobes()"></div>'
        '<div class="iv-modal-panel" role="dialog" aria-modal="true" aria-label="Назначить собеседование">'
        '<div class="iv-modal-head"><span class="iv-modal-title">Назначить собеседование</span>'
        '<button type="button" class="iv-modal-x" onclick="closeSobes()" aria-label="Закрыть">&#10005;</button></div>'
        '<div class="iv-modal-body">'
        '<div id="ivGrid"><div class="iv-loading">Загрузка…</div></div>'
        '<div class="iv-assign" id="ivAssign" hidden>'
        '<span class="iv-when" id="ivWhen"></span>'
        '<select id="ivResp" aria-label="Ответственный"></select>'
        '<button type="button" class="iv-assign-btn" id="ivAssignBtn" onclick="ivDoAssign()">Назначить</button>'
        '</div>'
        '<div class="iv-toast" id="ivToast" role="status" aria-live="polite"></div>'
        '</div></div></div>'
        + _IV_STYLE + _IV_SCRIPT
    )
