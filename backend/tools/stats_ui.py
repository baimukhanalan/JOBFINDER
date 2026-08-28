"""Server-rendered `/stats` page: where we applied, who replies, who interviews,
who rejects — organized BY COMPANY so effort can be steered to what pays off.

All charts are inline SVG/CSS (no external libraries) to stay self-contained.
Data comes from `stats.get_stats()`. No stack/brand strings appear here.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from html import escape

from backend.tools import mailcrm_ui

# outcome colors, aligned with the inbox _KIND palette
_C = {
    "interview": "#1a73e8", "offer": "#188038", "rejection": "#d93025",
    "action_needed": "#b06000", "ack": "#8a9099", "other": "#c3c8ce",
    "accent": "#1a73e8", "ink": "#202124", "mute": "#5f6368",
}
_OUTCOME_LABELS = {
    "offer": "Офферы", "interview": "Собеседования", "action_needed": "Требует действия",
    "rejection": "Отказы", "ack": "Заявка принята", "other": "Прочее",
}


def _fmt(n) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except Exception:
        return str(n)


def _kpi(label: str, value, sub: str = "", color: str = "") -> str:
    c = f"color:{color};" if color else ""
    subhtml = f'<div class="st-kpi-sub">{escape(sub)}</div>' if sub else ""
    return (f'<div class="st-kpi"><div class="st-kpi-v" style="{c}">{_fmt(value)}</div>'
            f'<div class="st-kpi-l">{escape(label)}</div>{subhtml}</div>')


def _funnel(stages: list[tuple[str, int, str]], base: int) -> str:
    """stages: [(label, value, color)]. Bar width ∝ value/base."""
    rows = []
    prev = None
    for label, val, color in stages:
        w = max(2.0, 100.0 * val / base) if base else 0.0
        pct = f"{100.0*val/base:.1f}%" if base else "—"
        step = ""
        if prev is not None and prev:
            step = f'<span class="st-fn-step">↓ {100.0*val/prev:.1f}% от пред.</span>'
        rows.append(
            f'<div class="st-fn-row"><div class="st-fn-head"><b>{escape(label)}</b>'
            f'<span>{_fmt(val)} · {pct}{step}</span></div>'
            f'<div class="st-fn-track"><div class="st-fn-bar" style="width:{w:.2f}%;'
            f'background:{color}"></div></div></div>')
        prev = val
    return f'<div class="st-fn">{"".join(rows)}</div>'


def _donut(segments: list[tuple[str, int, str]], total: int) -> str:
    """SVG donut of outcome composition (few, meaningful slices — the one place
    a pie/donut is the right tool)."""
    r, cx, cy = 62, 80, 80
    circ = 2 * math.pi * r
    arcs = []
    off = 0.0
    for _label, val, color in segments:
        if val <= 0 or total <= 0:
            continue
        frac = val / total
        dash = frac * circ
        arcs.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
            f'stroke-width="26" stroke-dasharray="{dash:.2f} {circ-dash:.2f}" '
            f'stroke-dashoffset="{-off:.2f}" transform="rotate(-90 {cx} {cy})"></circle>')
        off += dash
    legend = []
    for label, val, color in segments:
        if val <= 0:
            continue
        pct = f"{100.0*val/total:.0f}%" if total else "—"
        legend.append(
            f'<div class="st-lg"><span class="st-dot" style="background:{color}"></span>'
            f'{escape(label)} <b>{_fmt(val)}</b> <span class="st-mute">{pct}</span></div>')
    return (f'<div class="st-donut-wrap"><svg viewBox="0 0 160 160" class="st-donut" '
            f'role="img" aria-label="Состав ответов">{"".join(arcs)}'
            f'<text x="80" y="76" text-anchor="middle" class="st-donut-n">{_fmt(total)}</text>'
            f'<text x="80" y="94" text-anchor="middle" class="st-donut-t">ответов</text></svg>'
            f'<div class="st-legend">{"".join(legend)}</div></div>')


def _hbars(rows: list[tuple[str, int, int]], color: str, unit: str = "") -> str:
    """rows: [(label, value, secondary)]. Bars scaled to the max value."""
    mx = max((v for _l, v, _s in rows), default=0) or 1
    out = []
    for label, val, sec in rows:
        w = 100.0 * val / mx
        sechtml = f'<span class="st-mute"> · {_fmt(sec)}{unit}</span>' if sec else ""
        out.append(
            f'<div class="st-hb"><div class="st-hb-l">{escape(label)}</div>'
            f'<div class="st-hb-track"><div class="st-hb-bar" style="width:{w:.1f}%;'
            f'background:{color}"></div></div>'
            f'<div class="st-hb-v">{_fmt(val)}{sechtml}</div></div>')
    return f'<div class="st-hbars">{"".join(out)}</div>'


def _sparkbars(vals: list[int], color: str, labels: list[str]) -> str:
    mx = max(vals, default=0) or 1
    bars = []
    for v, lab in zip(vals, labels):
        h = max(2.0, 100.0 * v / mx)
        bars.append(f'<span class="st-sb" style="height:{h:.0f}%;background:{color}" '
                    f'title="{escape(lab)}: {v}"></span>')
    return f'<div class="st-spark">{"".join(bars)}</div>'


def _company_table(companies: list[dict]) -> str:
    head = (
        '<tr>'
        '<th data-k="name" class="st-th st-l">Компания</th>'
        '<th data-k="applied" class="st-th st-num">Подано</th>'
        '<th data-k="submitted" class="st-th st-num">Сабмит</th>'
        '<th data-k="replied" class="st-th st-num">Ответы</th>'
        '<th data-k="reply_rate" class="st-th st-num">% отв.</th>'
        '<th data-k="interview" class="st-th st-num st-sorted">Собес.</th>'
        '<th data-k="rejection" class="st-th st-num">Отказы</th>'
        '<th data-k="offer" class="st-th st-num">Офф.</th>'
        '<th data-k="interview_rate" class="st-th st-num">% собес.</th>'
        '</tr>')
    rows = []
    for r in companies:
        ir = r["interview_rate"]
        irbar = min(100.0, ir * 3)  # visual amplifier (rates are small)
        rows.append(
            f'<tr data-name="{escape(r["name"].lower())}" data-applied="{r["applied"]}" '
            f'data-submitted="{r["submitted"]}" data-replied="{r["replied"]}" '
            f'data-reply_rate="{r["reply_rate"]}" data-interview="{r["interview"]}" '
            f'data-rejection="{r["rejection"]}" data-offer="{r["offer"]}" '
            f'data-interview_rate="{ir}">'
            f'<td class="st-l"><b>{escape(r["name"])}</b></td>'
            f'<td class="st-num">{_fmt(r["applied"])}</td>'
            f'<td class="st-num st-mute">{_fmt(r["submitted"])}</td>'
            f'<td class="st-num">{_fmt(r["replied"])}</td>'
            f'<td class="st-num st-mute">{r["reply_rate"]:.0f}%</td>'
            f'<td class="st-num"><b style="color:{_C["interview"]}">{r["interview"]}</b></td>'
            f'<td class="st-num" style="color:{_C["rejection"]}">{r["rejection"] or ""}</td>'
            f'<td class="st-num">{("🎉" + str(r["offer"])) if r["offer"] else ""}</td>'
            f'<td class="st-num"><span class="st-irbar" style="width:{irbar:.0f}%"></span>'
            f'<span class="st-irtxt">{ir:.1f}%</span></td>'
            f'</tr>')
    return (f'<table class="st-tbl" id="stTbl"><thead>{head}</thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _focus_lists(companies: list[dict]) -> str:
    invest = sorted(
        [c for c in companies if c["applied"] >= 20 and c["interview"] > 0],
        key=lambda c: c["interview_rate"], reverse=True)[:6]
    waste = sorted(
        [c for c in companies if c["applied"] >= 50 and c["reply_rate"] < 3.0],
        key=lambda c: c["applied"], reverse=True)[:6]

    def li(c, right):
        return (f'<div class="st-focus-row"><span class="st-focus-co">{escape(c["name"])}</span>'
                f'<span class="st-mute">{_fmt(c["applied"])} подано</span>'
                f'<span class="st-focus-r">{right}</span></div>')
    inv = "".join(li(c, f'<b style="color:{_C["interview"]}">{c["interview"]} собес · {c["interview_rate"]:.0f}%</b>') for c in invest)
    wst = "".join(li(c, f'<b style="color:{_C["rejection"]}">{c["reply_rate"]:.0f}% ответов</b>') for c in waste)
    return (
        '<div class="st-focus">'
        f'<div class="st-focus-card st-focus-good"><div class="st-focus-h">✅ Куда вкладываться '
        '<span class="st-mute">(лучшая конверсия в собеседование)</span></div>'
        f'{inv or "<div class=st-mute>нет данных</div>"}</div>'
        f'<div class="st-focus-card st-focus-bad"><div class="st-focus-h">🚫 Куда льём объём впустую '
        '<span class="st-mute">(много подано, почти нет ответов)</span></div>'
        f'{wst or "<div class=st-mute>нет данных</div>"}</div>'
        '</div>')


def _trend(trend: list[dict]) -> str:
    if not trend:
        return '<div class="st-mute">нет данных о динамике</div>'
    labs = [datetime.fromtimestamp(t["day"], tz=timezone.utc).strftime("%d.%m") for t in trend]
    totals = [t["total"] for t in trend]
    inters = [t["interview"] for t in trend]
    rejs = [t["rejection"] for t in trend]
    return (
        '<div class="st-trend">'
        f'<div class="st-trend-row"><span class="st-trend-l">Все ответы</span>'
        f'{_sparkbars(totals, _C["ack"], labs)}<span class="st-trend-mx">макс {max(totals)}</span></div>'
        f'<div class="st-trend-row"><span class="st-trend-l">Собеседования</span>'
        f'{_sparkbars(inters, _C["interview"], labs)}<span class="st-trend-mx">макс {max(inters)}</span></div>'
        f'<div class="st-trend-row"><span class="st-trend-l">Отказы</span>'
        f'{_sparkbars(rejs, _C["rejection"], labs)}<span class="st-trend-mx">макс {max(rejs)}</span></div>'
        f'<div class="st-trend-days">{"".join(f"<span>{l}</span>" for l in labs)}</div>'
        '</div>')


def render_page(force: bool = False) -> str:
    from backend.tools import stats
    b = stats.get_stats(force=force)
    t = b["totals"]
    gen = datetime.fromtimestamp(b["generated_at"], tz=timezone.utc).astimezone().strftime("%H:%M")

    kpis = "".join([
        _kpi("Подано (вакансий)", t["applied"], sub=f'{_fmt(t["attempts"])} попыток с повторами'),
        _kpi("Подтверждено сабмитов", t["submitted"], color=_C["mute"]),
        _kpi("Ответили", t["replied"], sub=f'{t["reply_rate"]:.0f}% от поданных', color=_C["accent"]),
        _kpi("Собеседования", t["interview"], sub=f'{t["interview_rate"]:.1f}% от поданных', color=_C["interview"]),
        _kpi("Отказы", t["rejection"], color=_C["rejection"]),
        _kpi("Офферы", t["offer"], color=_C["offer"]),
    ])

    funnel = _funnel([
        ("Подано (вакансий)", t["applied"], _C["accent"]),
        ("Ответили", t["replied"], "#5b9bf0"),
        ("Собеседования", t["interview"], _C["interview"]),
        ("Офферы", t["offer"], _C["offer"]),
    ], base=t["applied"])

    donut = _donut(
        [(_OUTCOME_LABELS[k], b["outcome_totals"].get(k, 0), _C[k])
         for k in ["ack", "other", "rejection", "interview", "action_needed", "offer"]],
        total=sum(b["outcome_totals"].values()))

    ats = _hbars([(r["ats"], r["applied"], r["interview"]) for r in b["ats"]],
                 _C["accent"], unit=" собес.")
    regions = _hbars([(r["region"], r["applied"], 0) for r in b["regions"]], "#8a9099")

    body = f"""
<style>{_CSS}</style>
<div class="page-head"><h1 class="st-title">Статистика подач</h1>
<span class="st-mute st-gen">обновлено {gen} · {_fmt(t['companies'])} компаний · <a href="/stats?refresh=1">обновить данные</a></span></div>

<div class="st-kpis">{kpis}</div>

<div class="st-grid2">
  <section class="st-card"><h2 class="st-h">Воронка</h2>{funnel}
    <p class="st-note">Подано → получили любой ответ → позвали на собеседование → оффер.
    «Сабмит» ({_fmt(t['submitted'])}) — заявки, чья отправка подтверждена на стороне работодателя.</p>
  </section>
  <section class="st-card"><h2 class="st-h">Состав ответов</h2>{donut}
    <p class="st-note">Здесь круговая диаграмма уместна — это доли одного целого (все ответы),
    и их немного. Для сравнения компаний ниже используются столбцы, а не круг.</p>
  </section>
</div>

<section class="st-card">{_focus_lists(b['companies'])}</section>

<section class="st-card"><div class="st-h-row"><h2 class="st-h">По компаниям</h2>
<span class="st-mute">клик по заголовку — сортировка · сейчас по числу собеседований (кликните «% собес.» для конверсии)</span></div>
<div class="st-tbl-wrap">{_company_table(b['companies'])}</div></section>

<div class="st-grid2">
  <section class="st-card"><h2 class="st-h">По системе подачи</h2>{ats}
    <p class="st-note">Столбец — сколько подано; справа — сколько собеседований. Видно, где отдача выше.</p></section>
  <section class="st-card"><h2 class="st-h">По регионам</h2>{regions}</section>
</div>

<section class="st-card"><h2 class="st-h">Динамика по дням</h2>{_trend(b['trend'])}</section>
{_JS}
"""
    return mailcrm_ui._page("stats", body)


_CSS = """
.st-title{font-size:20px;font-weight:800;margin:0}
.st-gen{font-size:12px}
.st-mute{color:#80868b;font-weight:500}
.st-card{background:#fff;border:1px solid #e8eaed;border-radius:12px;padding:16px 18px;margin-bottom:16px}
.st-h{font-size:14px;font-weight:700;margin:0 0 12px}
.st-h-row{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.st-h-row .st-h{margin:0}
.st-note{font-size:12px;color:#80868b;margin:12px 0 0;line-height:1.5}
.st-grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:760px){.st-grid2{grid-template-columns:1fr}}
/* KPI */
.st-kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:16px}
@media(max-width:900px){.st-kpis{grid-template-columns:repeat(3,1fr)}}
@media(max-width:460px){.st-kpis{grid-template-columns:repeat(2,1fr)}}
.st-kpi{background:#fff;border:1px solid #e8eaed;border-radius:12px;padding:14px 14px 12px}
.st-kpi-v{font-size:26px;font-weight:800;line-height:1.1;font-family:var(--ff-mono,monospace)}
.st-kpi-l{font-size:12px;color:#5f6368;margin-top:3px;font-weight:600}
.st-kpi-sub{font-size:11px;color:#80868b;margin-top:2px}
/* funnel */
.st-fn-row{margin-bottom:12px}
.st-fn-head{display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:4px}
.st-fn-head span{color:#5f6368}
.st-fn-step{color:#aaa;margin-left:8px;font-size:11px}
.st-fn-track{height:16px;background:#f1f3f4;border-radius:6px;overflow:hidden}
.st-fn-bar{height:100%;border-radius:6px;min-width:2px;transition:width .3s}
/* donut */
.st-donut-wrap{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.st-donut{width:150px;height:150px;flex:0 0 auto}
.st-donut-n{font-size:24px;font-weight:800;fill:#202124;font-family:var(--ff-mono,monospace)}
.st-donut-t{font-size:11px;fill:#80868b}
.st-legend{display:flex;flex-direction:column;gap:5px;font-size:12.5px}
.st-lg{white-space:nowrap}
.st-dot{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
/* hbars */
.st-hb{display:grid;grid-template-columns:90px 1fr auto;align-items:center;gap:10px;margin-bottom:8px;font-size:12.5px}
.st-hb-l{font-weight:600;text-transform:capitalize;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.st-hb-track{height:12px;background:#f1f3f4;border-radius:5px;overflow:hidden}
.st-hb-bar{height:100%;border-radius:5px}
.st-hb-v{font-variant-numeric:tabular-nums;white-space:nowrap}
/* focus */
.st-focus{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:760px){.st-focus{grid-template-columns:1fr}}
.st-focus-card{border-radius:10px;padding:12px 14px}
.st-focus-good{background:#e6f4ea;border:1px solid #ceead6}
.st-focus-bad{background:#fce8e6;border:1px solid #f7ccc8}
.st-focus-h{font-size:13px;font-weight:700;margin-bottom:8px}
.st-focus-row{display:flex;align-items:center;gap:10px;font-size:12.5px;padding:4px 0;border-top:1px solid rgba(0,0,0,.05)}
.st-focus-row:first-of-type{border-top:0}
.st-focus-co{font-weight:700;min-width:96px}
.st-focus-r{margin-left:auto;white-space:nowrap}
/* table */
.st-tbl-wrap{max-height:560px;overflow:auto;border:1px solid #eef0f2;border-radius:8px}
.st-tbl{width:100%;border-collapse:collapse;font-size:12.5px}
.st-tbl thead th{position:sticky;top:0;background:#f8f9fa;z-index:1}
.st-th{padding:9px 10px;text-align:right;font-weight:700;color:#5f6368;cursor:pointer;user-select:none;white-space:nowrap;border-bottom:1px solid #e8eaed}
.st-th.st-l{text-align:left}
.st-th:hover{color:#1a73e8}
.st-th.st-sorted{color:#1a73e8}
.st-tbl td{padding:7px 10px;border-bottom:1px solid #f1f3f4}
.st-tbl td.st-num{text-align:right;font-variant-numeric:tabular-nums}
.st-tbl td.st-l{text-align:left}
.st-tbl tbody tr:hover{background:#f8f9fa}
.st-irbar{display:inline-block;height:7px;background:#1a73e8;border-radius:4px;vertical-align:1px;margin-right:6px;max-width:70px}
.st-irtxt{font-variant-numeric:tabular-nums}
/* trend */
.st-trend-row{display:grid;grid-template-columns:100px 1fr 70px;align-items:end;gap:10px;margin-bottom:10px}
.st-trend-l{font-size:12px;font-weight:600;color:#5f6368}
.st-trend-mx{font-size:11px;color:#aaa;text-align:right}
.st-spark{display:flex;align-items:flex-end;gap:2px;height:44px}
.st-sb{flex:1;border-radius:2px 2px 0 0;min-height:2px}
.st-trend-days{display:flex;gap:2px;margin-left:110px;font-size:9px;color:#aaa}
.st-trend-days span{flex:1;text-align:center;overflow:hidden}
@media(max-width:760px){.st-trend-days{display:none}.st-trend-row{grid-template-columns:80px 1fr 46px}}
"""

_JS = """<script>
(function(){
  var tbl=document.getElementById('stTbl'); if(!tbl) return;
  var dir={};
  tbl.querySelectorAll('th[data-k]').forEach(function(th){
    th.addEventListener('click', function(){
      var k=th.dataset.k, num=k!=='name';
      dir[k]=!dir[k]; var asc=dir[k];
      var tb=tbl.tBodies[0];
      var rows=[].slice.call(tb.rows);
      rows.sort(function(a,b){
        var x=a.dataset[k], y=b.dataset[k];
        if(num){x=parseFloat(x)||0;y=parseFloat(y)||0;return asc?x-y:y-x;}
        return asc?(''+x).localeCompare(y):(''+y).localeCompare(x);
      });
      rows.forEach(function(r){tb.appendChild(r);});
      tbl.querySelectorAll('.st-th').forEach(function(h){h.classList.remove('st-sorted');});
      th.classList.add('st-sorted');
    });
  });
})();
</script>"""
