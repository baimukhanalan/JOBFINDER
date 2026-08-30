"""Deterministic posted-pay-range extractor for catalog job descriptions.

Mirrors the deterministic-first design of `applier/regions.py`: a pure function run
at collect time (and in a backfill pass) that reads the description and returns the
posted compensation range, annualized to USD integers. It is intentionally
CONSERVATIVE — it extracts only a range/figure anchored to a compensation phrase (or
a two-sided ``$X — $Y`` shape with a currency), and rejects dollar amounts that are
funding / deal-size / valuation noise. Whatever it can't confidently read is left
unknown for the (optional) LLM residue pass, exactly like an unclassified region.

The number it reports is the range the posting STATES (mostly a base range, per US
pay-transparency law) — not a fabricated total comp. No network, no brand strings.
"""
from __future__ import annotations

import re

_NUM = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{1,3}(?:\.\d+)?\s?[kK]|\d{1,7}(?:\.\d+)?)"
_SYM = r"[$£€]"
# Allow an optional currency code (and the word "and") BETWEEN the two numbers so
# "£35,000 GBP and £49,000 GBP" reads as a range, not just the first figure.
_RANGE = re.compile(rf"({_SYM})\s?({_NUM})\s*(?:[A-Za-z]{{3}}\s*)?(?:-|–|—|to|and)\s*({_SYM})?\s?({_NUM})")
_SINGLE = re.compile(rf"({_SYM})\s?({_NUM})")
_ANCHOR = re.compile(
    r"(base\s+pay|base\s+salary|salary\s+range|compensation\s+range|pay\s+range|"
    r"target\s+comp|expected\s+salary|annual\s+salary|\bsalary\b|\bcompensation\b|"
    r"\bOTE\b|per\s+year|/\s*year|annually|/\s*yr)", re.I)
_NEG = re.compile(
    r"(funding|raised|valuation|deal\s+size|\bARR\b|\brevenue\b|series\s+[a-e]\b|"
    r"market\s+cap|\bfunded\b|\binvest)", re.I)
# A $ amount sitting in an equity / bonus / relocation / stipend / 401(k) context is NOT
# the base salary — do not report it as posted pay (e.g. "New hire equity: $24,000-$36,000").
_NONBASE = re.compile(
    r"(equity|\bstock\b|\bRSU\b|option\s+grant|new\s+hire|refresh|sign[-\s]?on|"
    r"signing|relocation|stipend|per\s+pay\s+period|401\s*\(?k\)?)", re.I)
# Below this, an "annual" figure is a misparse (a monthly/hourly/per-period number, or
# noise) — a real annual salary for these roles is never under $15k.
_MIN_ANNUAL = 15000
_MB = re.compile(r"\s*(m|b|mm|million|billion|bn)\b", re.I)


def _num(tok: str) -> float:
    t = tok.replace(",", "").replace(" ", "").lower()
    if t.endswith("k"):
        return float(t[:-1]) * 1000
    return float(t)


def _currency(ctx: str) -> str:
    c = ctx.lower()
    if "£" in ctx or "gbp" in c:
        return "GBP"
    if "€" in ctx or "eur" in c:
        return "EUR"
    if "cad" in c or "c$" in c:
        return "CAD"
    if "aud" in c or "a$" in c:
        return "AUD"
    return "USD"


def _period(ctx: str, max_val: float, boxed: bool) -> str:
    c = ctx.lower()
    if re.search(r"per\s*hour|/\s*hr\b|/\s*hour|\bhourly\b|an\s+hour|a\s+hour", c):
        return "hour"
    if re.search(r"per\s*month|/\s*mo\b|\bmonthly\b|a\s+month", c):
        return "month"
    # a small, un-grouped, non-k figure ($30.00) is an hourly rate, not $30 a year
    if not boxed and max_val < 2000:
        return "hour"
    return "year"


def _annual(v: float, period: str) -> int:
    if period == "hour":
        v = v * 2080
    elif period == "month":
        v = v * 12
    return int(round(v))


def _result(lo, hi, currency, source) -> dict:
    return {"comp_min": lo, "comp_max": hi, "comp_currency": currency, "comp_source": source}


def extract_comp(description: str | None) -> dict:
    """Return {comp_min, comp_max, comp_currency, comp_source}. comp_source is
    'rule' on a confident extraction, else 'unknown' with the values None."""
    out = _result(None, None, None, "unknown")
    if not description:
        return out
    d = re.sub(r"\s+", " ", description)

    # 1) prefer an explicit two-sided range
    for m in _RANGE.finditer(d):
        n1, n2 = m.group(2), m.group(4)
        end = m.end()
        left = d[max(0, m.start() - 60):m.start()]
        right = d[end:end + 18]
        if _NEG.search(left):
            continue
        if _MB.match(right):  # "$11B", "$5M" — funding, not pay
            continue
        v1, v2 = _num(n1), _num(n2)
        lo, hi = (min(v1, v2), max(v1, v2))
        near = d[max(0, m.start() - 45):end + 12]
        if _NONBASE.search(near):  # equity/bonus/relocation/stipend — not base pay
            continue
        currency = _currency(d[max(0, m.start() - 2):end + 6])
        # accept if anchored, or currency-tagged, or the numbers already read like a salary
        boxed = ("," in n1) or ("k" in n1.lower())
        annual_lo = _annual(lo, _period(near, hi, boxed))
        annual_hi = _annual(hi, _period(near, hi, boxed))
        if annual_hi < _MIN_ANNUAL:  # too small to be an annual salary (per-period/misparse)
            continue
        if not (_ANCHOR.search(near) or currency != "USD"
                or re.search(r"USD|CAD|GBP|EUR", near) or annual_lo >= 20000):
            continue
        return _result(annual_lo, annual_hi, currency, "rule")

    # 2) fall back to a single figure — stricter: must be anchored
    for m in _SINGLE.finditer(d):
        n = m.group(2)
        end = m.end()
        right = d[end:end + 18]
        if _MB.match(right):
            continue
        near = d[max(0, m.start() - 45):end + 12]
        if _NEG.search(near) or _NONBASE.search(near) or not _ANCHOR.search(near):
            continue
        v = _num(n)
        boxed = ("," in n) or ("k" in n.lower())
        annual = _annual(v, _period(near, v, boxed))
        if annual < _MIN_ANNUAL:  # too small to be an annual salary, and not clearly hourly
            continue
        currency = _currency(d[max(0, m.start() - 2):end + 6])
        return _result(annual, annual, currency, "rule")

    return out
