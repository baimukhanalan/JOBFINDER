"""Pay-region filter: only apply to roles in HIGH-pay regions.

Product decision: skip roles that require residence in a low-pay region (LatAm,
South/SE Asia, Africa, Eastern Europe) — the pay there isn't worth applying to.
Keep US / Canada / UK / Ireland / Western + Northern Europe / Australia / NZ, plus
generic remote and unclassified locations (lenient — the human still reviews).

A company whitelist (KEEP_COMPANIES, e.g. Salmon) bypasses the filter entirely.

Everything is overridable at runtime (no code change) via comma-separated env vars:
  HIGHPAY_EXTRA=...      add high-pay place names
  LOWPAY_EXTRA=...       add low-pay place names to skip
  GEO_KEEP_COMPANIES=... company names/slugs that always pass
e.g. to also apply to Poland: export LOWPAY_REMOVE=poland,romania  (see below).
"""
from __future__ import annotations

import os
import re

_HIGHPAY = {
    "united states", "usa", "u.s.a", "u.s", "america", "canada", "ontario", "toronto",
    "vancouver", "montreal", "united kingdom", "uk", "england", "london", "scotland",
    "wales", "ireland", "dublin", "germany", "berlin", "munich", "france", "paris",
    "netherlands", "amsterdam", "switzerland", "zurich", "sweden", "stockholm", "norway",
    "oslo", "denmark", "copenhagen", "finland", "helsinki", "australia", "sydney",
    "melbourne", "new zealand", "spain", "madrid", "barcelona", "belgium", "brussels",
    "austria", "vienna", "luxembourg", "iceland", "emea",
}
_LOWPAY = {
    "brazil", "são paulo", "sao paulo", "rio de janeiro", "mexico", "mexico city",
    "guadalajara", "colombia", "bogota", "argentina", "buenos aires", "chile", "peru",
    "ecuador", "costa rica", "latam", "latin america", "india", "bengaluru", "bangalore",
    "mumbai", "delhi", "hyderabad", "pune", "gurgaon", "noida", "chennai", "philippines",
    "manila", "cebu", "indonesia", "jakarta", "vietnam", "hanoi", "malaysia", "thailand",
    "pakistan", "bangladesh", "sri lanka", "nigeria", "lagos", "kenya", "nairobi", "egypt",
    "cairo", "south africa", "morocco", "poland", "warsaw", "romania", "bucharest",
    "ukraine", "kyiv", "bulgaria", "serbia", "belarus", "turkey", "turkiye", "istanbul",
}
KEEP_COMPANIES = {"salmon"}

_REMOTE_RE = re.compile(r"(?i)\b(remote|anywhere|worldwide|global|distributed)\b")


def _env_set(name: str) -> set[str]:
    return {x.strip().lower() for x in os.getenv(name, "").split(",") if x.strip()}


# apply env overrides once at import
_HIGHPAY |= _env_set("HIGHPAY_EXTRA")
_LOWPAY = (_LOWPAY | _env_set("LOWPAY_EXTRA")) - _env_set("LOWPAY_REMOVE")
KEEP_COMPANIES |= _env_set("GEO_KEEP_COMPANIES")


def _compile(words: set[str]) -> re.Pattern:
    # word-boundary match so 'us' doesn't fire inside 'houston'/'belarus'
    return re.compile(r"(?i)\b(?:" + "|".join(sorted((re.escape(w) for w in words), key=len,
                                                     reverse=True)) + r")\b")


_HIGH_RE = _compile(_HIGHPAY)
_LOW_RE = _compile(_LOWPAY)


def is_highpay(location: str, company: str = "") -> bool:
    """True if a role is worth applying to on pay-region grounds."""
    if any(k in (company or "").lower() for k in KEEP_COMPANIES):
        return True
    loc = location or ""
    if _HIGH_RE.search(loc):
        return True                 # names a high-pay place (wins over any low-pay mention)
    if _LOW_RE.search(loc):
        return False                # names ONLY low-pay place(s) -> skip
    if not loc.strip() or _REMOTE_RE.search(loc):
        return True                 # generic remote / unspecified -> allow
    return True                     # unrecognized location -> lenient allow (human reviews)
