"""Unit tests for the mass-hiring source row-decision helpers (no network).

These pin the per-job decision for every connector so the fetchers can't silently regress.
Connectors added/fixed 2026-08-28 (live-probed via subagents): Amazon re-diagnosed, and
Teleperformance / TTEC / CVS Health / Sutherland / Working Solutions wired in.
"""
from backend.tools import mass_hiring as mh


# ---- Amazon --------------------------------------------------------------------
# A remote posting is marked by city.startswith("Virtual"); US-eligibility is country_code=="USA".
# normalized_location is 'USA' OR a state-tagged 'Texas, USA' (must NOT be exact-matched to 'USA').

def test_amazon_us_virtual_is_kept():
    row = mh._amazon_row({
        "id": "1", "title": "Customer Service Representative",
        "city": "Virtual", "country_code": "USA", "normalized_location": "USA",
        "job_path": "/en/jobs/1",
    })
    assert row is not None
    assert row["source"] == "amazon"
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True


def test_amazon_state_tagged_virtual_is_kept():
    # The real mass-hiring CS roles are state-tagged: city "Virtual Location - Arizona",
    # normalized_location "Arizona, USA". The OLD exact `normalized_location in ('USA',)` dropped these.
    row = mh._amazon_row({
        "id": "9", "title": "Bilingual Technical Customer Support, Ring",
        "city": "Virtual Location - Arizona", "country_code": "USA",
        "normalized_location": "Arizona, USA", "url_next_step": "https://account.amazon.jobs/jobs/9/apply",
    })
    assert row is not None
    assert row["category"] == "customer_support"
    assert row["apply_url"].startswith("https://account.amazon.jobs")


def test_amazon_offshore_virtual_is_dropped():
    # Amazon's virtual CS is mostly offshore GBR/ZAF language moderation.
    assert mh._amazon_row({
        "id": "2", "title": "Customer Service Associate - Arabic Moderator",
        "city": "Virtual", "country_code": "ZAF", "normalized_location": "ZAF", "job_path": "/x",
    }) is None


def test_amazon_us_but_onsite_is_dropped():
    assert mh._amazon_row({
        "id": "3", "title": "Customer Service Representative",
        "city": "Seattle", "country_code": "USA", "normalized_location": "Washington, USA", "job_path": "/x",
    }) is None


def test_amazon_us_virtual_senior_is_dropped():
    assert mh._amazon_row({
        "id": "4", "title": "Senior Customer Service Manager",
        "city": "Virtual", "country_code": "USA", "normalized_location": "USA", "job_path": "/x",
    }) is None


# ---- Workday (CVS Health + Concentrix share _workday_row) -----------------------

def test_workday_cvs_state_code_wfh_is_kept():
    # CVS marks remote US with a 2-letter state prefix: "RI - Work from home" (us_eligible misses it,
    # _has_us_state catches it). source_id is the req number from bulletFields.
    row = mh._workday_row(
        {"locationsText": "RI - Work from home", "title": "Provider Customer Service Representative",
         "bulletFields": ["R1002362"], "externalPath": "/job/RI---Work-from-home/x_R1002362"},
        "cvshealth", "CVS Health", "cvshealth.wd1.myworkdayjobs.com", "CVS_Health_Careers")
    assert row is not None
    assert row["source_id"] == "R1002362"
    assert row["category"] == "customer_support"
    assert row["apply_url"].startswith("https://cvshealth.wd1.myworkdayjobs.com/en-US/CVS_Health_Careers")


def test_workday_concentrix_usa_wah_is_kept():
    row = mh._workday_row(
        {"locationsText": "USA Work at Home", "title": "Licensed Health Insurance Rep",
         "bulletFields": ["R1732661"], "externalPath": "/x_R1732661"},
        "concentrix", "Concentrix", "cnx.wd1.myworkdayjobs.com", "external_global")
    assert row is not None
    assert row["source_id"] == "R1732661"
    assert row["us_eligible"] is True


def test_workday_offshore_wah_is_dropped():
    assert mh._workday_row(
        {"locationsText": "PHL Work at Home", "title": "Customer Service Representative",
         "bulletFields": ["R1"], "externalPath": "/x_R1"},
        "concentrix", "Concentrix", "cnx.wd1.myworkdayjobs.com", "external_global") is None


def test_workday_onsite_is_dropped():
    assert mh._workday_row(
        {"locationsText": "Frisco, TX", "title": "Customer Service Representative",
         "bulletFields": ["R2"], "externalPath": "/x_R2"},
        "concentrix", "Concentrix", "cnx.wd1.myworkdayjobs.com", "external_global") is None


def test_workday_senior_wah_is_dropped():
    assert mh._workday_row(
        {"locationsText": "USA Work at Home", "title": "Principal Architect: Google Cloud CX",
         "bulletFields": ["R3"], "externalPath": "/x_R3"},
        "concentrix", "Concentrix", "cnx.wd1.myworkdayjobs.com", "external_global") is None


# ---- Teleperformance (Umbraco) --------------------------------------------------

def test_tp_us_wfh_is_kept():
    row = mh._tp_row({
        "externalId": "87136", "title": "Healthcare Customer Service Representative - Remote",
        "location": "Remote", "country": "United States", "workFromHome": "Yes",
        "url": "https://careersus-teleperformance.icims.com/jobs/87136/x/job",
    })
    assert row is not None
    assert row["source"] == "teleperformance"
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True


def test_tp_non_us_is_dropped():
    assert mh._tp_row({
        "externalId": "1", "title": "Customer Service Representative - Remote",
        "location": "Remote", "country": "Spain", "workFromHome": "Yes", "url": "u",
    }) is None


def test_tp_onsite_is_dropped():
    assert mh._tp_row({
        "externalId": "2", "title": "Customer Service Representative",
        "location": "TX", "country": "United States", "workFromHome": "No", "url": "u",
    }) is None


# ---- TTEC (title-based; location span is unreliable) ----------------------------

def test_ttec_remote_usa_is_kept():
    row = mh._ttec_row("93403411936", "Customer Service Representative – Remote in USA",
                       "/en/job/austin/csr-remote-in-usa/44028/93403411936")
    assert row is not None
    assert row["category"] == "customer_support"
    assert row["apply_url"] == "https://www.ttecjobs.com/en/job/austin/csr-remote-in-usa/44028/93403411936"


def test_ttec_remote_in_state_is_kept():
    row = mh._ttec_row("1", "Customer Service Representative – Remote in Virginia", "/x")
    assert row is not None


def test_ttec_offshore_remote_is_dropped():
    # The location span may say Philippines; title has a remote signal but NO US signal → drop.
    assert mh._ttec_row("2", "HealthCare Customer Service Representative - Remote", "/x") is None


def test_ttec_dev_and_nonmass_are_dropped():
    assert mh._ttec_row("3", "Data Engineer (Remote)", "/x") is None                 # dev
    assert mh._ttec_row("4", "Production Clerk - Remote in Virginia", "/x") is None   # not mass-hiring


def test_ttec_non_remote_is_dropped():
    assert mh._ttec_row("5", "Customer Service Representative - Austin, TX", "/x") is None


# ---- Sutherland (SmartRecruiters) -----------------------------------------------

def test_smartrecruiters_us_remote_is_kept():
    row = mh._smartrecruiters_row({
        "id": "744000145824589", "name": "Customer Service Representative - Temporary",
        "location": {"country": "us", "remote": True, "fullLocation": "Houston, TX, United States"},
        "releasedDate": "2026-08-26T19:51:03.176Z",
    }, "sutherland", "Sutherland")
    assert row is not None
    assert row["us_eligible"] is True
    assert row["apply_url"] == "https://jobs.smartrecruiters.com/Sutherland/744000145824589"


def test_smartrecruiters_us_onsite_is_dropped():
    assert mh._smartrecruiters_row({
        "id": "1", "name": "Customer Service Representative",
        "location": {"country": "us", "remote": False, "fullLocation": "Chesapeake, VA, United States"},
    }, "sutherland", "Sutherland") is None


def test_smartrecruiters_offshore_is_dropped():
    assert mh._smartrecruiters_row({
        "id": "2", "name": "Customer Service Representative",
        "location": {"country": "eg", "remote": True, "fullLocation": "Cairo, Egypt"},
    }, "sutherland", "Sutherland") is None


def test_smartrecruiters_senior_is_dropped():
    assert mh._smartrecruiters_row({
        "id": "3", "name": "Operations Director (Remote - US Base)",
        "location": {"country": "us", "remote": True, "fullLocation": "Rochester, NY, United States"},
    }, "sutherland", "Sutherland") is None


# ---- Working Solutions (Algolia) ------------------------------------------------

def test_working_solutions_us_is_kept():
    row = mh._ws_row({
        "id": 568821, "title": "Health Insurance Enrollment Representative, Customer Service - Remote",
        "country": ["United States"], "category": ["Customer Service"],
    })
    assert row is not None
    assert row["category"] == "customer_support"
    assert row["apply_url"] == "https://apply.workingsolutions.com/job/568821"


def test_working_solutions_canada_only_is_dropped():
    assert mh._ws_row({
        "id": 1, "title": "Customer Service Representative - Remote", "country": ["Canada"],
    }) is None


# ---- US-state / title helpers ---------------------------------------------------

def test_has_us_state():
    assert mh._has_us_state("RI - Work from home") is True
    assert mh._has_us_state("Work At Home-Texas") is True
    assert mh._has_us_state("PHL Work at Home") is False
    assert mh._has_us_state("USA Work at Home") is False   # handled by us_eligible, not this


def test_title_us():
    assert mh._title_us("CSR – Remote in USA") is True
    assert mh._title_us("CSR – Remote in Virginia") is True
    assert mh._title_us("CSR - Remote in Philippines") is False
    assert mh._title_us("Customer Service Representative - Remote") is False


# ---- Kelly (WP REST, proxied) ---------------------------------------------------

def test_kelly_us_remote_is_kept():
    row = mh._kelly_row({
        "id": 1, "link": "https://www.mykelly.com/job/10277091-x/", "date": "2026-08-20T00:00:00",
        "title": {"rendered": "Call Center Customer Service Representative"},
        "acf": {"remote": "1", "country_code": "US", "job_id": "10277091",
                "_job_location": "San Diego, CA, United States"},
    })
    assert row is not None
    assert row["source"] == "kelly"
    assert row["source_id"] == "10277091"
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True


def test_kelly_onsite_is_dropped():
    assert mh._kelly_row({
        "id": 2, "title": {"rendered": "Customer Service Representative"},
        "acf": {"remote": "0", "country_code": "US", "_job_location": "Troy, MI, United States"},
    }) is None


def test_kelly_non_us_remote_is_dropped():
    assert mh._kelly_row({
        "id": 3, "title": {"rendered": "Customer Service Representative"},
        "acf": {"remote": "1", "country_code": "CA", "geolocation_country": "Canada"},
    }) is None


def test_kelly_senior_is_dropped():
    assert mh._kelly_row({
        "id": 4, "title": {"rendered": "Senior Customer Success Manager"},
        "acf": {"remote": "1", "country_code": "US", "_job_location": "Remote, United States"},
    }) is None


# ---- Maximus (Avature) ----------------------------------------------------------

def _mx(title, classification="Customer Service & Call Center Careers", jid="42174",
        loc="United States"):
    return {"id": jid, "fields": {
        "schemaField_3_293_3": {"stringValue": title},
        "schemaField_3_481_3": {"stringValue": classification},
        "jobLocation": {"stringValue": loc, "jsonValue": {"country": {"name": "United States"}}},
        "postedDate": {"stringValue": "2026-08-25"}}}


def test_maximus_remote_csr_is_kept():
    row = mh._maximus_row(_mx("CSR II Operations (Temporary, Remote Lawrence KS)"),
                          "https://maximus.avature.net/careers/Job-Application?folderId=42174")
    assert row is not None
    assert row["source"] == "maximus"
    assert row["source_id"] == "42174"
    assert row["category"] == "customer_support"
    assert row["apply_url"].endswith("folderId=42174")


def test_maximus_onsite_is_dropped():
    # "On-Site" title, classification has no remote word → not remote → dropped.
    assert mh._maximus_row(_mx("CSR II Operations (On-Site Lawrence KS)")) is None


def test_maximus_remote_from_classification_is_kept():
    # Remote signalled only in the classification text still counts.
    row = mh._maximus_row(_mx("Customer Service Representative",
                              classification="Remote Customer Service & Call Center"))
    assert row is not None


def test_maximus_senior_remote_is_dropped():
    assert mh._maximus_row(_mx("Senior Manager, Remote Operations")) is None


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
