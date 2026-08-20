"""Unified "all jobs" feed across every tracked ATS board — served at /jobs.

Mobile-first card list. Jobs come LIVE from each company's public ATS board
(reusing ``roles_dashboard.fetch_jobs`` + its ~10-min per-company cache); the
aggregate "all companies" list is fetched in parallel and cached the same TTL.
Per-posting status badges are read from our own tracker
(``roles_dashboard.counts_by_url``) and stay empty ("— не подавались") until the
apply engine actually files applications.

The page reuses the mail-CRM shell (``mailcrm_ui._page``) so it inherits the same
sidebar + mobile styling; only the jobs-specific CSS/JS live here.
"""
from __future__ import annotations

import concurrent.futures
import html
import time
import urllib.parse

from backend.tools import mailcrm_ui
from backend.tools import roles_dashboard as rd

esc = html.escape

PAGE = 30
_ALL_TTL = 600  # seconds
_all_cache: dict = {"jobs": None, "ts": 0.0}


# ---- data ----------------------------------------------------------------------
def companies() -> list[dict]:
    """Renderable target companies (ashby/greenhouse/lever), registry order."""
    return rd.supported_targets()


def fetch_company(key: str) -> list[dict]:
    """One company's live postings, each tagged with its company key + name."""
    tgt = rd._target(key)
    ckey = tgt.get("key", key)
    name = tgt.get("company", ckey.title())
    jobs = rd.fetch_jobs(ckey)
    for j in jobs:
        j["_ck"] = ckey
        j["_cname"] = name
    return jobs


def fetch_all(force: bool = False) -> list[dict]:
    """Every tracked board, fetched in parallel and cached. A board that fails to
    fetch is skipped (never blocks the whole feed)."""
    now = time.time()
    if not force and _all_cache["jobs"] is not None and now - _all_cache["ts"] < _ALL_TTL:
        return _all_cache["jobs"]
    targets = [t for t in companies() if t.get("key")]
    out: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_company, t["key"]) for t in targets]
        for fut in concurrent.futures.as_completed(futs):
            try:
                out.extend(fut.result())
            except Exception:
                pass
    out.sort(key=lambda j: (not rd._is_online(j), (j.get("_cname") or ""), (j.get("title") or "")))
    _all_cache["jobs"] = out
    _all_cache["ts"] = now
    return out


def _dataset(company: str, q: str, remote: bool) -> list[dict]:
    jobs = fetch_company(company) if company else fetch_all()
    if remote:
        jobs = [j for j in jobs if rd._is_remote(j)]
    if q:
        ql = q.lower()
        jobs = [j for j in jobs if ql in (
            f'{j.get("title","")} {j.get("_cname","")} {j.get("location") or ""} '
            f'{j.get("department") or ""} {j.get("descriptionPlain") or ""}').lower()]
    return jobs


def _status_map() -> dict:
    try:
        return rd.counts_by_url("")
    except Exception:
        return {}


# ---- rendering -----------------------------------------------------------------
def _qs(company: str = "", q: str = "", remote: bool = False, **extra) -> str:
    d: dict = {}
    if company:
        d["company"] = company
    if q:
        d["q"] = q
    if remote:
        d["remote"] = "1"
    for k, v in extra.items():
        if v:
            d[k] = v
    return ("?" + urllib.parse.urlencode(d)) if d else ""


def _job_url(j: dict) -> str:
    return (j.get("applyUrl") or j.get("jobUrl") or "").split("?")[0]


def _status_badge(j: dict, smap: dict) -> str:
    c = smap.get(_job_url(j)) if _job_url(j) else None
    if not c or not (c.get("submitted") or c.get("accepted") or c.get("rejected")):
        return '<span class="jc-st jc-st-none">— не подавались</span>'
    parts = []
    if c.get("submitted"):
        parts.append(f'<span class="jc-b sub">Подано {c["submitted"]}</span>')
    if c.get("accepted"):
        parts.append(f'<span class="jc-b acc">Интервью {c["accepted"]}</span>')
    if c.get("rejected"):
        parts.append(f'<span class="jc-b rej">Отказ {c["rejected"]}</span>')
    return '<span class="jc-st">' + "".join(parts) + "</span>"


def _card(j: dict, smap: dict) -> str:
    title = esc(j.get("title", "") or "(без названия)")
    cname = esc(j.get("_cname", "") or "")
    url = _job_url(j)
    wt = rd._workplace(j)
    wt_cls = {"remote": "wp-remote", "hybrid": "wp-hybrid"}.get(wt.lower(), "wp-onsite")
    loc = esc(j.get("location") or "")
    dept = esc(j.get("department") or "")
    meta_bits = []
    if loc:
        meta_bits.append(f'<span class="jc-loc">📍 {loc}</span>')
    if dept:
        meta_bits.append(f'<span class="jc-dept">{dept}</span>')
    meta = f'<div class="jc-meta">{"".join(meta_bits)}</div>' if meta_bits else ""
    desc = j.get("descriptionHtml") or ("<p>" + esc(j.get("descriptionPlain", "") or "") + "</p>")
    open_link = (f'<a class="jc-open" href="{esc(url)}" target="_blank" rel="noopener">Открыть ↗</a>'
                 if url else "")
    return (
        '<article class="jcard">'
        f'<div class="jc-top"><span class="jc-co">{cname}</span>'
        f'<span class="wp {wt_cls}">{esc(wt)}</span></div>'
        f'<div class="jc-title">{title}</div>{meta}'
        f'<div class="jc-row">{_status_badge(j, smap)}{open_link}</div>'
        f'<details class="jc-desc"><summary>Описание</summary><div class="desc">{desc}</div></details>'
        "</article>")


def _chips(active: str, q: str, remote: bool) -> str:
    def chip(key: str, label: str, on: bool) -> str:
        cls = "chip on" if on else "chip"
        href = "/jobs" + _qs(company=key, q=q, remote=remote)
        return f'<a class="{cls}" href="{href}">{esc(label)}</a>'
    out = [chip("", "Все", not active)]
    for t in companies():
        k = t.get("key", "")
        out.append(chip(k, t.get("company", k), active == k))
    return f'<div class="chips">{"".join(out)}</div>'


def render_page(company: str = "", q: str = "", remote: bool = False) -> str:
    company = (company or "").lower()
    jobs = _dataset(company, q, remote)
    total = len(jobs)
    smap = _status_map()
    cards = "".join(_card(j, smap) for j in jobs[:PAGE])
    has_more = 1 if total > PAGE else 0
    title_txt = "Все вакансии" if not company else esc(rd._target(company).get("company", company))
    rem_href = "/jobs" + _qs(company=company, q=q, remote=not remote)
    rem_cls = "rtoggle on" if remote else "rtoggle"
    search = (
        '<form class="jsearch" method="get" action="/jobs">'
        + (f'<input type="hidden" name="company" value="{esc(company)}">' if company else "")
        + (f'<input type="hidden" name="remote" value="1">' if remote else "")
        + f'<input type="search" name="q" value="{esc(q)}" placeholder="Поиск: должность, компания, локация…">'
        + '<button class="ghost" type="submit">Найти</button>'
        + (f'<a class="ghost" href="/jobs{_qs(company=company, remote=remote)}">Сброс</a>' if q else "")
        + "</form>")
    head = (
        '<div class="jhead"><div class="jh-row1">'
        f'<div class="jh-title">{title_txt} <span class="jh-n">{total}</span></div>'
        f'<a class="{rem_cls}" href="{rem_href}">Только Remote</a></div>'
        f"{search}</div>")
    empty = '<div class="empty">Вакансий не найдено</div>' if not jobs else ""
    body = (
        _JOBS_CSS + head + _chips(company, q, remote)
        + f'<div class="joblist" id="joblist">{cards}</div>{empty}'
        + f'<div id="jobmore" data-more="{has_more}" data-offset="{PAGE}" style="height:1px"></div>'
        + _JOBS_JS)
    return mailcrm_ui._page("jobs", body)


def render_more(company: str = "", q: str = "", offset: int = 0, remote: bool = False) -> str:
    company = (company or "").lower()
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    jobs = _dataset(company, q, remote)
    smap = _status_map()
    return "".join(_card(j, smap) for j in jobs[offset:offset + PAGE])


_JOBS_CSS = """<style>
.jhead{display:flex;flex-direction:column;gap:10px;margin-bottom:6px}
.jh-row1{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.jh-title{font-size:19px;font-weight:700;color:var(--ink)}
.jh-n{color:var(--ink-mute);font-weight:600;font-size:14px;margin-left:4px}
.rtoggle{font-size:12.5px;font-weight:600;color:var(--ink-mute);text-decoration:none;border:1px solid var(--line);border-radius:999px;padding:6px 12px;white-space:nowrap}
.rtoggle.on{color:#188038;border-color:#188038;background:#e6f4ea}
.jsearch{display:flex;gap:8px;flex-wrap:wrap}
.jsearch input[type=search]{flex:1;min-width:0}
.chips{display:flex;gap:7px;overflow-x:auto;padding:2px 0 12px;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{white-space:nowrap;padding:7px 13px;border-radius:999px;border:1px solid var(--line);color:var(--ink-mute);background:var(--panel);font-size:13px;font-weight:600;text-decoration:none;min-height:36px;display:flex;align-items:center}
.chip.on{background:#1a73e8;border-color:#1a73e8;color:#fff}
.joblist{display:flex;flex-direction:column;gap:10px}
.jcard{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:13px 14px}
.jc-top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:3px}
.jc-co{font-size:12px;font-weight:700;color:var(--ink-mute);text-transform:uppercase;letter-spacing:.03em}
.wp{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:999px;border:1px solid var(--line);text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
.wp-remote{color:#188038;border-color:#bcdfc4}.wp-hybrid{color:#1a73e8;border-color:#b8d3f5}.wp-onsite{color:var(--ink-mute)}
.jc-title{font-size:15.5px;font-weight:600;color:var(--ink);line-height:1.3;margin-bottom:5px}
.jc-meta{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}
.jc-meta span{font-size:12.5px;color:var(--ink-mute)}
.jc-row{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.jc-st{display:flex;gap:6px;flex-wrap:wrap;font-size:12.5px}
.jc-st-none{color:var(--ink-mute)}
.jc-b{font-size:11.5px;font-weight:700;padding:2px 8px;border-radius:6px}
.jc-b.sub{color:#1a73e8;background:#e8f0fe}.jc-b.acc{color:#188038;background:#e6f4ea}.jc-b.rej{color:#d93025;background:#fce8e6}
.jc-open{font-size:13.5px;font-weight:600;color:#1a73e8;text-decoration:none;white-space:nowrap;padding:6px 0;min-height:36px;display:inline-flex;align-items:center}
.jc-desc{margin-top:9px}
.jc-desc>summary{cursor:pointer;color:var(--ink-mute);font-size:13px;user-select:none;padding:5px 0}
.jc-desc[open]>summary{color:#1a73e8;margin-bottom:6px}
.jc-desc .desc{font-size:13.5px;line-height:1.55;color:var(--ink);max-height:360px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:11px 12px;background:var(--bg-app)}
.jc-desc .desc img{max-width:100%;height:auto}
.jc-desc .desc table{max-width:100%;display:block;overflow-x:auto}
.jc-desc .desc a{color:#1a73e8}
.empty{color:var(--ink-mute);text-align:center;padding:44px 0}
@media(max-width:760px){.jh-title{font-size:17px}.jc-title{font-size:15px}}
</style>"""

_JOBS_JS = """<script>
(function(){
  var list=document.getElementById('joblist'), more=document.getElementById('jobmore');
  if(!list||!more)return;
  var loading=false, PAGE=30;
  async function loadMore(){
    if(loading||more.dataset.more!=='1')return;
    loading=true;
    try{
      var sp=new URLSearchParams(location.search);
      sp.set('offset', more.dataset.offset);
      var r=await fetch('/jobs/more?'+sp.toString());
      if(r.ok){
        var txt=await r.text();
        var added=(txt.match(/class="jcard"/g)||[]).length;
        if(added){list.insertAdjacentHTML('beforeend',txt);
          more.dataset.offset=String((parseInt(more.dataset.offset,10)||0)+added);}
        if(added<PAGE)more.dataset.more='0';
      }
    }catch(e){}finally{loading=false;}
  }
  window.addEventListener('scroll',function(){
    if(window.innerHeight+window.scrollY>=document.documentElement.scrollHeight-500)loadMore();
  },{passive:true});
})();
</script>"""
