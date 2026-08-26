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
.mh-refresh{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--card);
  color:var(--ink);border-radius:9px;padding:8px 13px;font:inherit;font-weight:600;font-size:13px;cursor:pointer;}
.mh-refresh:hover{background:var(--hover);}
.mh-chips{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 18px;}
.mh-chip{border:1px solid var(--line);border-radius:999px;padding:6px 13px;font-size:13px;font-weight:600;
  color:var(--ink-soft);text-decoration:none;background:var(--card);white-space:nowrap;}
.mh-chip.on{background:var(--accent,#2f6fed);border-color:var(--accent,#2f6fed);color:#fff;}
.mh-card{border:1px solid var(--line);border-radius:14px;background:var(--card);margin-bottom:12px;overflow:hidden;}
.mh-crow{display:flex;align-items:center;gap:12px;padding:14px 16px;cursor:pointer;list-style:none;}
.mh-crow::-webkit-details-marker{display:none;}
.mh-score{flex:0 0 auto;width:46px;height:46px;border-radius:11px;display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:17px;font-variant-numeric:tabular-nums;}
.mh-score.hi{background:#0f7b3e;color:#fff;}.mh-score.mid{background:#e6f0ff;color:#1a4fb0;}
.mh-score.lo{background:var(--hover);color:var(--ink-soft);}
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
@media(max-width:760px){.mh-meta{margin-left:0;text-align:left;width:100%;}
  .mh-job{padding-left:16px;}.mh-apply{margin-left:0;}}
</style>
"""


def _job_row(j: dict) -> str:
    url = _esc(j.get("apply_url"))
    title = _esc(j.get("title"))
    loc = _esc(j.get("location_raw") or "Remote")
    sal = ""
    if j.get("salary_min") or j.get("salary_max"):
        lo, hi = j.get("salary_min") or 0, j.get("salary_max") or 0
        sal = f' · <span class="mh-jloc">${lo:,}–${hi:,}</span>' if lo and hi else ""
    return (f'<div class="mh-job"><a class="mh-jtitle" href="{url}" target="_blank" '
            f'rel="noopener">{title}</a><span class="mh-jloc">{loc}</span>{sal}'
            f'<a class="mh-apply" href="{url}" target="_blank" rel="noopener">подать вручную →</a></div>')


def _company_card(c: dict, category: str | None) -> str:
    key = c.get("company_key")
    js = mass_hiring.jobs(company_key=key, category=category, limit=60)
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


def render_page(category: str | None = None) -> str:
    st = mass_hiring.stats()
    cos = mass_hiring.companies(category=category or None, limit=200)
    # category chips
    chips = [('<a class="mh-chip {on}" href="/mass-hiring">Все</a>').format(on="" if category else "on")]
    for k, lbl in mass_hiring.CATEGORY_LABELS.items():
        n = st["by_category"].get(k, 0)
        if not n and k != category:
            continue
        on = "on" if category == k else ""
        chips.append(f'<a class="mh-chip {on}" href="/mass-hiring?category={k}">{lbl} · {n}</a>')

    body = "".join(_company_card(c, category or None) for c in cos) or \
        ('<div class="mh-empty">Пока пусто. Нажми «Обновить», чтобы собрать вакансии '
         'из источников (Conduent / Alorica / Himalayas / …).</div>')

    head = (
        f'<div class="mh-wrap">{_CSS}'
        f'<div class="mh-head"><div><h1>Mass Hiring</h1>'
        f'<p class="mh-sub">Remote · US · масс-хайринг — подаёшься вручную (бот сюда не подаёт)</p></div>'
        f'<div class="mh-meta">{st["active"]} вакансий · {st["companies"]} компаний<br>'
        f'обновлено {_ago(st.get("last_collected", 0))}'
        f'<form method="post" action="/mass-hiring/collect" style="margin-top:8px">'
        f'<button class="mh-refresh" type="submit">↻ Обновить</button></form></div></div>'
        f'<div class="mh-chips">{"".join(chips)}</div>'
        f'{body}</div>')
    return mailcrm_ui._page("masshiring", head)
