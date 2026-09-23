"""Network-free unit tests for backend/tools/connectors_seasonal.py.

Every `_<source>_row(...)` is a PURE decision (no network / no DB): we feed synthetic ATS-shaped
dicts and assert the two HARD RULES — `_is_remote`/location must be REMOTE, and `categorize()` must
return a mass-hiring ENTRY bucket — plus the seasonal / remote / US(+offshore) edge cases.
"""
from backend.tools import connectors_seasonal as cs


# --------------------------------------------------------------------------------------------------
# Jibe (Ulta)
# --------------------------------------------------------------------------------------------------
def test_ulta_remote_entry_kept():
    d = {"title": "Customer Care Representative - Work from Home",
         "full_location": "Remote, United States", "state": "", "country_code": "US",
         "req_id": "555", "apply_url": "https://cdcmcareers-ulta.icims.com/jobs/555/login"}
    row = cs._ulta_row(d)
    assert row and row["category"] == "customer_support"
    assert row["us_eligible"] is True
    assert row["source"] == "ulta" and row["source_id"] == "555"
    assert row["apply_url"].endswith("/555/login")


def test_ulta_state_remote_flag_kept():
    d = {"title": "Guest Services Contact Center Agent", "full_location": "Ohio",
         "state": "Remote", "country_code": "US", "req_id": "9"}
    row = cs._ulta_row(d)
    assert row and row["category"] == "customer_support"
    # apply_url falls back to the jibe host when none is supplied
    assert "ulta.jibeapply.com" in row["apply_url"]


def test_ulta_senior_engineer_dropped_even_if_remote():
    d = {"title": "Sr Data Engineer (Remote)", "full_location": "Bolingbrook, Illinois",
         "state": "Illinois", "country_code": "US", "req_id": "490486"}
    assert cs._ulta_row(d) is None            # _DEV / _NOT_MASS veto


def test_ulta_non_remote_store_dropped():
    d = {"title": "Beauty Advisor", "full_location": "Austin, Texas", "state": "Texas",
         "country_code": "US", "req_id": "1"}
    assert cs._ulta_row(d) is None            # not remote


def test_ulta_offshore_dropped():
    d = {"title": "Customer Service Representative - Work from Home",
         "full_location": "Remote, Philippines", "state": "", "country_code": "PH", "req_id": "2"}
    assert cs._ulta_row(d) is None            # non-US country code


def test_ulta_missing_title_or_id():
    assert cs._ulta_row({"title": "", "country_code": "US", "req_id": "1"}) is None
    assert cs._ulta_row({"title": "Customer Service Rep - Remote", "country_code": "US"}) is None
    assert cs._ulta_row("nope") is None


# --------------------------------------------------------------------------------------------------
# Oracle ORC (Macy's, site CX_1001)
# --------------------------------------------------------------------------------------------------
def test_macys_remote_entry_kept_and_site_fixed():
    j = {"Title": "Marketplace Seller Support Specialist (REMOTE)",
         "PrimaryLocation": "New York, NY, United States", "PrimaryLocationCountry": "US",
         "Id": "77", "PostedDate": "2026-09-20T00:00:00+0000"}
    row = cs._macys_row(j)
    assert row and row["category"] == "customer_support"
    assert row["source"] == "macys" and row["us_eligible"] is True
    assert "/sites/CX_1001/job/" in row["apply_url"]          # site rewritten, not CX_1
    assert "/sites/CX_1/job/" not in row["apply_url"]


def test_macys_non_us_dropped():
    j = {"Title": "Customer Service Representative (Remote)", "PrimaryLocation": "Manila",
         "PrimaryLocationCountry": "PH", "Id": "1"}
    assert cs._macys_row(j) is None


def test_macys_onsite_store_dropped():
    j = {"Title": "Retail Sales Associate", "PrimaryLocation": "Boston, MA, United States",
         "PrimaryLocationCountry": "US", "Id": "2"}
    assert cs._macys_row(j) is None                          # not remote


def test_macys_national_bare_country_remote_kept():
    # ORC treats a bare "United States" national posting as remote
    j = {"Title": "Customer Care Representative", "PrimaryLocation": "United States",
         "PrimaryLocationCountry": "US", "Id": "3"}
    row = cs._macys_row(j)
    assert row and row["category"] == "customer_support"


# --------------------------------------------------------------------------------------------------
# Workday (Nordstrom, title_remote)
# --------------------------------------------------------------------------------------------------
def test_nordstrom_remote_title_kept():
    j = {"title": "Customer Care Representative - Remote", "locationsText": "United States",
         "externalPath": "/job/United-States/Customer-Care-Rep_R-1", "bulletFields": ["R-1"]}
    row = cs._nordstrom_row(j)
    assert row and row["category"] == "customer_support"
    assert row["us_eligible"] is True and row["source"] == "nordstrom"
    assert "R-1" in row["apply_url"]


def test_nordstrom_store_role_dropped():
    j = {"title": "Barista - Specialty Coffee - Bellevue Square", "locationsText": "Bellevue, WA",
         "externalPath": "/job/Bellevue-WA/Barista_R-2", "bulletFields": ["R-2"]}
    assert cs._nordstrom_row(j) is None                      # not remote


def test_nordstrom_remote_but_senior_dropped():
    j = {"title": "Senior Engineer - Remote", "locationsText": "United States",
         "externalPath": "/job/United-States/Senior-Engineer_R-3", "bulletFields": ["R-3"]}
    assert cs._nordstrom_row(j) is None                      # _NOT_MASS/_DEV veto


# --------------------------------------------------------------------------------------------------
# iCIMS (IBEX)
# --------------------------------------------------------------------------------------------------
def test_ibex_wah_us_kept():
    row = cs._ibex_row("26589", "Customer Service Representative - Work at Home", "US",
                       "https://uscareers-ibex.icims.com/jobs/26589/csr/job?in_iframe=1")
    assert row and row["category"] == "customer_support"
    assert row["us_eligible"] is True and row["source"] == "ibex"
    assert row["apply_url"] == "https://uscareers-ibex.icims.com/jobs/26589/csr/job"  # query stripped


def test_ibex_title_us_state_kept_without_loc():
    row = cs._ibex_row("100", "Customer Service Representative - Work from Home in Texas", "", "")
    assert row and row["category"] == "customer_support"


def test_ibex_onsite_dropped():
    assert cs._ibex_row("2", "Sales Representative - Onsite", "US", "") is None


def test_ibex_offshore_dropped():
    # a WAH title but a non-US (Philippines) location and no US title signal → dropped
    assert cs._ibex_row("3", "Customer Service - Work at Home", "Philippines", "") is None


def test_ibex_missing_fields():
    assert cs._ibex_row("", "Customer Service - Remote", "US", "") is None
    assert cs._ibex_row("4", "", "US", "") is None


# --------------------------------------------------------------------------------------------------
# Greenhouse fintech (Mercury / Current / Rocket Money / LendingTree)
# --------------------------------------------------------------------------------------------------
def _gh_job(title, loc):
    return {"title": title, "location": {"name": loc}, "id": 1,
            "absolute_url": "https://job-boards.greenhouse.io/x/jobs/1", "updated_at": ""}


def test_mercury_remote_support_kept():
    row = cs._mercury_row(_gh_job("Customer Support Specialist", "San Francisco, CA; Remote"))
    assert row and row["category"] == "customer_support" and row["source"] == "mercury"


def test_current_remote_us_kept():
    row = cs._current_row(_gh_job("Client Experience Associate", "Remote (US)"))
    assert row and row["category"] == "customer_support" and row["source"] == "current"


def test_rocketmoney_remote_ops_kept():
    row = cs._rocketmoney_row(_gh_job("Operations Associate", "Remote (Eastern Time Zone)"))
    assert row and row["category"] == "operations" and row["company"] == "Rocket Money"


def test_lendingtree_call_center_kept():
    row = cs._lendingtree_row(_gh_job("Call Center Representative (Appointment Setter)", "Remote"))
    assert row and row["category"] == "customer_support" and row["source"] == "lendingtree"


def test_greenhouse_non_remote_dropped():
    assert cs._mercury_row(_gh_job("Customer Support Specialist", "New York, NY")) is None


def test_greenhouse_senior_dropped():
    assert cs._mercury_row(_gh_job("Senior Customer Support Manager", "Remote (US)")) is None


# --------------------------------------------------------------------------------------------------
# Ashby (Angi) / Lever (Ro)
# --------------------------------------------------------------------------------------------------
def test_angi_inside_sales_remote_kept():
    j = {"title": "Inside Sales Representative", "location": "Remote - United States",
         "secondaryLocations": [], "isRemote": True, "id": "a1",
         "jobUrl": "https://jobs.ashbyhq.com/angi/a1", "employmentType": "FullTime"}
    row = cs._angi_row(j)
    assert row and row["category"] == "sales" and row["source"] == "angi"


def test_angi_office_only_dropped():
    j = {"title": "Inside Sales Representative", "location": "New York, NY",
         "secondaryLocations": [], "isRemote": False, "id": "a2"}
    assert cs._angi_row(j) is None


def test_ro_remote_recruiting_coord_kept():
    j = {"text": "Talent Coordinator (Temp)", "categories": {"location": "New York, NY or Remote",
         "allLocations": ["New York, NY or Remote"], "commitment": "Temp"},
         "workplaceType": "remote", "country": "US", "id": "r1",
         "hostedUrl": "https://jobs.lever.co/ro/r1", "createdAt": 0}
    row = cs._ro_row(j)
    assert row and row["category"] == "recruiting" and row["source"] == "ro"


def test_ro_foreign_dropped():
    j = {"text": "Customer Support Associate", "categories": {"location": "Remote",
         "allLocations": ["Remote"]}, "workplaceType": "remote", "country": "PH", "id": "r2"}
    assert cs._ro_row(j) is None                             # foreign country → dropped


# --------------------------------------------------------------------------------------------------
# Dayforce (generic reader — pure decision)
# --------------------------------------------------------------------------------------------------
def test_dayforce_remote_us_list_location_kept():
    p = {"Title": "Customer Service Representative - Remote",
         "Locations": [{"City": "", "State": "", "Country": "United States"}],
         "ParentRequisitionCode": "REQ1",
         "JobDetailUrl": "/CandidatePortal/en-US/acme/Posting/REQ1", "PostedDate": ""}
    row = cs._dayforce_row(p, "tranzact", "TRANZACT", "acme.dayforcehcm.com")
    assert row and row["category"] == "customer_support"
    assert row["us_eligible"] is True and row["source_id"] == "REQ1"
    assert row["apply_url"].startswith("https://acme.dayforcehcm.com/")


def test_dayforce_string_location_kept():
    p = {"Title": "Licensed Insurance Sales Agent - Work From Home", "Location": "Remote, US",
         "Id": "9", "JobDetailUrl": "https://x.dayforcehcm.com/j/9"}
    row = cs._dayforce_row(p, "tz", "TZ", "x.dayforcehcm.com")
    assert row and row["category"] == "sales"


def test_dayforce_non_remote_dropped():
    p = {"Title": "Customer Service Representative", "Location": "Dallas, TX", "Id": "3"}
    assert cs._dayforce_row(p, "tz", "TZ", "x.dayforcehcm.com") is None


def test_dayforce_canada_dropped():
    p = {"Title": "Customer Service Representative - Remote", "Location": "Remote, Canada", "Id": "4"}
    assert cs._dayforce_row(p, "tz", "TZ", "x.dayforcehcm.com") is None


def test_dayforce_missing_fields():
    assert cs._dayforce_row({"Title": "Remote CSR"}, "tz", "TZ", "h") is None   # no ref id
    assert cs._dayforce_row({"Title": ""}, "tz", "TZ", "h") is None
    assert cs._dayforce_row("nope", "tz", "TZ", "h") is None


# --------------------------------------------------------------------------------------------------
# Registry sanity
# --------------------------------------------------------------------------------------------------
def test_all_fetchers_present():
    for name in ("macys", "ulta", "nordstrom", "ibex", "mercury", "current",
                 "rocketmoney", "lendingtree", "angi", "ro"):
        assert callable(cs._ALL[name])
    assert callable(cs.fetch_dayforce)
