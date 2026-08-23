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


def resolve_company_key(company_name: str) -> str:
    """Map a typed/picked company NAME to its company_key (exact case-insensitive match,
    then a substring hit). '' when nothing matches — the caller then treats the text as a
    free-text search. Used by the /catalog route to redirect the picker to the canonical
    ?company=<key> URL so pagination + bookmarks stay clean."""
    cn = (company_name or "").strip().lower()
    if not cn:
        return ""
    try:
        comps = catalog_db.companies(remote_only=True)
    except Exception:
        return ""
    hit = ([c for c in comps if (c.get("company") or "").lower() == cn]
           or [c for c in comps if cn in (c.get("company") or "").lower()])
    return (hit[0].get("company_key") or "") if hit else ""


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

    jid = j.get("id")
    fill_row = (
        f'<div class="cat-fill-row"><span class="cat-fill-lbl">▶ Заполнить вживую:</span>'
        f'<button class="cat-fill cat-fill-m" data-id="{jid}" data-gender="male" '
        f'onclick="fillJob(this)">♂ Муж</button>'
        f'<button class="cat-fill cat-fill-f" data-id="{jid}" data-gender="female" '
        f'onclick="fillJob(this)">♀ Жен</button>'
        f'<span class="cat-fill-res"></span></div>') if jid else ""

    return (
        '<article class="cat-card">'
        f'<div class="cat-top"><span class="cat-co">{cname}</span>'
        f'<span class="cat-wp {wt_cls}">{esc(wt)}</span></div>'
        f'<div class="cat-title">{title}</div>{meta}'
        f'<div class="cat-row">{qbadge}{open_link}</div>'
        f'{fill_row}{desc_det}{qblock}'
        "</article>")


# Region axis for the catalog — a job's regions[] ∈ {US,CA,UK,OTHER} (multi). This is
# the primary filter for the agency flow: pick a country, apply with a candidate who is
# actually authorized there.
_REGIONS = [("US", "🇺🇸 США"), ("CA", "🇨🇦 Канада"), ("UK", "🇬🇧 UK"), ("OTHER", "🌍 Другие")]


def _region_bar(active: str, q: str, company: str, by_region: dict, total: int) -> str:
    def pill(key: str, label: str, n: int, on: bool) -> str:
        cls = "cat-reg on" if on else "cat-reg"
        href = "/catalog" + _qs(company=company, q=q, region=key)
        return f'<a class="{cls}" href="{href}">{label} <b>{n}</b></a>'
    out = [pill("", "Все", total, not active)]
    for code, label in _REGIONS:
        out.append(pill(code, label, by_region.get(code, 0), active == code))
    return f'<div class="cat-regions">{"".join(out)}</div>'


def render_page(company: str = "", q: str = "", region: str = "",
                company_name: str = "") -> str:
    company = (company or "").strip()
    q = (q or "").strip()
    company_name = (company_name or "").strip()
    region = (region or "").strip().upper()
    if region not in ("US", "CA", "UK", "OTHER"):
        region = ""
    try:
        comps = catalog_db.companies(remote_only=True)
    except Exception:
        comps = []
    # Resolve a typed/picked company NAME -> its company_key (list_jobs filters on the key).
    # Exact case-insensitive match first, then a substring hit; if nothing matches, fall
    # back to the free-text search so the box is never a dead end (a typo still finds rows).
    if company_name and not company:
        cn = company_name.lower()
        hit = ([c for c in comps if (c.get("company") or "").lower() == cn]
               or [c for c in comps if cn in (c.get("company") or "").lower()])
        if hit:
            company = hit[0].get("company_key") or ""
        elif not q:
            q = company_name
    jobs = catalog_db.list_jobs(company=company or None, q=q or None,
                                remote_only=True, limit=PAGE, offset=0,
                                region=region or None)
    cards = "".join(_card(j) for j in jobs)
    has_more = 1 if len(jobs) == PAGE else 0

    try:
        cnt = catalog_db.counts()
        remote_total = cnt.get("remote", 0)
        by_region = cnt.get("by_region", {})
    except Exception:
        remote_total, by_region = 0, {}

    active_cname = ""
    if company:
        # use the active company's display name if we can find it
        active_cname = company
        for c in comps:
            if (c.get("company_key") or "") == company:
                active_cname = c.get("company") or company
                break
        title_txt = esc(active_cname)
        head_n = ""
    else:
        title_txt = "Каталог"
        n = by_region.get(region, 0) if region else remote_total
        head_n = f'<span class="cat-h-n">{n}</span>'

    # Company picker: native-autocomplete over every company (name + job count). Type
    # "salmon" -> pick -> a clean per-company view (?company=<key>). Distinct from the
    # free-text q search below and — unlike it — kept visible on mobile too.
    dl = "".join(
        f'<option value="{esc(c.get("company") or "")}">'
        f'{c.get("n")} {_plural(c.get("n") or 0, "вакансия", "вакансии", "вакансий")}</option>'
        for c in comps if c.get("company"))
    company_pick = (
        '<form class="cat-company" method="get" action="/catalog" role="search">'
        + (f'<input type="hidden" name="region" value="{esc(region)}">' if region else "")
        + '<input list="cat-companies" name="company_name" autocomplete="off" '
        + f'value="{esc(active_cname)}" placeholder="🏢 Найти компанию…" aria-label="Поиск компании">'
        + '<button class="ghost" type="submit">Открыть</button>'
        + (f'<a class="ghost" href="/catalog{_qs(region=region)}">Сброс</a>' if company else "")
        + f'</form><datalist id="cat-companies">{dl}</datalist>')

    search = (
        '<form class="cat-search" method="get" action="/catalog">'
        + (f'<input type="hidden" name="company" value="{esc(company)}">' if company else "")
        + (f'<input type="hidden" name="region" value="{esc(region)}">' if region else "")
        + f'<input type="search" name="q" value="{esc(q)}" placeholder="Поиск: должность, компания, описание…">'
        + '<button class="ghost" type="submit">Найти</button>'
        + (f'<a class="ghost" href="/catalog{_qs(company=company, region=region)}">Сброс</a>' if q else "")
        + "</form>")
    head = (
        '<div class="cat-head"><div class="cat-h-row">'
        f'<div class="cat-h-title">{title_txt} {head_n}</div></div>'
        f"{company_pick}{search}</div>")
    empty = '<div class="empty">Вакансий не найдено</div>' if not jobs else ""
    body = (
        _CAT_CSS + head
        + _region_bar(region, q, company, by_region, remote_total)
        + f'<div class="cat-list" id="catlist">{cards}</div>{empty}'
        + f'<div id="catmore" data-more="{has_more}" data-offset="{PAGE}" style="height:1px"></div>'
        + _CAT_JS)
    return mailcrm_ui._page("catalog", body)


def render_more(company: str = "", q: str = "", offset: int = 0, region: str = "") -> str:
    company = (company or "").strip()
    q = (q or "").strip()
    region = (region or "").strip().upper()
    if region not in ("US", "CA", "UK", "OTHER"):
        region = ""
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    jobs = catalog_db.list_jobs(company=company or None, q=q or None,
                                remote_only=True, limit=PAGE, offset=offset,
                                region=region or None)
    return "".join(_card(j) for j in jobs)


_CAT_CSS = """<style>
.cat-head{display:flex;flex-direction:column;gap:10px;margin-bottom:6px}
.cat-h-row{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.cat-h-title{font-size:19px;font-weight:700;color:var(--ink)}
.cat-h-n{color:var(--ink-mute);font-weight:600;font-size:14px;margin-left:4px}
.cat-search{display:flex;gap:8px;flex-wrap:wrap}
.cat-search input[type=search]{flex:1;min-width:0}
.cat-company{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.cat-company input[list]{flex:1;min-width:0;padding:9px 12px;border:1px solid var(--line-strong);border-radius:8px;font-size:15px;background:var(--panel);color:var(--ink)}
.cat-company input[list]:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 2px rgba(26,115,232,.18)}
.cat-regions{display:flex;gap:8px;overflow-x:auto;padding:2px 0 10px;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.cat-regions::-webkit-scrollbar{display:none}
.cat-reg{display:inline-flex;align-items:center;gap:7px;white-space:nowrap;padding:9px 15px;border-radius:999px;border:1px solid var(--line-strong);background:var(--panel);color:var(--ink-soft);font-size:14px;font-weight:600;text-decoration:none;min-height:42px}
.cat-reg b{font-family:var(--ff-mono,monospace);font-weight:500;font-size:12px;color:var(--ink-mute)}
.cat-reg:hover{border-color:var(--accent);text-decoration:none}
.cat-reg.on{background:var(--accent);border-color:var(--accent);color:#fff;box-shadow:0 2px 8px -2px rgba(26,115,232,.5)}
.cat-reg.on b{color:rgba(255,255,255,.85)}
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
.cat-fill-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:9px}
.cat-fill-lbl{font-size:13px;font-weight:700;color:var(--fg,#202124);opacity:.85}
.cat-fill{color:#fff;border:none;border-radius:8px;padding:9px 15px;font-size:13.5px;font-weight:700;cursor:pointer;min-height:40px}
.cat-fill-m{background:#1a73e8;box-shadow:0 2px 8px -2px rgba(26,115,232,.5)}
.cat-fill-f{background:#c2559b;box-shadow:0 2px 8px -2px rgba(194,85,155,.5)}
.cat-fill:hover{filter:brightness(1.06)}
.cat-fill:disabled{opacity:.75;cursor:default;box-shadow:none}
.cat-fill-res a{color:#1a73e8;font-weight:700;font-size:13px;text-decoration:none}
.cat-fill-res a:hover{text-decoration:underline}
/* Hide the inline catalog search on mobile — the Gmail top pill already searches
   the catalog there (this rule lives here, not in mailcrm _CSS, so it wins over
   the later .cat-search{display:flex} in this same stylesheet). */
@media(max-width:760px){.cat-h-title{font-size:17px}.cat-title{font-size:15px}.cat-search{display:none}}
</style>"""

_CAT_JS = """<script>
// One-click: start the fill (the server first points the co-pilot at THIS job, then
// generates + fills in the background) and go straight to noVNC to WATCH that job fill.
// Redirect the SAME tab — window.open('_blank') is popup-blocked on mobile (that was the
// "have to tap Open noVNC again" step), and the co-pilot is already on the right job so
// noVNC never shows a stale one. Global (used by cards added via infinite scroll too).
window.fillJob = async function(btn){
  var id=btn.dataset.id, gender=btn.dataset.gender||'',
      row=btn.closest('.cat-fill-row'),
      res=row?row.querySelector('.cat-fill-res'):null,
      btns=row?row.querySelectorAll('.cat-fill'):[btn],
      label=btn.textContent;
  if(btn.disabled) return;
  var NOVNC='/vnc/vnc_lite.html?path=vnc/websockify&scale=true';
  btns.forEach(function(b){b.disabled=true;}); btn.textContent='⏳…'; if(res) res.textContent='';
  try{
    var body='gender='+encodeURIComponent(gender);
    var j=await (await fetch('/catalog/'+id+'/fill',{method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body})).json();
    window.location.href = j.novnc || NOVNC;   // watch THIS job fill live, same tab
  }catch(e){
    btns.forEach(function(b){b.disabled=false;}); btn.textContent=label;
    if(res) res.innerHTML=' <a href="'+NOVNC+'" target="_blank" rel="noopener">Открыть noVNC ↗</a>';
  }
};
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
