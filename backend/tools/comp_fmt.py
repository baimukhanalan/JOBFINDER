"""Shared compensation formatting for the operator surfaces (catalog card, interview
modal, −60 reminder). One place so the posted vs. estimated labels stay consistent.

A job dict may carry POSTED pay (`comp_min`/`comp_max`, from the JD, ~48% of rows) and/or
a RESEARCHED estimate (`est_base_*` base range + `est_total_*` total comp = base+bonus+
equity). Posted pay is authoritative and labelled «по вакансии»; the estimate is labelled
«оценка». Neutral Russian labels only — never disclose how the estimate was produced.
"""
from __future__ import annotations


# Currency symbol per ISO code — so £/€ posted pay never renders with a misleading '$'.
_SYMBOL = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "C$", "AUD": "A$"}


def fmt_money(n, cur: str = "USD") -> str:
    """A compact money label in the given currency: 125000 -> '$125k' (USD) / '£125k'
    (GBP), 1_800_000 -> '$1.8M'. Falsy/≤0 -> ''."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    sym = _SYMBOL.get((cur or "USD").upper(), "$")
    if n >= 1_000_000:
        s = f"{sym}{n / 1_000_000:.1f}M"
        return s.replace(".0M", "M")
    return f"{sym}{round(n / 1000)}k"


def money_range(lo, hi, cur: str = "USD") -> str:
    """'$120k–$160k' when both present, else the single side, else ''."""
    a, b = fmt_money(lo, cur), fmt_money(hi, cur)
    if a and b:
        return a if a == b else f"{a}–{b}"
    return a or b or ""


def comp_summary(job: dict) -> dict:
    """{'posted','est_base','est_total'} formatted ranges for a job dict (empty strings
    when a piece is absent). Posted pay uses its own currency; the researched estimate
    is annualized USD."""
    j = job or {}
    pc = j.get("comp_currency") or "USD"
    ec = j.get("est_comp_currency") or "USD"
    return {
        "posted": money_range(j.get("comp_min"), j.get("comp_max"), pc),
        "est_base": money_range(j.get("est_base_min"), j.get("est_base_max"), ec),
        "est_total": money_range(j.get("est_total_min"), j.get("est_total_max"), ec),
    }


def has_comp(job: dict) -> bool:
    s = comp_summary(job)
    return bool(s["posted"] or s["est_base"] or s["est_total"])


def _exceeds(a, b) -> bool:
    """True when a > b as ints — used to show the estimated TOTAL next to a POSTED range
    only when it meaningfully exceeds it (i.e. adds bonus/equity on top of the posted base).
    A lower estimate next to a higher posted range just reads as contradictory noise."""
    try:
        return int(a) > int(b)
    except (TypeError, ValueError):
        return False


def comp_html(job: dict) -> str:
    """A compact comp line for a job card / context row. Posted range (authoritative) if
    present — plus the estimated TOTAL only when it exceeds the posted ceiling; else the
    estimated base + total. '' if nothing. Label text is wrapped in `.cmp-lbl`."""
    job = job or {}
    s = comp_summary(job)
    parts = []
    if s["posted"]:
        parts.append(f'<b>{s["posted"]}</b> <span class="cmp-lbl">по вакансии</span>')
        if s["est_total"] and _exceeds(job.get("est_total_max"), job.get("comp_max")):
            parts.append(f'~{s["est_total"]} <span class="cmp-lbl">total · оценка</span>')
    else:
        if s["est_base"]:
            parts.append(f'<b>{s["est_base"]}</b> <span class="cmp-lbl">база · оценка</span>')
        if s["est_total"]:
            parts.append(f'~{s["est_total"]} <span class="cmp-lbl">total · оценка</span>')
    return " · ".join(parts)


def comp_text(job: dict) -> str:
    """Plain-text comp line (Telegram / logs), same posted-vs-estimate logic as comp_html."""
    job = job or {}
    s = comp_summary(job)
    parts = []
    if s["posted"]:
        parts.append(f'{s["posted"]} (по вакансии)')
        if s["est_total"] and _exceeds(job.get("est_total_max"), job.get("comp_max")):
            parts.append(f'~{s["est_total"]} total · оценка')
    else:
        if s["est_base"]:
            parts.append(f'{s["est_base"]} база · оценка')
        if s["est_total"]:
            parts.append(f'~{s["est_total"]} total · оценка')
    return " · ".join(parts)
