"""Tests for the compensation formatter (backend.tools.comp_fmt) and the deterministic
estimated-comp fallback (backend.applier.est_comp). Pure — no DB, no network.
"""
from __future__ import annotations

from backend.applier import est_comp
from backend.tools import comp_fmt


def test_fmt_money():
    assert comp_fmt.fmt_money(125000) == "$125k"
    assert comp_fmt.fmt_money(90000) == "$90k"
    assert comp_fmt.fmt_money(1_800_000) == "$1.8M"
    assert comp_fmt.fmt_money(2_000_000) == "$2M"
    assert comp_fmt.fmt_money(0) == ""
    assert comp_fmt.fmt_money(None) == ""
    assert comp_fmt.fmt_money(-5) == ""


def test_fmt_money_currency():
    assert comp_fmt.fmt_money(35000, "GBP") == "£35k"
    assert comp_fmt.fmt_money(120000, "EUR") == "€120k"
    assert comp_fmt.fmt_money(90000, "CAD") == "C$90k"
    assert comp_fmt.fmt_money(125000, "USD") == "$125k"
    assert comp_fmt.fmt_money(125000, None) == "$125k"  # missing currency -> $


def test_posted_uses_its_own_currency():
    html = comp_fmt.comp_html({"comp_min": 35000, "comp_max": 49000, "comp_currency": "GBP"})
    assert "£35k–£49k" in html and "$" not in html


def test_cleared_posted_falls_back_to_estimate():
    # a job whose (garbage) posted pay was nulled shows the estimate, not an empty line
    html = comp_fmt.comp_html({"comp_min": None, "comp_max": None,
                               "est_base_min": 110000, "est_base_max": 140000,
                               "est_total_min": 150000, "est_total_max": 200000})
    assert "по вакансии" not in html and "$110k–$140k" in html and "оценка" in html


def test_money_range():
    assert comp_fmt.money_range(120000, 160000) == "$120k–$160k"
    assert comp_fmt.money_range(100000, 100000) == "$100k"   # collapse equal ends
    assert comp_fmt.money_range(None, 90000) == "$90k"
    assert comp_fmt.money_range(None, None) == ""


def test_comp_html_posted_and_est():
    j = {"comp_min": 120000, "comp_max": 160000,
         "est_base_min": 130000, "est_base_max": 170000,
         "est_total_min": 180000, "est_total_max": 260000}
    html = comp_fmt.comp_html(j)
    assert "$120k–$160k" in html and "по вакансии" in html
    assert "$180k–$260k" in html and "total" in html and "оценка" in html


def test_comp_html_est_only_shows_base_when_no_posted():
    j = {"est_base_min": 90000, "est_base_max": 120000,
         "est_total_min": 100000, "est_total_max": 140000}
    html = comp_fmt.comp_html(j)
    assert "$90k–$120k" in html and "база" in html
    assert "по вакансии" not in html


def test_comp_html_empty_when_nothing():
    assert comp_fmt.comp_html({}) == ""
    assert comp_fmt.comp_text({}) == ""
    assert comp_fmt.has_comp({}) is False


def test_comp_text_plain():
    j = {"est_base_min": 90000, "est_base_max": 120000,
         "est_total_min": 100000, "est_total_max": 140000}
    t = comp_fmt.comp_text(j)
    assert "$90k–$120k база" in t and "~$100k–$140k total" in t


def test_branding_neutral():
    j = {"comp_min": 120000, "comp_max": 160000, "est_total_min": 180000, "est_total_max": 260000}
    low = (comp_fmt.comp_html(j) + comp_fmt.comp_text(j)).lower()
    for banned in ("claude", "anthropic", "openai", " gpt", "llm", " ai ", " ии"):
        assert banned not in low, banned


def test_est_comp_estimate_known_role():
    e = est_comp.estimate("Engineering", ["US"])
    assert e["est_base_min"] == 150000 and e["est_total_max"] == 270000
    # total is never below base
    assert e["est_total_min"] >= e["est_base_min"]


def test_est_comp_primary_region_and_fallbacks():
    # US wins when present among several
    assert est_comp._primary_region(["OTHER", "US"]) == "US"
    # empty / None -> US default
    assert est_comp._primary_region([]) == "US"
    assert est_comp._primary_region(None) == "US"
    # unknown role -> the broad default range, never a crash
    e = est_comp.estimate("No Such Role", ["OTHER"])
    assert e["est_base_min"] == est_comp._DEFAULT[0]
    # a role present but region missing -> falls back to that role's US row
    e2 = est_comp.estimate("Design", ["ZZ"])
    assert e2["est_base_min"] == est_comp._MED["Design"]["US"][0]


def test_est_comp_all_roles_have_four_regions():
    for role, by_region in est_comp._MED.items():
        assert set(by_region) == {"US", "CA", "UK", "OTHER"}, role
        for reg, tup in by_region.items():
            assert len(tup) == 4 and all(isinstance(x, int) and x > 0 for x in tup)
