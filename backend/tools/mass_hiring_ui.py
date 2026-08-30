"""Server-rendered «Mass Hiring» tab — REMOTE-only, mass-hiring US jobs the human applies to
by hand. Reads backend.tools.mass_hiring (the SEPARATE mass_hiring_jobs table), renders companies
ranked by mass_hiring_score with an expandable job list, each job linking OUT to its own apply
page. No auto-apply anywhere — this is a discovery surface, decoupled from the /catalog engine."""
from __future__ import annotations

import html
import time

from backend.tools import mailcrm_ui, mass_hiring

_SRC_LABEL = {"conduent": "Conduent", "alorica": "Alorica", "concentrix": "Concentrix",
              "amazon": "Amazon", "himalayas": "Himalayas", "remotive": "Remotive",
              "remoteok": "RemoteOK"}

# Neutral RU labels for the employer-segment heuristic (no stack disclosure).
_SEG_LABEL = {"staffing": "Кадровые / аутсорсинг", "government": "Госсектор",
              "education": "Образование", "healthcare": "Здравоохранение",
              "nonprofit": "НКО", "general": "Крупный работодатель"}


_JS = """
<script>
async function mhFill(id, btn){
  if(!id) return;
  btn.disabled = true; btn.textContent = 'Готовлю…';
  try{
    const r = await fetch('/mass-hiring/' + id + '/fill', {method:'POST'});
    const j = await r.json();
    if(j.novnc) window.open(j.novnc, '_blank', 'noopener');
    btn.textContent = 'Заполняется — смотри в окне';
    mhPoll(id, btn);
  }catch(e){ btn.textContent = 'Ошибка'; btn.disabled = false; }
}
async function mhPoll(id, btn){
  try{
    const r = await fetch('/mass-hiring/' + id + '/fill_status');
    const j = await r.json();
    if(j.state === 'done'){ btn.textContent = j.dry_run ? 'Заполнено (тест) ✓' : 'Подано ✓'; return; }
    if(j.state === 'error'){ btn.textContent = 'Ошибка: ' + (j.error || ''); btn.disabled = false; return; }
    setTimeout(function(){ mhPoll(id, btn); }, 3000);
  }catch(e){ setTimeout(function(){ mhPoll(id, btn); }, 5000); }
}
</script>
"""


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _ago(ts: int) -> str:
    if not ts:
        return "—"
    d = int(time.time()) - int(ts)
    if d < 3600:
        return f"{d // 60} мин назад"
    if d < 86400:
        return f"{d // 3600} ч назад"
    return f"{d // 86400} дн назад"


def _score_cls(s: int) -> str:
    return "hi" if s >= 40 else ("mid" if s >= 15 else "lo")


_CSS = """
<style>
.mh-wrap{max-width:1040px;margin:0 auto;padding:18px 16px 60px;}
.mh-head{display:flex;flex-wrap:wrap;align-items:flex-end;gap:12px;margin-bottom:6px;}
.mh-head h1{font-size:26px;font-weight:700;margin:0;letter-spacing:-.02em;}
.mh-sub{color:var(--ink-soft);font-size:14px;margin:2px 0 0;}
.mh-meta{margin-left:auto;text-align:right;color:var(--ink-soft);font-size:13px;line-height:1.5;}
.mh-refresh{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--panel);
  color:var(--ink);border-radius:9px;padding:8px 13px;font:inherit;font-weight:600;font-size:13px;cursor:pointer;}
.mh-refresh:hover{background:var(--panel-2);}
.mh-chips{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 18px;}
.mh-chip{border:1px solid var(--line);border-radius:999px;padding:6px 13px;font-size:13px;font-weight:600;
  color:var(--ink-soft);text-decoration:none;background:var(--panel);white-space:nowrap;}
.mh-chip.on{background:var(--accent,#2f6fed);border-color:var(--accent,#2f6fed);color:#fff;}
.mh-chip-star.on{background:#f5a623;border-color:#f5a623;color:#fff;}
.mh-star{flex:0 0 auto;color:#f5a623;font-size:15px;line-height:1;margin-right:5px;
  text-shadow:0 1px 3px rgba(245,166,35,.45);}
.mh-pay{font-size:12.5px;font-weight:700;color:#0f7b3e;font-variant-numeric:tabular-nums;white-space:nowrap;}
.mh-pay.est{color:var(--ink-soft);font-weight:600;}
.mh-est-t{font-size:10px;font-weight:600;opacity:.65;}
.mh-fill{margin-left:8px;border:1px solid var(--accent,#2f6fed);background:var(--accent,#2f6fed);
  color:#fff;border-radius:8px;padding:5px 11px;font:inherit;font-size:12px;font-weight:700;
  cursor:pointer;white-space:nowrap;}
.mh-fill:hover{filter:brightness(1.07);}
.mh-fill:disabled{opacity:.6;cursor:default;}
.mh-card{border:1px solid var(--line);border-radius:14px;background:var(--panel);margin-bottom:12px;overflow:hidden;}
.mh-crow{display:flex;align-items:center;gap:12px;padding:14px 16px;cursor:pointer;list-style:none;}
.mh-crow::-webkit-details-marker{display:none;}
.mh-score{flex:0 0 auto;width:46px;height:46px;border-radius:11px;display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:17px;font-variant-numeric:tabular-nums;}
.mh-score.hi{background:#0f7b3e;color:#fff;}.mh-score.mid{background:#e6f0ff;color:#1a4fb0;}
.mh-score.lo{background:var(--panel-2);color:var(--ink-soft);}
.mh-cinfo{min-width:0;flex:1 1 auto;}
.mh-cname{font-weight:700;font-size:16px;letter-spacing:-.01em;}
.mh-cstats{color:var(--ink-soft);font-size:13px;margin-top:2px;}
.mh-src{display:inline-block;font-size:11px;font-weight:600;color:var(--ink-soft);border:1px solid var(--line);
  border-radius:6px;padding:1px 6px;margin-left:6px;vertical-align:1px;}
.mh-caret{flex:0 0 auto;color:var(--ink-soft);transition:transform .15s;}
details[open] .mh-caret{transform:rotate(90deg);}
.mh-jobs{border-top:1px solid var(--line);}
.mh-job{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 10px;padding:11px 16px 11px 74px;border-top:1px solid var(--line);}
.mh-job:first-child{border-top:none;}
.mh-jtitle{font-weight:600;font-size:14px;color:var(--ink);text-decoration:none;}
.mh-jtitle:hover{text-decoration:underline;color:var(--accent,#2f6fed);}
.mh-jloc{color:var(--ink-soft);font-size:12.5px;}
.mh-apply{margin-left:auto;font-size:12.5px;font-weight:600;color:var(--accent,#2f6fed);text-decoration:none;white-space:nowrap;}
.mh-empty{text-align:center;color:var(--ink-soft);padding:60px 20px;}
.mh-emp{border:1px solid var(--line);border-radius:14px;background:var(--panel);margin-bottom:18px;overflow:hidden;}
.mh-emp-sum{display:flex;align-items:center;gap:10px;padding:13px 16px;cursor:pointer;list-style:none;}
.mh-emp-sum::-webkit-details-marker{display:none;}
.mh-emp-t{font-weight:700;font-size:15px;letter-spacing:-.01em;}
.mh-emp-c{color:var(--ink-soft);font-size:12.5px;margin-left:auto;}
.mh-emp-list{border-top:1px solid var(--line);}
.mh-emp-row{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 10px;padding:10px 16px;border-top:1px solid var(--line);}
.mh-emp-row:first-child{border-top:none;}
.mh-emp-n{font-weight:600;font-size:13.5px;color:var(--ink);}
.mh-emp-seg{font-size:11px;font-weight:600;color:var(--ink-soft);border:1px solid var(--line);border-radius:6px;padding:1px 6px;}
.mh-emp-s{color:var(--ink-soft);font-size:12px;margin-left:auto;font-variant-numeric:tabular-nums;}
.mh-emp-note{color:var(--ink-soft);font-size:12px;padding:10px 16px 13px;border-top:1px solid var(--line);}
@media(max-width:760px){.mh-meta{margin-left:0;text-align:left;width:100%;}
  .mh-job{padding-left:16px;}.mh-apply{margin-left:0;}.mh-emp-s{margin-left:0;}}
</style>
"""


def _fmt_rate(x: float) -> str:
    return f"${x:.0f}" if abs(x - round(x)) < 0.5 else f"${x:.1f}"


def _pay_html(j: dict) -> str:
    """Hourly pay next to the job: real posted rate (green) or a labeled estimate (muted)."""
    hp = mass_hiring.hourly_pay(j)
    if not hp:
        return ""
    lo, hi, est = hp
    rng = _fmt_rate(lo) if abs(lo - hi) < 0.5 else f"{_fmt_rate(lo)}–{_fmt_rate(hi)[1:]}"
    if est:
        return (f'<span class="mh-pay est" title="Оценка по типу роли — точную ставку '
                f'смотри в вакансии">≈{rng}/ч <span class="mh-est-t">оц.</span></span>')
    return f'<span class="mh-pay" title="Ставка из вакансии">{rng}/ч</span>'


def _job_row(j: dict) -> str:
    url = _esc(j.get("apply_url"))
    title = _esc(j.get("title"))
    loc = _esc(j.get("location_raw") or "Remote")
    star = ('<span class="mh-star" title="Стабильная оплата (не комиссия)">★</span>'
            if j.get("comp_type") != "variable" else "")
    # Auto-fill (dry-run) is supported only where we have a working strategy — Avature (Maximus).
    fill = ""
    if "avature.net" in (j.get("apply_url") or "").lower():
        fill = (f'<button class="mh-fill" type="button" onclick="mhFill({int(j.get("id") or 0)},this)" '
                f'title="Заполнить форму автоматически (тест — ничего не отправляется)">Заполнить (тест)</button>')
    return (f'<div class="mh-job">{star}<a class="mh-jtitle" href="{url}" target="_blank" '
            f'rel="noopener">{title}</a><span class="mh-jloc">{loc}</span>{_pay_html(j)}'
            f'<a class="mh-apply" href="{url}" target="_blank" rel="noopener">подать вручную →</a>{fill}</div>')


def _company_card(c: dict, category: str | None, comp: str | None = None) -> str:
    key = c.get("company_key")
    js = mass_hiring.jobs(company_key=key, category=category, limit=60, comp=comp)
    src = {j.get("source") for j in js}
    src_tag = "".join(f'<span class="mh-src">{_SRC_LABEL.get(s, s)}</span>' for s in sorted(src))
    score = int(c.get("mass_hiring_score") or 0)
    caret = ('<svg class="mh-caret" width="18" height="18" viewBox="0 0 24 24" fill="none" '
             'stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>')
    stats = f'{c["active_jobs"]} вакансий'
    if c.get("cs_jobs"):
        stats += f' · {c["cs_jobs"]} customer support'
    return (
        f'<details class="mh-card"><summary class="mh-crow">'
        f'<div class="mh-score {_score_cls(score)}">{score}</div>'
        f'<div class="mh-cinfo"><div class="mh-cname">{_esc(c.get("company"))}{src_tag}</div>'
        f'<div class="mh-cstats">{stats}</div></div>{caret}</summary>'
        f'<div class="mh-jobs">{"".join(_job_row(j) for j in js)}</div></details>')


def _everify_panel(limit: int = 40) -> str:
    """Compact reference panel of large US employers (10k+ workforce) ranked by hiring-site
    count — a mass-hiring SIGNAL, not job postings. Reads ONLY the cached file (no network on
    render) and is fully guarded: any error yields an empty string so the board never breaks."""
    try:
        from backend.tools import everify_employers
        data = everify_employers.load_cached()
        emps = (data.get("employers") or [])[:limit]
        if not emps:
            return ""
        rows = []
        for e in emps:
            seg = _SEG_LABEL.get(e.get("segment") or "general", "Крупный работодатель")
            states = list(e.get("states") or [])[:5]
            extra = e.get("additional_state_count") or 0
            geo = ", ".join(states) + (f" +{extra}" if extra else "")
            sites = int(e.get("hiring_sites") or 0)
            rows.append(
                f'<div class="mh-emp-row"><span class="mh-emp-n">{_esc(e.get("name"))}</span>'
                f'<span class="mh-emp-seg">{_esc(seg)}</span>'
                f'<span class="mh-emp-s">{sites:,} площадок найма'
                + (f' · {_esc(geo)}' if geo else '') + '</span></div>')
        total = int(data.get("count") or len(emps))
        return (
            f'<details class="mh-emp"><summary class="mh-emp-sum">'
            f'<span class="mh-emp-t">Крупные работодатели США (10 000+ сотрудников)</span>'
            f'<span class="mh-emp-c">{total} компаний · по числу площадок найма</span></summary>'
            f'<div class="mh-emp-list">{"".join(rows)}</div>'
            f'<div class="mh-emp-note">Справочный список крупнейших работодателей США — '
            f'сигнал масс-хайринга для ручного поиска вакансий (не автоподача).</div></details>')
    except Exception:
        return ""


def _qs(category: str | None, comp: str | None) -> str:
    parts = []
    if category:
        parts.append(f"category={category}")
    if comp:
        parts.append(f"comp={comp}")
    return ("?" + "&".join(parts)) if parts else ""


def render_page(category: str | None = None, comp: str | None = None) -> str:
    st = mass_hiring.stats()
    cos = mass_hiring.companies(category=category or None, limit=200, comp=comp or None)
    # category chips (preserve the active comp filter)
    chips = [f'<a class="mh-chip {"" if category else "on"}" '
             f'href="/mass-hiring{_qs(None, comp)}">Все</a>']
    for k, lbl in mass_hiring.CATEGORY_LABELS.items():
        n = st["by_category"].get(k, 0)
        if not n and k != category:
            continue
        on = "on" if category == k else ""
        chips.append(f'<a class="mh-chip {on}" href="/mass-hiring{_qs(k, comp)}">{lbl} · {n}</a>')
    # stable-comp toggle (★) — preserve the active category
    stable_on = "on" if comp == "fixed" else ""
    stable_href = _qs(category, None if comp == "fixed" else "fixed")
    chips.append(f'<a class="mh-chip mh-chip-star {stable_on}" '
                 f'href="/mass-hiring{stable_href}">★ Стабильная зп</a>')

    body = "".join(_company_card(c, category or None, comp or None) for c in cos) or \
        ('<div class="mh-empty">Пока пусто. Нажми «Обновить», чтобы собрать вакансии '
         'из источников (Conduent / Alorica / Himalayas / …).</div>')

    head = (
        f'<div class="mh-wrap">{_CSS}'
        f'<div class="mh-head"><div><h1>Mass Hiring</h1>'
        f'<p class="mh-sub">Remote · US · масс-хайринг — подаёшься вручную (бот сюда не подаёт)<br>'
        f'<span style="color:#f5a623">★</span> — стабильная оплата (не комиссия) · ставка «оц.» — оценка по типу роли</p></div>'
        f'<div class="mh-meta">{st["active"]} вакансий · {st["companies"]} компаний<br>'
        f'обновлено {_ago(st.get("last_collected", 0))}'
        f'<form method="post" action="/mass-hiring/collect" style="margin-top:8px">'
        f'<button class="mh-refresh" type="submit">↻ Обновить</button></form></div></div>'
        f'<div class="mh-chips">{"".join(chips)}</div>'
        f'{_everify_panel()}'
        f'{body}</div>{_JS}')
    return mailcrm_ui._page("masshiring", head)
