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
from backend.tools import comp_fmt
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
        return "Удалённо", "cat-wp-remote"
    if "hybrid" in raw:
        return "Гибрид", "cat-wp-hybrid"
    if raw in ("onsite", "on-site", "on site", "office"):
        return "Офис", "cat-wp-onsite"
    # no explicit workplace value → fall back to is_remote flag
    if j.get("is_remote"):
        return "Удалённо", "cat-wp-remote"
    return "Офис", "cat-wp-onsite"


# ---- rendering -----------------------------------------------------------------
def _questions_block(questions: list) -> str:
    if not questions:
        return ""
    items = []
    for qz in questions:
        if not isinstance(qz, dict):
            continue
        label = esc(str(qz.get("label") or "").strip() or "(без текста)")
        # Compact markup on purpose (a first page carries ~900 of these rows): the row is a
        # bare <li>, `*` = required (<b>), <i> = the field type — styled via .cat-qlist.
        req = '<b title="обязательный">*</b>' if qz.get("required") else ""
        qtype = esc(str(qz.get("type") or "").strip())
        tag = f'<i>{qtype}</i>' if qtype else ""
        items.append(f'<li><span>{label}{req}</span>{tag}</li>')
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

    comp = comp_fmt.comp_html(j)
    comp_row = f'<div class="cat-comp">{comp}</div>' if comp else ""

    jid = j.get("id")
    # The description is the heavy part of a card (the full JD HTML, ~25 KB each); it is
    # NOT inlined — the <details> lazy-loads it from /catalog/{id}/desc on first open
    # (catLoadDesc, fetched once, marked data-loaded). Keeps the page light on a phone.
    if jid:
        desc_det = (
            f'<details class="cat-det cat-descdet" data-desc="/catalog/{jid}/desc" '
            'ontoggle="catLoadDesc(this)"><summary>Описание</summary>'
            '<div class="cat-desc cat-desc-lazy">Загружаю…</div></details>')
    else:
        desc_det = ('<details class="cat-det cat-descdet"><summary>Описание</summary>'
                    f'<div class="cat-desc">{desc_html(j)}</div></details>')

    questions = j.get("questions") or []
    qblock = _questions_block(questions)

    # The title itself is the link to the source posting — no separate "Открыть" button.
    if url:
        title_html = (f'<a class="cat-title" href="{esc(url)}" target="_blank" '
                      f'rel="noopener" title="{title}">{title}</a>')
    else:
        title_html = f'<div class="cat-title" title="{title}">{title}</div>'

    # No per-card action any more (owner 2026-09-09: «убрать М/Ж и Заполнить — вручную ничего не
    # будет»): a job is applied to through a CAMPAIGN (select cards → «Кампания» sheet, which has
    # its own gender). The row keeps only the «Описание · Вопросы» toggles; an opened <details>
    # takes the full width (.cat-dets:has(details[open])). The one-click /catalog/{id}/fill route
    # still exists for «Незавершённые → Докрутить».
    if jid:
        fill_row = (
            '<div class="cat-fill-row">'
            f'<div class="cat-dets">{desc_det}{qblock}</div></div>')
        # Selection control (round checkbox, 44px tap target) — ticked cards feed the
        # bottom «Выбрано N · Настроить кампанию» bar. State lives in sessionStorage
        # (survives search / pagination / tab switches); syncPicks() re-checks after
        # every list re-render.
        pick = (
            f'<label class="cat-pick" title="Выбрать в кампанию">'
            f'<input type="checkbox" data-id="{jid}" onchange="pickJob(this)" '
            f'aria-label="Выбрать вакансию"><span></span></label>')
    else:
        fill_row = ""
        pick = ""

    # a card without a fill row (no id) still shows its details below the comp line
    tail = fill_row if fill_row else f'<div class="cat-dets">{desc_det}{qblock}</div>'
    return (
        f'<article class="cat-card" data-id="{jid or ""}">'
        f'<div class="cat-top"><span class="cat-co">{cname}</span>'
        f'<span class="cat-wp {wt_cls}">{esc(wt)}</span>{pick}</div>'
        f'{title_html}{meta}{comp_row}'
        f'{tail}'
        "</article>")


def desc_html(job: dict) -> str:
    """The description body of a card — the stored JD HTML when the collector kept it,
    else the plain text as one escaped paragraph. Shared by the inline fallback and the
    lazy GET /catalog/{id}/desc fragment so both render identically."""
    d = job.get("description_html")
    if d:
        return d
    return "<p>" + esc(job.get("description") or "") + "</p>"


def fetch_desc_job(job_id: int) -> dict | None:
    """The minimal row the lazy description fragment needs. `catalog_db.get_job` selects
    `_JOB_COLS`, which deliberately omits the heavy `description_html`, so this reads just
    the two description columns (a tiny primary-key SELECT). None when the id is unknown."""
    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return None
    with catalog_db._cur() as cur:
        cur.execute("SELECT id, description, description_html FROM job_catalog WHERE id=%s",
                    (job_id,))
        r = cur.fetchone()
        return dict(r) if r else None


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


# catalog_db.counts() runs 6 COUNT(*) scans (~120 ms) and only feeds the header count + the
# region chip counts, which change once a night (the collector cron) — cache it 60 s.
_COUNTS_TTL = 60.0
_COUNTS_CACHE: dict = {"ts": 0.0, "val": None}


def _counts_cached() -> dict:
    import time
    now = time.monotonic()
    if _COUNTS_CACHE["val"] is not None and now - _COUNTS_CACHE["ts"] < _COUNTS_TTL:
        return _COUNTS_CACHE["val"]
    val = catalog_db.counts()
    _COUNTS_CACHE["ts"], _COUNTS_CACHE["val"] = now, val
    return val


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
        cnt = _counts_cached()
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
        'placeholder="Должность, компания или страна…" autocomplete="off" '
        'aria-label="Поиск вакансий">')
    # Header = the canonical shell header (mailcrm_ui._page_head) hosting the «Вакансии» pill
    # switcher, so Каталог / Mass Hiring / Незавершённые share ONE header structure + button scale.
    # Owner-requested exception kept: the header's single control is «Фильтры» (the launch lives in
    # that sheet's sticky footer — no duplicate top button). On a company drill-down the company name
    # is the meta line. The wide search input sits in its own row right below the header.
    _seg = mailcrm_ui.vacancies_seg("catalog", ({} if company else {"catalog": n}))
    _filters_btn = (
        '<button class="hbtn cat-filters-btn" id="fltBtn" onclick="toggleFilters()" '
        'aria-expanded="false" title="Фильтры и запуск подачи">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>Фильтры'
        + (f'<span class="cat-filters-tag">{reg_tag}</span>' if reg_tag else '')
        + '</button>')
    head = (
        mailcrm_ui._page_head("Каталог", icons=_filters_btn,
                              meta=(title_txt if company else None), seg_html=_seg)
        + f'<div class="cat-search-row">{search}</div>')

    # Everything secondary — country filter, mass-apply, proxy — lives in ONE collapsed
    # settings sheet, so the main view is just search + jobs.
    region_chips = _region_bar(region, q, company, by_region, remote_total)
    # Mass-apply filters: persona sex (female/male), a specific company (value = its
    # company_key; the active company page pre-selects), and region. list_jobs filters
    # jobs by company/region; gender is passed through to the persona per job.
    gender_opts = ('<option value="">Пол: любой</option>'
                   '<option value="female">Женщины</option>'
                   '<option value="male">Мужчины</option>')
    comp_rows = []
    for c in comps:
        ck = c.get("company_key") or ""
        if not ck:
            continue
        sel = " selected" if ck == company else ""
        comp_rows.append(f'<option value="{esc(ck)}"{sel}>'
                         f'{esc(c.get("company") or ck)} ({c.get("n", 0)})</option>')
    comp_opts = '<option value="">Все компании</option>' + "".join(comp_rows)
    # Region select mirrors the company one: only regions actually present in the
    # catalog (count > 0), with their counts, ordered by _REGIONS.
    region_opts = '<option value="">Регион: все</option>' + "".join(
        f'<option value="{code}"{" selected" if code == region else ""}>'
        f'{label} ({by_region.get(code, 0)})</option>'
        for code, label in _REGIONS if by_region.get(code, 0) > 0)
    bulk_bar = (
        '<div class="cat-bulk" id="catbulk">'
        f'<select class="cat-bulk-sel" id="bulkGender" aria-label="Пол">{gender_opts}</select>'
        f'<select class="cat-bulk-sel" id="bulkCompany" aria-label="Компания">{comp_opts}</select>'
        f'<select class="cat-bulk-sel" id="bulkRegion" aria-label="Регион">{region_opts}</select>'
        '<label class="cat-bulk-n">Кол-во'
        '<input type="number" id="bulkN" min="1" step="1" placeholder="Все" '
        'inputmode="numeric" title="Пусто = все доступные вакансии"></label>'
        '<label class="cat-bulk-n">Потоков'
        '<input type="number" id="bulkW" min="1" max="18" step="1" placeholder="Авто" '
        'inputmode="numeric" title="Пусто = авто (сервер сам держит нагрузку); либо число 1–18">'
        '</label></div>')  # launch button/stop/progress live in the sheet's sticky footer
    proxy_block = (
        '<div class="px-status" id="pxStatus">'
        '<span class="px-dot"></span>'
        '<span class="px-summary" id="pxSummary">—</span>'
        '<button class="px-toggle" id="pxToggle" onclick="pxToggleList()" hidden>показать</button>'
        '</div>'
        '<div class="cat-proxy-list" id="pxList" hidden></div>'
        # the POOL of phones (their mobile IPs) on the Tailscale tailnet (mobile_proxy.py): an
        # aggregate status line + master on/off + a discovered/manual endpoint list + add-by-IP.
        # While any phone is online every fill round-robins across them.
        '<div class="px-mobile" id="pxMobile">'
        '<div class="px-mobile-row"><span class="px-dot" id="pxMobDot"></span>'
        '<b>Мобильные прокси</b> <span class="px-mobile-st" id="pxMobSt">—</span>'
        '<label class="px-mobile-sw"><input type="checkbox" id="pxMobOn" onchange="pxMobileSave(this)">'
        '<span>вкл</span></label></div>'
        '<div class="px-mobile-list" id="pxMobList"></div>'
        '<div class="px-mobile-row"><input type="text" class="px-mobile-in" id="pxMobSrv" '
        'placeholder="socks5://100.x.y.z:1080" autocomplete="off" spellcheck="false">'
        '<button type="button" class="px-toggle" onclick="pxMobileAdd()">добавить</button>'
        '<button type="button" class="px-toggle" onclick="pxMobileRefresh(true)">найти · проверить</button></div>'
        '<div class="cat-proxy-hint">Телефоны в сети Tailscale с запущенным SOCKS-прокси (в основном '
        'iPhone). Онлайн-телефоны находятся сами; можно добавить адрес 100.x.y.z вручную. Пока хоть один '
        'онлайн — подачи идут с мобильных IP по кругу (снимает спам-отказы Ashby); ни одного — откат на '
        'пул/напрямую.</div></div>'
        '<details class="px-add">'
        '<summary>Добавить прокси</summary>'
        '<textarea id="pxText" placeholder="host:port:user:pass&#10;'
        'user:pass@host:port&#10;socks5://host:port&#10;(по одному в строке)"></textarea>'
        '<div class="cat-proxy-hint">http/https проверяются реальным запросом (виден egress-IP); '
        'socks5 — только доступность, в браузере socks5 работает лишь без логина/пароля. '
        'Мёртвые удаляются автоматически.</div>'
        '<div class="cat-proxy-row">'
        '<button class="cat-proxy-go" onclick="pxUpload()">Проверить</button>'
        '<button class="cat-proxy-clr" onclick="pxClear()">Очистить пул</button>'
        '<span class="cat-proxy-msg" id="pxMsg"></span></div>'
        '</details>')
    # Campaigns are CREATED from the card selection (tick cards → bottom bar → the campaign
    # sheet); this section only LISTS them (pause / delete).
    campaigns_block = (
        '<div class="cs-camp">'
        '<div class="cat-proxy-hint">Отметьте вакансии галочкой в списке — внизу появится '
        '«Настроить кампанию». Кампания каждый день подаёт N заявок по кругу по выбранным '
        'вакансиям под одним именем, каждый раз со свежим резюме.</div>'
        '<div class="cs-camp-list" id="campList">—</div>'
        '</div>')
    # Selection bar (hidden until ≥1 card is ticked) + the campaign sheet it opens. The
    # sheet reuses the .cat-modal chrome (desktop dialog / phone bottom-sheet) but is a
    # SEPARATE element from #catSettings. The two segmented controls share the card's
    # .cat-sex look (pickSex toggles any .cat-sex group).
    _seg_btn = (lambda attr, val, lbl, on:
                f'<button type="button" class="cat-sex-b{" on" if on else ""}" {attr}="{val}" '
                f'onclick="pickSex(this)" aria-pressed="{"true" if on else "false"}">{lbl}</button>')
    sex_seg = ('<div class="cat-sex camp-seg" id="campSex" role="group" aria-label="Пол персоны">'
               + _seg_btn("data-gender", "male", "М", True)
               + _seg_btn("data-gender", "female", "Ж", False) + '</div>')
    # a free number (owner: «чтобы кастомно можно было писать»), with quick chips for the common values
    per_seg = ('<div class="camp-per" id="campPer" role="group" aria-label="Подач в день">'
               '<input type="number" id="campPerN" class="camp-per-n" min="1" max="100" value="2" '
               'inputmode="numeric" aria-label="Подач в день" oninput="campPerTyped(this)">'
               '<div class="cat-sex camp-seg camp-per-chips">'
               + "".join(f'<button type="button" class="cat-sex-b{" on" if i == 2 else ""}" data-per="{i}" '
                         f'onclick="campPerPick(this)">{i}</button>' for i in (1, 2, 3, 5, 10))
               + '</div></div>')
    selbar = (
        '<div class="cat-selbar" id="catSelBar" role="region" aria-label="Выбранные вакансии">'
        '<span class="cat-selbar-n" id="catSelN">Выбрано 0</span>'
        '<button type="button" class="ghost cat-selbar-all" onclick="catSelectAll(this)">Все</button>'
        '<button type="button" class="ghost cat-selbar-clear" onclick="clearPicks()">Снять</button>'
        '<button type="button" class="primary cat-selbar-go" onclick="openCampSheet()">'
        'Кампания</button></div>')
    camp_sheet = (
        '<div class="cat-modal" id="campSheet" hidden>'
        '<div class="cat-modal-backdrop" onclick="closeCampSheet()"></div>'
        '<div class="cat-modal-panel" role="dialog" aria-modal="true" aria-label="Кампания">'
        '<div class="cat-modal-head"><span class="cat-modal-title">Кампания · '
        '<span id="campSheetN">0 вакансий</span></span>'
        '<button class="cat-modal-x" onclick="closeCampSheet()" aria-label="Закрыть">&#10005;</button></div>'
        '<div class="cat-modal-body">'
        '<div class="cs-sec"><div class="cs-label">Имя персоны</div>'
        '<input type="text" class="camp-input" id="campName" placeholder="Авто" '
        'autocomplete="off" maxlength="80" aria-label="Имя персоны (необязательно)">'
        '<div class="cat-proxy-hint">Пусто — имя подберётся автоматически. Имя одно на всю '
        'кампанию, почта и резюме новые на каждую подачу.</div></div>'
        f'<div class="cs-sec"><div class="cs-label">Пол</div>{sex_seg}</div>'
        f'<div class="cs-sec"><div class="cs-label">Подач в день</div>{per_seg}'
        '<div class="cat-proxy-hint">Каждый день по столько заявок, по кругу по выбранным '
        'вакансиям.</div></div>'
        '<div class="cs-sec"><div class="cs-label">Вакансии <b id="campJobsN">0</b></div>'
        '<div class="camp-jobs" id="campJobs"></div></div>'
        '</div>'
        '<div class="cat-modal-foot">'
        '<span class="cat-bulk-prog" id="campMsg"></span>'
        '<button class="cat-launch" id="campGo" onclick="mkCampaign()">Создать кампанию</button>'
        '</div></div></div>')
    toast = '<div class="cat-toast" id="catToast" role="status" aria-live="polite"></div>'
    settings = (
        '<div class="cat-modal" id="catSettings" hidden>'
        '<div class="cat-modal-backdrop" onclick="toggleFilters()"></div>'
        '<div class="cat-modal-panel" role="dialog" aria-modal="true" aria-label="Фильтры">'
        '<div class="cat-modal-head"><span class="cat-modal-title">Фильтры</span>'
        '<button class="cat-modal-x" onclick="toggleFilters()" aria-label="Закрыть">&#10005;</button></div>'
        '<div class="cat-modal-body">'
        f'<div class="cs-sec"><div class="cs-label">Регион</div>{region_chips}</div>'
        f'<div class="cs-sec"><div class="cs-label">Массовая подача</div>{bulk_bar}'
        '<div class="cat-bulk-report" id="bulkReport"></div></div>'
        f'<div class="cs-sec"><div class="cs-label">Кампании (каждый день)</div>{campaigns_block}</div>'
        '<div class="cs-sec"><div class="cs-label">Прокси <b id="pxCount">0</b></div>'
        f'<div class="cat-proxy-body">{proxy_block}</div></div>'
        '</div>'  # /cat-modal-body — filters above, launch pinned below
        '<div class="cat-modal-foot">'
        '<span class="cat-bulk-prog" id="bulkProg"></span>'
        '<button class="cat-bulk-stop" id="bulkStop" style="display:none" onclick="bulkStop()">Стоп</button>'
        '<button class="cat-launch" id="bulkGo" onclick="bulkFillAll()">▶ Запустить подачу</button>'
        '</div></div></div>')

    list_html = cards or '<div class="empty">Вакансий не найдено</div>'
    body = (
        _CAT_CSS + head + settings + camp_sheet + selbar + toast
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
.cat-h-title{display:flex;align-items:baseline;gap:10px;min-width:0;flex:1;color:var(--ink)}
.cat-h-co{color:var(--ink-mute);font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cat-h-n{color:var(--ink-mute);font-weight:600;font-size:14px;margin-left:4px}
/* One wide search field (pill). Live-filters as you type / on Enter. */
.cat-search-row{margin:-6px 0 12px}
.cat-q{width:100%;box-sizing:border-box;padding:12px 16px;border:1px solid var(--line-strong);border-radius:var(--r-full);font-size:15px;background:var(--panel);color:var(--ink)}
.cat-q::placeholder{color:var(--ink-mute)}
.cat-q:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgb(26 115 232/.15)}
/* Filters button — opens the settings sheet; shows the active region as a tag. */
.cat-filters-btn{display:inline-flex;align-items:center;gap:8px;flex:0 0 auto;background:var(--panel);color:var(--ink-soft);border:1px solid var(--line-strong);border-radius:var(--r-full);height:var(--ctl-h);padding:0 var(--ctl-px);font-size:var(--ctl-fs);font-weight:600;cursor:pointer}
.cat-h-btns{display:flex;align-items:center;gap:8px;flex:0 0 auto}
.cat-filters-btn svg{width:15px;height:15px;flex:0 0 auto}
/* Sheet sticky footer: filters scroll above, the launch button is pinned at the bottom. */
.cat-modal-foot{flex:0 0 auto;display:flex;align-items:center;gap:10px;padding:14px 20px;border-top:1px solid var(--line);background:var(--panel)}
.cat-launch{flex:1;display:inline-flex;align-items:center;justify-content:center;gap:8px;background:var(--accent);color:#fff;border:0;border-radius:var(--r-full);height:var(--ctl-h);padding:0 var(--ctl-px);font-size:var(--ctl-fs);font-weight:700;cursor:pointer;box-shadow:0 1px 2px rgba(12,71,194,.3)}
.cat-launch:hover{background:var(--accent-deep)}
.cat-launch:active{transform:translateY(1px)}
.cat-launch:disabled{opacity:.5;cursor:default;box-shadow:none}
/* in the footer the progress text must not force a full-width wrap (it does in the old bar) */
.cat-modal-foot .cat-bulk-prog{flex:0 1 auto;margin:0;min-width:0}
.cat-filters-btn:hover{border-color:var(--accent);color:var(--ink)}
.cat-filters-btn[aria-expanded=true]{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.cat-filters-tag{font-size:12px;font-weight:700;color:var(--accent);background:var(--accent-soft);border-radius:var(--r-full);padding:2px 9px}
.cat-filters-btn[aria-expanded=true] .cat-filters-tag{background:var(--panel)}
/* Filters — a MODAL dialog (regions + mass-apply + proxy). */
.cat-modal{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;padding:20px}
.cat-modal[hidden]{display:none}
.cat-modal-backdrop{position:absolute;inset:0;background:rgba(15,23,42,.55);animation:cm-fade .18s ease}
.cat-modal-panel{position:relative;display:flex;flex-direction:column;width:min(640px,100%);max-height:88vh;background:var(--panel);border:1px solid var(--line);border-radius:16px;box-shadow:0 24px 64px rgba(15,23,42,.30);overflow:hidden;animation:cm-pop .22s cubic-bezier(.22,.61,.36,1)}
.cat-modal-head{flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;padding:15px 20px;border-bottom:1px solid var(--line)}
.cat-modal-title{font-size:16px;font-weight:700;color:var(--ink)}
.cat-modal-x{width:var(--ctl-h);height:var(--ctl-h);border:none;background:transparent;border-radius:50%;font-size:16px;color:var(--ink-soft);cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s}
.cat-modal-x:hover{background:var(--panel-2)}
.cat-modal-x:hover{background:var(--line);color:var(--ink)}
.cat-modal-body{overflow:auto;padding:4px 20px 20px}
.cs-sec{padding:16px 0;border-bottom:1px solid var(--line)}
.cs-sec:last-child{border-bottom:0;padding-bottom:2px}
.cs-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-mute);margin:0 0 13px;display:flex;align-items:center;gap:8px}
.cs-label b{font-family:var(--ff-mono);font-weight:600;color:var(--accent);margin-left:auto;font-size:13px}
@keyframes cm-fade{from{opacity:0}to{opacity:1}}
@keyframes cm-pop{from{opacity:0;transform:translateY(12px) scale(.985)}to{opacity:1;transform:none}}
@keyframes cm-sheet{from{transform:translateY(100%)}to{transform:none}}
@media (prefers-reduced-motion:reduce){.cat-modal-backdrop,.cat-modal-panel{animation:none}}
.cat-regions{display:flex;flex-wrap:wrap;gap:8px}
.cat-regions::-webkit-scrollbar{display:none}
.cat-reg{display:inline-flex;align-items:center;gap:7px;white-space:nowrap;height:var(--ctl-h);padding:0 var(--ctl-px);border-radius:var(--r-full);border:1px solid var(--line-strong);background:var(--panel);color:var(--ink-soft);font-size:var(--ctl-fs);font-weight:600;text-decoration:none}
.cat-reg b{font-family:var(--ff-mono,monospace);font-weight:500;font-size:12px;color:var(--ink-mute)}
.cat-reg:hover{border-color:var(--accent);text-decoration:none}
.cat-reg.on{background:var(--accent);border-color:var(--accent);color:#fff;box-shadow:0 2px 8px -2px rgba(12,71,194,.5)}
.cat-reg.on b{color:rgba(255,255,255,.85)}
.cat-list{display:flex;flex-direction:column;gap:10px}
/* room under the list so the fixed selection bar never covers the last card's controls */
.cat-list.selon{padding-bottom:84px}
.cat-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:13px 14px;transition:border-color .15s,background-color .15s,box-shadow .15s}
.cat-card.sel{border-color:var(--accent);background:var(--accent-soft);box-shadow:0 0 0 1px var(--accent) inset}
.cat-top{display:flex;align-items:center;gap:8px;margin-bottom:3px}
.cat-co{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;font-weight:700;color:var(--ink-mute);text-transform:uppercase;letter-spacing:.03em}
/* Selection checkbox: a 26px round control inside a 44px tap target (negative margins keep
   the top row 26px tall). Ticked -> accent fill + white check; the card gets .sel. */
.cat-pick{flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;margin:-9px -11px -9px -6px;cursor:pointer;position:relative;border-radius:50%}
.cat-pick input{position:absolute;opacity:0;width:1px;height:1px;margin:0;pointer-events:none}
.cat-pick span{width:26px;height:26px;border-radius:50%;border:2px solid var(--line-strong);background:var(--panel);display:flex;align-items:center;justify-content:center;transition:background-color .12s,border-color .12s,transform .12s;box-sizing:border-box}
.cat-pick span::after{content:"";width:6px;height:11px;border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg) translate(-1px,-1px);opacity:0}
.cat-pick:hover span{border-color:var(--accent)}
.cat-pick input:checked+span{background:var(--accent);border-color:var(--accent)}
.cat-pick input:checked+span::after{opacity:1}
.cat-pick input:focus-visible+span{box-shadow:0 0 0 3px var(--accent-soft)}
.cat-pick:active span{transform:scale(.92)}
/* Selection bar: fixed, centered pill; hidden at 0 selected, slides in when >=1. Sits ABOVE
   the shell's mobile tab bar (var(--jf-tabbar)) and BELOW the .cat-modal sheets (z 1000). */
.cat-selbar{position:fixed;left:0;right:0;bottom:18px;margin:0 auto;width:max-content;max-width:min(520px,calc(100vw - 24px));z-index:45;display:flex;align-items:center;gap:10px;padding:8px 8px 8px 18px;background:var(--panel);border:1px solid var(--line-strong);border-radius:var(--r-full);box-shadow:0 14px 36px -10px rgba(15,23,42,.42),0 2px 8px -2px rgba(15,23,42,.18);opacity:0;visibility:hidden;transform:translateY(14px);transition:opacity .2s ease,transform .22s cubic-bezier(.22,.61,.36,1),visibility 0s linear .22s}
.cat-selbar.on{opacity:1;visibility:visible;transform:none;transition:opacity .2s ease,transform .22s cubic-bezier(.22,.61,.36,1)}
.cat-selbar-n{font-size:14px;font-weight:700;color:var(--ink);white-space:nowrap}
.cat-selbar-n b{font-family:var(--ff-mono);font-weight:600;color:var(--accent)}
.cat-selbar .cat-selbar-all{flex:0 0 auto}
.cat-selbar .cat-selbar-clear{flex:0 0 auto}
.cat-selbar .cat-selbar-go{flex:0 0 auto;white-space:nowrap}
/* Campaign sheet controls */
.camp-input{width:100%;box-sizing:border-box;height:var(--ctl-h);padding:0 14px;border:1px solid var(--line-strong);border-radius:var(--r-full);font-size:15px;background:var(--panel);color:var(--ink);min-height:0}
.camp-input::placeholder{color:var(--ink-mute)}
.camp-input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.camp-seg{display:inline-flex}
.camp-seg .cat-sex-b{min-width:44px}
.camp-per{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.camp-per-n{width:84px;height:var(--ctl-h);border:1px solid var(--line-strong);border-radius:var(--r-full);
  padding:0 12px;font-size:16px;font-weight:700;text-align:center;background:var(--panel);color:var(--ink);
  -moz-appearance:textfield}
.camp-per-n::-webkit-outer-spin-button,.camp-per-n::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
.camp-per-n:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(12,71,194,.15)}
.camp-per-chips .cat-sex-b{min-width:38px}
.camp-jobs{display:flex;flex-direction:column;gap:2px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--bg-app);padding:6px 12px;font-size:13px;line-height:1.4}
.camp-job{display:flex;align-items:baseline;gap:8px;padding:5px 0;border-bottom:1px solid var(--line);min-width:0}
.camp-job:last-child{border-bottom:0}
.camp-job-co{flex:0 0 auto;font-size:11px;font-weight:700;color:var(--ink-mute);text-transform:uppercase;letter-spacing:.03em;max-width:38%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.camp-job-t{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink)}
.camp-job-more{color:var(--ink-mute);padding:5px 0;font-size:12.5px}
/* Toast (3 s) after a campaign is created */
.cat-toast{position:fixed;left:50%;bottom:calc(var(--jf-tabbar,0px) + env(safe-area-inset-bottom) + 26px);transform:translate(-50%,16px);z-index:1100;background:var(--ink);color:#fff;font-size:13.5px;font-weight:500;padding:11px 18px;border-radius:var(--r-full);box-shadow:0 8px 24px -6px rgba(32,33,36,.5);opacity:0;transition:opacity .25s,transform .25s;pointer-events:none;max-width:88vw;text-align:center}
.cat-toast.show{opacity:1;transform:translate(-50%,0)}
@media (prefers-reduced-motion:reduce){.cat-selbar,.cat-toast,.cat-pick span,.cat-card{transition:none}}
.cat-wp{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:999px;border:1px solid var(--line);white-space:nowrap}
.cat-wp-remote{color:#188038;border-color:#bcdfc4}.cat-wp-hybrid{color:var(--accent);border-color:#b8d3f5}.cat-wp-onsite{color:var(--ink-mute)}
.cat-title{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font-size:15.5px;font-weight:600;color:var(--ink);line-height:1.3;min-height:calc(1.3em*2);margin-bottom:5px;text-decoration:none}
a.cat-title:hover{color:var(--accent);text-decoration:underline}
.cat-meta{font-size:12.5px;color:var(--ink-mute);margin-bottom:8px}
.cat-meta span{color:inherit}
.cat-comp{font-size:13px;color:var(--ink-soft);margin:-2px 0 9px;display:flex;flex-wrap:wrap;gap:4px 8px;align-items:baseline}
.cat-comp b{font-weight:700;color:var(--ink)}
.cat-comp .cmp-lbl{font-size:11px;color:var(--ink-mute);font-weight:500}
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
.cat-qlist li{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;padding:10px 12px;border-bottom:1px solid var(--line);font-size:13px;line-height:1.4}
.cat-qlist li:last-child{border-bottom:0}
.cat-qlist li>span{color:var(--ink);min-width:0}
.cat-qlist b{color:var(--danger);font-weight:700;margin-left:3px}
.cat-qlist i{flex:0 0 auto;margin-top:1px;font-style:normal;font-family:var(--ff-mono);font-size:10.5px;color:var(--ink-mute);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:1px 7px;white-space:nowrap}
.empty{color:var(--ink-mute);text-align:center;padding:44px 0}
.cat-fill-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}
/* Описание · Вопросы toggles share the action row; an open one drops below at full width */
.cat-dets{display:flex;align-items:center;flex-wrap:wrap;gap:0 16px}
.cat-dets .cat-det{margin-top:0}
.cat-dets:has(details[open]){flex:1 1 100%;margin-left:0;flex-direction:column;align-items:stretch}
.cat-dets:has(details[open]) .cat-det{width:100%}
/* COMPACT phone card (owner: «карточки всё ещё большие» — was ~380px, one control per line) */
@media(max-width:760px){
  .cat-card{padding:10px 12px}
  .cat-top{margin-bottom:2px}
  .cat-title{font-size:14.5px;min-height:0;margin-bottom:3px;line-height:1.28}
  .cat-meta{margin-bottom:3px;font-size:12px}
  .cat-comp{margin:0 0 4px;font-size:12.5px;gap:3px 6px}
  .cat-fill-row{margin-top:7px;padding-top:8px;gap:8px}
  .cat-det>summary{padding:6px 0;font-size:12.5px}
}
/* Sex is a compact segmented toggle, not two big buttons — one modifier for the single
   primary action below. */
.cat-sex{display:inline-flex;align-items:center;height:var(--ctl-h);background:var(--panel-2);border:1px solid var(--line-strong);border-radius:var(--r-full);padding:3px}
.cat-sex-b{border:0;background:transparent;color:var(--ink-mute);font-size:var(--ctl-fs);font-weight:600;line-height:1;min-width:40px;height:calc(var(--ctl-h) - 8px);padding:0 12px;border-radius:var(--r-full);cursor:pointer;display:inline-flex;align-items:center;justify-content:center}
.cat-sex-b.on{background:var(--panel);color:var(--accent);box-shadow:0 1px 2px rgba(0,0,0,.12)}
@media(max-width:760px){.cat-h-row{flex-wrap:wrap}.cat-h-title{flex:1 1 100%;order:2}.cat-h-btns{order:1;margin-left:auto}}
.cat-fill{display:inline-flex;align-items:center;justify-content:center;background:var(--accent);color:#fff;border:none;border-radius:var(--r-full);height:var(--ctl-h);padding:0 var(--ctl-px);font-size:var(--ctl-fs);font-weight:600;cursor:pointer}
.cat-fill:hover{background:var(--accent-deep)}
.cat-fill:disabled{opacity:.6;cursor:default}
.cat-fill-res a{color:var(--accent);font-weight:600;font-size:13px;text-decoration:none}
.cat-fill-res a:hover{text-decoration:underline}
.cat-bulk{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0}
.cat-bulk-go{display:inline-flex;align-items:center;justify-content:center;gap:8px;background:#0b8043;color:#fff;border:none;border-radius:var(--r-full);padding:10px 18px;font-size:13.5px;font-weight:700;cursor:pointer;min-height:42px;box-shadow:0 1px 2px rgba(11,128,67,.3)}
.cat-bulk-go:hover{background:#0a7038}
.cat-bulk-go:active{transform:translateY(1px)}
.cat-bulk-go:disabled{opacity:.5;cursor:default;box-shadow:none}
.cat-bulk-stop{display:inline-flex;align-items:center;justify-content:center;gap:6px;background:var(--danger);color:#fff;border:none;border-radius:var(--r-full);height:var(--ctl-h);padding:0 var(--ctl-px);font-size:var(--ctl-fs);font-weight:700;cursor:pointer}
.cat-bulk-prog{flex:1 1 100%;font-size:12.5px;font-weight:600;color:var(--ink-soft);margin:0}
.cat-bulk-n{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:700;color:var(--ink-soft)}
.cat-bulk-n input{width:76px;box-sizing:border-box;padding:10px 12px;border:1px solid var(--line-strong);border-radius:var(--r-full);font-size:14px;font-weight:700;background:var(--panel);color:var(--ink);min-height:42px;text-align:center}
.cat-bulk-n input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgb(26 115 232/.15)}
.cat-bulk-sel{height:var(--ctl-h);max-width:230px;padding:0 12px;border:1px solid var(--line-strong);border-radius:var(--r-full);font-size:var(--ctl-fs);font-weight:600;background:var(--panel);color:var(--ink);cursor:pointer;transition:border-color .15s,box-shadow .15s}
.cat-bulk-sel:hover{border-color:var(--accent)}
.cat-bulk-sel:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgb(26 115 232/.15)}
.cat-bulk-report{margin-top:10px;font-size:12.5px;color:var(--ink-soft);line-height:1.55}
.cat-bulk-report:empty{display:none}
.cat-bulk-report .r-sub{color:var(--ink-mute)}
.cat-bulk-report a{color:var(--accent);font-weight:600;text-decoration:none}
.cat-bulk-report a:hover{text-decoration:underline}
.cat-proxy-body{max-width:640px}
.cat-proxy-body textarea{width:100%;min-height:110px;box-sizing:border-box;font-family:var(--ff-mono);font-size:12.5px;line-height:1.5;border:1px solid var(--line-strong);border-radius:var(--r-sm);padding:10px;resize:vertical;background:var(--bg-app);color:var(--ink)}
.cat-proxy-hint{font-size:11.5px;line-height:1.45;color:var(--ink-mute);margin:6px 0 10px}
.px-mobile{margin:10px 0 6px;padding:10px 12px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--panel)}
.px-mobile-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:6px;font-size:13px}
.px-mobile-row:first-child{margin-top:0}
.px-mobile-st{color:var(--ink-soft)}
.px-mobile-sw{margin-left:auto;display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:var(--ink-soft);cursor:pointer}
.px-mobile-sw input{width:18px;height:18px;margin:0}
.px-mobile-in{flex:1 1 180px;min-width:0;height:36px;border:1px solid var(--line-strong);border-radius:var(--r-full);padding:0 12px;font-size:14px;font-family:var(--ff-mono);background:var(--panel);color:var(--ink)}
.px-mobile .px-dot{background:var(--ink-mute);box-shadow:none}.px-mobile .px-dot.px-ok{background:var(--ok);box-shadow:0 0 0 3px rgba(11,128,67,.18)}.px-mobile .px-dot.px-bad{background:var(--danger)}
.px-mobile-list{margin:6px 0 0;display:flex;flex-direction:column;gap:4px}
.px-mob-ep{display:flex;align-items:center;gap:7px;font-size:12.5px;flex-wrap:wrap}
.px-mob-srv{font-family:var(--ff-mono);color:var(--ink)}
.px-mob-tag{font-size:11px;color:var(--ink-soft);border:1px solid var(--line);border-radius:999px;padding:0 7px}
.px-mob-ip{color:var(--ink-soft);margin-left:auto;font-family:var(--ff-mono);font-size:11.5px}
.px-mob-x{border:0;background:none;color:var(--ink-mute);cursor:pointer;font-size:14px;line-height:1;padding:0 2px}
.px-mob-x:hover{color:var(--danger)}
.cs-camp-list{margin-top:8px;display:flex;flex-direction:column;gap:6px;font-size:13px}
.cs-camp-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:6px 0;border-top:1px solid var(--line)}
.cs-camp-row button{border:1px solid var(--line-strong);background:var(--panel);color:var(--ink-soft);border-radius:var(--r-sm);padding:3px 9px;font-size:12px;cursor:pointer}
.cs-camp-row button:hover{background:var(--panel-2)}
.camp-tally{font-family:var(--ff-mono);font-size:11.5px;color:var(--ink-mute);white-space:nowrap}
.camp-jrnl{margin:2px 0 8px;padding:6px 0 2px;display:flex;flex-direction:column;gap:6px;border-top:1px dashed var(--line)}
.camp-jrnl-row{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-size:12px;line-height:1.35}
.camp-when{font-family:var(--ff-mono);font-size:10.5px;color:var(--ink-mute);white-space:nowrap}
.camp-what{flex:1 1 150px;min-width:0;color:var(--ink)}
.camp-who{font-family:var(--ff-mono);font-size:10px;color:var(--ink-mute);width:100%;word-break:break-all}
.camp-badge{flex:0 0 auto;font-size:11px;font-weight:700;border-radius:var(--r-sm);padding:1px 7px;white-space:nowrap}
.camp-badge-ok{background:#e6f4ea;color:var(--ok)}
.camp-badge-bad{background:#fce8e6;color:var(--danger)}
.camp-badge-warn{background:var(--warn-soft);color:var(--warn)}
.camp-badge-mute{background:var(--panel-2);color:var(--ink-soft)}
.cc-dot.cc-on{color:var(--ok)}.cc-dot.cc-off{color:var(--ink-mute)}
.cat-proxy-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.cat-proxy-go{display:inline-flex;align-items:center;justify-content:center;background:var(--accent);color:#fff;border:none;border-radius:var(--r-full);height:var(--ctl-h);padding:0 var(--ctl-px);font-size:var(--ctl-fs);font-weight:600;cursor:pointer}
.cat-proxy-go:hover{background:var(--accent-deep)}
.cat-proxy-clr{display:inline-flex;align-items:center;justify-content:center;background:var(--panel);color:var(--danger);border:1px solid var(--line-strong);border-radius:var(--r-full);height:var(--ctl-h);padding:0 var(--ctl-px);font-size:var(--ctl-fs);font-weight:600;cursor:pointer}
.cat-proxy-clr:hover{border-color:var(--danger)}
.cat-proxy-msg{font-size:12.5px;font-weight:600;color:var(--ink-soft)}
.px-status{display:flex;align-items:center;gap:10px;font-size:14px}
.px-dot{width:9px;height:9px;border-radius:50%;background:#0b8043;box-shadow:0 0 0 3px rgba(11,128,67,.18);flex:0 0 auto}
.px-status.px-empty .px-dot{background:var(--ink-mute);box-shadow:0 0 0 3px rgba(100,116,139,.15)}
.px-summary{font-weight:600;color:var(--ink)}
.px-toggle{margin-left:auto;display:inline-flex;align-items:center;background:transparent;border:1px solid var(--line-strong);border-radius:var(--r-full);height:var(--chip-h);padding:0 var(--chip-px);font-size:var(--chip-fs);font-weight:600;color:var(--ink-soft);cursor:pointer;transition:border-color .15s,color .15s}
.px-toggle:hover{border-color:var(--accent);color:var(--accent)}
.px-add{margin-top:14px;border-top:1px dashed var(--line);padding-top:12px}
.px-add>summary{cursor:pointer;font-size:12.5px;font-weight:600;color:var(--accent);list-style:none;user-select:none;display:inline-flex;align-items:center;gap:7px;padding:2px 0}
.px-add>summary::-webkit-details-marker{display:none}
.px-add>summary::before{content:'+';font-weight:700;font-size:15px;line-height:1;width:12px;text-align:center}
.px-add[open]>summary::before{content:'\2212'}
.px-add>summary:hover{text-decoration:underline}
.cat-proxy-list{margin:12px 0 2px;display:flex;flex-wrap:wrap;gap:6px;max-height:210px;overflow:auto;padding:2px}
.cat-proxy-list[hidden]{display:none}
.px-ip{font-family:var(--ff-mono);font-size:11.5px;color:#0b8043;background:rgba(11,128,67,.1);border-radius:var(--r-sm);padding:3px 9px}
/* Mobile: the Gmail top pill IS the search there, so hide this page's own wide input;
   keep the header to just title + Фильтры. */
@media(max-width:760px){
  .cat-q{display:none}
  /* the shell's top pill carries the funnel (.gm-tune -> toggleFilters) on phones, so the
     header's own «Фильтры» button goes; with .head-actions then empty the shell hides it. */
  .cat-filters-btn{display:none}
  .cat-search-row{display:none}
  /* selection bar: full width minus margins, above the shell's fixed bottom tab bar */
  .cat-selbar{left:12px;right:12px;width:auto;max-width:none;margin:0;bottom:calc(var(--jf-tabbar,0px) + env(safe-area-inset-bottom) + 10px);padding:8px 8px 8px 14px;gap:6px}
  .cat-selbar-n{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}
  .cat-selbar .cat-selbar-all,.cat-selbar .cat-selbar-clear{padding:0 11px}
  .cat-selbar .cat-selbar-go{padding:0 14px}
  .cat-list.selon{padding-bottom:calc(var(--jf-tabbar,0px) + 84px)}
  .cat-head{gap:6px;margin-bottom:2px}
  .cat-h-title{font-size:17px}
  .cat-title{font-size:15px}
  .cat-bulk{flex:1 1 auto}
  .cat-bulk-go{flex:1 1 auto}
  .cat-proxy-body{max-width:none}
  .cat-reg{min-height:40px}
  /* Filters modal becomes a bottom-sheet on phones. */
  .cat-modal{padding:0;align-items:flex-end}
  .cat-modal-panel{width:100%;max-height:92vh;border-radius:18px 18px 0 0;border-bottom:0;animation:cm-sheet .26s cubic-bezier(.22,.61,.36,1)}
  .cat-modal-foot{padding-bottom:calc(14px + env(safe-area-inset-bottom))}
  .cat-modal-head{padding:14px 18px}
  .cat-modal-body{padding:2px 18px 22px}
}
</style>"""

_CAT_JS = """<script>
// Catalog page script. RE-ENTRANT by contract: the «Вакансии» tabs swap <main>'s innerHTML and
// re-run this inline script in the SAME document, so: only var / function declarations /
// window.x= at top level (no top-level const/let), every window/document-level listener is
// registered with the shell's per-page AbortSignal (window.jfPage), and every async chain
// captures window.__jfGen and bails once the page was switched away. Both shell globals are
// optional — without them the page behaves as a plain document.
function catSig(){ return (window.jfPage||{}).signal; }
function catEsc(s){ return String(s==null?'':s).replace(/[&<>"']/g,function(ch){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch];}); }
function catPlural(n,one,few,many){ n=Math.abs(n|0); var a=n%10, b=n%100;
  if(a===1&&b!==11) return one; if(a>=2&&a<=4&&!(b>=12&&b<=14)) return few; return many; }
// ♂/♀ segmented toggle (per card + the campaign sheet's М/Ж and 1..5 groups): mark the
// tapped segment active within its .cat-sex group.
window.pickSex = function(b){
  var g=b.closest('.cat-sex'); if(!g) return;
  g.querySelectorAll('.cat-sex-b').forEach(function(x){
    var on=(x===b); x.classList.toggle('on', on); x.setAttribute('aria-pressed', on?'true':'false');
  });
};
// «Все»: put EVERY job of the current search (not just the rendered page) into the selection —
// ids come from /catalog/ids with the live query. Lives in the bottom selection bar (which is
// only visible once ≥1 card is picked); «Снять» clears. The bar's «Выбрано N» reflects the count.
// NB: named catSelectAll, NOT selectAll — the shell _JS (loaded AFTER this page's <main>) declares a
// top-level `function selectAll(){…}` for the mail inbox, which hoists over any `window.selectAll=`
// we set here and turned «Все» into a silent no-op (it selected `.maillist .mitem`, absent on /catalog).
window.catSelectAll = async function(btn){
  var qp=(window.catQuery?window.catQuery():{}), sp=new URLSearchParams();
  if(qp.q) sp.set('q', qp.q); if(qp.region) sp.set('region', qp.region); if(qp.company) sp.set('company', qp.company);
  var old=btn.textContent; btn.disabled=true; btn.textContent='…';
  var gen=window.__jfGen;
  try{
    var r=await fetch('/catalog/ids?'+sp.toString()), j=r.ok?await r.json():{jobs:[]};
    if(gen!==window.__jfGen) return;
    var S=window.catSel;
    (j.jobs||[]).forEach(function(x){ var id=parseInt(x.id,10); if(!(id>0)) return;
      S.ids.add(id); S.meta[id]={co:String(x.company||''), t:String(x.title||'')}; });
    catSaveSel(); syncPicks();
  }catch(e){}
  finally{ btn.disabled=false; btn.textContent=old; }
};
// ---- card selection -> campaign ---------------------------------------------------
// Selected ids (+ company/title for the sheet's list) persist in sessionStorage so a
// search, a pagination scroll or a tab switch never loses the pick. Reloaded on every run
// of this script (re-entrant); the DOM checkboxes are re-synced by syncPicks().
window.catSel = (function(){
  var ids=[], meta={};
  try{ ids=JSON.parse(sessionStorage.getItem('cat_sel')||'[]'); }catch(e){ ids=[]; }
  try{ meta=JSON.parse(sessionStorage.getItem('cat_sel_meta')||'{}'); }catch(e){ meta={}; }
  if(!Array.isArray(ids)) ids=[];
  if(!meta || typeof meta!=='object') meta={};
  var set=new Set(); ids.forEach(function(x){ x=parseInt(x,10); if(x>0) set.add(x); });
  return {ids:set, meta:meta};
})();
function catSaveSel(){
  var S=window.catSel, m={};
  S.ids.forEach(function(i){ if(S.meta[i]) m[i]=S.meta[i]; });
  S.meta=m;
  try{ sessionStorage.setItem('cat_sel', JSON.stringify(Array.from(S.ids)));
       sessionStorage.setItem('cat_sel_meta', JSON.stringify(m)); }catch(e){}
}
window.pickJob = function(cb){
  var id=parseInt(cb.dataset.id,10); if(!(id>0)) return;
  var S=window.catSel, card=cb.closest('.cat-card');
  if(cb.checked){
    S.ids.add(id);
    if(card){ var co=card.querySelector('.cat-co'), t=card.querySelector('.cat-title');
      S.meta[id]={co:(co?co.textContent:'').trim(), t:(t?t.textContent:'').trim()}; }
  }else{ S.ids.delete(id); delete S.meta[id]; }
  if(card) card.classList.toggle('sel', cb.checked);
  catSaveSel(); renderSelBar();
};
window.syncPicks = function(root){
  var S=window.catSel;
  (root||document).querySelectorAll('.cat-pick input[type=checkbox]').forEach(function(cb){
    if(!cb.dataset.id) return;                      // skip any checkbox without a job id
    var on=S.ids.has(parseInt(cb.dataset.id,10)); cb.checked=on;
    var card=cb.closest('.cat-card'); if(card) card.classList.toggle('sel', on);
  });
  renderSelBar();
};
window.clearPicks = function(){
  var S=window.catSel; S.ids.clear(); S.meta={}; catSaveSel(); syncPicks();
};
window.renderSelBar = function(){
  var n=window.catSel.ids.size, bar=document.getElementById('catSelBar'),
      nEl=document.getElementById('catSelN'), list=document.getElementById('catlist');
  if(nEl) nEl.innerHTML='Выбрано <b>'+n+'</b>';
  if(bar) bar.classList.toggle('on', n>0);
  if(list) list.classList.toggle('selon', n>0);
};
// The campaign sheet (#campSheet, .cat-modal chrome): name (optional) · М/Ж · 1..5 per day ·
// the selected jobs. «Создать кампанию» posts ONE daily campaign over the selection.
// «Подач в день»: a free number input + quick chips that set it (a typed value un-highlights the chips)
window.campPerPick = function(b){var n=document.getElementById('campPerN');if(n)n.value=b.dataset.per||'2';
  var all=b.parentNode.querySelectorAll('.cat-sex-b');for(var i=0;i<all.length;i++)all[i].classList.toggle('on',all[i]===b);};
window.campPerTyped = function(inp){var v=String(parseInt(inp.value,10)||'');
  var all=document.querySelectorAll('#campPer .cat-sex-b');for(var i=0;i<all.length;i++)all[i].classList.toggle('on',all[i].dataset.per===v);};
window.openCampSheet = function(){
  var S=window.catSel, ids=Array.from(S.ids); if(!ids.length) return;
  var s=document.getElementById('campSheet'); if(!s) return;
  var n=ids.length, MAX=6;
  var nEl=document.getElementById('campSheetN');
  if(nEl) nEl.textContent=n+' '+catPlural(n,'вакансия','вакансии','вакансий');
  var jn=document.getElementById('campJobsN'); if(jn) jn.textContent=n;
  var box=document.getElementById('campJobs');
  if(box){
    var rows=ids.slice(0,MAX).map(function(i){ var m=S.meta[i]||{};
      return '<div class="camp-job"><span class="camp-job-co">'+catEsc(m.co||'')+'</span>'
        +'<span class="camp-job-t">'+catEsc(m.t||('#'+i))+'</span></div>'; });
    if(n>MAX) rows.push('<div class="camp-job-more">и ещё '+(n-MAX)+'</div>');
    box.innerHTML=rows.join('');
  }
  var msg=document.getElementById('campMsg'); if(msg) msg.textContent='';
  var go=document.getElementById('campGo'); if(go) go.disabled=false;
  s.removeAttribute('hidden'); document.body.style.overflow='hidden';
};
window.closeCampSheet = function(){
  var s=document.getElementById('campSheet'); if(!s) return;
  s.setAttribute('hidden','');
  var f=document.getElementById('catSettings');
  if(!f || f.hasAttribute('hidden')) document.body.style.overflow='';
};
var catToastT;
window.catToast = function(msg){
  var t=document.getElementById('catToast'); if(!t) return;
  t.textContent=msg; t.classList.add('show');
  clearTimeout(catToastT); catToastT=setTimeout(function(){ t.classList.remove('show'); }, 3000);
};
// Lazy job description: fetched ONCE on the first open of the card's «Описание».
window.catLoadDesc = async function(det){
  if(!det || !det.open || det.dataset.loaded) return;
  var url=det.dataset.desc, box=det.querySelector('.cat-desc'); if(!url||!box) return;
  det.dataset.loaded='1';
  var gen=window.__jfGen;
  try{
    var r=await fetch(url), txt=r.ok?await r.text():'';
    if(gen!==window.__jfGen) return;
    box.innerHTML=txt||'<p>Описание недоступно</p>'; box.classList.remove('cat-desc-lazy');
  }catch(e){
    if(gen!==window.__jfGen) return;
    box.textContent='Не удалось загрузить — откройте ещё раз'; delete det.dataset.loaded;
  }
};
// Bulk "apply to all": ONE sequential queue on the server over every greenhouse+ashby
// job (Lever/Workable are skipped server-side — live captcha). Auto-submits per job.
// We only START/STOP/POLL here — the fill+submit logic is untouched. It runs long; the
// batch survives leaving this page (server-side thread), poll resumes on reload.
window.bulkFillAll = async function(){
  var go=document.getElementById('bulkGo'), prog=document.getElementById('bulkProg'),
      nEl=document.getElementById('bulkN'), gEl=document.getElementById('bulkGender'),
      cEl=document.getElementById('bulkCompany'), rEl=document.getElementById('bulkRegion'),
      wEl=document.getElementById('bulkW');
  if(!go || go.disabled) return;
  var raw=(nEl&&nEl.value||'').trim();
  var n=parseInt(raw,10);
  var all=!(n>=1);                 // пусто / не число / <=0 => все доступные
  if(!all){ if(n>20000) n=20000; if(nEl) nEl.value=n; }
  var countStr=all?'':String(n);
  var nLbl=all?'ВСЕ доступные вакансии':('до '+n+' вакансий');
  var wraw=(wEl&&wEl.value||'').trim();
  var wnum=parseInt(wraw,10);
  var wAuto=!(wnum>=1);              // пусто / не число => авто (сервер сам решает)
  if(!wAuto){ if(wnum>18) wnum=18; if(wEl) wEl.value=wnum; }
  var wStr=wAuto?'':String(wnum);
  var wLbl=wAuto?'авто':String(wnum);
  var gender=(gEl&&gEl.value)||'', company=(cEl&&cEl.value)||'', region=(rEl&&rEl.value)||'';
  var cLbl=(cEl&&cEl.selectedIndex>0)?cEl.options[cEl.selectedIndex].text:'все компании';
  var gLbl=gender==='female'?'женщины':(gender==='male'?'мужчины':'любой пол');
  if(!confirm('Массовая подача: '+nLbl+'\\n'
      +'Пол: '+gLbl+' · Компания: '+cLbl+(region?(' · Регион: '+region):'')+' · Потоков: '+wLbl+'\\n\\n'
      +'Greenhouse/Ashby заполняются и авто-отправляются параллельными браузерами'
      +(wAuto?' (число подбирается автоматически под нагрузку сервера)':(' ('+wLbl+' потоков)'))+'. '
      +'Lever/Workable (капча) сразу уходят в «Незавершённые» на ручное дожатие. '
      +'Прервать — «Стоп».')) return;
  go.disabled=true; if(prog) prog.textContent='Запуск…';
  var gen=window.__jfGen;
  try{
    var body='count='+encodeURIComponent(countStr)+'&gender='+encodeURIComponent(gender)
        +'&company='+encodeURIComponent(company)+'&region='+encodeURIComponent(region)
        +'&workers='+encodeURIComponent(wStr);
    var j=await (await fetch('/catalog/fill_all',{method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body})).json();
    if(gen!==window.__jfGen) return;
    if(j.started===false && prog){ prog.textContent = j.error||'Уже идёт'; }
    else if(prog && j.total!==undefined){ prog.textContent='Найдено '+j.total+' — запуск…'; }
  }catch(e){ if(gen!==window.__jfGen) return; go.disabled=false; if(prog) prog.textContent='Ошибка запуска'; return; }
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
  var gen=window.__jfGen;
  function again(ms){ setTimeout(function(){ if(gen===window.__jfGen) bulkPoll(); }, ms); }
  try{
    var s=await (await fetch('/catalog/fill_all_status')).json();
    if(gen!==window.__jfGen) return;
    var line=(s.done||0)+'/'+(s.total||0)+' · ✓'+(s.ok||0)+' ✗'+(s.failed||0)
             +(s.current?(' · '+s.current):'');
    if(s.state==='running'){
      go.style.display='none'; go.disabled=true; stop.style.display='';
      prog.textContent=line; again(3000);
    }else if(s.state==='done'||s.state==='stopped'){
      stop.style.display='none'; go.style.display=''; go.disabled=false;
      prog.textContent=(s.state==='stopped'?'Остановлено':'Готово')+': '+line;
      bulkReport();
    }else{
      go.disabled=false;
    }
  }catch(e){ again(5000); }
}
bulkPoll();   // resume progress if a batch is already running when the page loads

// Filters/settings sheet holds regions + mass-apply + proxy — declutters the top. Callable
// from OUTSIDE <main> too (the shell's mobile top-pill funnel button).
window.toggleFilters=function(){
  var s=document.getElementById('catSettings'), b=document.getElementById('fltBtn');
  if(!s) return;
  var willOpen=s.hasAttribute('hidden');
  if(willOpen){ s.removeAttribute('hidden'); document.body.style.overflow='hidden'; pxRefresh(); pxMobileRefresh(); bulkPoll(); bulkReport(); loadCampaigns(); }
  else{ s.setAttribute('hidden',''); document.body.style.overflow=''; }
  if(b) b.setAttribute('aria-expanded', willOpen?'true':'false');
};
document.addEventListener('keydown',function(e){
  if(e.key!=='Escape') return;
  var c=document.getElementById('campSheet');
  if(c && !c.hasAttribute('hidden')){ window.closeCampSheet(); return; }
  var s=document.getElementById('catSettings');
  if(s && !s.hasAttribute('hidden')) window.toggleFilters();
},{signal:catSig()});
// Last bulk-run report (survives restart — read from logs/bulk_apply_last.json).
async function bulkReport(){
  var el=document.getElementById('bulkReport'); if(!el) return;
  var gen=window.__jfGen;
  try{
    var r=await (await fetch('/catalog/fill_all_report')).json();
    if(gen!==window.__jfGen) return;
    if(!r || !r.run_id){ el.innerHTML=''; return; }
    var st=r.state==='running'?'идёт':(r.state==='stopped'?'остановлен':'завершён');
    el.innerHTML=
      '<div>Прогон <b>'+catEsc(r.run_id)+'</b> — '+st+'</div>'+
      '<div class="r-sub">заполнено '+(r.filled_ok||0)+' · ошибок '+(r.errors||0)+
      ' · клик Submit '+(r.submit_clicked||0)+' · подтверждено '+(r.submit_confirmed||0)+
      ' · пропущено '+(r.skipped||0)+' из '+(r.total||0)+'</div>'+
      '<div><a href="/catalog/fill_all_log" download>Скачать лог</a></div>';
  }catch(e){}
}
// Proxy pool: upload a list, invalid ones dropped on validation, applications then
// rotate through the survivors (a different egress IP per submit).
function _pxAgo(ts){
  if(!ts) return 'ещё не проверялись';
  var s=Math.max(0,Math.floor(Date.now()/1000-ts));
  if(s<60) return 'проверка только что';
  var m=Math.floor(s/60); if(m<60) return 'проверка '+m+' мин назад';
  var h=Math.floor(m/60); if(h<24) return 'проверка '+h+' ч назад';
  return 'проверка '+Math.floor(h/24)+' дн назад';
}
function pxRenderList(ips){
  var list=document.getElementById('pxList'); if(!list) return;
  list.innerHTML=(ips||[]).map(function(x){
    return '<span class="px-ip">'+catEsc(x.ip||x.server||'')+'</span>';}).join('');
}
async function pxRefresh(){
  var c=document.getElementById('pxCount'), sum=document.getElementById('pxSummary'),
      st=document.getElementById('pxStatus'), tog=document.getElementById('pxToggle'),
      list=document.getElementById('pxList');
  var gen=window.__jfGen;
  try{
    var s=await (await fetch('/proxies')).json();
    if(gen!==window.__jfGen) return;
    var n=s.count||0;
    if(c) c.textContent=n;
    if(sum) sum.textContent=n+(n===1?' живой · ':' живых · ')+_pxAgo(s.last_check);
    if(st) st.classList.toggle('px-empty', n===0);
    if(tog) tog.hidden=(n===0);
    if(list && !list.hidden) pxRenderList(s.ips);
  }catch(e){}
}
// «Мобильные прокси» (a pool of phones over Tailscale): aggregate status + on/off + endpoint list
// + add/remove. Re-entrant, gen-guarded.
window.pxMobileRefresh=async function(force){
  var st=document.getElementById('pxMobSt'), dot=document.getElementById('pxMobDot'),
      on=document.getElementById('pxMobOn'), list=document.getElementById('pxMobList');
  if(!st) return;
  var gen=window.__jfGen; if(force && st) st.textContent='ищу телефоны…';
  try{
    var j=await (await fetch('/proxies/mobile'+(force?'?force=1':''))).json();
    if(gen!==window.__jfGen) return;
    if(on) on.checked=!!j.enabled;
    var n=j.n_online||0, m=j.n_configured||0, tn=j.tailnet?(' · '+j.tailnet):'';
    st.textContent = !j.configured ? ('нет телефонов'+tn)
      : (!j.enabled ? (m+' настроено · выключено'+tn) : (n+' онлайн из '+m+tn));
    if(dot){ dot.className='px-dot'+(j.configured&&j.enabled ? (n>0?' px-ok':' px-bad') : ''); }
    if(list){
      var eps=j.endpoints||[];
      list.innerHTML = eps.length ? eps.map(function(e){
        var cls=e.alive?'px-ok':'px-bad', tag=e.source==='discovered'?'авто':'вручную';
        return '<div class="px-mob-ep"><span class="px-dot '+cls+'"></span>'
          +'<span class="px-mob-srv">'+(e.server||'').replace('socks5://','')+'</span>'
          +'<span class="px-mob-tag">'+tag+'</span>'
          +'<span class="px-mob-ip">'+(e.alive?('IP '+(e.egress||'?')):'офлайн')+'</span>'
          +(e.source==='manual'?'<button type="button" class="px-mob-x" data-rm="'+catEsc(e.server||'')+'" onclick="pxMobileRemove(this)" title="убрать">✕</button>':'')
          +'</div>';
      }).join('') : '<div class="cat-proxy-hint" style="margin:0">Телефоны не найдены. Подключи телефон к tailnet, запусти на нём SOCKS на порту 1080, затем «найти».</div>';
    }
  }catch(e){ st.textContent='ошибка'; }
};
window.pxMobileSave=async function(cb){
  var on=document.getElementById('pxMobOn'), st=document.getElementById('pxMobSt');
  var fd=new FormData(); fd.append('enabled', on&&on.checked?'1':'0');
  if(st) st.textContent='сохраняю…';
  try{ await fetch('/proxies/mobile',{method:'POST',body:fd}); }catch(e){}
  pxMobileRefresh(true);
};
window.pxMobileAdd=async function(){
  var srv=document.getElementById('pxMobSrv'), st=document.getElementById('pxMobSt');
  if(!srv || !srv.value.trim()) return;
  var fd=new FormData(); fd.append('add', srv.value.trim());
  if(st) st.textContent='добавляю…';
  try{ await fetch('/proxies/mobile',{method:'POST',body:fd}); srv.value=''; }catch(e){}
  pxMobileRefresh(true);
};
window.pxMobileRemove=async function(el){
  var server=(el&&el.dataset&&el.dataset.rm)||el; if(!server) return;
  var fd=new FormData(); fd.append('remove', server);
  try{ await fetch('/proxies/mobile',{method:'POST',body:fd}); }catch(e){}
  pxMobileRefresh(true);
};
window.pxToggleList=async function(){
  var list=document.getElementById('pxList'), tog=document.getElementById('pxToggle');
  if(!list) return;
  if(list.hidden){
    if(tog) tog.textContent='загрузка…';
    try{ var s=await (await fetch('/proxies')).json(); pxRenderList(s.ips); }catch(e){ pxRenderList([]); }
    list.hidden=false; if(tog) tog.textContent='скрыть';
  }else{
    list.hidden=true; if(tog) tog.textContent='показать список';
  }
};
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
    pxRefresh();
  }catch(e){ if(msg) msg.textContent='Ошибка запроса'; }
};
window.pxClear=async function(){
  if(!confirm('Очистить весь пул прокси?')) return;
  try{ await fetch('/proxies/clear',{method:'POST'}); }catch(e){}
  var msg=document.getElementById('pxMsg'); if(msg) msg.textContent='Пул очищен';
  var list=document.getElementById('pxList'); if(list){ list.innerHTML=''; list.hidden=true; }
  var tog=document.getElementById('pxToggle'); if(tog){ tog.hidden=true; tog.textContent='показать список'; }
  pxRefresh();
};
pxRefresh();   // show pool summary on load

(function(){
  var list=document.getElementById('catlist'), more=document.getElementById('catmore');
  if(!list) return;
  var qp=new URLSearchParams(location.search);
  var region=qp.get('region')||'', company=(qp.get('company')||'').trim(),
      curQ=(qp.get('q')||'').trim();
  // the live query, for «Все» select-all (reads the closure vars, so it follows the live search)
  window.catQuery=function(){ return {q:curQ, region:region, company:company}; };
  var loading=false, PAGE=30, seq=0, sig=catSig();
  // the shell's mobile top-pill funnel shows the active region filter as an "on" state
  var tune=document.querySelector('.gm-tune'); if(tune) tune.classList.toggle('on', !!region);
  function fragUrl(offset){
    var sp=new URLSearchParams();
    if(curQ) sp.set('q', curQ);
    if(region) sp.set('region', region);
    if(company) sp.set('company', company);
    sp.set('offset', offset);
    return '/catalog/more?'+sp.toString();
  }
  async function runSearch(){
    var mine=++seq, gen=window.__jfGen; loading=true;
    try{
      var r=await fetch(fragUrl(0)), txt=r.ok?await r.text():'';
      if(mine!==seq || gen!==window.__jfGen) return;   // a newer keystroke / another page
      list.innerHTML = txt.trim() || '<div class="empty">Вакансий не найдено</div>';
      syncPicks(list);
      var added=(txt.match(/class="cat-card"/g)||[]).length;
      if(more){ more.dataset.offset=String(added); more.dataset.more=(added>=PAGE)?'1':'0'; }
      window.scrollTo(0,0);
      // mirror the live query into the URL (replace, not push) so the shell's tab switch brings the
      // user back to this search and Back/reload restore it
      try{ var u=new URL(location.href); if(curQ) u.searchParams.set('q',curQ); else u.searchParams.delete('q');
        history.replaceState(history.state,'',u.pathname+u.search); }catch(e){}
    }catch(e){}finally{ loading=false; }
  }
  async function loadMore(){
    if(loading||!more||more.dataset.more!=='1')return;
    loading=true;
    var gen=window.__jfGen;
    try{
      var r=await fetch(fragUrl(more.dataset.offset));
      if(gen!==window.__jfGen) return;
      if(r.ok){
        var txt=await r.text();
        if(gen!==window.__jfGen) return;
        var added=(txt.match(/class="cat-card"/g)||[]).length;
        if(added){list.insertAdjacentHTML('beforeend',txt); syncPicks(list);
          more.dataset.offset=String((parseInt(more.dataset.offset,10)||0)+added);}
        if(added<PAGE)more.dataset.more='0';
      }
    }catch(e){}finally{loading=false;}
  }
  window.addEventListener('scroll',function(){
    if(window.innerHeight+window.scrollY>=document.documentElement.scrollHeight-500)loadMore();
  },{passive:true,signal:sig});
  // Live search: type in the desktop input OR the mobile top pill — debounced, and
  // Enter is intercepted so it filters in place instead of reloading.
  var deb;
  function onType(v){ curQ=(v||'').trim(); clearTimeout(deb); deb=setTimeout(runSearch,250); }
  [document.getElementById('catq'),
   document.querySelector('.gm-search input[type=search]')].forEach(function(inp){
    if(!inp) return;
    if(curQ) inp.value=curQ;
    if(inp.form) inp.form.addEventListener('submit',function(e){ e.preventDefault(); onType(inp.value); },{signal:sig});
    inp.addEventListener('input',function(){ onType(inp.value); },{signal:sig});
  });
  syncPicks(list);   // restore the persisted selection onto the freshly rendered cards
})();
// ---- recurring apply campaigns (a selection of jobs, daily, N/day, fresh résumé) ----
window.mkCampaign = async function(){
  var ids=Array.from(window.catSel.ids); if(!ids.length) return;
  var msg=document.getElementById('campMsg'), go=document.getElementById('campGo');
  if(go && go.disabled) return;
  var name=((document.getElementById('campName')||{}).value||'').trim();
  var sx=document.querySelector('#campSex .cat-sex-b.on'), gender=sx?(sx.dataset.gender||'male'):'male';
  var pn=document.getElementById('campPerN'), per=Math.max(1,Math.min(100,parseInt(pn&&pn.value,10)||2));
  var body='target_kind=jobs&job_ids='+encodeURIComponent(ids.join(','))
    +'&name='+encodeURIComponent(name)+'&gender='+encodeURIComponent(gender)
    +'&per_day='+encodeURIComponent(per);
  if(go) go.disabled=true; if(msg) msg.textContent='Создаю…';
  var gen=window.__jfGen;
  try{
    var r=await fetch('/catalog/campaigns',{method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body});
    var j={}; try{ j=await r.json(); }catch(e){ j={}; }
    if(gen!==window.__jfGen) return;
    if(!r.ok || j.error || !j.created){
      if(msg) msg.textContent=j.error||('Ошибка '+r.status);
      if(go) go.disabled=false; return;
    }
    closeCampSheet(); clearPicks();
    catToast('Кампания создана — первая подача по расписанию');
    loadCampaigns();
  }catch(e){
    if(gen!==window.__jfGen) return;
    if(msg) msg.textContent='Ошибка запроса'; if(go) go.disabled=false;
  }
};
window.loadCampaigns = async function(){
  var box=document.getElementById('campList'); if(!box) return;
  var gen=window.__jfGen;
  try{
    var j=await (await fetch('/catalog/campaigns')).json(), cs=j.campaigns||[];
    if(gen!==window.__jfGen) return;
    if(!cs.length){ box.textContent='Пока нет кампаний'; return; }
    box.innerHTML=cs.map(function(c){
      var per=(parseInt(c.per_day,10)||1)+'/день';
      var tgt=c.target_kind==='job'?('вакансия #'+(parseInt(c.job_id,10)||0))
            :c.target_kind==='jobs'?('вакансий: '+((c.job_ids||[]).length))
            :('поиск: '+catEsc((c.q||'').trim()||'все')+(c.region?(' · '+catEsc(c.region)):''));
      var id=parseInt(c.id,10)||0;
      var s=c.summary||{};
      var okN=s.confirmed||0, badN=(s.spam||0)+(s.needs_correction||0), deadN=s.dead||0;
      var tally='<span class="camp-tally">✅ '+okN+' · ⛔ '+badN+' · ☠ '+deadN+'</span>';
      return '<div class="cs-camp-row"><span class="cc-dot '+(c.active?'cc-on':'cc-off')+'">●</span> '
        +'<b>'+catEsc(c.name||'')+'</b> · '+tgt+' · '+per+' '+tally
        +' <button type="button" onclick="toggleCampLog('+id+')">журнал</button>'
        +' <button type="button" onclick="toggleCampaign('+id+','+(c.active?'0':'1')+')">'
        +(c.active?'пауза':'вкл')+'</button>'
        +' <button type="button" onclick="delCampaign('+id+')">удалить</button></div>'
        +'<div class="camp-jrnl" id="campjrnl-'+id+'" hidden></div>';
    }).join('');
  }catch(e){ if(gen===window.__jfGen) box.textContent='—'; }
};
window.toggleCampaign = async function(id,on){
  try{ await fetch('/catalog/campaigns/'+id+'/toggle',{method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'active='+on}); }catch(e){}
  loadCampaigns();
};
window.delCampaign = async function(id){
  if(!confirm('Удалить кампанию?')) return;
  try{ await fetch('/catalog/campaigns/'+id+'/delete',{method:'POST'}); }catch(e){}
  loadCampaigns();
};
function campBadge(o){
  var m={confirmed:['✅ Подтверждено','ok'],spam:['⛔ Спам','bad'],
    needs_correction:['⚠ Правка формы','warn'],dead:['☠ Вакансия снята','mute'],
    clicked:['◷ Нажато','mute'],error:['✕ Ошибка','bad'],skipped:['— Пропущено','mute']};
  var v=m[o]||['—','mute'];
  return '<span class="camp-badge camp-badge-'+v[1]+'">'+v[0]+'</span>';
}
window.toggleCampLog = async function(id){
  var box=document.getElementById('campjrnl-'+id); if(!box) return;
  if(!box.hasAttribute('hidden')){ box.setAttribute('hidden',''); return; }
  box.removeAttribute('hidden'); box.textContent='Загружаю…';
  var gen=window.__jfGen;
  try{
    var j=await (await fetch('/catalog/campaigns/'+id+'/events')).json();
    if(gen!==window.__jfGen) return;
    var evs=j.events||[];
    if(!evs.length){ box.textContent='Пока нет подач'; return; }
    box.innerHTML=evs.map(function(e){
      var co=catEsc(e.company||''), ti=catEsc(e.title||'');
      var what=(co?('<b>'+co+'</b>'):'')+(co&&ti?' · ':'')+ti;
      return '<div class="camp-jrnl-row">'
        +'<span class="camp-when">'+catEsc((e.ts||'').slice(0,16))+'</span>'
        +'<span class="camp-what">'+(what||('#'+ (parseInt(e.job_id,10)||0)))+'</span>'
        +campBadge(e.outcome)
        +(e.mailbox?('<span class="camp-who">'+catEsc(e.mailbox)+'</span>'):'')
        +'</div>';
    }).join('');
  }catch(e){ if(gen===window.__jfGen) box.textContent='—'; }
};
</script>"""
