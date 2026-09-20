"""Server-rendered «Сегодня» dashboard (`/stats/today`): everything the platform did TODAY.

Data from `today_dash.get_today()`. All charts are inline SVG/CSS — no external libraries,
no inline JS (so the page has nothing for `test_inline_js_syntax.py` to trip on). Reuses the
`mailcrm_ui._page` chrome. Neutral Russian labels; source/lane labels are employer company
names (business data), never internal tool / ATS / vendor / model stack names.
"""
from __future__ import annotations

from html import escape

from backend.tools import mailcrm_ui

_C = {
    "accent": "#0c47c2", "offer": "#188038", "interview": "#0c47c2",
    "assess": "#7b1fa2", "invite": "#b06000", "mute": "#5f6368", "sub": "#80868b",
}


def _fmt(n) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except Exception:
        return str(n)


def _kpi(label: str, value, sub: str = "", color: str = "") -> str:
    c = f"color:{color};" if color else ""
    subhtml = f'<div class="td-kpi-sub">{escape(sub)}</div>' if sub else ""
    return (f'<div class="td-kpi"><div class="td-kpi-v" style="{c}">{_fmt(value)}</div>'
            f'<div class="td-kpi-l">{escape(label)}</div>{subhtml}</div>')


def _hbars(rows: list[tuple[str, int, str]], color: str) -> str:
    """rows: [(label, value, secondary_text)]. Bars scaled to the max value."""
    if not rows:
        return '<div class="td-mute">нет данных</div>'
    mx = max((v for _l, v, _s in rows), default=0) or 1
    out = []
    for label, val, sec in rows:
        w = 100.0 * val / mx
        sechtml = f'<span class="td-mute"> · {escape(sec)}</span>' if sec else ""
        out.append(
            f'<div class="td-hb"><div class="td-hb-l">{escape(label)}</div>'
            f'<div class="td-hb-track"><div class="td-hb-bar" style="width:{w:.1f}%;'
            f'background:{color}"></div></div>'
            f'<div class="td-hb-v">{_fmt(val)}{sechtml}</div></div>')
    return f'<div class="td-hbars">{"".join(out)}</div>'


def _chips(rows: list[dict], key_label="label", key_val="n") -> str:
    if not rows:
        return '<div class="td-mute">—</div>'
    out = [f'<span class="td-chip">{escape(str(r[key_label]))} <b>{_fmt(r[key_val])}</b></span>'
           for r in rows]
    return f'<div class="td-chips">{"".join(out)}</div>'


def _sal(card: dict) -> str:
    lbl = card.get("salary_label") or ""
    if not lbl:
        return '<span class="td-mute">—</span>'
    cls = "td-sal td-sal-est" if card.get("salary_estimated") else "td-sal"
    return f'<span class="{cls}">{escape(lbl)}/год</span>'


def _cand_table(cards: list[dict], stage_col: bool = False) -> str:
    if not cards:
        return '<div class="td-empty">сегодня пусто</div>'
    head = ('<tr><th class="td-l">Кандидат</th><th class="td-l">Компания</th>'
            '<th class="td-l">Роль</th>'
            + ('<th class="td-l">Этап</th>' if stage_col else '')
            + '<th class="td-num">Зарплата (прибл.)</th></tr>')
    rows = []
    _STAGE = {"offer": "🎉 оффер", "interview": "📅 собес"}
    for c in cards:
        stage = (f'<td>{_STAGE.get(c.get("stage"), "")}</td>' if stage_col else '')
        who = escape((c.get("email") or "").split("@")[0])
        rows.append(
            f'<tr><td class="td-l td-mono">{who}</td>'
            f'<td class="td-l"><b>{escape(c.get("company") or c.get("source") or "—")}</b></td>'
            f'<td class="td-l">{escape(c.get("role") or "—")}</td>'
            f'{stage}'
            f'<td class="td-num">{_sal(c)}</td></tr>')
    return (f'<div class="td-tbl-wrap"><table class="td-tbl"><thead>{head}</thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def render_page(force: bool = False) -> str:
    from backend.tools import today_dash
    b = today_dash.get_today(force=force)
    from datetime import datetime, timezone
    gen = datetime.fromtimestamp(b["generated_at"], tz=timezone.utc).astimezone().strftime("%H:%M")

    sub = b["submissions"]
    inv = b["assessments"]["invites"]
    solv = b["assessments"]["solved"]
    iv = b["interviews"]
    off = b["offers"]

    kpis = "".join([
        _kpi("Подано сегодня", sub["total_confirmed"],
             sub=f'{_fmt(sub["total_attempts"])} попыток', color=_C["accent"]),
        _kpi("Инвайты на тесты", inv["total_personas"],
             sub=f'{_fmt(inv["total_msgs"])} писем', color=_C["invite"]),
        _kpi("Тесты решено", solv["total"], sub="за сегодня", color=_C["assess"]),
        _kpi("Собеседования", iv["total"], sub="пришли сегодня", color=_C["interview"]),
        _kpi("Офферы пришли", off["total"], sub="за сегодня", color=_C["offer"]),
    ])

    # Подачи по каналам
    sub_bars = _hbars(
        [(l["label"], l["confirmed"], f'{_fmt(l["attempts"])} попыток') for l in sub["lanes"]],
        _C["accent"])

    # Ассессменты: приглашения
    inv_src = _hbars([(r["label"], r["personas"], f'{_fmt(r["msgs"])} писем')
                      for r in inv["by_source"]], _C["invite"])
    inv_role = _chips(inv["by_role"], key_val="personas")

    # Ассессменты: пройдено
    solv_src = _hbars([(r["label"], r["n"], "") for r in solv["by_source"]], _C["assess"])
    solv_role = _chips(solv["by_role"])
    solv_tbl = _cand_table(solv["cards"])

    # Собеседования
    iv_tbl = _cand_table(iv["cards"])

    # Офферы (только реальные — пришедшие сегодня)
    off_tbl = _cand_table(off["cards"])

    # Кандидаты с оффером/собеседованием
    cand_tbl = _cand_table(b["candidates"], stage_col=True)

    head = (
        '<div class="td-head"><div class="td-head-l">'
        f'<h1 class="td-title">Сегодня</h1>'
        f'<span class="td-mute td-gen">{escape(b["day_label"])} · обновлено {gen}</span></div>'
        '<div class="td-head-r">'
        '<a href="/stats" class="td-btn">← Статистика</a>'
        '<a href="/stats/today?refresh=1" class="td-btn td-btn-primary">Обновить</a>'
        '</div></div>')

    body = f"""
<style>{_CSS}</style>
{head}
<div class="td-kpis">{kpis}</div>

<section class="td-card"><h2 class="td-h">Подачи по каналам</h2>{sub_bars}
<p class="td-note">Столбец — подтверждённые подачи; справа — сколько попыток всего.
Всего подтверждено сегодня: <b>{_fmt(sub['total_confirmed'])}</b>.</p></section>

<div class="td-grid2">
  <section class="td-card"><h2 class="td-h">Тесты: приглашения сегодня</h2>
    <div class="td-sub">По каналам (кандидатов · писем)</div>{inv_src}
    <div class="td-sub" style="margin-top:12px">По направлениям</div>{inv_role}</section>
  <section class="td-card"><h2 class="td-h">Тесты: пройдено сегодня</h2>
    <div class="td-sub">По каналам</div>{solv_src}
    <div class="td-sub" style="margin-top:12px">По направлениям</div>{solv_role}</section>
</div>

<section class="td-card"><h2 class="td-h">Кто решил тест сегодня</h2>
<p class="td-note">Кандидаты, реально сдавшие ассессмент сегодня. Зарплата приблизительная:
точная вилка по вакансии, иначе медиана по направлению.</p>{solv_tbl}</section>

<section class="td-card"><h2 class="td-h">Собеседования сегодня</h2>{iv_tbl}</section>

<section class="td-card"><h2 class="td-h">Офферы пришли сегодня</h2>
<p class="td-note">Реальные офферы, полученные сегодня (с 00:00). Всего:
<b>{_fmt(off['total'])}</b>.</p>{off_tbl}</section>

<section class="td-card"><h2 class="td-h">Кандидаты с оффером / собеседованием</h2>
<p class="td-note">Компания и приблизительная зарплата по каждому. Собеседования и офферы,
пришедшие сегодня.</p>{cand_tbl}</section>
"""
    return mailcrm_ui._page("stats", body)


_CSS = """
.td-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.td-title{font-size:22px;font-weight:800;margin:0;letter-spacing:-.02em}
.td-gen{font-size:12px;display:block;margin-top:2px}
.td-head-r{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.td-btn{display:inline-flex;align-items:center;height:36px;padding:0 14px;border-radius:9px;
  border:1px solid #d7dbe0;background:#fff;color:#3c4043;font-size:13px;font-weight:600;text-decoration:none}
.td-btn:hover{background:#f6f8fc;text-decoration:none}
.td-btn-primary{background:#0c47c2;border-color:#0c47c2;color:#fff}
.td-btn-primary:hover{background:#0a3aa0}
.td-mute{color:#80868b;font-weight:500}
.td-sub{font-size:12px;color:#5f6368;font-weight:600;margin-bottom:7px}
.td-card{background:#fff;border:1px solid #e8eaed;border-radius:12px;padding:16px 18px;margin-bottom:16px}
.td-h{font-size:14px;font-weight:700;margin:0 0 12px}
.td-note{font-size:12px;color:#80868b;margin:12px 0 0;line-height:1.5}
.td-grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:760px){.td-grid2{grid-template-columns:1fr}}
/* KPI */
.td-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
@media(max-width:460px){.td-kpis{grid-template-columns:repeat(2,1fr)}}
.td-kpi{background:#fff;border:1px solid #e8eaed;border-radius:12px;padding:14px 14px 12px}
.td-kpi-v{font-size:26px;font-weight:800;line-height:1.1;font-family:var(--ff-mono,monospace)}
.td-kpi-l{font-size:12px;color:#5f6368;margin-top:3px;font-weight:600}
.td-kpi-sub{font-size:11px;color:#80868b;margin-top:2px}
/* hbars */
.td-hb{display:grid;grid-template-columns:minmax(96px,150px) 1fr auto;align-items:center;gap:10px;margin-bottom:8px;font-size:12.5px}
.td-hb-l{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.td-hb-track{height:12px;background:#f1f3f4;border-radius:5px;overflow:hidden}
.td-hb-bar{height:100%;border-radius:5px;min-width:2px}
.td-hb-v{font-variant-numeric:tabular-nums;white-space:nowrap}
/* chips */
.td-chips{display:flex;flex-wrap:wrap;gap:7px}
.td-chip{display:inline-flex;align-items:center;gap:5px;background:#f1f3f4;border-radius:8px;
  padding:4px 10px;font-size:12.5px;color:#3c4043}
.td-chip b{font-variant-numeric:tabular-nums;color:#202124}
/* table */
.td-tbl-wrap{max-height:460px;overflow:auto;border:1px solid #eef0f2;border-radius:8px}
.td-tbl{width:100%;border-collapse:collapse;font-size:12.5px}
.td-tbl thead th{position:sticky;top:0;background:#f8f9fa;z-index:1;padding:8px 10px;
  font-weight:700;color:#5f6368;white-space:nowrap;border-bottom:1px solid #e8eaed;text-align:right}
.td-tbl th.td-l{text-align:left}
.td-tbl td{padding:7px 10px;border-bottom:1px solid #f1f3f4}
.td-tbl td.td-l{text-align:left}
.td-tbl td.td-num{text-align:right;font-variant-numeric:tabular-nums}
.td-tbl tbody tr:hover{background:#f8f9fa}
.td-mono{font-family:var(--ff-mono,monospace);color:#5f6368}
.td-sal{font-weight:700;color:#188038}
.td-sal-est{color:#5f6368;font-weight:600}
.td-empty{color:#80868b;font-size:13px;padding:10px 2px}
"""
