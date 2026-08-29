"""Unit tests for the deterministic posted-comp extractor (no network)."""
from backend.applier.comp_extract import extract_comp


def _mm(desc):
    r = extract_comp(desc)
    return (r["comp_min"], r["comp_max"], r["comp_currency"], r["comp_source"])


def test_labeled_range():
    assert _mm("Base Compensation Range $110,000 — $160,000 USD Benefits") == (110000, 160000, "USD", "rule")


def test_base_pay_range():
    assert _mm("visit careers . Base Pay Range $168,750 — $270,000 USD Don't meet")[:2] == (168750, 270000)


def test_salary_range_prefix():
    assert _mm("United States Salary Range $115,200 — $194,400 USD How")[:2] == (115200, 194400)


def test_second_number_without_dollar():
    assert _mm("base pay range (CA, WA, NY) per year: $ 195,000 - 255,000 other")[:2] == (195000, 255000)


def test_low_annual_range():
    assert _mm("The annual salary range for this full-time position is $46,500 — $104,650 USD")[:2] == (46500, 104650)


def test_k_suffix():
    assert _mm("Salary range $120k - $150k depending on experience")[:2] == (120000, 150000)


def test_hourly_annualized():
    lo, hi, cur, src = _mm("Pay range: $30.00 - $35.00 per hour plus benefits")
    assert (lo, hi) == (round(30 * 2080), round(35 * 2080)) == (62400, 72800)


def test_single_figure():
    assert _mm("We offer a competitive base salary of $120,000 for this role")[:2] == (120000, 120000)


def test_funding_is_not_comp():
    r = extract_comp("We've raised $781M in funding and our last valuation was $11B.")
    assert r["comp_source"] == "unknown" and r["comp_min"] is None


def test_deal_size_is_not_comp():
    r = extract_comp("operated with an average deal size of $100k+ and closed 1M+ ARR")
    assert r["comp_source"] == "unknown" and r["comp_min"] is None


def test_no_money_at_all():
    r = extract_comp("Great mission-driven team. Apply now to join us building the future.")
    assert r["comp_source"] == "unknown" and r["comp_min"] is None and r["comp_max"] is None


def test_currency_gbp():
    lo, hi, cur, src = _mm("Salary range £70,000 - £90,000 per year")
    assert cur == "GBP" and (lo, hi) == (70000, 90000)


def test_shape_always_has_keys():
    r = extract_comp("")
    assert set(r) == {"comp_min", "comp_max", "comp_currency", "comp_source"}
