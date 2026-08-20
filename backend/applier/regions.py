"""Classify which countries/regions a (remote) job is open to.

Deterministic-first, mirroring boards.py's blob pattern (lower-cased join of
title+location+description[:1500]). Returns a subset of REGION_CODES.
Multi-eligibility: North America -> US+CA; worldwide/anywhere -> all four.
Short/ambiguous tokens use word boundaries to avoid false positives
("business" must not match US).
"""
from __future__ import annotations
import re

REGION_CODES = ("US", "CA", "UK", "OTHER")

_WORLDWIDE_RE = re.compile(
    r"\b(worldwide|work from anywhere|remote anywhere|anywhere in the world|global(?:ly)?)\b")
_NA_RE = re.compile(
    r"\bnorth america\b|\bus\s*&\s*canada\b|\bus\s*/\s*canada\b|\bus and canada\b|\busa\s*/\s*canada\b")
_US_STRONG_RE = re.compile(
    r"\bunited states\b|\bu\.?s\.?a\b|\bu\.s\.\b|\bus[- ]based\b|\bus[- ]only\b"
    r"|\bremote\s*[-,(]\s*us\b")
_CA_RE = re.compile(
    r"\bcanada\b|\bcanadian\b|\bontario\b|\bquebec\b|\bbritish columbia\b|\balberta\b"
    r"|\btoronto\b|\bvancouver\b|\bmontreal\b")
_UK_STRONG_RE = re.compile(
    r"\bunited kingdom\b|\bengland\b|\bscotland\b|\bwales\b|\blondon\b|\bbritain\b|\bbritish\b")
# Short ambiguous tokens ("us","uk") — scanned ONLY over title+location (see _loc_blob),
# never the description, or "join us"/"contact us" would false-positive everywhere.
_US_LOC_RE = re.compile(r"\bus\b(?!\s*[/&]?\s*canada)")
_UK_LOC_RE = re.compile(r"\buk\b|\bu\.k\.\b")
_OTHER_RE = re.compile(
    r"\bemea\b|\bapac\b|\banz\b|\beurope(?:an)?\b|\blatam\b|\blatin america\b|\bsouth america\b"
    r"|\bindia\b|\bphilippines\b|\bpakistan\b|\bgermany\b|\bfrance\b|\bspain\b|\bnetherlands\b"
    r"|\bpoland\b|\bportugal\b|\bromania\b|\bireland\b|\baustralia\b|\bnew zealand\b"
    r"|\bsingapore\b|\bbrazil\b|\bmexico\b|\bargentina\b|\bcolombia\b|\bafrica\b|\bjapan\b")


def _blob(job: dict) -> str:
    """Full text for multi-word/unambiguous markers (incl. description head)."""
    return " ".join([
        job.get("title", "") or "",
        job.get("location", "") or "",
        (job.get("description", "") or "")[:1500],
    ]).lower()


def _loc_blob(job: dict) -> str:
    """Title+location only — the safe scope for short ambiguous tokens (us/uk)."""
    return " ".join([job.get("title", "") or "", job.get("location", "") or ""]).lower()


def classify_regions(job: dict) -> list[str]:
    """Deterministic region set; [] if no rule fires (caller may LLM-fallback)."""
    blob = _blob(job)
    if _WORLDWIDE_RE.search(blob):
        return list(REGION_CODES)
    loc = _loc_blob(job)
    found: set[str] = set()
    if _NA_RE.search(blob):
        found.update(("US", "CA"))
    if _US_STRONG_RE.search(blob) or _US_LOC_RE.search(loc):
        found.add("US")
    if _CA_RE.search(blob):
        found.add("CA")
    if _UK_STRONG_RE.search(blob) or _UK_LOC_RE.search(loc):
        found.add("UK")
    if _OTHER_RE.search(blob):
        found.add("OTHER")
    # multi-eligibility: keep every region that fired.
    return [c for c in REGION_CODES if c in found]


def _llm_regions(job: dict) -> list[str]:
    """Ask the local Sumrak LLM to pick regions; returns a subset of REGION_CODES ([] on any failure)."""
    from backend.config import settings
    if not settings.llm_url:
        return []
    import json as _json
    import httpx
    prompt = (
        "You classify which regions a REMOTE job is open to. "
        "Reply with ONLY a JSON array using codes from US, CA, UK, OTHER "
        "(OTHER = open to some region but not US/CA/UK). Empty array if unclear.\n\n"
        f"Title: {job.get('title','')}\nLocation: {job.get('location','')}\n"
        f"Description: {(job.get('description','') or '')[:1200]}"
    )
    try:
        r = httpx.post(
            f"{settings.llm_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_key}", "Content-Type": "application/json"},
            json={"model": settings.llm_model, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.0, "max_tokens": 40, "stream": False},
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\[.*?\]", text, re.S)
        raw = _json.loads(m.group(0)) if m else []
        return [c for c in REGION_CODES if c in {str(x).upper() for x in raw}]
    except Exception:
        return []


def classify_with_source(job: dict, use_llm: bool = True) -> tuple[list[str], str]:
    """Deterministic first; LLM only on the residue. Returns (regions, source)."""
    rule = classify_regions(job)
    if rule:
        return rule, "rule"
    if use_llm:
        llm = _llm_regions(job)
        if llm:
            return llm, "llm"
    return [], "unknown"
