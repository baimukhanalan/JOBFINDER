"""Deterministic ESTIMATED-comp fallback for NEW catalog jobs (self-heal).

The one-time research fleet estimated base + total compensation for every current
company×role×region combo (stored in job_catalog.est_*). For jobs collected LATER we
don't re-run the fleet: `catalog_collector.backfill_est_comp` first tries to INHERIT the
estimate from an existing sibling of the same (company, role, region) combo, and only
falls back to this module when the combo is brand-new.

`_MED[role][region]` = (base_min, base_max, total_min, total_max), annualized USD — the
MEDIAN of the fleet's researched values per functional role × primary region (US/CA/UK/
OTHER). Regenerated from job_catalog with a percentile_cont(0.5) group query; refresh it
if the role taxonomy or the market shifts materially. Not a claim about any one posting —
a market-norm ballpark for an unseen combo.
"""
from __future__ import annotations

_MED: dict[str, dict[str, tuple]] = {
    "Customer Support & Success": {"US": (62000, 88000, 65000, 95000), "CA": (65000, 90000, 70000, 100000), "UK": (58000, 80000, 65000, 93500), "OTHER": (55000, 80000, 60000, 95000)},
    "Data & ML": {"US": (140000, 190000, 200000, 270000), "CA": (105000, 140000, 132500, 180000), "UK": (90000, 130000, 110000, 170000), "OTHER": (80000, 130000, 95000, 150000)},
    "Design": {"US": (145000, 180000, 165000, 220000), "CA": (90000, 120000, 100000, 145000), "UK": (95000, 135000, 112500, 190000), "OTHER": (45000, 80000, 50000, 95000)},
    "Engineering": {"US": (150000, 195000, 190000, 270000), "CA": (130000, 170000, 165000, 240000), "UK": (95000, 150000, 120000, 190000), "OTHER": (70000, 115000, 85000, 150000)},
    "Executive / Leadership": {"US": (190000, 245000, 240000, 360000), "CA": (220000, 270000, 320000, 480000), "UK": (190000, 245000, 240000, 360000), "OTHER": (105000, 150000, 180000, 280000)},
    "Finance & Accounting": {"US": (110000, 150000, 125000, 165000), "CA": (122500, 155000, 142500, 185000), "UK": (85000, 120000, 96500, 137500), "OTHER": (28000, 42000, 30000, 45000)},
    "Legal & Compliance": {"US": (130000, 165000, 157500, 215000), "CA": (90000, 130000, 100000, 155000), "UK": (85000, 115000, 95000, 135000), "OTHER": (55000, 90000, 60000, 100000)},
    "Marketing & Comms": {"US": (50000, 75000, 52000, 80000), "CA": (85000, 120000, 95000, 145000), "UK": (70000, 100000, 85000, 140000), "OTHER": (55000, 85000, 60000, 100000)},
    "Operations": {"US": (90000, 130000, 100000, 150000), "CA": (100000, 140000, 110000, 160000), "UK": (75000, 115000, 85000, 142500), "OTHER": (35000, 65000, 40000, 70000)},
    "Other": {"US": (65000, 105000, 68000, 112000), "CA": (82000, 113000, 86000, 120000), "UK": (35000, 55000, 35000, 60000), "OTHER": (15000, 25000, 15500, 28000)},
    "People & Recruiting": {"US": (95000, 130000, 115000, 155000), "CA": (90000, 120000, 100000, 135000), "UK": (68000, 95000, 78000, 115000), "OTHER": (42000, 62000, 45000, 68000)},
    "Product": {"US": (165000, 210000, 205000, 290000), "CA": (132500, 165000, 160000, 230000), "UK": (95000, 125000, 110000, 155000), "OTHER": (70000, 110000, 85000, 140000)},
    "Sales / GTM": {"US": (90000, 140000, 150000, 260000), "CA": (85000, 115000, 140000, 185000), "UK": (85000, 120000, 150000, 200000), "OTHER": (60000, 90000, 90000, 145000)},
}

# Fallback for a role_category not in the table (or None) — a broad mid-market range.
_DEFAULT = (70000, 110000, 80000, 130000)


def _primary_region(regions) -> str:
    """One region for the lookup: US > CA > UK > OTHER; default US when unknown/empty."""
    regions = regions or []
    for r in ("US", "CA", "UK", "OTHER"):
        if r in regions:
            return r
    return "US"


def estimate(role_category: str | None, regions) -> dict:
    """Deterministic estimated comp for a (role_category, regions) with no researched
    value — the median of the fleet's estimates for that role × primary region."""
    row = _MED.get(role_category or "", {})
    b_min, b_max, t_min, t_max = row.get(_primary_region(regions)) or row.get("US") or _DEFAULT
    return {"est_base_min": b_min, "est_base_max": b_max,
            "est_total_min": t_min, "est_total_max": t_max}
