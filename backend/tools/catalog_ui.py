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
        meta_bits.append(f'<span class="cat-loc">{loc}</span>')
    if dept:
        meta_bits.append(f'<span class="cat-dept">{dept}</span>')
    meta = f'<div class="cat-meta">{" · ".join(meta_bits)}</div>' if meta_bits else ""

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

    # The title itself is the link to the source posting — no separate "Открыть" button.
    if url:
        title_html = (f'<a class="cat-title" href="{esc(url)}" target="_blank" '
                      f'rel="noopener">{title}</a>')
    else:
        title_html = f'<div class="cat-title">{title}</div>'

    jid = j.get("id")
    # ONE primary action per card ("Заполнить") + a compact М/Ж sex toggle (no emoji).
    if jid:
        fill_row = (
            '<div class="cat-fill-row">'
            '<div class="cat-sex" role="group" aria-label="Пол персоны">'
            '<button type="button" class="cat-sex-b on" data-gender="male" '
            'onclick="pickSex(this)" aria-pressed="true">М</button>'
            '<button type="button" class="cat-sex-b" data-gender="female" '
            'onclick="pickSex(this)" aria-pressed="false">Ж</button></div>'
            f'<button class="cat-fill" data-id="{jid}" onclick="fillJob(this)">Заполнить</button>'
            '<span class="cat-fill-res"></span></div>')
    else:
        fill_row = ""

    return (
        '<article class="cat-card">'
        f'<div class="cat-top"><span class="cat-co">{cname}</span>'
        f'<span class="cat-wp {wt_cls}">{esc(wt)}</span></div>'
        f'{title_html}{meta}'
        f'{fill_row}{desc_det}{qblock}'
        "</article>")


# Region axis for the catalog — a job's regions[] ∈ {US,CA,UK,OTHER} (multi). This is
# the primary filter for the agency flow: pick a country, apply with a candidate who is
# actually authorized there.
_REGIONS = [("US", "США"), ("CA", "Канада"), ("UK", "UK"), ("OTHER", "Другие")]


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

    # Header: title + a single "Фильтры" button (opens the settings sheet). The active
    # region shows as a tag on the button so it reads even while the sheet is closed.
    _REG_NAMES = {"": "Все", "US": "США", "CA": "Канада", "UK": "UK", "OTHER": "Другие"}
    reg_tag = _REG_NAMES.get(region, "Все") if not company else esc(active_cname)
    # ONE wide search input. Filters the list in place as you type OR on Enter (see
    # _CAT_JS). No company logo, no separate button. On mobile the top pill is the search,
    # so this input is desktop-only.
    search = (
        f'<input id="catq" class="cat-q" type="search" value="{esc(q)}" '
        'placeholder="Поиск: должность или компания…" autocomplete="off" '
        'aria-label="Поиск вакансий">')
    head = (
        '<div class="cat-head">'
        f'<div class="cat-h-row"><div class="cat-h-title">{title_txt} {head_n}</div>'
        '<button class="cat-filters-btn" id="fltBtn" onclick="toggleFilters()" '
        f'aria-expanded="false">Фильтры<span class="cat-filters-tag">{reg_tag}</span></button>'
        f'</div>{search}</div>')

    # Everything secondary — country filter, mass-apply, proxy — lives in ONE collapsed
    # settings sheet, so the main view is just search + jobs.
    region_chips = _region_bar(region, q, company, by_region, remote_total)
    bulk_bar = (
        '<div class="cat-bulk" id="catbulk">'
        '<button class="cat-bulk-go" id="bulkGo" onclick="bulkFillAll()">Подать на все</button>'
        '<button class="cat-bulk-stop" id="bulkStop" style="display:none" '
        'onclick="bulkStop()">Стоп</button>'
        '<span class="cat-bulk-prog" id="bulkProg"></span></div>')
    proxy_block = (
        '<textarea id="pxText" placeholder="host:port:user:pass&#10;'
        'user:pass@host:port&#10;socks5://host:port&#10;(по одному в строке)"></textarea>'
        '<div class="cat-proxy-hint">http/https проверяются реальным запросом (виден egress-IP); '
        'socks5 — только проверка доступности, и в браузере socks5 работает лишь без логина/пароля.</div>'
        '<div class="cat-proxy-row">'
        '<button class="cat-proxy-go" onclick="pxUpload()">Загрузить и проверить</button>'
        '<button class="cat-proxy-clr" onclick="pxClear()">Очистить</button>'
        '<span class="cat-proxy-msg" id="pxMsg"></span></div>'
        '<div class="cat-proxy-list" id="pxList"></div>')
    settings = (
        '<div class="cat-settings" id="catSettings" hidden>'
        f'<div class="cs-sec"><div class="cs-label">Регион</div>{region_chips}</div>'
        f'<div class="cs-sec"><div class="cs-label">Массовая подача</div>{bulk_bar}</div>'
        '<div class="cs-sec"><div class="cs-label">Прокси <b id="pxCount">0</b></div>'
        f'<div class="cat-proxy-body">{proxy_block}</div></div>'
        '</div>')

    list_html = cards or '<div class="empty">Вакансий не найдено</div>'
    body = (
        _CAT_CSS + head + settings
        + f'<div class="cat-list" id="catlist">{list_html}</div>'
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
.cat-head{display:flex;flex-direction:column;gap:10px;margin-bottom:6px}
.cat-h-row{display:flex;align-items:center;justify-content:space-between;gap:10px}
.cat-h-title{font-size:19px;font-weight:700;color:var(--ink)}
.cat-h-n{color:var(--ink-mute);font-weight:600;font-size:14px;margin-left:4px}
/* One wide search field (pill). Live-filters as you type / on Enter. */
.cat-q{width:100%;box-sizing:border-box;padding:12px 16px;border:1px solid var(--line-strong);border-radius:var(--r-full);font-size:15px;background:var(--panel);color:var(--ink)}
.cat-q::placeholder{color:var(--ink-mute)}
.cat-q:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgb(26 115 232/.15)}
/* Filters button — opens the settings sheet; shows the active region as a tag. */
.cat-filters-btn{display:inline-flex;align-items:center;gap:8px;flex:0 0 auto;background:var(--panel);color:var(--ink-soft);border:1px solid var(--line-strong);border-radius:var(--r-full);padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer;min-height:38px}
.cat-filters-btn:hover{border-color:var(--accent);color:var(--ink)}
.cat-filters-btn[aria-expanded=true]{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.cat-filters-tag{font-size:12px;font-weight:700;color:var(--accent);background:var(--accent-soft);border-radius:var(--r-full);padding:2px 9px}
.cat-filters-btn[aria-expanded=true] .cat-filters-tag{background:var(--panel)}
/* Settings sheet — regions + mass-apply + proxy, collapsed by default. */
.cat-settings{margin:2px 0 10px;padding:2px 14px 12px;border:1px solid var(--line);border-radius:var(--r);background:var(--panel)}
.cat-settings[hidden]{display:none}
.cs-sec{padding:13px 0;border-top:1px solid var(--line)}
.cs-sec:first-child{border-top:0}
.cs-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--ink-mute);margin-bottom:10px}
.cs-label b{font-family:var(--ff-mono);font-weight:500;color:var(--accent);margin-left:2px}
.cat-regions{display:flex;flex-wrap:wrap;gap:8px}
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
.cat-title{display:block;font-size:15.5px;font-weight:600;color:var(--ink);line-height:1.3;margin-bottom:5px;text-decoration:none}
a.cat-title:hover{color:var(--accent);text-decoration:underline}
.cat-meta{font-size:12.5px;color:var(--ink-mute);margin-bottom:8px}
.cat-meta span{color:inherit}
.cat-det{margin-top:4px}
.cat-det>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:8px;color:var(--ink-soft);font-size:13px;font-weight:600;user-select:none;padding:7px 0}
.cat-det>summary::-webkit-details-marker{display:none}
.cat-det>summary::before{content:"";width:6px;height:6px;border-right:2px solid currentColor;border-bottom:2px solid currentColor;transform:rotate(-45deg);transition:transform .18s;flex:0 0 auto}
.cat-det[open]>summary::before{transform:rotate(45deg)}
.cat-det[open]>summary{color:var(--accent);margin-bottom:8px}
.cat-desc{font-size:13.5px;line-height:1.55;color:var(--ink);max-height:340px;overflow:auto;border:1px solid var(--line);border-radius:var(--r-sm);padding:12px 13px;background:var(--bg-app)}
.cat-desc img{max-width:100%;height:auto}
.cat-desc table{max-width:100%;display:block;overflow-x:auto}
.cat-desc a{color:var(--accent)}
.cat-qlist{list-style:none;margin:0;padding:0;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--bg-app);overflow:auto;max-height:360px}
.cat-q{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;padding:10px 12px;border-bottom:1px solid var(--line);font-size:13px;line-height:1.4}
.cat-q:last-child{border-bottom:0}
.cat-qlbl{color:var(--ink);min-width:0}
.cat-req{color:var(--danger);font-weight:700;margin-left:3px}
.cat-qtype{flex:0 0 auto;margin-top:1px;font-family:var(--ff-mono);font-size:10.5px;color:var(--ink-mute);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:1px 7px;white-space:nowrap}
.empty{color:var(--ink-mute);text-align:center;padding:44px 0}
.cat-fill-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}
/* Sex is a compact segmented toggle, not two big buttons — one modifier for the single
   primary action below. */
.cat-sex{display:inline-flex;background:var(--panel-2);border:1px solid var(--line-strong);border-radius:var(--r-full);padding:2px}
.cat-sex-b{border:0;background:transparent;color:var(--ink-mute);font-size:13px;font-weight:600;line-height:1;min-width:38px;height:32px;padding:0 10px;border-radius:var(--r-full);cursor:pointer;display:inline-flex;align-items:center;justify-content:center}
.cat-sex-b.on{background:var(--panel);color:var(--accent);box-shadow:0 1px 2px rgba(0,0,0,.12)}
.cat-fill{display:inline-flex;align-items:center;background:var(--accent);color:#fff;border:none;border-radius:var(--r-full);padding:9px 22px;font-size:13.5px;font-weight:600;cursor:pointer;min-height:38px}
.cat-fill:hover{background:var(--accent-deep)}
.cat-fill:disabled{opacity:.6;cursor:default}
.cat-fill-res a{color:var(--accent);font-weight:600;font-size:13px;text-decoration:none}
.cat-fill-res a:hover{text-decoration:underline}
.cat-bulk{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0}
.cat-bulk-go{display:inline-flex;align-items:center;justify-content:center;gap:8px;background:#0b8043;color:#fff;border:none;border-radius:var(--r-full);padding:10px 18px;font-size:13.5px;font-weight:700;cursor:pointer;min-height:42px;box-shadow:0 1px 2px rgba(11,128,67,.3)}
.cat-bulk-go:hover{background:#0a7038}
.cat-bulk-go:active{transform:translateY(1px)}
.cat-bulk-go:disabled{opacity:.5;cursor:default;box-shadow:none}
.cat-bulk-stop{display:inline-flex;align-items:center;justify-content:center;gap:6px;background:var(--danger);color:#fff;border:none;border-radius:var(--r-full);padding:10px 16px;font-size:13.5px;font-weight:700;cursor:pointer;min-height:42px}
.cat-bulk-prog{flex:1 1 100%;font-size:12.5px;font-weight:600;color:var(--ink-soft);margin:0}
.cat-proxy-body{max-width:640px}
.cat-proxy-body textarea{width:100%;min-height:110px;box-sizing:border-box;font-family:var(--ff-mono);font-size:12.5px;line-height:1.5;border:1px solid var(--line-strong);border-radius:var(--r-sm);padding:10px;resize:vertical;background:var(--bg-app);color:var(--ink)}
.cat-proxy-hint{font-size:11.5px;line-height:1.45;color:var(--ink-mute);margin:6px 0 10px}
.cat-proxy-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.cat-proxy-go{background:var(--accent);color:#fff;border:none;border-radius:var(--r-full);padding:9px 16px;font-size:13px;font-weight:600;cursor:pointer;min-height:40px}
.cat-proxy-go:hover{background:var(--accent-deep)}
.cat-proxy-clr{background:var(--panel);color:var(--danger);border:1px solid var(--line-strong);border-radius:var(--r-full);padding:9px 16px;font-size:13px;font-weight:600;cursor:pointer;min-height:40px}
.cat-proxy-clr:hover{border-color:var(--danger)}
.cat-proxy-msg{font-size:12.5px;font-weight:600;color:var(--ink-soft)}
.cat-proxy-list{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px}
.px-ip{font-family:var(--ff-mono);font-size:11.5px;color:#0b8043;background:rgba(11,128,67,.1);border-radius:var(--r-sm);padding:3px 9px}
/* Mobile: the Gmail top pill IS the search there, so hide this page's own wide input;
   keep the header to just title + Фильтры. */
@media(max-width:760px){
  .cat-q{display:none}
  .cat-head{gap:6px;margin-bottom:2px}
  .cat-h-title{font-size:17px}
  .cat-title{font-size:15px}
  .cat-bulk{flex:1 1 auto}
  .cat-bulk-go{flex:1 1 auto}
  .cat-proxy-body{max-width:none}
  .cat-reg{min-height:40px}
}
</style>"""

_CAT_JS = """<script>
// One-click: start the fill (the server first points the co-pilot at THIS job, then
// generates + fills in the background) and go straight to noVNC to WATCH that job fill.
// Redirect the SAME tab — window.open('_blank') is popup-blocked on mobile (that was the
// "have to tap Open noVNC again" step), and the co-pilot is already on the right job so
// noVNC never shows a stale one. Global (used by cards added via infinite scroll too).
// ♂/♀ segmented toggle: mark the tapped segment active (per card).
window.pickSex = function(b){
  var g=b.closest('.cat-sex'); if(!g) return;
  g.querySelectorAll('.cat-sex-b').forEach(function(x){
    var on=(x===b); x.classList.toggle('on', on); x.setAttribute('aria-pressed', on?'true':'false');
  });
};
window.fillJob = async function(btn){
  if(btn.disabled) return;
  var id=btn.dataset.id,
      row=btn.closest('.cat-fill-row'),
      sel=row?row.querySelector('.cat-sex-b.on'):null,
      gender=sel?(sel.dataset.gender||''):'',
      res=row?row.querySelector('.cat-fill-res'):null,
      label=btn.textContent;
  var NOVNC='/vnc/vnc_lite.html?path=vnc/websockify&scale=true';
  btn.disabled=true; btn.textContent='⏳…'; if(res) res.textContent='';
  try{
    var body='gender='+encodeURIComponent(gender);
    var j=await (await fetch('/catalog/'+id+'/fill',{method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body})).json();
    window.location.href = j.novnc || NOVNC;   // watch THIS job fill live, same tab
  }catch(e){
    btn.disabled=false; btn.textContent=label;
    if(res) res.innerHTML=' <a href="'+NOVNC+'" target="_blank" rel="noopener">Открыть noVNC ↗</a>';
  }
};
// Bulk "apply to all": ONE sequential queue on the server over every greenhouse+ashby
// job (Lever/Workable are skipped server-side — live captcha). Auto-submits per job.
// We only START/STOP/POLL here — the fill+submit logic is untouched. It runs long; the
// batch survives leaving this page (server-side thread), poll resumes on reload.
window.bulkFillAll = async function(){
  var go=document.getElementById('bulkGo'), prog=document.getElementById('bulkProg');
  if(go.disabled) return;
  if(!confirm('Подать на ВСЕ вакансии (greenhouse + ashby) по очереди, с автоматической '
      +'отправкой?\\n\\nЭто реальные заявки в реальные ATS одна за другой — займёт очень '
      +'долго. Lever/Workable пропускаются (капча). Прервать — кнопкой «Стоп» '
      +'(остановится после текущей).')) return;
  go.disabled=true; if(prog) prog.textContent='Запуск…';
  try{
    var j=await (await fetch('/catalog/fill_all',{method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},body:''})).json();
    if(j.started===false && prog){ prog.textContent = j.error||'Уже идёт'; }
  }catch(e){ go.disabled=false; if(prog) prog.textContent='Ошибка запуска'; return; }
  bulkPoll();
};
window.bulkStop = async function(){
  var prog=document.getElementById('bulkProg');
  try{ await fetch('/catalog/fill_all_stop',{method:'POST'}); }catch(e){}
  if(prog) prog.textContent='Останавливается после текущей…';
};
async function bulkPoll(){
  var go=document.getElementById('bulkGo'), stop=document.getElementById('bulkStop'),
      prog=document.getElementById('bulkProg');
  if(!go||!stop||!prog) return;
  try{
    var s=await (await fetch('/catalog/fill_all_status')).json();
    var line=(s.done||0)+'/'+(s.total||0)+' · ✓'+(s.ok||0)+' ✗'+(s.failed||0)
             +(s.current?(' · '+s.current):'');
    if(s.state==='running'){
      go.style.display='none'; go.disabled=true; stop.style.display='';
      prog.textContent=line; setTimeout(bulkPoll, 3000);
    }else if(s.state==='done'||s.state==='stopped'){
      stop.style.display='none'; go.style.display=''; go.disabled=false;
      prog.textContent=(s.state==='stopped'?'Остановлено':'Готово')+': '+line;
    }else{
      go.disabled=false;
    }
  }catch(e){ setTimeout(bulkPoll, 5000); }
}
bulkPoll();   // resume progress if a batch is already running when the page loads

// Filters/settings sheet holds regions + mass-apply + proxy — declutters the top.
window.toggleFilters=function(){
  var s=document.getElementById('catSettings'), b=document.getElementById('fltBtn');
  if(!s) return;
  var willOpen=s.hasAttribute('hidden');
  if(willOpen){ s.removeAttribute('hidden'); pxRefresh(); bulkPoll(); }
  else{ s.setAttribute('hidden',''); }
  if(b) b.setAttribute('aria-expanded', willOpen?'true':'false');
};
// Proxy pool: upload a list, invalid ones dropped on validation, applications then
// rotate through the survivors (a different egress IP per submit).
async function pxRefresh(){
  var c=document.getElementById('pxCount'), list=document.getElementById('pxList');
  try{
    var s=await (await fetch('/proxies')).json();
    if(c) c.textContent=s.count||0;
    if(list) list.innerHTML=(s.ips||[]).map(function(x){
      return '<span class="px-ip">'+((x.ip||x.server||'')+'').replace(/</g,'&lt;')+'</span>';}).join('');
  }catch(e){}
}
window.pxUpload=async function(){
  var t=document.getElementById('pxText').value, msg=document.getElementById('pxMsg');
  if(!t.trim()){ if(msg) msg.textContent='Вставь список прокси'; return; }
  if(msg) msg.textContent='Проверяю…';
  try{
    var j=await (await fetch('/proxies/upload',{method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},
        body:'text='+encodeURIComponent(t)})).json();
    if(j.error){ if(msg) msg.textContent='Ошибка: '+j.error; return; }
    if(msg) msg.textContent='Оставлено '+j.kept+' · отброшено '+j.dropped+' · в пуле '+j.count;
    var c=document.getElementById('pxCount'); if(c) c.textContent=j.count||0;
    pxRefresh();
  }catch(e){ if(msg) msg.textContent='Ошибка запроса'; }
};
window.pxClear=async function(){
  if(!confirm('Очистить весь пул прокси?')) return;
  try{ await fetch('/proxies/clear',{method:'POST'}); }catch(e){}
  var msg=document.getElementById('pxMsg'); if(msg) msg.textContent='Пул очищен';
  var c=document.getElementById('pxCount'); if(c) c.textContent='0';
  var list=document.getElementById('pxList'); if(list) list.innerHTML='';
};
pxRefresh();   // show the current pool size on load

(function(){
  var list=document.getElementById('catlist'), more=document.getElementById('catmore');
  if(!list) return;
  var qp=new URLSearchParams(location.search);
  var region=qp.get('region')||'', curQ=(qp.get('q')||'').trim();
  var loading=false, PAGE=30, seq=0;
  function fragUrl(offset){
    var sp=new URLSearchParams();
    if(curQ) sp.set('q', curQ);
    if(region) sp.set('region', region);
    sp.set('offset', offset);
    return '/catalog/more?'+sp.toString();
  }
  async function runSearch(){
    var mine=++seq; loading=true;
    try{
      var r=await fetch(fragUrl(0)), txt=r.ok?await r.text():'';
      if(mine!==seq) return;                 // a newer keystroke already fired
      list.innerHTML = txt.trim() || '<div class="empty">Вакансий не найдено</div>';
      var added=(txt.match(/class="cat-card"/g)||[]).length;
      if(more){ more.dataset.offset=String(added); more.dataset.more=(added>=PAGE)?'1':'0'; }
      window.scrollTo(0,0);
    }catch(e){}finally{ loading=false; }
  }
  async function loadMore(){
    if(loading||!more||more.dataset.more!=='1')return;
    loading=true;
    try{
      var r=await fetch(fragUrl(more.dataset.offset));
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
  // Live search: type in the desktop input OR the mobile top pill — debounced, and
  // Enter is intercepted so it filters in place instead of reloading.
  var deb;
  function onType(v){ curQ=(v||'').trim(); clearTimeout(deb); deb=setTimeout(runSearch,250); }
  [document.getElementById('catq'),
   document.querySelector('.gm-search input[type=search]')].forEach(function(inp){
    if(!inp) return;
    if(curQ) inp.value=curQ;
    if(inp.form) inp.form.addEventListener('submit',function(e){ e.preventDefault(); onType(inp.value); });
    inp.addEventListener('input',function(){ onType(inp.value); });
  });
})();
</script>"""
