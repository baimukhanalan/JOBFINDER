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

# STRICT worldwide-eligibility — only phrases that actually mean "we hire anywhere".
# Bare "global"/"globally"/"worldwide" is company-marketing fluff ("users worldwide",
# "expand globally", "250 locations globally") that appeared in ~55% of JDs and used to
# short-circuit every job to all-four regions, overriding the real country in `location`.
_WORLDWIDE_RE = re.compile(
    r"work (?:from |remotely from )?anywhere"
    r"|(?:hire|employ|recruit)\w*\s+(?:from\s+)?anywhere"
    r"|anywhere in the world|from any country|in any country"
    r"|open to (?:candidates|applicants)\s+(?:from\s+)?(?:anywhere|worldwide|any country|"
    r"any location|around the world|globally)"
    r"|(?:candidates|applicants)\s+(?:from\s+)?(?:anywhere|around the world)\s+"
    r"(?:are welcome|may apply|can apply)"
    r"|location[- ]independent|remote,?\s+worldwide|worldwide,?\s+remote")
# A short curated `location` field IS a real signal (unlike the description), so bare
# "worldwide/anywhere/global" there does mean all-four.
_LOC_WORLDWIDE_RE = re.compile(r"\b(worldwide|anywhere|global(?:ly)?|everywhere)\b")
_NA_RE = re.compile(
    r"\bnorth america\b|\bus\s*&\s*canada\b|\bus\s*/\s*canada\b|\bus and canada\b|\busa\s*/\s*canada\b"
    r"|\bnoram\b|\bnamer\b|\bamericas\b|\bamer\b")
_US_STRONG_RE = re.compile(
    r"\bunited states\b|\bu\.?s\.?a\b|\bu\.s\.\b|\bus[- ]based\b|\bus[- ]only\b"
    r"|\bremote\s*[-,(]\s*us\b")
# US state NAMES — a location given as US cities/states ("New York City, Santa Barbara")
# carries no "US" token but is unmistakably US. Scanned over the location scope only.
# NOTE "georgia" is intentionally OMITTED — it is both a US state and a country. It is
# disambiguated in _regions_from_location: a bare "Georgia" (no US context) is the COUNTRY.
_US_STATE_RE = re.compile(
    r"\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|"
    r"hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|"
    r"massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|"
    r"new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|"
    r"oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|"
    r"texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|"
    r"district of columbia|washington,? d\.?c\.?)\b")
# Major US job-hub CITIES (dominantly-US, low int'l collision) — recover common location
# strings like "San Francisco, CA" that carry no state name and an ambiguous ", CA" code.
_US_CITY_RE = re.compile(
    r"\b(san francisco|sf bay area|bay area|san jose|los angeles|san diego|new york city|nyc\b|seattle|austin|"
    r"boston|chicago|denver|atlanta|dallas|houston|miami|philadelphia|phoenix|minneapolis|"
    r"nashville|charlotte|san antonio|brooklyn|palo alto|mountain view|sunnyvale|"
    r"santa clara|santa monica|santa barbara|san carlos|menlo park|cupertino|redwood city|"
    r"oakland|sacramento|pittsburgh|tampa|orlando|raleigh|durham|cincinnati|cleveland|"
    r"kansas city|saint louis|st\.? louis|indianapolis|milwaukee|las vegas|san mateo|"
    r"irvine|bellevue|fremont|plano|chattanooga|salt lake city|boulder)\b")
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
    # macro-regions
    r"\bemea\b|\bapac\b|\banz\b|\bmena\b|\bcis\b|\bnordics?\b|\bbenelux\b|\bbalkans?\b|\bdach\b|\biberia\b"
    r"|\beurope(?:an)?\b|\basia(?:n|-pacific| pacific)?\b|\blatam\b|\blatin america\b"
    r"|\bsouth america\b|\bcentral america\b|\bmiddle east\b|\bafrica\b|\boceania\b"
    # Europe
    r"|\bgermany\b|\bfrance\b|\bspain\b|\bnetherlands\b|\bpoland\b|\bportugal\b|\bromania\b"
    r"|\bireland\b|\bitaly\b|\bhungary\b|\bczechia?\b|\bczech republic\b|\bslovakia\b"
    r"|\bslovenia\b|\bcroatia\b|\bserbia\b|\bbulgaria\b|\bgreece\b|\baustria\b|\bswitzerland\b"
    r"|\bbelgium\b|\bsweden\b|\bnorway\b|\bdenmark\b|\bfinland\b|\bestonia\b|\blatvia\b"
    r"|\blithuania\b|\bukraine\b|\bcyprus\b|\bmalta\b|\bluxembourg\b|\biceland\b|\bturkey\b"
    r"|\barmenia\b|\bmoldova\b|\bbosnia\b|\bmontenegro\b|\balbania\b|\bmacedonia\b"
    # APAC / South Asia
    r"|\bindia\b|\bphilippines\b|\bpakistan\b|\baustralia\b|\bnew zealand\b|\bsingapore\b"
    r"|\bjapan\b|\bchina\b|\bhong kong\b|\btaiwan\b|\bkorea\b|\bindonesia\b|\bvietnam\b"
    r"|\bthailand\b|\bmalaysia\b|\bbangladesh\b|\bsri lanka\b|\bnepal\b"
    # LATAM
    r"|\bbrazil\b|\bmexico\b|\bargentina\b|\bcolombia\b|\bchile\b|\bperu\b|\buruguay\b"
    r"|\becuador\b|\bguatemala\b|\bcosta rica\b|\bbolivia\b|\bvenezuela\b|\bparaguay\b"
    r"|\bdominican\b|\bpanama\b|\bhonduras\b|\bnicaragua\b|\bel salvador\b"
    # MEA
    r"|\bnigeria\b|\bkenya\b|\bsouth africa\b|\bghana\b|\bmorocco\b|\begypt\b|\bisrael\b"
    r"|\buae\b|\bdubai\b|\bsaudi\b|\bqatar\b|\bkazakhstan\b|\bazerbaijan\b")


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


def _regions_from_location(job: dict) -> list[str]:
    """Regions implied by the `location` field ALONE. [] if location is empty or names no
    place. This is authoritative: a "Remote - Japan" posting is Japan-eligibility, full stop."""
    loc = re.sub(r"_+", " ", (job.get("location") or "").strip().lower())
    if not loc:
        return []
    # Country-FIRST: a named country wins over a "worldwide" word, so "Anywhere in the
    # United States" is US-only (not all-four just because it contains "anywhere").
    found: set[str] = set()
    if _NA_RE.search(loc):
        found.update(("US", "CA"))
    if (_US_STRONG_RE.search(loc) or _US_LOC_RE.search(loc)
            or _US_STATE_RE.search(loc) or _US_CITY_RE.search(loc)):
        found.add("US")
    if _CA_RE.search(loc):
        found.add("CA")
    if _UK_STRONG_RE.search(loc) or _UK_LOC_RE.search(loc):
        found.add("UK")
    if _OTHER_RE.search(loc):
        found.add("OTHER")
    # "Georgia": a US state ONLY with US context (which set US above); a bare "Georgia"
    # with no other signal is the COUNTRY (e.g. a Tbilisi-based employer) -> OTHER.
    if not found and re.search(r"\bgeorgia\b", loc):
        found.add("OTHER")
    if found:
        return [c for c in REGION_CODES if c in found]
    # no specific place named -> a genuine "worldwide/anywhere" location opens all regions
    if _LOC_WORLDWIDE_RE.search(loc) or _WORLDWIDE_RE.search(loc):
        return list(REGION_CODES)
    return []


def classify_regions(job: dict) -> list[str]:
    """Deterministic region set; [] if no rule fires (caller may LLM-fallback).

    LOCATION-FIRST: if `location` names a place, it RESTRICTS eligibility (the JD saying
    "global" no longer promotes a "Remote - Japan" role to US/CA/UK). Only when location is
    uninformative do we fall back to full-text signals.
    """
    loc_regions = _regions_from_location(job)
    if loc_regions:
        return loc_regions
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
