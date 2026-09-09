"""Render the /health admin page — the operator's map of the whole deployment: one card per group
(services / crons / data / external deps / system / incidents), each row a status dot + name + live
detail, tap to expand its «почему хрупко / что делать» hint. Uses the shared shell (`mailcrm_ui._page`
+ the canonical `_page_head`) and theme tokens; phone-first (390px). Read-only view over
`health.gather()`; auto-refreshes every 60 s unless a hint is open."""
from __future__ import annotations

import html

from backend.tools import health, mailcrm_ui

_STATUS_WORD = {"ok": "OK", "warn": "Внимание", "down": "Сбой", "info": "Справка"}
_OVERALL_WORD = {"ok": "Всё исправно", "warn": "Есть предупреждения", "down": "Есть сбои"}
_GROUP_SVG = {
    "pm2": "M4 6h16M4 12h16M4 18h16",
    "crons": "M12 8v4l3 3M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z",
    "data": "M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
    "deps": "M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7",
    "system": "M4 4h16v12H4zM8 20h8M12 16v4",
    "incidents": "M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
}
_IC_REFRESH = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
               'stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.4"/>'
               '<polyline points="21 3 21 9 15 9"/></svg>')
_IC_CHEV = ('<svg class="hz-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>')

_CSS = """
<style>
.hz-wrap{max-width:1040px;margin:0 auto;padding:0 0 64px}
.hz-top{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 14px}
.hz-badge{display:inline-flex;align-items:center;gap:8px;height:var(--ctl-h);padding:0 16px;border-radius:var(--r-full);
  font-weight:700;font-size:var(--ctl-fs);color:#fff;background:var(--ink-mute);white-space:nowrap}
.hz-badge.ok{background:var(--ok)}.hz-badge.warn{background:var(--warn)}.hz-badge.down{background:var(--danger)}
.hz-badge i{width:8px;height:8px;border-radius:50%;background:#fff;opacity:.9}
.hz-nav{display:flex;gap:6px;flex-wrap:wrap;flex:1;min-width:0}
.hz-nav a{display:inline-flex;align-items:center;gap:6px;height:var(--chip-h);padding:0 var(--chip-px);border-radius:var(--r-full);
  border:1px solid var(--line-strong);background:var(--panel);color:var(--ink-soft);font-size:var(--chip-fs);font-weight:600;
  text-decoration:none;white-space:nowrap}
.hz-nav a:hover{background:var(--panel-2);color:var(--ink);text-decoration:none}
.hz-nav a b{font-family:var(--ff-mono);font-weight:500;font-size:11px;color:var(--ink-mute)}
.hz-dot{flex:0 0 auto;width:10px;height:10px;border-radius:50%;background:var(--ink-mute);box-shadow:0 0 0 2px var(--panel)}
.hz-dot.ok{background:var(--ok)}.hz-dot.warn{background:var(--warn)}.hz-dot.down{background:var(--danger)}.hz-dot.info{background:var(--accent)}
.hz-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);margin:0 0 14px;overflow:hidden;scroll-margin-top:72px}
.hz-card>header{display:flex;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--line);font-weight:700;font-size:14.5px;color:var(--ink)}
.hz-card>header svg{width:18px;height:18px;flex:0 0 auto;color:var(--ink-soft)}
.hz-card>header .hz-cnts{margin-left:auto;display:flex;gap:5px;flex:0 0 auto}
.hz-cnt{display:inline-flex;align-items:center;gap:4px;height:var(--chip-sm-h);padding:0 8px;border-radius:var(--r-full);
  font-family:var(--ff-mono);font-size:var(--chip-sm-fs);font-weight:600;color:#fff;background:var(--ink-mute)}
.hz-cnt.ok{background:var(--ok)}.hz-cnt.warn{background:var(--warn)}.hz-cnt.down{background:var(--danger)}
.hz-row{border-top:1px solid var(--line)}
.hz-row:first-of-type{border-top:0}
.hz-row>summary,.hz-row>.hz-line{list-style:none;display:flex;align-items:flex-start;gap:10px;padding:10px 14px;min-height:44px;cursor:pointer}
.hz-row>.hz-line{cursor:default}
.hz-row>summary::-webkit-details-marker{display:none}
.hz-row>summary:hover{background:var(--panel-2)}
.hz-row.down>summary{background:rgba(217,48,37,.06)}.hz-row.down>summary:hover{background:rgba(217,48,37,.1)}
.hz-row.warn>summary{background:rgba(176,96,0,.06)}.hz-row.warn>summary:hover{background:rgba(176,96,0,.1)}
.hz-row .hz-dot{margin-top:5px}
.hz-main{flex:1;min-width:0}
.hz-name{font-weight:600;font-size:13.5px;color:var(--ink);display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;line-height:1.35}
.hz-sched{font-family:var(--ff-mono);font-size:11px;font-weight:400;color:var(--ink-mute);white-space:nowrap}
.hz-detail{color:var(--ink-soft);font-size:12.5px;word-break:break-word;font-variant-numeric:tabular-nums;margin-top:2px;line-height:1.4}
.hz-row.down .hz-detail{color:var(--danger)}
.hz-row.warn .hz-detail{color:var(--warn)}
.hz-chev{flex:0 0 auto;width:16px;height:16px;color:var(--ink-mute);margin-top:4px;transition:transform .15s}
.hz-row[open]>summary .hz-chev{transform:rotate(180deg)}
.hz-hint{padding:0 14px 12px 34px;font-size:12.5px;color:var(--ink-soft);line-height:1.55}
.hz-hint b{color:var(--ink)}
.hz-hint code{font-family:var(--ff-mono);font-size:11.5px;background:var(--panel-2);padding:1px 4px;border-radius:4px}
.hz-empty{padding:14px;color:var(--ink-mute);font-size:13px}
.hz-foot{color:var(--ink-mute);font-size:12px;text-align:center;padding:8px 0 0}
@media(max-width:760px){
  .hz-wrap{padding-bottom:40px}
  .hz-top{gap:8px;margin-bottom:12px;flex-direction:column;align-items:flex-start}
  .hz-nav{width:100%}
  .hz-badge{height:var(--chip-h);font-size:var(--chip-fs);padding:0 12px}
  .hz-nav{gap:5px}
  .hz-nav a{height:var(--chip-sm-h);padding:0 8px;font-size:var(--chip-sm-fs)}
  .hz-card{margin-bottom:12px;border-radius:var(--r-sm)}
  .hz-card>header{padding:10px 12px;font-size:14px}
  .hz-row>summary,.hz-row>.hz-line{padding:10px 12px;gap:9px}
  .hz-hint{padding:0 12px 12px 12px}
  .hz-name{font-size:13.5px}
}
@media(prefers-reduced-motion:reduce){.hz-chev{transition:none}}
</style>
"""


def _dot(st: str) -> str:
    return f'<span class="hz-dot {html.escape(st)}"></span>'


def _row(r: dict) -> str:
    st = r.get("status", "warn")
    if st not in _STATUS_WORD:
        st = "warn"
    name = html.escape(str(r.get("name", "?")))
    sched = r.get("sched") or ""
    sched_html = f'<span class="hz-sched">{html.escape(sched)}</span>' if sched else ""
    detail = html.escape(str(r.get("detail", "")))
    hint = r.get("hint") or ""       # trusted, code-defined HTML (bold/code) — not user input
    main = (f'{_dot(st)}<div class="hz-main"><div class="hz-name"><span>{name}</span>{sched_html}</div>'
            f'<div class="hz-detail">{detail}</div></div>')
    if hint:
        return (f'<details class="hz-row {st}"><summary aria-label="{name}: {_STATUS_WORD[st]}">{main}{_IC_CHEV}</summary>'
                f'<div class="hz-hint">{hint}</div></details>')
    return f'<div class="hz-row {st}"><div class="hz-line">{main}</div></div>'


def _counts_html(c: dict) -> str:
    bits = []
    for st in ("down", "warn", "ok"):
        n = int(c.get(st, 0) or 0)
        if n:
            bits.append(f'<span class="hz-cnt {st}" title="{_STATUS_WORD[st]}">{n}</span>')
    return "".join(bits)


def _card(sec: dict) -> str:
    key = html.escape(sec.get("key", ""))
    rows = "".join(_row(r) for r in sec.get("rows", []))
    icon = _GROUP_SVG.get(sec.get("key", ""), "")
    svg = (f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
           f'stroke-linecap="round" stroke-linejoin="round"><path d="{icon}"/></svg>') if icon else ""
    n = len(sec.get("rows", []))
    return (f'<section class="hz-card" id="hz-{key}"><header>{svg}<span>{html.escape(sec.get("title", ""))}</span>'
            f'<span class="hz-sched">{n}</span><span class="hz-cnts">{_counts_html(sec.get("counts", {}))}</span></header>'
            f'{rows or "<div class=hz-empty>—</div>"}</section>')


def _nav(sections: list[dict]) -> str:
    links = []
    for sec in sections:
        st = sec.get("status", "ok")
        n = len(sec.get("rows", []))
        links.append(f'<a href="#hz-{html.escape(sec.get("key", ""))}">{_dot(st)}{html.escape(sec.get("title", ""))}'
                     f'<b>{n}</b></a>')
    return f'<nav class="hz-nav">{"".join(links)}</nav>'


def _meta(h: dict) -> str:
    c = h.get("counts", {})
    down, warn, ok = int(c.get("down", 0)), int(c.get("warn", 0)), int(c.get("ok", 0))
    parts = []
    if down:
        parts.append(f'<b style="color:var(--danger)">{health._plural(down, "сбой", "сбоя", "сбоев")}</b>')
    if warn:
        parts.append(f'<b style="color:var(--warn)">{health._plural(warn, "предупреждение", "предупреждения", "предупреждений")}</b>')
    if not down and not warn:
        parts.append(f'<b style="color:var(--ok)">всё исправно</b>')
    parts.append(f"{ok} ok")
    ts = html.escape(str(h.get("ts", "")))[11:19] or html.escape(str(h.get("ts", "")))
    parts.append(f"обновлено {ts} (за {h.get('elapsed', '?')} с)")
    slow = [(k, v) for k, v in (h.get("timings") or {}).items() if v >= 1.5]
    if slow:
        k, v = slow[0]
        parts.append(f'<span title="самый медленный зонд">медленно: {html.escape(k)} {v} с</span>')
    return " · ".join(parts)


def render_page() -> str:
    h = health.gather()
    ov = h.get("overall", "warn")
    sections = h.get("sections", [])
    badge = f'<span class="hz-badge {html.escape(ov)}"><i></i>{_OVERALL_WORD.get(ov, ov)}</span>'
    icons = f'<a class="iconbtn" href="/health" aria-label="Обновить" title="Обновить">{_IC_REFRESH}</a>'
    info = ("Карта всего развёртывания: каждый сервис, крон, хранилище и внешняя зависимость с живым "
            "статусом. Нажмите на строку — раскроется, почему это место хрупкое и что делать при сбое. "
            "Красное = сбой (уходит в Telegram каждые 15 мин), жёлтое = предупреждение.")
    head = mailcrm_ui._page_head("Health", None, None, icons, _meta(h), info)
    body = (f'{_CSS}<div class="hz-wrap">{head}'
            f'<div class="hz-top">{badge}{_nav(sections)}</div>'
            f'{"".join(_card(s) for s in sections)}'
            f'<div class="hz-foot">Обновляется каждую минуту, пока не раскрыта ни одна подсказка · '
            f'<a href="/health.json" style="color:var(--accent)">JSON</a></div></div>')
    # Auto-refresh every 60 s — but never while the operator is reading an expanded hint.
    body += ('<script>setInterval(function(){if(!document.querySelector("details.hz-row[open]"))'
             'location.reload()},60000)</script>')
    return mailcrm_ui._page("health", body)
