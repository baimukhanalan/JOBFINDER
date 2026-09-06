"""Render the /health admin page — one status card per section (services / crons / data / display /
system), each row a colored dot + name + detail. Uses the shared shell (`mailcrm_ui._page`) + theme
tokens; auto-refreshes every 30s. Read-only view over `health.gather()`."""
from __future__ import annotations

import html

from backend.tools import health, mailcrm_ui

_DOT = {"ok": "#1a9d4e", "warn": "#c9820a", "down": "#c62828"}
_WORD = {"ok": "OK", "warn": "Внимание", "down": "Проблема"}

_CSS = """
<style>
.hz-wrap{max-width:1000px;margin:0 auto;padding:8px 4px 40px}
.hz-sec{background:var(--panel,#fff);border:1px solid var(--line,#e6e6e6);border-radius:12px;
  margin:14px 0;overflow:hidden}
.hz-sec h3{margin:0;padding:12px 16px;font-size:14px;font-weight:700;border-bottom:1px solid var(--line,#eee);
  background:rgba(0,0,0,.02)}
.hz-row{display:flex;align-items:flex-start;gap:10px;padding:10px 16px;border-top:1px solid var(--line,#f0f0f0)}
.hz-row:first-of-type{border-top:none}
.hz-dot{flex:0 0 auto;width:10px;height:10px;border-radius:50%;margin-top:5px}
.hz-name{flex:0 0 240px;font-weight:600;font-size:13.5px}
.hz-detail{flex:1;color:var(--muted,#666);font-size:13px;word-break:break-word;font-variant-numeric:tabular-nums}
.hz-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:6px 4px 0}
.hz-badge{font-weight:800;font-size:15px;padding:6px 14px;border-radius:999px;color:#fff}
.hz-sub{color:var(--muted,#666);font-size:13px}
@media(max-width:640px){.hz-name{flex-basis:140px}}
</style>
"""


def _row(r: dict) -> str:
    st = r.get("status", "warn")
    return (f'<div class="hz-row"><span class="hz-dot" style="background:{_DOT.get(st, "#999")}"></span>'
            f'<span class="hz-name">{html.escape(str(r.get("name", "?")))}</span>'
            f'<span class="hz-detail">{html.escape(str(r.get("detail", "")))}</span></div>')


def _section(sec: dict) -> str:
    rows = "".join(_row(r) for r in sec.get("rows", []))
    return f'<div class="hz-sec"><h3>{html.escape(sec.get("title", ""))}</h3>{rows or "<div class=hz-row>—</div>"}</div>'


def render_page() -> str:
    h = health.gather()
    ov = h.get("overall", "warn")
    c = h.get("counts", {})
    badge = (f'<span class="hz-badge" style="background:{_DOT.get(ov)}">{_WORD.get(ov, ov)}</span>')
    head = (f'<div class="hz-head">{badge}'
            f'<span class="hz-sub">🟢 {c.get("ok", 0)} · 🟡 {c.get("warn", 0)} · 🔴 {c.get("down", 0)}'
            f' &nbsp;·&nbsp; обновлено {html.escape(h.get("ts", ""))} '
            f'&nbsp;·&nbsp; <a href="/health" style="color:var(--accent,#0c47c2)">обновить ↻</a></span></div>')
    sections = "".join(_section(s) for s in h.get("sections", []))
    body = (f'{_CSS}<div class="hz-wrap">'
            f'{mailcrm_ui._page_head("Health", None, None, "", "здоровье сервисов, кронов и данных")}'
            f'{head}{sections}</div>')
    # auto-refresh every 30s (meta-less: a tiny script, respects the shell)
    body += '<script>setTimeout(()=>location.reload(),30000)</script>'
    return mailcrm_ui._page("health", body)
