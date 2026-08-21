"""Каталог tab — server-rendered browser over the Postgres `job_catalog`, served
at /catalog.

Backed by the persisted catalog (Postgres) rather than live ATS boards — it replaced
the old live "Вакансии" feed, which was removed. The whole point of this table is that each row
carries not just a description but the application-form *questions* — so cards
surface a "❓ N вопросов" badge and expand into a readable question list (label,
required `*`, field type).

The catalog is ALWAYS remote-only. Data comes from ``backend.tools.catalog_db``.
The page reuses the mail-CRM shell (``mailcrm_ui._page``) so it inherits the same
sidebar + mobile styling; only the catalog-specific CSS/JS live here.
"""
from __future__ import annotations

import html
import urllib.parse

from backend.tools import catalog_db
from backend.tools import mailcrm_ui

esc = html.escape

PAGE = 30


# ---- helpers -------------------------------------------------------------------
def _qs(company: str = "", q: str = "", **extra) -> str:
    d: dict = {}
    if company:
        d["company"] = company
    if q:
        d["q"] = q
    for k, v in extra.items():
        if v:
            d[k] = v
    return ("?" + urllib.parse.urlencode(d)) if d else ""


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _workplace(j: dict) -> tuple[str, str]:
    """(label, css-class) for the workplace pill."""
    raw = (j.get("workplace") or "").strip().lower()
    if "remote" in raw:
        return "Remote", "cat-wp-remote"
    if "hybrid" in raw:
        return "Hybrid", "cat-wp-hybrid"
    if raw in ("onsite", "on-site", "on site", "office"):
        return "OnSite", "cat-wp-onsite"
    # no explicit workplace value → fall back to is_remote flag
    if j.get("is_remote"):
        return "Remote", "cat-wp-remote"
    return "OnSite", "cat-wp-onsite"


# ---- rendering -----------------------------------------------------------------
def _questions_block(questions: list) -> str:
    if not questions:
        return ""
    items = []
    for qz in questions:
        if not isinstance(qz, dict):
            continue
        label = esc(str(qz.get("label") or "").strip() or "(без текста)")
        req = '<span class="cat-req" title="обязательный">*</span>' if qz.get("required") else ""
        qtype = esc(str(qz.get("type") or "").strip())
        tag = f'<span class="cat-qtype">{qtype}</span>' if qtype else ""
        items.append(f'<li class="cat-q"><span class="cat-qlbl">{label}{req}</span>{tag}</li>')
    if not items:
        return ""
    n = len(items)
    return (
        f'<details class="cat-det cat-qdet"><summary>Вопросы ({n})</summary>'
        f'<ul class="cat-qlist">{"".join(items)}</ul></details>')


def _card(j: dict) -> str:
    title = esc(j.get("title") or "(без названия)")
    cname = esc(j.get("company") or j.get("company_key") or "")
    url = (j.get("url") or "").strip()
    wt, wt_cls = _workplace(j)
    loc = esc(j.get("location") or "")
    dept = esc(j.get("department") or "")
    meta_bits = []
    if loc:
        meta_bits.append(f'<span class="cat-loc">📍 {loc}</span>')
    if dept:
        meta_bits.append(f'<span class="cat-dept">{dept}</span>')
    meta = f'<div class="cat-meta">{"".join(meta_bits)}</div>' if meta_bits else ""

    qc = int(j.get("q_count") or 0)
    qbadge = (f'<span class="cat-qbadge">❓ {qc} '
              f'{_plural(qc, "вопрос", "вопроса", "вопросов")}</span>') if qc > 0 else ""

    desc_html = j.get("description_html")
    if desc_html:
        desc = desc_html
    else:
        desc = "<p>" + esc(j.get("description") or "") + "</p>"
    desc_det = (
        '<details class="cat-det cat-descdet"><summary>Описание</summary>'
        f'<div class="cat-desc">{desc}</div></details>')

    questions = j.get("questions") or []
    qblock = _questions_block(questions)

    open_link = (f'<a class="cat-open" href="{esc(url)}" target="_blank" rel="noopener">Открыть ↗</a>'
                 if url else "")

    return (
        '<article class="cat-card">'
        f'<div class="cat-top"><span class="cat-co">{cname}</span>'
        f'<span class="cat-wp {wt_cls}">{esc(wt)}</span></div>'
        f'<div class="cat-title">{title}</div>{meta}'
        f'<div class="cat-row">{qbadge}{open_link}</div>'
        f'{desc_det}{qblock}'
        "</article>")


def _chips(active: str, q: str) -> str:
    def chip(key: str, label: str, on: bool) -> str:
        cls = "cat-chip on" if on else "cat-chip"
        href = "/catalog" + _qs(company=key, q=q)
        return f'<a class="{cls}" href="{href}">{esc(label)}</a>'
    out = [chip("", "Все", not active)]
    for c in catalog_db.companies(remote_only=True)[:40]:
        k = c.get("company_key") or ""
        name = c.get("company") or k
        out.append(chip(k, name, active == k))
    return f'<div class="cat-chips">{"".join(out)}</div>'


def render_page(company: str = "", q: str = "") -> str:
    company = (company or "").strip()
    q = (q or "").strip()
    jobs = catalog_db.list_jobs(company=company or None, q=q or None,
                                remote_only=True, limit=PAGE, offset=0)
    cards = "".join(_card(j) for j in jobs)
    has_more = 1 if len(jobs) == PAGE else 0

    try:
        remote_total = catalog_db.counts().get("remote", 0)
    except Exception:
        remote_total = 0

    if company:
        # use the active company's display name if we can find it
        cname = company
        for c in catalog_db.companies(remote_only=True):
            if (c.get("company_key") or "") == company:
                cname = c.get("company") or company
                break
        title_txt = esc(cname)
        head_n = ""
    else:
        title_txt = "Каталог (Remote)"
        head_n = f'<span class="cat-h-n">{remote_total}</span>'

    search = (
        '<form class="cat-search" method="get" action="/catalog">'
        + (f'<input type="hidden" name="company" value="{esc(company)}">' if company else "")
        + f'<input type="search" name="q" value="{esc(q)}" placeholder="Поиск: должность, компания, описание…">'
        + '<button class="ghost" type="submit">Найти</button>'
        + (f'<a class="ghost" href="/catalog{_qs(company=company)}">Сброс</a>' if q else "")
        + "</form>")
    head = (
        '<div class="cat-head"><div class="cat-h-row">'
        f'<div class="cat-h-title">{title_txt} {head_n}</div></div>'
        f"{search}</div>")
    empty = '<div class="empty">Вакансий не найдено</div>' if not jobs else ""
    body = (
        _CAT_CSS + head + _chips(company, q)
        + f'<div class="cat-list" id="catlist">{cards}</div>{empty}'
        + f'<div id="catmore" data-more="{has_more}" data-offset="{PAGE}" style="height:1px"></div>'
        + _CAT_JS)
    return mailcrm_ui._page("catalog", body)


def render_more(company: str = "", q: str = "", offset: int = 0) -> str:
    company = (company or "").strip()
    q = (q or "").strip()
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    jobs = catalog_db.list_jobs(company=company or None, q=q or None,
                                remote_only=True, limit=PAGE, offset=offset)
    return "".join(_card(j) for j in jobs)


_CAT_CSS = """<style>
.cat-head{display:flex;flex-direction:column;gap:10px;margin-bottom:6px}
.cat-h-row{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.cat-h-title{font-size:19px;font-weight:700;color:var(--ink)}
.cat-h-n{color:var(--ink-mute);font-weight:600;font-size:14px;margin-left:4px}
.cat-search{display:flex;gap:8px;flex-wrap:wrap}
.cat-search input[type=search]{flex:1;min-width:0}
.cat-chips{display:flex;gap:7px;overflow-x:auto;padding:2px 0 12px;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.cat-chips::-webkit-scrollbar{display:none}
.cat-chip{white-space:nowrap;padding:7px 13px;border-radius:999px;border:1px solid var(--line);color:var(--ink-mute);background:var(--panel);font-size:13px;font-weight:600;text-decoration:none;min-height:36px;display:flex;align-items:center}
.cat-chip.on{background:#1a73e8;border-color:#1a73e8;color:#fff}
.cat-list{display:flex;flex-direction:column;gap:10px}
.cat-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:13px 14px}
.cat-top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:3px}
.cat-co{font-size:12px;font-weight:700;color:var(--ink-mute);text-transform:uppercase;letter-spacing:.03em}
.cat-wp{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:999px;border:1px solid var(--line);text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
.cat-wp-remote{color:#188038;border-color:#bcdfc4}.cat-wp-hybrid{color:#1a73e8;border-color:#b8d3f5}.cat-wp-onsite{color:var(--ink-mute)}
.cat-title{font-size:15.5px;font-weight:600;color:var(--ink);line-height:1.3;margin-bottom:5px}
.cat-meta{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}
.cat-meta span{font-size:12.5px;color:var(--ink-mute)}
.cat-row{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;min-height:22px}
.cat-qbadge{font-size:12px;font-weight:700;color:#b06000;background:#fef3e0;border:1px solid #fadfb0;padding:2px 9px;border-radius:999px;white-space:nowrap}
.cat-open{font-size:13.5px;font-weight:600;color:#1a73e8;text-decoration:none;white-space:nowrap;padding:6px 0;min-height:36px;display:inline-flex;align-items:center;margin-left:auto}
.cat-det{margin-top:9px}
.cat-det>summary{cursor:pointer;color:var(--ink-mute);font-size:13px;user-select:none;padding:5px 0}
.cat-det[open]>summary{color:#1a73e8;margin-bottom:6px}
.cat-desc{font-size:13.5px;line-height:1.55;color:var(--ink);max-height:360px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:11px 12px;background:var(--bg-app)}
.cat-desc img{max-width:100%;height:auto}
.cat-desc table{max-width:100%;display:block;overflow-x:auto}
.cat-desc a{color:#1a73e8}
.cat-qlist{list-style:none;margin:0;padding:0;border:1px solid var(--line);border-radius:8px;background:var(--bg-app);overflow:hidden}
.cat-q{display:flex;align-items:baseline;justify-content:space-between;gap:10px;padding:9px 12px;border-bottom:1px solid var(--line);font-size:13.5px}
.cat-q:last-child{border-bottom:0}
.cat-qlbl{color:var(--ink);line-height:1.4;min-width:0}
.cat-req{color:#d93025;font-weight:700;margin-left:3px}
.cat-qtype{flex:0 0 auto;font-family:var(--ff-mono,monospace);font-size:10.5px;color:var(--ink-mute);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:1px 7px;white-space:nowrap}
.empty{color:var(--ink-mute);text-align:center;padding:44px 0}
@media(max-width:760px){.cat-h-title{font-size:17px}.cat-title{font-size:15px}}
</style>"""

_CAT_JS = """<script>
(function(){
  var list=document.getElementById('catlist'), more=document.getElementById('catmore');
  if(!list||!more)return;
  var loading=false, PAGE=30;
  async function loadMore(){
    if(loading||more.dataset.more!=='1')return;
    loading=true;
    try{
      var sp=new URLSearchParams(location.search);
      sp.set('offset', more.dataset.offset);
      var r=await fetch('/catalog/more?'+sp.toString());
      if(r.ok){
        var txt=await r.text();
        var added=(txt.match(/class="cat-card"/g)||[]).length;
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
