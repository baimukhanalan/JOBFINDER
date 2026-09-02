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


# ---- UnitedHealth (TalentBrew) + Humana (Phenom) --------------------------------

def test_talentbrew_row_is_kept():
    row = mh._talentbrew_row("unitedhealth", "UnitedHealth Group", "9001",
                             "Customer Service Representative", "/job/x/9001",
                             "https://careers.unitedhealthgroup.com")
    assert row is not None
    assert row["source"] == "unitedhealth"
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True
    assert row["apply_url"] == "https://careers.unitedhealthgroup.com/job/x/9001"


def test_talentbrew_empty_title_dropped():
    assert mh._talentbrew_row("unitedhealth", "UnitedHealth Group", "1", "", "/x",
                              "https://careers.unitedhealthgroup.com") is None


def test_humana_us_remote_is_kept():
    row = mh._humana_row({
        "jobId": "R-1", "title": "Inbound Contacts Representative",
        "country": "United States of America", "isRemote": "Yes", "city": "Remote",
        "cityStateCountry": "Remote, Indiana, United States of America",
        "applyUrl": "https://careers.humana.com/job/R-1"})
    assert row is not None
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True


def test_humana_non_us_is_dropped():
    assert mh._humana_row({
        "jobId": "R-2", "title": "Customer Service Representative",
        "country": "Philippines", "isRemote": "Yes", "city": "Remote"}) is None


def test_humana_onsite_is_dropped():
    assert mh._humana_row({
        "jobId": "R-3", "title": "Customer Service Representative",
        "country": "United States of America", "isRemote": "No", "city": "Louisville"}) is None


# ---- category: health-insurer entry roles + clinical drop -----------------------

def test_care_and_member_roles_categorize():
    assert mh.categorize("Care Coordinator II") == "customer_support"
    assert mh.categorize("Care Navigator") == "customer_support"
    assert mh.categorize("Member Advocate II") == "customer_support"
    assert mh.categorize("Correspondence Representative") == "customer_support"
    assert mh.categorize("Community Health Worker") == "customer_support"
    assert mh.categorize("Claims Research & Resolution Representative") == "customer_support"
    assert mh.categorize("Clinical Administrative Coordinator") == "customer_support"
    assert mh.categorize("Collections Representative") == "customer_support"


def test_clinical_and_senior_care_roles_are_dropped():
    assert mh.categorize("Care Manager RN") is None           # nurse (clinical)
    assert mh.categorize("Registered Nurse Care Coordinator") is None
    assert mh.categorize("Staff Pharmacist") is None
    assert mh.categorize("Appeals Medical Director") is None    # senior + clinical
    assert mh.categorize("Behavioral Health Therapist") is None
    assert mh.categorize("Care Management Director") is None     # senior


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


# ---- comp_type: stable fixed pay vs commission / percent-of-sales --------------

def test_comp_type_sales_category_is_variable():
    assert mh.comp_type("Sales Development Representative", "sales") == "variable"
    assert mh.comp_type("Inside Sales Associate", "sales") == "variable"


def test_comp_type_commission_title_is_variable_even_in_cs():
    # a commission signal in the title flips it regardless of category
    assert mh.comp_type("Customer Service Rep (base + commission)", "customer_support") == "variable"
    assert mh.comp_type("Retention Specialist — uncapped OTE", "customer_support") == "variable"
    assert mh.comp_type("Telesales Associate, 100% Commission", "customer_support") == "variable"


def test_comp_type_plain_support_is_fixed():
    assert mh.comp_type("Customer Service Representative - Remote", "customer_support") == "fixed"
    assert mh.comp_type("Care Coordinator", "customer_support") == "fixed"
    assert mh.comp_type("Virtual Assistant", "virtual_assistant") == "fixed"
    # "commission" as a substring of an unrelated word must not trip it (word-boundary)
    assert mh.comp_type("Commissions Analyst Support", "operations") == "fixed"


# ---- hourly pay normalization + estimate fallback ------------------------------

def test_to_hourly_normalizes_by_magnitude():
    assert mh.to_hourly(18) == 18                       # already hourly
    assert mh.to_hourly(0) is None
    assert mh.to_hourly(None) is None
    # monthly (Job Duck ~$1150/mo) -> hourly
    assert abs(mh.to_hourly(1150) - 1150 * 12 / 2080) < 1e-6
    # annual ($46,990) -> hourly ~$22.6
    assert abs(mh.to_hourly(46990) - 46990 / 2080) < 1e-6


def test_hourly_pay_prefers_posted_over_estimate():
    lo, hi, est = mh.hourly_pay({"category": "customer_support", "salary_min": 18, "salary_max": 24})
    assert (lo, hi, est) == (18.0, 24.0, False)
    # annual posted range normalizes to hourly, still not an estimate
    lo, hi, est = mh.hourly_pay({"category": "customer_support",
                                 "salary_min": 46990, "salary_max": 71385})
    assert est is False and lo < hi and 20 < lo < 40


def test_hourly_pay_falls_back_to_category_estimate():
    lo, hi, est = mh.hourly_pay({"category": "customer_support"})
    assert (lo, hi, est) == (15.0, 21.0, True)
    # a category with no estimate and no posted pay -> None
    assert mh.hourly_pay({"category": None}) is None


# ---- posted-wage prose parsing (TTEC discloses pay only in the detail page) -------

def test_parse_hourly_wage_ttec_starting_at():
    # the exact TTEC phrasing (job 518): a single starting rate, cents preserved
    lo, hi, raw = mh._parse_hourly_wage("Base hourly wage starting at $21.65.")
    assert lo == 21.65 and hi is None and raw == "$21.65/hr"


def test_parse_hourly_wage_range_per_hour():
    lo, hi, raw = mh._parse_hourly_wage("Pay range is $18.00 - $24.50 per hour, plus benefits.")
    assert lo == 18.0 and hi == 24.5
    lo, hi, _ = mh._parse_hourly_wage("Earn $17 to $20 an hour")
    assert (lo, hi) == (17.0, 20.0)


def test_parse_hourly_wage_bare_per_hour():
    lo, hi, _ = mh._parse_hourly_wage("This role pays $19.50 per hour.")
    assert lo == 19.5 and hi is None
    lo, hi, _ = mh._parse_hourly_wage("Compensation: $22/hr")
    assert lo == 22.0 and hi is None


def test_parse_hourly_wage_rejects_annual_and_bonus():
    # annual salary -> not hourly context, and out of the plausible hourly band
    assert mh._parse_hourly_wage("Salary of $45,000/year plus equity.") == (None, None, None)
    assert mh._parse_hourly_wage("Base salary $60,000 - $80,000 annually.") == (None, None, None)
    # a signing bonus mentioned near no hourly context is ignored
    assert mh._parse_hourly_wage("Enjoy a $5,000 signing bonus!") == (None, None, None)
    # empty / no money
    assert mh._parse_hourly_wage("") == (None, None, None)
    assert mh._parse_hourly_wage("Great remote role, apply today.") == (None, None, None)


# ---- mass_hiring_apply: work-city from the title (residence-screener coherence) ---

def test_city_from_title():
    from backend.tools import mass_hiring_apply as mha
    assert mha._city_from_title("CSR II Operations (Temporary, Remote Lawrence KS)") == "Lawrence, KS, United States"
    assert mha._city_from_title("CSR I Operations (Temporary, Remote McAllen, TX)") == "McAllen, TX, United States"
    assert mha._city_from_title("Bilingual CSR (Remote - New York, NY)") == "New York, NY, United States"
    assert mha._city_from_title("Fully Remote Customer Service Representative") == ""   # no city named
    assert mha._city_from_title("") == ""
