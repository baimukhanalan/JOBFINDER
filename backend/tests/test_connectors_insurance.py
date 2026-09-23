"""Network-free unit tests for the insurance-AEP mass-hiring connectors.

Every `_<source>_row` decision is tested against the TWO HARD RULES (remote + categorize entry
bucket) and the remote/US edge cases. No network — the row helpers are pure.
"""
import pytest

from backend.tools.connectors_insurance import (
    _selectquote_row,
    _healthmarkets_row,
    AEP_SALES_TITLES_DROPPED,
)
from backend.tools.mass_hiring import categorize


# --------------------------------------------------------------------------- SelectQuote (Jibe)
def _sq(title, *, state="", full="", short="", cc="US", country="United States",
        req_id="123", slug="123", smin=None, smax=None):
    return {
        "title": title, "state": state, "full_location": full, "short_location": short,
        "country_code": cc, "country": country, "req_id": req_id, "slug": slug,
        "salary_min_value": smin, "salary_max_value": smax, "apply_url": "",
        "posted_date": "", "create_date": "", "employment_type": "Full-time",
    }


def test_selectquote_remote_entry_row_kept():
    # An entry title that categorize() accepts, remote via full_location.
    d = _sq("Customer Service Representative", full="Remote, United States")
    row = _selectquote_row(d)
    assert row is not None
    assert row["source"] == "selectquote"
    assert row["company"] == "SelectQuote"
    assert row["source_id"] == "123"
    assert row["us_eligible"] is True
    assert row["category"] == "customer_support"
    assert "icims" in row["apply_url"] or "jibeapply" in row["apply_url"]


def test_selectquote_remote_via_state_flag():
    d = _sq("Insurance Agent", state="Remote", full="")
    row = _selectquote_row(d)
    assert row is not None and row["category"] == "customer_support"


def test_selectquote_non_remote_dropped():
    # A city-based (non-remote) posting is rejected by the remote hard rule.
    d = _sq("Insurance Agent", state="Kansas", full="Overland Park, Kansas, United States")
    assert _selectquote_row(d) is None


def test_selectquote_non_us_dropped():
    d = _sq("Customer Service Representative", full="Remote, Philippines",
            cc="PH", country="Philippines")
    assert _selectquote_row(d) is None


def test_selectquote_senior_dropped_by_categorize():
    d = _sq("Senior Data Engineer", full="Remote, United States")
    assert _selectquote_row(d) is None


def test_selectquote_salary_passthrough():
    d = _sq("Customer Support Specialist", full="Remote, United States", smin=40000, smax=55000)
    row = _selectquote_row(d)
    assert row is not None
    assert row["salary_min"] == 40000 and row["salary_max"] == 55000


def test_selectquote_bad_salary_ignored():
    d = _sq("Customer Support Specialist", full="Remote, United States", smin="", smax=0)
    row = _selectquote_row(d)
    assert row is not None
    assert row["salary_min"] is None and row["salary_max"] is None


def test_selectquote_missing_title_or_id():
    assert _selectquote_row(_sq("", full="Remote, United States")) is None
    assert _selectquote_row(_sq("Insurance Agent", full="Remote, United States",
                                req_id=None, slug=None)) is None
    assert _selectquote_row({}) is None
    assert _selectquote_row("not a dict") is None


def test_selectquote_blank_country_code_us_kept():
    # No country_code but country == United States → kept.
    d = _sq("Insurance Agent", full="Remote, United States", cc="")
    assert _selectquote_row(d) is not None
    # No country_code and a foreign country → dropped.
    d2 = _sq("Insurance Agent", full="Remote", cc="", country="Canada")
    assert _selectquote_row(d2) is None


def test_selectquote_aep_sales_titles_currently_dropped_by_categorize():
    """Documents the categorize() gap: the live AEP licensed-sales titles are remote-US but are
    DROPPED by the current categorize() (no insurance-sales bucket). If categorize() is widened,
    these must start to pass — this test then flips to assert they are KEPT (see module note)."""
    for t in AEP_SALES_TITLES_DROPPED:
        d = _sq(t, full="Remote, United States")
        # Hard-rule contract: a row only exists when categorize() accepts the title.
        assert (_selectquote_row(d) is not None) == (categorize(t) is not None)


# --------------------------------------------------------------------------- HealthMarkets (Radancy)
def test_healthmarkets_remote_entry_row_kept():
    row = _healthmarkets_row("46672138816", "Insurance Advisor - Remote",
                             "Remote, United States", "/en/job/remote/insurance-advisor/35899/1")
    assert row is not None
    assert row["source"] == "healthmarkets"
    assert row["company"] == "HealthMarkets"
    assert row["source_id"] == "46672138816"
    assert row["us_eligible"] is True
    assert row["category"] == "customer_support"
    assert row["apply_url"].startswith("https://careers.healthmarkets.com/en/job/")


def test_healthmarkets_absolute_href_kept():
    row = _healthmarkets_row("1", "Work From Home Insurance Representative", "Virtual, US",
                             "https://careers.healthmarkets.com/en/job/1")
    assert row is not None
    assert row["apply_url"] == "https://careers.healthmarkets.com/en/job/1"


def test_healthmarkets_non_remote_local_dropped():
    # HealthMarkets' local field-agent postings (a city location, no remote signal) are dropped.
    assert _healthmarkets_row("2", "Insurance Advisor", "Parkersburg, WV", "/en/job/2") is None


def test_healthmarkets_non_entry_title_dropped():
    assert _healthmarkets_row("3", "Regional Sales Director - Remote", "Remote", "/en/job/3") is None


def test_healthmarkets_missing_fields():
    assert _healthmarkets_row("", "Insurance Advisor - Remote", "Remote", "/x") is None
    assert _healthmarkets_row("4", "", "Remote", "/x") is None


def test_healthmarkets_href_fallback():
    row = _healthmarkets_row("9", "Remote Insurance Representative", "Remote", "")
    assert row is not None
    assert row["apply_url"] == "https://careers.healthmarkets.com/en/job/9"


# --------------------------------------------------------------------------- module hygiene
def test_row_shape_has_required_keys():
    row = _selectquote_row(_sq("Customer Service Representative", full="Remote, United States"))
    for k in ("source", "source_id", "company", "company_key", "title", "category",
              "comp_type", "auto_status", "location_raw", "us_eligible", "apply_url"):
        assert k in row
