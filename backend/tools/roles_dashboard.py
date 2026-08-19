"""Live per-company roles dashboards — served by the dashboard app, not static files.

Originally Salmon-only; now generalized over the target registry
(`backend/data/targets.json`) so every company gets the SAME Salmon-style table:

  * job list + full descriptions come from that company's Ashby job-board API
    (per-company cache, ~10 min).
  * per-vacancy counts (Submitted / Accepted / Rejected) are computed LIVE from our
    own tracking (uploads/prefill/<profile>/report.json + status.json). Each report
    carries the `company` it was filed against, so counts group per company with NO
    extra network call.

A reset epoch (uploads/roles_reset.json, now a {company: ts} map) lets us "start from
zero" per company: only applications recorded AFTER that company's epoch are counted,
without deleting anything. An empty company key ("") is the global/back-compat view.

Routes are wired in backend/dashboard_app.py:
  GET  /roles                       -> index: every tracked company + its live counts
  GET  /roles?company=<key>         -> that company's live HTML table
  GET  /roles/counts?company=<key>  -> {apply_url: {submitted, accepted, rejected}, "_totals": {...}}
  GET  /roles/summary               -> {key: {company, submitted, accepted, rejected}} (network-free)
  GET  /roles/events?company=<key>  -> SSE push of the counts
  POST /roles/reset?company=<key>   -> set that company's epoch to now (zero its counts)
"""
from __future__ import annotations

import html
import json
import time
from collections import defaultdict
from pathlib import Path

from backend import status_store
from backend.applier import ats_boards
from backend.applier.runner import OUT_ROOT

RESET_FILE = OUT_ROOT.parent / "roles_reset.json"
_SUBMITTED_STATUSES = {"submitted", "interview", "rejected"}  # actually filed
_DEFAULT_COMPANY = "salmon"  # back-compat: bare fetch_jobs()/render target Salmon

# per-company job cache: {company_key: {"jobs": [...], "ts": float}}
_cache: dict[str, dict] = {}
_JOBS_TTL = 600  # seconds


# ---- target registry -----------------------------------------------------------
def list_targets() -> list[dict]:
    """Every registered company (enabled or not) from targets.json. The dashboard
    tracks ALL of them; the `enabled` flag only governs the apply engine. Falls back
    to Salmon-only if the registry is unreadable."""
    try:
        from backend.applier.batch import load_targets
        data = load_targets(enabled_only=False)
        if data:
            return data
    except Exception:
        pass
    return [{"key": "salmon", "company": "Salmon", "ats": "ashby",
             "slug": "salmon-group", "enabled": True}]


def supported_targets() -> list[dict]:
    """Registry entries whose ATS the dashboard can actually render (ashby, greenhouse,
    lever — all no-account public boards), in registry order."""
    return [t for t in list_targets() if t.get("ats", "ashby") in ats_boards.SUPPORTED]


# Back-compat alias (older callers / earlier name).
ashby_targets = supported_targets


def _target(company_key: str) -> dict:
    """Resolve a company key -> its target row (defaults to Salmon)."""
    key = (company_key or _DEFAULT_COMPANY).lower()
    for t in list_targets():
        if t.get("key", "").lower() == key:
            return t
    return {"key": "salmon", "company": "Salmon", "ats": "ashby", "slug": "salmon-group"}


def _name_to_key() -> dict[str, str]:
    """Map a report's stored company NAME ('OpenAI') -> registry key ('openai'),
    so counts (which read report['company']) group per company without the board API."""
    out: dict[str, str] = {}
    for t in list_targets():
        name = (t.get("company") or t.get("key") or "").strip().lower()
        if name:
            out[name] = t.get("key", "").lower()
    return out


# ---- reset epoch (per company) -------------------------------------------------
def _load_epochs() -> dict:
    try:
        raw = json.loads(RESET_FILE.read_text())
    except Exception:
        return {}
    if isinstance(raw, dict) and "ts" in raw and "_global" not in raw:
        # legacy single-epoch file {"ts": ...} -> treat as the global epoch
        return {"_global": float(raw.get("ts", 0.0))}
    return raw if isinstance(raw, dict) else {}


def read_epoch(company: str = "") -> float:
    """Epoch for a company: its own if set, else the global one, else 0."""
    ep = _load_epochs()
    key = (company or "").lower()
    if key and key in ep:
        try:
            return float(ep[key])
        except Exception:
            return 0.0
    try:
        return float(ep.get("_global", 0.0))
    except Exception:
        return 0.0


def reset(company: str = "") -> float:
    """Zero the counters for one company (or the global view when company='')."""
    now = time.time()
    ep = _load_epochs()
    ep[(company or "_global").lower()] = now
    RESET_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESET_FILE.write_text(json.dumps(ep), encoding="utf-8")
    return now


# ---- live counts from our tracker ----------------------------------------------
def _iter_apps():
    """Yield one dict per pre-filled application (skipping gated-out ones):
    {company_key, url, profile, jid, status, mtime}. Single pass over all reports."""
    n2k = _name_to_key()
    for rep_path in OUT_ROOT.glob("*/*/report.json"):
        try:
            mtime = rep_path.stat().st_mtime
            rep = json.loads(rep_path.read_text())
        except Exception:
            continue
        if rep.get("gated_out"):  # match gate rejected it — never pre-filled, don't count
            continue
        url = (rep.get("apply_url") or "").split("?")[0]
        if not url:
            continue
        profile, jid = rep_path.parents[1].name, rep_path.parent.name
        st = status_store.load(profile).get(jid, {}).get("status", "pending")
        ck = n2k.get((rep.get("company") or "").strip().lower(), "")
        yield {"company_key": ck, "url": url, "profile": profile,
               "jid": jid, "status": st, "mtime": mtime}


def counts_by_url(company: str = "") -> dict:
    """Per-URL {submitted, accepted, rejected} + `_totals`. When `company` is given,
    only that company's applications are counted (grouped by report['company']) and
    its own reset epoch applies; empty company = global back-compat view."""
    key = (company or "").lower()
    epoch = read_epoch(key)
    per: dict[str, dict[str, int]] = defaultdict(
        lambda: {"submitted": 0, "accepted": 0, "rejected": 0})
    for a in _iter_apps():
        if key and a["company_key"] != key:
            continue
        if a["mtime"] < epoch:  # older than this company's reset -> ignored
            continue
        st, url = a["status"], a["url"]
        if st in _SUBMITTED_STATUSES:
            per[url]["submitted"] += 1
        if st == "interview":
            per[url]["accepted"] += 1
        elif st == "rejected":
            per[url]["rejected"] += 1
    out: dict = {u: v for u, v in per.items()}
    out["_totals"] = {
        "submitted": sum(v["submitted"] for v in per.values()),
        "accepted": sum(v["accepted"] for v in per.values()),
        "rejected": sum(v["rejected"] for v in per.values()),
    }
    return out


def counts_by_company() -> dict:
    """Network-free aggregate per registry key -> {submitted, accepted, rejected},
    each honoring that company's reset epoch. Powers the index cards."""
    agg: dict[str, dict[str, int]] = defaultdict(
        lambda: {"submitted": 0, "accepted": 0, "rejected": 0})
    epochs = {k.get("key", "").lower(): read_epoch(k.get("key", "")) for k in list_targets()}
    for a in _iter_apps():
        ck = a["company_key"]
        if not ck:
            continue
        if a["mtime"] < epochs.get(ck, 0.0):
            continue
        st = a["status"]
        if st in _SUBMITTED_STATUSES:
            agg[ck]["submitted"] += 1
        if st == "interview":
            agg[ck]["accepted"] += 1
        elif st == "rejected":
            agg[ck]["rejected"] += 1
    return {k: v for k, v in agg.items()}


# ---- job list (per-company cache) ----------------------------------------------
def fetch_jobs(company: str = _DEFAULT_COMPANY, force: bool = False) -> list[dict]:
    key = (company or _DEFAULT_COMPANY).lower()
    c = _cache.get(key)
    if not force and c and c["jobs"] is not None and (time.time() - c["ts"]) < _JOBS_TTL:
        return c["jobs"]
    tgt = _target(key)
    jobs = ats_boards.fetch_board(tgt.get("ats", "ashby"), tgt["slug"])
    jobs.sort(key=lambda j: (not _is_online(j), j.get("title", "")))
    _cache[key] = {"jobs": jobs, "ts": time.time()}
    return jobs


def _workplace(job: dict) -> str:
    return job.get("workplaceType") or ("Remote" if job.get("isRemote") else "OnSite")


def _is_online(job: dict) -> bool:
    return _workplace(job).lower() in ("remote", "hybrid") or bool(job.get("isRemote"))


def _is_remote(job: dict) -> bool:
    """Strictly remote — excludes Hybrid (which requires some office days)."""
    return _workplace(job).lower() == "remote"


def _row(job: dict) -> str:
    title = html.escape(job.get("title", ""))
    url = (job.get("applyUrl") or job.get("jobUrl") or "").split("?")[0]
    wt = _workplace(job)
    loc = html.escape(job.get("location") or "")
    dept = html.escape(job.get("department") or "")
    desc = job.get("descriptionHtml") or ("<p>" + html.escape(job.get("descriptionPlain", "")) + "</p>")
    online = "1" if _is_online(job) else "0"
    wt_cls = {"remote": "wp-remote", "hybrid": "wp-hybrid"}.get(wt.lower(), "wp-onsite")
    blob = html.escape((job.get("title", "") + " " + dept + " " + loc + " " +
                        (job.get("descriptionPlain", "") or "")).lower())
    return f"""
<tr class="role" data-online="{online}" data-wp="{wt.lower()}" data-url="{html.escape(url)}" data-search="{blob}">
  <td class="c-pos"><div class="pos">{title}</div>
    <div class="meta"><span class="wp {wt_cls}">{html.escape(wt)}</span>
      {f'<span class="loc">{loc}</span>' if loc else ''}
      {f'<span class="dept">{dept}</span>' if dept else ''}</div></td>
  <td class="c-link">{f'<a href="{html.escape(url)}" target="_blank" rel="noopener">Apply&nbsp;↗</a>' if url else '—'}</td>
  <td class="c-desc"><details><summary>Показать описание</summary><div class="desc">{desc}</div></details></td>
  <td class="c-num sub" data-k="submitted">·</td>
  <td class="c-num acc" data-k="accepted">·</td>
  <td class="c-num rej" data-k="rejected">·</td>
</tr>"""


def render_live_html(jobs: list[dict], company: str = _DEFAULT_COMPANY) -> str:
    tgt = _target(company)
    key = tgt.get("key", _DEFAULT_COMPANY)
    name = tgt.get("company", key.title())
    online_n = sum(1 for j in jobs if _is_online(j))
    rows = "\n".join(_row(j) for j in jobs)
    return _TEMPLATE.format(rows=rows, total=len(jobs), online=online_n,
                            company=html.escape(key), company_name=html.escape(name))


def render_index_html(targets: list[dict], agg: dict) -> str:
    cards = []
    for t in targets:
        key = html.escape(t.get("key", ""))
        name = html.escape(t.get("company", key))
        note = html.escape(t.get("note", ""))
        on = "on" if t.get("enabled") else "off"
        on_txt = "подаём" if t.get("enabled") else "только трекинг"
        c = agg.get(t.get("key", "").lower(), {"submitted": 0, "accepted": 0, "rejected": 0})
        cards.append(f"""
  <a class="card" href="/roles?company={key}">
    <div class="ch"><span class="cn">{name}</span><span class="pill {on}">{on_txt}</span></div>
    <div class="cnote">{note}</div>
    <div class="cnums">
      <span class="nu sub"><b>{c['submitted']}</b><small>подано</small></span>
      <span class="nu acc"><b>{c['accepted']}</b><small>принято</small></span>
      <span class="nu rej"><b>{c['rejected']}</b><small>отказ</small></span>
    </div>
    <div class="copen">Открыть таблицу →</div>
  </a>""")
    return _INDEX_TEMPLATE.format(cards="\n".join(cards), n=len(targets))


# Kept for standalone regeneration if ever needed (bakes a Salmon snapshot).
def build(company: str = _DEFAULT_COMPANY) -> Path:
    out = OUT_ROOT.parent / f"roles_dashboard_{company}.html"
    out.write_text(render_live_html(fetch_jobs(company), company), encoding="utf-8")
    return out


_TEMPLATE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{company_name} — открытые вакансии (live)</title>
<style>
  :root{{--bg:#0f1512;--card:#161d1a;--ink:#e7ede9;--ink2:#9fb0a8;--ink3:#6f7f77;
    --rule:#232d29;--accent:#4fbfa8;--sub:#5b9dd6;--acc:#4fbf7b;--rej:#e0796a}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif}}
  .wrap{{max-width:1200px;margin:0 auto;padding:28px 18px 80px}}
  h1{{font:600 26px/1.15 ui-monospace,Menlo,monospace;letter-spacing:-.02em;margin:0 0 6px}}
  .back{{color:var(--ink3);text-decoration:none;font-size:13px}} .back:hover{{color:var(--accent)}}
  .live{{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--acc);
    margin-left:8px;vertical-align:middle;animation:pulse 1.6s infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.25}}}}
  .sub{{color:var(--ink2);margin:0 0 20px}}
  .stats{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:20px}}
  .stat{{background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:10px 14px;min-width:110px}}
  .stat b{{font:600 22px ui-monospace,Menlo,monospace;display:block;font-variant-numeric:tabular-nums}}
  .stat small{{color:var(--ink3);font-size:12px;text-transform:uppercase;letter-spacing:.08em}}
  .stat.acc b{{color:var(--acc)}} .stat.rej b{{color:var(--rej)}} .stat.sub b{{color:var(--sub)}}
  .controls{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px;align-items:center}}
  input[type=search]{{flex:1;min-width:220px;background:var(--card);border:1px solid var(--rule);
    border-radius:8px;padding:10px 12px;color:var(--ink);font-size:14px}}
  .seg{{display:flex;border:1px solid var(--rule);border-radius:8px;overflow:hidden}}
  .seg button{{background:var(--card);border:0;color:var(--ink2);padding:9px 13px;font:600 13px system-ui;cursor:pointer}}
  .seg button.on{{background:var(--accent);color:#04120e}}
  .reset{{background:#2a1a18;border:1px solid #4a2b26;color:var(--rej);border-radius:8px;
    padding:9px 13px;font:600 13px system-ui;cursor:pointer}}
  .count{{color:var(--ink3);font-size:13px;margin-left:auto}}
  .twrap{{overflow-x:auto;border:1px solid var(--rule);border-radius:10px}}
  table{{border-collapse:collapse;width:100%;min-width:820px}}
  th,td{{text-align:left;padding:11px 13px;border-bottom:1px solid var(--rule);vertical-align:top}}
  th{{font:600 11px ui-monospace,Menlo,monospace;letter-spacing:.1em;text-transform:uppercase;
    color:var(--ink3);position:sticky;top:0;background:var(--bg);z-index:1}}
  tbody tr:hover{{background:#131a17}}
  .c-num{{text-align:center;font-variant-numeric:tabular-nums;font:600 15px ui-monospace,Menlo,monospace;width:70px;color:var(--ink3)}}
  .c-num.pos-acc{{color:var(--acc)}} .c-num.pos-rej{{color:var(--rej)}} .c-num.pos-sub{{color:var(--sub)}}
  .pos{{font-weight:600}}
  .meta{{display:flex;flex-wrap:wrap;gap:6px;margin-top:5px}} .meta span{{font-size:11.5px;color:var(--ink3)}}
  .wp{{padding:1px 7px;border-radius:20px;border:1px solid var(--rule);text-transform:uppercase;letter-spacing:.05em;font-weight:600}}
  .wp-remote{{color:var(--acc);border-color:#2a4b3b}} .wp-hybrid{{color:var(--sub);border-color:#2a3e4b}} .wp-onsite{{color:var(--ink3)}}
  .c-link a{{color:var(--accent);text-decoration:none;white-space:nowrap;font-weight:600}}
  .c-desc{{max-width:520px}}
  details summary{{cursor:pointer;color:var(--ink2);font-size:13px;user-select:none}}
  details[open] summary{{color:var(--accent);margin-bottom:8px}}
  .desc{{font-size:13.5px;background:#0c110f;border:1px solid var(--rule);border-radius:8px;
    padding:12px 14px;max-height:420px;overflow:auto}}
  .desc p{{margin:6px 0}} .desc ul{{margin:6px 0;padding-left:18px}} .desc li{{margin:3px 0}} .desc a{{color:var(--sub)}}
  @media(prefers-color-scheme:light){{:root{{--bg:#f3f6f4;--card:#fff;--ink:#12201b;
    --ink2:#4a5a52;--ink3:#7b8b83;--rule:#dde5e1}}.desc{{background:#f7faf8}}th{{background:#f3f6f4}}}}
</style></head><body><div class="wrap">
  <a class="back" href="/roles">← Все компании</a>
  <h1>{company_name} — открытые вакансии<span class="live" title="live"></span></h1>
  <p class="sub">Живой дашборд: вакансии из ATS компании, счётчики (подано/принято/отказ) читаются
    напрямую из трекера и обновляются <b>мгновенно</b> (SSE-пуш ~1&nbsp;сек), как только бот
    отработал. <span id="upd"></span></p>
  <div class="stats">
    <div class="stat"><b>{total}</b><small>всего вакансий</small></div>
    <div class="stat"><b>{online}</b><small>online (remote/hybrid)</small></div>
    <div class="stat sub"><b id="t-sub">0</b><small>подано (наши)</small></div>
    <div class="stat acc"><b id="t-acc">0</b><small>принято</small></div>
    <div class="stat rej"><b id="t-rej">0</b><small>отказ</small></div>
  </div>
  <div class="controls">
    <input type="search" id="q" placeholder="Поиск: должность, отдел, ключевые слова в описании…">
    <div class="seg" id="wp">
      <button data-f="online" class="on">Только online</button>
      <button data-f="all">Все</button>
      <button data-f="remote">Remote</button>
      <button data-f="hybrid">Hybrid</button>
    </div>
    <button class="reset" id="reset">Сбросить счётчики</button>
    <span class="count" id="count"></span>
  </div>
  <div class="twrap"><table>
    <thead><tr><th>Позиция</th><th>Ссылка</th><th>Описание вакансии</th>
      <th style="text-align:center">Подано</th><th style="text-align:center">Принято</th>
      <th style="text-align:center">Отказ</th></tr></thead>
    <tbody id="rows">{rows}</tbody>
  </table></div>
</div>
<script>
  const COMPANY="{company}";
  const rows=[...document.querySelectorAll('tr.role')];
  const q=document.getElementById('q'), count=document.getElementById('count');
  let filter='online';
  function applyFilter(){{
    const term=(q.value||'').trim().toLowerCase(); let n=0;
    for(const r of rows){{
      const wp=r.dataset.wp, online=r.dataset.online==='1';
      const okF = filter==='all'||(filter==='online'&&online)||(filter==='remote'&&wp==='remote')||(filter==='hybrid'&&wp==='hybrid');
      const okQ = !term||r.dataset.search.includes(term);
      const show=okF&&okQ; r.style.display=show?'':'none'; if(show)n++;
    }}
    count.textContent=n+' вакансий';
  }}
  q.addEventListener('input',applyFilter);
  document.querySelectorAll('#wp button').forEach(b=>b.addEventListener('click',()=>{{
    document.querySelectorAll('#wp button').forEach(x=>x.classList.remove('on'));
    b.classList.add('on'); filter=b.dataset.f; applyFilter();
  }}));
  // live counts — Server-Sent Events (near-instant, ~1s) with a poll fallback.
  function applyCounts(d){{
    for(const r of rows){{
      const c=d[r.dataset.url]||{{submitted:0,accepted:0,rejected:0}};
      setCell(r,'submitted',c.submitted,'pos-sub'); setCell(r,'accepted',c.accepted,'pos-acc'); setCell(r,'rejected',c.rejected,'pos-rej');
    }}
    const t=d._totals||{{submitted:0,accepted:0,rejected:0}};
    document.getElementById('t-sub').textContent=t.submitted;
    document.getElementById('t-acc').textContent=t.accepted;
    document.getElementById('t-rej').textContent=t.rejected;
    document.getElementById('upd').textContent='Обновлено: '+new Date().toLocaleTimeString();
  }}
  function setCell(r,k,v,cls){{ const td=r.querySelector('.c-num[data-k="'+k+'"]'); if(!td)return;
    if(td.textContent!=String(v)){{ td.textContent=v; td.style.transition='none'; td.style.background=v>0?'rgba(79,191,168,.25)':'';
      setTimeout(()=>{{td.style.transition='background .8s';td.style.background='';}},60); }}
    td.classList.toggle(cls, v>0); }}
  async function poll(){{
    try{{ applyCounts(await (await fetch('/roles/counts?company='+COMPANY,{{cache:'no-store'}})).json()); }}
    catch(e){{ document.getElementById('upd').textContent='нет связи с трекером…'; }}
  }}
  // SSE = push updates the moment the bot's work changes the counts.
  let es=null;
  function connectSSE(){{
    if(!window.EventSource) return;
    es=new EventSource('/roles/events?company='+COMPANY);
    es.onmessage=(e)=>{{ try{{ applyCounts(JSON.parse(e.data)); }}catch(_){{}} }};
    es.onerror=()=>{{ if(es){{es.close();es=null;}} setTimeout(connectSSE,3000); }}; // auto-reconnect
  }}
  document.getElementById('reset').addEventListener('click', async ()=>{{
    if(!confirm('Обнулить счётчики для {company_name}? Ранее поданные перестанут учитываться.'))return;
    await fetch('/roles/reset?company='+COMPANY,{{method:'POST'}}); poll();
  }});
  applyFilter(); poll(); connectSSE(); setInterval(poll, 5000); // poll = fallback if SSE drops
</script></body></html>"""


_INDEX_TEMPLATE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JobFinder — компании ({n})</title>
<style>
  :root{{--bg:#0f1512;--card:#161d1a;--ink:#e7ede9;--ink2:#9fb0a8;--ink3:#6f7f77;
    --rule:#232d29;--accent:#4fbfa8;--sub:#5b9dd6;--acc:#4fbf7b;--rej:#e0796a}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif}}
  .wrap{{max-width:1100px;margin:0 auto;padding:28px 18px 80px}}
  h1{{font:600 26px/1.15 ui-monospace,Menlo,monospace;letter-spacing:-.02em;margin:0 0 6px}}
  .live{{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--acc);
    margin-left:8px;vertical-align:middle;animation:pulse 1.6s infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.25}}}}
  .sub{{color:var(--ink2);margin:0 0 22px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}}
  .card{{display:block;background:var(--card);border:1px solid var(--rule);border-radius:12px;
    padding:16px 16px 14px;text-decoration:none;color:inherit;transition:border-color .15s,transform .1s}}
  .card:hover{{border-color:var(--accent);transform:translateY(-1px)}}
  .ch{{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px}}
  .cn{{font:600 18px ui-monospace,Menlo,monospace}}
  .pill{{font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px;border:1px solid var(--rule);white-space:nowrap}}
  .pill.on{{color:var(--acc);border-color:#2a4b3b}} .pill.off{{color:var(--ink3)}}
  .cnote{{color:var(--ink3);font-size:12.5px;min-height:34px;margin-bottom:10px}}
  .cnums{{display:flex;gap:14px;padding-top:10px;border-top:1px solid var(--rule)}}
  .nu b{{font:600 20px ui-monospace,Menlo,monospace;display:block;font-variant-numeric:tabular-nums}}
  .nu small{{color:var(--ink3);font-size:11px;text-transform:uppercase;letter-spacing:.06em}}
  .nu.sub b{{color:var(--sub)}} .nu.acc b{{color:var(--acc)}} .nu.rej b{{color:var(--rej)}}
  .copen{{margin-top:12px;color:var(--accent);font-size:13px;font-weight:600}}
  @media(prefers-color-scheme:light){{:root{{--bg:#f3f6f4;--card:#fff;--ink:#12201b;
    --ink2:#4a5a52;--ink3:#7b8b83;--rule:#dde5e1}}}}
</style></head><body><div class="wrap">
  <h1>JobFinder — компании<span class="live" title="live"></span></h1>
  <p class="sub">{n} компаний под трекингом. У каждой — своя live-таблица (позиции из Ashby +
    счётчики подано/принято/отказ). «подаём» = включена в apply-движок; «только трекинг» =
    дашборд без авто-подачи. Счётчики ниже обновляются автоматически.</p>
  <div class="grid" id="grid">{cards}</div>
</div>
<script>
  async function refresh(){{
    try{{
      const d=await (await fetch('/roles/summary',{{cache:'no-store'}})).json();
      for(const [k,c] of Object.entries(d)){{
        const card=document.querySelector('a.card[href$="company='+k+'"]'); if(!card)continue;
        const [s,a,r]=card.querySelectorAll('.nu b');
        s.textContent=c.submitted||0; a.textContent=c.accepted||0; r.textContent=c.rejected||0;
      }}
    }}catch(e){{}}
  }}
  refresh(); setInterval(refresh, 5000);
</script></body></html>"""


if __name__ == "__main__":
    import sys
    comp = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_COMPANY
    print("wrote", build(comp))
