"""Unit tests for the mass-hiring source row-decision helpers (no network).

Both connectors were silently returning 0 rows (2026-08-27):
  * Amazon: the remote test looked at `normalized_location` (the COUNTRY) for the word
    'virtual', which is never there — the marker is `city='Virtual'`.
  * Concentrix: a too-narrow searchText plus a `total==0` early-exit hid the US slice.
These tests pin the per-job decision so the fetchers can't silently regress again.
"""
from backend.tools import mass_hiring as mh


# ---- Amazon --------------------------------------------------------------------

def test_amazon_us_virtual_is_kept():
    row = mh._amazon_row({
        "id": "1", "title": "Customer Service Representative",
        "city": "Virtual", "normalized_location": "USA", "job_path": "/en/jobs/1",
    })
    assert row is not None
    assert row["source"] == "amazon"
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True
    assert row["apply_url"].startswith("https://www.amazon.jobs")


def test_amazon_offshore_virtual_is_dropped():
    # The real-world case: Amazon's virtual CS roles are mostly GBR/ZAF language moderation.
    assert mh._amazon_row({
        "id": "2", "title": "Customer Service Associate - Arabic Moderator",
        "city": "Virtual", "normalized_location": "ZAF", "job_path": "/x",
    }) is None


def test_amazon_us_but_onsite_is_dropped():
    # US customer service, but a physical site (city != Virtual) → not remote → dropped.
    assert mh._amazon_row({
        "id": "3", "title": "Customer Service Representative",
        "city": "Seattle", "normalized_location": "USA", "job_path": "/x",
    }) is None


def test_amazon_us_virtual_senior_is_dropped():
    # Remote + US, but a senior title is NOT a mass-hiring entry role.
    assert mh._amazon_row({
        "id": "4", "title": "Senior Customer Service Manager",
        "city": "Virtual", "normalized_location": "USA", "job_path": "/x",
    }) is None


# ---- Concentrix ----------------------------------------------------------------

def test_concentrix_usa_work_at_home_is_kept():
    row = mh._concentrix_row({
        "title": "Customer Service Representative",
        "locationsText": "USA Work at Home",
        "externalPath": "/en-US/external_global/job/USA-Work-at-Home/CSR_R123456",
    })
    assert row is not None
    assert row["source"] == "concentrix"
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True
    assert row["source_id"] == "R123456"


def test_concentrix_offshore_is_dropped():
    for loc in ("PHL Work-at-Home", "CZE - Work-at-Home", "MEX Work-at-Home"):
        assert mh._concentrix_row({
            "title": "Customer Service Representative", "locationsText": loc,
            "externalPath": "/x_R1",
        }) is None, loc


def test_concentrix_usa_but_senior_is_dropped():
    assert mh._concentrix_row({
        "title": "Principal Architect: AI & GCP Agentic Stack",
        "locationsText": "USA Work at Home", "externalPath": "/x_R2",
    }) is None


def test_concentrix_multi_location_without_us_signal_is_dropped():
    # Aggregate 'N Locations' postings carry no US signal in locationsText → dropped.
    assert mh._concentrix_row({
        "title": "Customer Service Representative", "locationsText": "13 Locations",
        "externalPath": "/x_R3",
    }) is None


# ---- category: insurance/healthcare "Rep" (not just "Representative") -----------

def test_insurance_rep_categorizes_but_bare_rep_does_not():
    # High-volume BPO enrollment roles are titled "...Insurance Rep", not "Representative".
    assert mh.categorize("Licensed Health Insurance Rep (Remote)") == "customer_support"
    assert mh.categorize("Health Insurance Representative") == "customer_support"
    # bare "Rep" outside the healthcare/insurance/financial bucket must NOT leak in
    assert mh.categorize("Sales Rep") is None
    assert mh.categorize("Legal Rep") is None
    # senior guard still wins
    assert mh.categorize("Senior Insurance Rep") is None
