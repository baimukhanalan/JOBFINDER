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


# ---- Concentrix jobs.concentrix.com jdq feed (_cnx_jdq_row, the real US frontline) -----------
# The corporate Workday board (external_global) is global/senior — only ~1 US remote entry role.
# The genuine US work-at-home frontline is on the first-party jdq feed, which aggregates BOTH the
# Workday reqs AND the Talkpush frontline campaigns. `_cnx_jdq_row` decodes one row (network-free).

def _jdq(**kw):
    base = {"job_title": "Customer Service Rep – Healthcare (Remote)", "remote_type": "fully_remote",
            "ats_external_id": "US-req-abc", "campaign_id": "req-abc", "id": "2100",
            "city": "Work At Home", "state": "Work At Home", "country": "United States of America",
            "apply_url": "https://concentrix.crew.talkpush.com/apply/concentrix/TOKEN",
            "job_type": "full_time", "created_at": "2026-08-21T18:24:25.585Z", "long_description": ""}
    base.update(kw)
    return base


def test_cnx_jdq_talkpush_frontline_csr_is_kept():
    # a genuine remote-US frontline CSR campaign on Talkpush, pay disclosed only in the description.
    row = mh._cnx_jdq_row(_jdq(
        job_title="Licensed Health Insurance Rep (Remote)",
        long_description="<p>The base salary range for this position is $21.00 – 23.00/hr.</p>"))
    assert row is not None
    assert row["source"] == "concentrix"
    assert row["source_id"] == "US-req-abc"
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True                          # country=USA is authoritative
    assert "talkpush.com" in row["apply_url"]                  # native Talkpush apply flow preserved
    assert row["salary_min"] == 21.0                           # hourly parsed from the prose


def test_cnx_jdq_workday_apply_url_is_kept_for_the_cron():
    # a jdq row whose apply_url is Workday keeps that host, so the Workday auto-apply cron
    # (apply_url ILIKE '%myworkdayjobs.com%') still picks it up.
    row = mh._cnx_jdq_row(_jdq(
        job_title="Health Insurance Representative", remote_type="fully_remote",
        ats_external_id="R1732661", city="", state="", street="USA Work at Home",
        apply_url="https://cnx.wd1.myworkdayjobs.com/en-US/external_global/job/x/apply"))
    assert row is not None
    assert row["source_id"] == "R1732661"
    assert "myworkdayjobs.com" in row["apply_url"]


def test_cnx_jdq_onsite_and_hybrid_are_dropped():
    assert mh._cnx_jdq_row(_jdq(remote_type="on_site")) is None
    assert mh._cnx_jdq_row(_jdq(remote_type="hybrid")) is None


def test_cnx_jdq_senior_remote_is_dropped():
    # a fully-remote but senior/corporate role → categorize() drops it (not a mass-hiring entry role).
    assert mh._cnx_jdq_row(_jdq(job_title="Principal Architect: AI & GCP Agentic Stack")) is None
    assert mh._cnx_jdq_row(_jdq(job_title="Director, Sales, B2B Sales Practice")) is None


def test_cnx_jdq_blank_remote_type_falls_back_to_text_scan():
    # remote_type absent → decide remote from the title/location text.
    kept = mh._cnx_jdq_row(_jdq(job_title="Customer Service Representative (Remote)", remote_type=None))
    assert kept is not None and kept["category"] == "customer_support"
    dropped = mh._cnx_jdq_row(_jdq(job_title="Customer Service Representative", remote_type=None,
                                   city="Frisco", state="Texas"))
    assert dropped is None                                      # no remote signal anywhere → drop


def test_cnx_jdq_missing_id_is_dropped():
    assert mh._cnx_jdq_row(_jdq(ats_external_id=None, campaign_id=None, id=None)) is None
# ---- Healthcare payers on Workday CxS (Elevance/Highmark/Sagility share _workday_row) -----------
# These tenants encode remote in the TITLE ("100% Virtual") and/or a "Working at Home" LOCATION
# facet (assume_remote), not the loc/path text — so the collector opts into title_remote +
# remote_location_facet. us_confirmed=True (US-only employers, no country facet).

def test_workday_elevance_title_remote_virtual_is_kept():
    # Elevance posts a remote member-services role at a PHYSICAL office with "100% Virtual" only in
    # the title. title_remote catches it (the loc/path have no remote word).
    row = mh._workday_row(
        {"locationsText": "TN-NASHVILLE, 501 GREAT CIRCLE RD",
         "title": "Patient Enrollment Specialist I (100% Virtual)",
         "bulletFields": ["JR203233"],
         "externalPath": "/job/TN-NASHVILLE-501-GREAT-CIRCLE-RD/Patient-Enrollment-Specialist-I_JR203233"},
        "elevance", "Elevance Health", "elevancehealth.wd1.myworkdayjobs.com", "ANT",
        us_confirmed=True, title_remote=True)
    assert row is not None
    assert row["source_id"] == "JR203233"
    assert row["us_eligible"] is True
    assert row["apply_url"].startswith("https://elevancehealth.wd1.myworkdayjobs.com/en-US/ANT")


def test_workday_title_remote_default_off_is_dropped():
    # Regression guard: the SAME row without title_remote (the existing callers' behaviour) is
    # dropped — a title-only remote signal never leaks into Concentrix/CVS/Centene/Cigna.
    j = {"locationsText": "TN-NASHVILLE, 501 GREAT CIRCLE RD",
         "title": "Patient Enrollment Specialist I (100% Virtual)",
         "bulletFields": ["JR203233"], "externalPath": "/job/TN-NASHVILLE/x_JR203233"}
    assert mh._workday_row(j, "elevance", "Elevance Health",
                           "elevancehealth.wd1.myworkdayjobs.com", "ANT", us_confirmed=True) is None


def test_workday_assume_remote_multiloc_is_kept():
    # A multi-location Highmark remote job: locationsText "51 Locations", no remote word in text.
    # assume_remote (the caller applied a remote LOCATION facet) keeps it; categorize matches
    # "Community Health Worker" (member-services entry).
    row = mh._workday_row(
        {"locationsText": "51 Locations", "title": "Community Health Worker - Medicaid",
         "bulletFields": ["J286859"], "externalPath": "/job/51-Locations/Community-Health-Worker_J286859"},
        "highmark", "Highmark Health", "highmarkhealth.wd1.myworkdayjobs.com", "highmark",
        us_confirmed=True, assume_remote=True)
    assert row is not None
    assert row["source_id"] == "J286859"
    # Without assume_remote (and no title_remote) the same multi-loc row has no remote text -> dropped.
    assert mh._workday_row(
        {"locationsText": "51 Locations", "title": "Community Health Worker - Medicaid",
         "bulletFields": ["J286859"], "externalPath": "/job/51-Locations/Community-Health-Worker_J286859"},
        "highmark", "Highmark Health", "highmarkhealth.wd1.myworkdayjobs.com", "highmark",
        us_confirmed=True) is None


def test_workday_assume_remote_onsite_title_is_dropped():
    # Sagility mislabels an onsite role onto a "Work@Home" facet — "(Onsite)" in the title + no real
    # remote text signal => rejected even under assume_remote.
    assert mh._workday_row(
        {"locationsText": "El Paso, TX", "title": "Care Advocate (Onsite) - El Paso, TX",
         "bulletFields": ["REQ-003714"], "externalPath": "/job/El-Paso/Care-Advocate_REQ-003714"},
        "sagility", "Sagility", "sagility.wd1.myworkdayjobs.com", "SagilityUSA",
        us_confirmed=True, assume_remote=True) is None


def test_workday_sagility_wfh_title_is_kept():
    # Sagility's loc "Work@Home USA" isn't matched by _is_remote ('@' != 'at'), but the TITLE says
    # "Work from Home" -> title_remote keeps it; source_id from bulletFields.
    row = mh._workday_row(
        {"locationsText": "Work@Home USA",
         "title": "Work from Home: Customer Service Representative (Healthcare)",
         "bulletFields": ["REQ-026850"], "externalPath": "/job/WorkHome-USA/CSR_REQ-026850"},
        "sagility", "Sagility", "sagility.wd1.myworkdayjobs.com", "SagilityUSA",
        us_confirmed=True, title_remote=True)
    assert row is not None
    assert row["source_id"] == "REQ-026850"
    assert row["category"] == "customer_support"


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


# ---- us_eligible: non-US remote must NOT leak in on the bare "remote" token --------
# _US_SPECIFIC (explicit US) is checked first, then a non-US region-lock ANYWHERE (rejects before
# the "remote" allow), then the generic anywhere/worldwide/global/bare-remote allow.

def test_us_eligible_rejects_non_us_remote():
    # every one of these used to return True because the broad allow (which included "remote")
    # was tested FIRST, and the non-US regex was `^`-anchored (so it missed a trailing lock).
    assert mh.us_eligible("India (Remote)") is False
    assert mh.us_eligible("Philippines, Remote") is False
    assert mh.us_eligible("EMEA remote") is False
    assert mh.us_eligible("Remote UK") is False           # the real remoteok leak (trailing lock)
    assert mh.us_eligible("Remote - Europe") is False
    assert mh.us_eligible("Canada") is False
    assert mh.us_eligible("Latin America (Remote)") is False


def test_us_eligible_keeps_us_remote():
    assert mh.us_eligible("") is True                     # unspecified → assume open
    assert mh.us_eligible("Remote") is True               # bare remote, no country → open
    assert mh.us_eligible("Anywhere") is True
    assert mh.us_eligible("Worldwide") is True
    assert mh.us_eligible("Remote, USA") is True
    assert mh.us_eligible("Remote - United States") is True
    assert mh.us_eligible("Remote (US)") is True
    assert mh.us_eligible("Remote - North America") is True
    assert mh.us_eligible("US or Canada") is True         # explicit US present → still eligible


def test_mk_row_non_us_remote_marks_ineligible():
    # a full row is still built (categorize passes), but flagged us_eligible=False so the
    # us_only=True collect filter drops it (the remoteok "Remote UK" row that used to leak).
    row = mh._mk_row("remoteok", "1", "Acme", "Customer Service Representative", "Remote UK", "u")
    assert row is not None
    assert row["us_eligible"] is False


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


# ---- Transcom (classic Avature portal) ------------------------------------------
# SearchJobs cards give title / location / jobId / a description snippet. US+remote is enforced
# client-side (a global BPO board); apply_url is on apply.careers.transcom.com (NOT *.avature.net)
# so the Maximus %avature% apply cron never touches it.

def _tc_url(jid="13462"):
    return f"https://apply.careers.transcom.com/en_US/careers/JobDetail/x/{jid}"


def test_transcom_us_wfh_is_kept():
    row = mh._transcom_row("13462", "Customer Service Advisor - Work-from-Home - US",
                           "United States", _tc_url(), "100% REMOTE - Work-at-Home role")
    assert row is not None
    assert row["source"] == "transcom"
    assert row["source_id"] == "13462"
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True
    assert row["auto_status"] == "needs_laptop"       # collect-first: not a wired auto lane (no «Авто» badge)
    assert "avature" not in row["apply_url"]          # must not trip the Maximus %avature% lane


def test_transcom_offshore_remote_is_dropped():
    assert mh._transcom_row("1", "Customer Service Representative", "Philippines",
                            _tc_url("1"), "Remote role in Manila") is None


def test_transcom_us_onsite_is_dropped():
    # No remote signal in title / location / description → dropped even though US.
    assert mh._transcom_row("2", "Customer Service Representative", "United States",
                            _tc_url("2"), "On-site role in our contact centre") is None


def test_transcom_senior_us_remote_is_dropped():
    assert mh._transcom_row("3", "Senior Manager, Remote Operations", "United States",
                            _tc_url("3"), "Remote") is None


def test_transcom_remote_from_description_is_kept():
    # Remote signalled only in the description snippet still counts (title/loc are neutral).
    row = mh._transcom_row("4", "Customer Care Representative", "United States", _tc_url("4"),
                           "This is a 100% work from home position")
    assert row is not None
    assert row["category"] == "customer_support"


def test_transcom_missing_id_or_title_dropped():
    assert mh._transcom_row("", "Customer Service Rep", "United States", _tc_url()) is None
    assert mh._transcom_row("5", "", "United States", _tc_url("5")) is None


# ---- Percepta (Taleo faceted REST) ----------------------------------------------
# A requisition's `column` array holds [title, locations-json]; linkedColumn indexes the title,
# locationsColumns the location column(s) whose value is a JSON array of Taleo codes.

def _pc(title, codes, jid="2449908", linked=0, loccols=(1,)):
    import json
    col = [""] * (max([linked] + list(loccols)) + 1)
    col[linked] = title
    for i in loccols:
        col[i] = json.dumps(codes)
    return {"jobId": jid, "column": col, "linkedColumn": linked, "locationsColumns": list(loccols)}


def test_percepta_us_remote_is_kept():
    row = mh._percepta_row(_pc("Bilingual French Customer Care Representative - Remote",
                               ["US-MI-Dearborn"]))
    assert row is not None
    assert row["source"] == "percepta"
    assert row["source_id"] == "2449908"
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True
    assert row["auto_status"] == "needs_laptop"       # collect-first: not a wired auto lane (no «Авто» badge)
    assert "Dearborn, MI, United States" in row["location_raw"]
    assert row["apply_url"].startswith(
        "https://percepta.taleo.net/careersection/10300/jobdetail.ftl?job=2449908")


def test_percepta_us_onsite_is_dropped():
    # A site-based CSR req (no remote word) is dropped by the remote-only rule.
    assert mh._percepta_row(_pc("Customer Service Representative", ["US-FL-Melbourne"])) is None


def test_percepta_offshore_remote_is_dropped():
    assert mh._percepta_row(
        _pc("Customer Service Representative - Remote", ["GB-ENG-Daventry"], jid="9")) is None


def test_percepta_senior_us_remote_is_dropped():
    assert mh._percepta_row(_pc("Senior Team Leader - Remote", ["US-FL-Melbourne"], jid="10")) is None


def test_percepta_empty_column_dropped():
    assert mh._percepta_row(
        {"jobId": "1", "column": [], "linkedColumn": 0, "locationsColumns": [1]}) is None


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


# ---- Foundever (SuccessFactors / Jobs2Web results table) ------------------------
# Location format is "<workplace>, <city|Any Location>, <ISO2>" — the LAST comma-token is the
# country code and a leading "Remote"/"Virtual" is the remote signal (both read off the string).

def test_foundever_us_remote_is_kept():
    row = mh._foundever_row("1232025900", "Bilingual Spanish Customer Service Associate",
                            "Remote, Any Location, US",
                            "/job/Remote-Bilingual-Spanish-Customer-Service-Associate-Any/1232025900/",
                            "Sep 17, 2026")
    assert row is not None
    assert row["source"] == "foundever"
    assert row["source_id"] == "1232025900"
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True
    assert row["apply_url"] == ("https://jobs.foundever.com/job/"
                                "Remote-Bilingual-Spanish-Customer-Service-Associate-Any/1232025900/")
    assert row["posted_at"] > 0


def test_foundever_state_coded_remote_is_kept():
    # a state-tagged remote row: "Remote, Mississippi, US" (misspelled cities happen too)
    row = mh._foundever_row("1413906900", "Remote Licensed Customer Service Representative",
                            "Remote, Mississippi, US", "/job/x/1413906900/")
    assert row is not None
    assert row["us_eligible"] is True
    assert row["category"] == "customer_support"


def test_foundever_full_country_name_is_kept():
    # some rows carry the full country name instead of the ISO2 code
    row = mh._foundever_row("1", "Customer Service Associate - Remote",
                            "Remote, Any Location, United States of America", "/job/x/1/")
    assert row is not None
    assert row["us_eligible"] is True


def test_foundever_onsite_us_is_dropped():
    # a physical US city with no remote workplace token → not remote → dropped
    assert mh._foundever_row("2", "Customer Service Representative",
                             "Las Vegas, Nevada, US", "/job/x/2/") is None


def test_foundever_non_us_remote_is_dropped():
    assert mh._foundever_row("3", "Customer Service Associate",
                             "Remote, Any Location, IN", "/job/x/3/") is None      # India
    assert mh._foundever_row("4", "Bilingual Customer Service Associate",
                             "Remote, Any Province, CA", "/job/x/4/") is None      # Canada


def test_foundever_senior_and_dev_us_remote_are_dropped():
    assert mh._foundever_row("5", "VP Global Operations", "Remote, Any Location, US", "/job/x/5/") is None
    assert mh._foundever_row("6", "Director Procurement - Technology Tower Lead",
                             "Remote, Texas, US", "/job/x/6/") is None
    assert mh._foundever_row("7", "Conversational AI Engineer",
                             "Remote, Any Location, US", "/job/x/7/") is None


def test_foundever_missing_id_or_title_is_dropped():
    assert mh._foundever_row(None, "Customer Service Representative", "Remote, Any Location, US", "/x") is None
    assert mh._foundever_row("8", "", "Remote, Any Location, US", "/x") is None


def test_foundever_date_parse():
    assert mh._foundever_date("Sep 2, 2026") > 0
    assert mh._foundever_date("") == 0
    assert mh._foundever_date("garbage") == 0


def test_foundever_country_and_us_helpers():
    assert mh._foundever_country("Remote, Any Location, US") == "US"
    assert mh._foundever_country("Cairo, Cairo, Egypt, EG") == "EG"
    assert mh._foundever_is_us("Remote, Mississippi, US") is True
    assert mh._foundever_is_us("Remote, Any Location, United States of America") is True
    assert mh._foundever_is_us("Remote, Any Location, GB") is False


def test_foundever_parse_extracts_rows():
    # the exact results-table row shape (td.colTitle / td.colLocation / span.jobDate)
    html = """
    <table><tbody>
      <tr class="data-row">
        <td class="colTitle"><span class="jobTitle"><a class="jobTitle-link"
            href="/job/Remote-Customer-Service-Associate-Any/999888/">Customer Service Associate - Remote</a></span>
          <div class="jobdetail-phone"><span class="jobDate visible-phone">Sep 2, 2026</span></div></td>
        <td class="colLocation"><span class="jobLocation">Remote, Any Location, US</span></td>
        <td class="colDepartment"><span class="jobDepartment">Customer Service</span></td>
      </tr>
    </tbody></table>"""
    parsed = mh._foundever_parse(html)
    assert len(parsed) == 1
    jid, title, loc, href, date = parsed[0]
    assert jid == "999888"
    assert title == "Customer Service Associate - Remote"
    assert loc == "Remote, Any Location, US"
    assert date == "Sep 2, 2026"
    # and it round-trips through the row builder
    row = mh._foundever_row(jid, title, loc, href, date)
    assert row is not None and row["category"] == "customer_support"


# ---- Gainwell Technologies (SuccessFactors RMK, same table as Foundever) --------
# Location format is "<city>, <state-code>, US, <zip>" (e.g. "Any city, MT, US, 99999") — US comes
# from the country token in the MIDDLE (not the last, which is the ZIP), and the REMOTE signal is in
# the TITLE ("... - Remote MT", "Remote, ...") not the location. `_gainwell_row` reflects both.

def test_gainwell_us_remote_from_title_is_kept():
    row = mh._gainwell_row("1426420500", "Healthcare Contact Center Representative - Remote U.S.",
                           "Any city, WI, US, 99999",
                           "/job/Any-city-Healthcare-Contact-Center-Representative-Remote-U_S_-WI-99999/1426420500/",
                           "Sep 3, 2026")
    assert row is not None
    assert row["source"] == "gainwell"
    assert row["source_id"] == "1426420500"
    assert row["company"] == "Gainwell Technologies"
    assert row["company_key"] == "gainwelltechnologies"      # _slug of the company name
    assert row["category"] == "customer_support"
    assert row["auto_status"] == "auto"
    assert row["us_eligible"] is True
    assert row["apply_url"] == (
        "https://jobs.gainwelltechnologies.com/job/"
        "Any-city-Healthcare-Contact-Center-Representative-Remote-U_S_-WI-99999/1426420500/")
    assert row["posted_at"] > 0


def test_gainwell_data_entry_remote_is_kept():
    row = mh._gainwell_row("1401833100", "Healthcare Data Entry Specialist - Remote MT",
                           "Any city, MT, US, 99999", "/job/x/1401833100/")
    assert row is not None
    assert row["category"] == "data_entry"
    assert row["us_eligible"] is True


def test_gainwell_city_state_remote_in_title_is_kept():
    # a physical home-office city in the location, remote signalled only in the title
    row = mh._gainwell_row("1366764400", "Call Center Representative (Healthcare) - Baton Rouge, LA - Remote",
                           "Baton Rouge, LA, US, 70809-0204", "/job/x/1366764400/")
    assert row is not None
    assert row["category"] == "customer_support"


def test_gainwell_onsite_no_remote_token_is_dropped():
    # a US location with NO remote token in the title → not remote → dropped
    assert mh._gainwell_row("2", "Call Center Representative (Healthcare)",
                            "Baton Rouge, LA, US, 70809-0204", "/job/x/2/") is None


def test_gainwell_non_us_is_dropped():
    assert mh._gainwell_row("3", "Customer Service Representative - Remote",
                            "Any city, ON, CA, A1A1A1", "/job/x/3/") is None      # Canada
    assert mh._gainwell_row("4", "Customer Service Representative - Remote",
                            "Bangalore, KA, IN, 560001", "/job/x/4/") is None      # India


def test_gainwell_senior_and_clinical_are_dropped():
    # Gainwell posts these alongside its CSR pipeline; categorize() must drop them
    assert mh._gainwell_row("5", "Provider Enrollment Team Lead Remote U.S.",
                            "Any city, DE, US, 99999", "/job/x/5/") is None       # Lead
    assert mh._gainwell_row("6", "Certified Senior Pharmacy Technician - Remote US",
                            "Any city, WV, US, 99999", "/job/x/6/") is None       # Senior/clinical
    assert mh._gainwell_row("7", "Drug Rebate Analyst - Remote West Virginia",
                            "Any city, WV, US, 99999", "/job/x/7/") is None       # not a CSR bucket


def test_gainwell_missing_id_or_title_is_dropped():
    assert mh._gainwell_row(None, "Call Center Representative - Remote",
                            "Any city, TX, US, 99999", "/x") is None
    assert mh._gainwell_row("8", "", "Any city, TX, US, 99999", "/x") is None


def test_gainwell_is_us_helper():
    assert mh._gainwell_is_us("Any city, MT, US, 99999") is True
    assert mh._gainwell_is_us("Baton Rouge, LA, US, 70809-0204") is True
    assert mh._gainwell_is_us("Charleston, WV, US, 25311 +1 more…") is True
    assert mh._gainwell_is_us("Any city, ON, CA, A1A1A1") is False
    assert mh._gainwell_is_us("Bangalore, KA, IN, 560001") is False


def test_gainwell_parse_extracts_rows():
    # the results table is byte-identical to Foundever's (parser is shared)
    html = """
    <table><tbody>
      <tr class="data-row">
        <td class="colTitle"><span class="jobTitle"><a class="jobTitle-link"
            href="/job/Any-city-Healthcare-Call-Center-Representative-Remote-MO-99999/1421281000/">Healthcare Call Center Representative - Remote</a></span>
          <div class="jobdetail-phone"><span class="jobDate visible-phone">Sep 14, 2026</span></div></td>
        <td class="colLocation"><span class="jobLocation">Any city, MO, US, 99999</span></td>
        <td class="colDepartment"><span class="jobDepartment">Operations</span></td>
      </tr>
    </tbody></table>"""
    parsed = mh._gainwell_parse(html)
    assert len(parsed) == 1
    jid, title, loc, href, date = parsed[0]
    assert jid == "1421281000"
    assert loc == "Any city, MO, US, 99999"
    row = mh._gainwell_row(jid, title, loc, href, date)
    assert row is not None and row["category"] == "customer_support"


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


# ---- Randstad USA (first-party search API, isRemote server filter) --------------
# lobId 1027 / "Randstad Careers" = Randstad hiring its OWN staff (routes to workgr8) — dropped;
# the placement lobs (308/4/337) apply natively at randstadusa.com.

def _rs(title, *, lob=308, lobname="Randstad Office and Administration", remote=True,
        city="Newark", st="DE", sal=None, apply=None, ats="AB_5065372", created=1789927309565):
    return {"isRemote": remote, "lobId": lob, "lobName": lobname, "atsReference": ats,
            "id": "6aadb42eed8b3140f5d88a74", "title": title,
            "jobLocation": {"city": city, "stateAbbreviation": st},
            "salary": sal or {"type": "per hour", "currency": "USD", "min": 19.99, "max": 20},
            "employmentType": "Full-time", "createdDate": created,
            "applyUrl": apply or f"https://www.randstadusa.com/jobs/apply/{lob}/{ats}/"}


def test_randstad_remote_placement_is_kept():
    row = mh._randstad_row(_rs("Remote Customer Service Representative"))
    assert row is not None
    assert row["source"] == "randstad"
    assert row["source_id"] == "AB_5065372"
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True
    assert row["salary_raw"] == "$19.99–$20/hr"
    assert row["apply_url"] == "https://www.randstadusa.com/jobs/apply/308/AB_5065372/"
    assert row["posted_at"] == 1789927309  # epoch MILLIS → seconds


def test_randstad_internal_hire_lob_is_dropped():
    # lobId 1027 = "Randstad Careers" (internal hire → workgr8.com apply), not a placement
    assert mh._randstad_row(_rs(
        "Customer Service Representative", lob=1027, lobname="Randstad Careers",
        apply="https://randstadnorthamerica.workgr8.com/jobs/52971/x/apply")) is None


def test_randstad_workgr8_apply_is_dropped():
    # belt-and-suspenders: any workgr8 apply host is an internal-hire req
    assert mh._randstad_row(_rs(
        "Customer Service Representative", lob=308,
        apply="https://randstadnorthamerica.workgr8.com/jobs/1/x/apply")) is None


def test_randstad_non_remote_is_dropped():
    assert mh._randstad_row(_rs("Customer Service Representative", remote=False)) is None


def test_randstad_senior_and_nonmass_are_dropped():
    assert mh._randstad_row(_rs("Senior Customer Success Manager")) is None
    assert mh._randstad_row(_rs("Staff Accountant")) is None   # not a mass-hiring category


def test_randstad_nationwide_and_annual_salary():
    # a nationwide-remote row carries city 'United States'; an annual salary range is stored raw
    row = mh._randstad_row(_rs("Data Entry Clerk", city="United States", st="",
                               sal={"type": "per year", "min": 50000, "max": 70000}))
    assert row is not None
    assert row["location_raw"] == "Remote, United States"
    assert row["category"] == "data_entry"
    assert row["salary_raw"] == "$50000–$70000/yr"


# ---- ManpowerGroup: Manpower + Experis (shared searchjobs API; title-first remote) -----------
# No reliable server remote field: remote decided from the TITLE (else an explicit desc signal),
# with a hard `hybrid` veto (a blank-location role was actually hybrid). Pay from the description.

def _mg(title, desc="", loc="Laredo, TX", jid="5892012", etype="Temporary",
        url="/en/job/sales/remote-call-center-representative-/5892012"):
    return {"jobID": jid, "jobTitle": title, "publicDescription": desc, "jobLocation": loc,
            "jobURL": url, "employmentType": etype, "publishfromDate": "2026-09-18T03:53:51Z"}


def test_manpower_remote_in_title_is_kept():
    row = mh._manpowergroup_row(
        _mg("Remote Call Center Representative",
            desc="<p>Political Survey Agents... Pay: $13.00/hour... Location: Remote...</p>"),
        "manpower", "Manpower", "www.manpower.com")
    assert row is not None
    assert row["source"] == "manpower"
    assert row["source_id"] == "5892012"
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True
    assert row["apply_url"] == "https://www.manpower.com/en/job/sales/remote-call-center-representative-/5892012"
    assert row["salary_min"] == 13.0          # parsed from the description prose


def test_manpower_hybrid_is_dropped():
    # a blank-location "Service Desk Analyst" that is actually HYBRID must NOT be kept
    assert mh._manpowergroup_row(
        _mg("Service Desk Analyst", loc="",
            desc="Columbus, OH Hybrid (2 days onsite and 3 days remote)... Pay Range: $21/hr"),
        "experis", "Experis", "www.experis.com") is None


def test_manpower_remote_only_in_description_is_kept():
    row = mh._manpowergroup_row(
        _mg("Customer Service Representative", loc="",
            desc="This is a fully remote position. Pay $17 per hour."),
        "manpower", "Manpower", "www.manpower.com")
    assert row is not None
    assert row["category"] == "customer_support"


def test_manpower_onsite_no_remote_signal_is_dropped():
    assert mh._manpowergroup_row(
        _mg("Customer Service Representative", loc="Dallas, TX",
            desc="Onsite role in our Dallas office."),
        "manpower", "Manpower", "www.manpower.com") is None


def test_manpower_senior_remote_is_dropped():
    assert mh._manpowergroup_row(
        _mg("Senior Remote Call Center Manager", desc="fully remote"),
        "manpower", "Manpower", "www.manpower.com") is None


def test_experis_remote_help_desk_is_kept():
    row = mh._manpowergroup_row(
        _mg("Remote Technical Support Specialist", jid="409326",
            desc="Fully remote contract role.", url="/en/job/409326/remote-tech-support"),
        "experis", "Experis", "www.experis.com")
    assert row is not None
    assert row["source"] == "experis"
    assert row["apply_url"] == "https://www.experis.com/en/job/409326/remote-tech-support"


# ---- Adecco (sitemap discovery → per-job detail API) ----------------------------
# Remote + US + OPEN read off the structured detail JSON; categorize() enforces mass-hiring.

def _ad(name, *, remote=True, country="USA", status="OPEN", city="Albuquerque",
        state="New Mexico", smin=19.9, smax=19.9, scale="Hour", jid="US_EN_99_027406_2589657"):
    return {"jobName": name, "jobId": jid, "cityName": city, "stateName": state,
            "countryId": country, "isRemote": remote, "jobStatusId": status,
            "contractTypeTitle": "Contract/Temporary", "minsalary": smin, "maxSalary": smax,
            "salaryTimeScale": scale, "salaryCurrencySymbol": "$",
            "postedDate": "2026-08-25T21:37:56Z"}


def test_adecco_remote_us_cs_is_kept():
    url = "https://www.adecco.com/en-us/job-search/remote-csr/us_en_99_027406_2589657"
    row = mh._adecco_row(_ad("Remote Customer Service Representative"), url)
    assert row is not None
    assert row["source"] == "adecco"
    assert row["source_id"] == "US_EN_99_027406_2589657"
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True
    assert row["apply_url"] == url
    assert row["salary_raw"] == "$19.9/hr"


def test_adecco_non_remote_is_dropped():
    assert mh._adecco_row(_ad("Call Center Customer Service Rep", remote=False), "u") is None


def test_adecco_non_us_is_dropped():
    assert mh._adecco_row(_ad("Customer Service Associate", country="CAN"), "u") is None


def test_adecco_closed_is_dropped():
    assert mh._adecco_row(_ad("Customer Service Representative", status="CLOSED"), "u") is None


def test_adecco_senior_and_nonmass_are_dropped():
    assert mh._adecco_row(_ad("Senior Customer Success Manager"), "u") is None
    assert mh._adecco_row(_ad("Machine Operator"), "u") is None    # not a mass-hiring category


def test_adecco_annual_salary_raw():
    row = mh._adecco_row(_ad("Data Entry Clerk", smin=40000, smax=52000, scale="Year"), "u")
    assert row is not None
    assert row["salary_raw"] == "$40000–$52000/yr"


# ---- Robert Half (<rhcl-job-card> SSR markup; remote worksite subslot) ----------

def _rh_card(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser").select_one("rhcl-job-card")


_RH_CARD = """
<rhcl-job-card job-id="04130-0013510008-usen" variant="card">
  <a href="https://www.roberthalf.com/us/en/job/houston-tx/administrative-assistant/04130-0013510008-usen"
     slot="headline">Administrative Assistant</a>
  <ul slot="job-info">
    <li data-subslot="location">Houston, TX</li>
    <li data-subslot="worksite">remote</li>
    <li data-subslot="type">Temporary / Contract</li>
    <li data-subslot="salary"><span data-subslot="salary-min">20</span> -
      <span data-subslot="salary-max">22</span> <span data-subslot="salary-currency">USD</span> /
      <span data-subslot="salary-period">Hourly</span></li>
    <li data-subslot="date">2026-08-28T00:00:00Z</li>
  </ul>
</rhcl-job-card>"""


def test_roberthalf_remote_admin_is_kept():
    row = mh._rh_row(_rh_card(_RH_CARD))
    assert row is not None
    assert row["source"] == "roberthalf"
    assert row["source_id"] == "04130-0013510008-usen"
    assert row["category"] == "virtual_assistant"       # Administrative Assistant
    assert row["us_eligible"] is True
    assert row["salary_raw"] == "$20–$22/hr"
    assert row["apply_url"].endswith("/04130-0013510008-usen")
    assert row["posted_at"] > 0


def test_roberthalf_onsite_worksite_is_dropped():
    card = _rh_card(_RH_CARD.replace('data-subslot="worksite">remote', 'data-subslot="worksite">onsite'))
    assert mh._rh_row(card) is None


def test_roberthalf_data_entry_remote_is_kept():
    html = _RH_CARD.replace("Administrative Assistant", "Data Entry Clerk")
    row = mh._rh_row(_rh_card(html))
    assert row is not None
    assert row["category"] == "data_entry"


def test_roberthalf_senior_is_dropped():
    html = _RH_CARD.replace("Administrative Assistant", "Senior Accounting Manager")
    assert mh._rh_row(_rh_card(html)) is None


def test_roberthalf_annual_salary_raw():
    html = (_RH_CARD.replace('salary-min">20', 'salary-min">45000')
            .replace('salary-max">22', 'salary-max">55000')
            .replace('salary-period">Hourly', 'salary-period">Yearly'))
    row = mh._rh_row(_rh_card(html))
    assert row is not None
    assert row["salary_raw"] == "$45000–$55000/yr"


# ---- Hilton (Oracle Recruiting Cloud, same REST shape as Alorica) -----------------
# jobs.hilton.com is Oracle ORC (efet.fa.us2.oraclecloud.com, site CX_1). Remote is read off the
# STRUCTURED WorkplaceTypeCode (ORA_REMOTE keep; ORA_ON_SITE/ORA_HYBRID veto), US off
# PrimaryLocationCountry. Marriott, by contrast, is NOT ORC (it fronts Jibe) — no collector here.

def _hilton_req(**kw):
    d = {"Id": "220900", "Title": "Customer Care Coordinator", "PrimaryLocation": "United States",
         "PrimaryLocationCountry": "US", "WorkplaceTypeCode": "ORA_REMOTE",
         "PostedDate": "2026-09-20"}
    d.update(kw)
    return d


def test_hilton_us_remote_entry_is_kept():
    row = mh._hilton_row(_hilton_req())
    assert row is not None
    assert row["source"] == "hilton"
    assert row["source_id"] == "220900"
    assert row["company"] == "Hilton"
    assert row["company_key"] == "hilton"
    assert row["category"] == "customer_support"
    assert row["auto_status"] == "needs_laptop"       # ORC tenant, collect-first (verify pass pending)
    assert row["us_eligible"] is True
    assert row["apply_url"] == (
        "https://efet.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/220900")
    assert row["posted_at"] > 0


def test_hilton_onsite_and_hybrid_vetoed():
    # the structured code wins — an ON_SITE/HYBRID req is dropped even if the title says "Remote".
    assert mh._hilton_row(_hilton_req(WorkplaceTypeCode="ORA_ON_SITE",
                                      Title="Customer Care Coordinator (Remote)")) is None
    assert mh._hilton_row(_hilton_req(WorkplaceTypeCode="ORA_HYBRID")) is None


def test_hilton_non_us_dropped():
    assert mh._hilton_row(_hilton_req(PrimaryLocationCountry="GB",
                                      PrimaryLocation="London, United Kingdom")) is None


def test_hilton_senior_corporate_remote_dropped_by_categorize():
    # the real current inventory: US ORA_REMOTE but corporate/senior -> categorize() drops it.
    for t in ("Director Sales and Marketing (Remote)", "Lead DevOps Engineer",
              "Senior Manager Service Improvement Program", "Recruiter, Recruitment Operations Center"):
        assert mh._hilton_row(_hilton_req(Title=t)) is None


def test_hilton_remote_via_title_fallback_when_code_absent():
    # a tenant that leaves WorkplaceTypeCode blank still passes when the title/location says remote.
    row = mh._hilton_row(_hilton_req(WorkplaceTypeCode="", Title="Customer Care Coordinator - Remote"))
    assert row is not None
    assert row["category"] == "customer_support"
    # …but a blank code with no remote signal anywhere is NOT assumed remote.
    assert mh._hilton_row(_hilton_req(WorkplaceTypeCode="",
                                      PrimaryLocation="McLean, VA, United States")) is None
