"""Unit tests for the extra mass-hiring connectors (backend/tools/connectors_extra.py) — no network.

Every per-employer `_<source>_row` is pinned against both HARD RULES:
  · _is_remote REMOTE      (an on-site / hybrid / non-remote row is dropped)
  · categorize() ENTRY     (a senior / manager / non-mass title is dropped)
For the CANADA employers (Ashby ca=True / Greenhouse ca=True / SmartRecruiters country='ca') a kept
row must also (a) have us_eligible FORCED True (keep on the North-America board) and (b) its location
be _is_ca_remote; a US-remote or non-CA row is dropped.
"""
from backend.tools import connectors_extra as ce
from backend.tools import mass_hiring as mh


# ================================================================================================
# Chewy — Workday (US). Remote from the title ("Work from Home in <state>"), US confirmed from the
# externalPath state slug when locationsText is bare. `_workday_row` forces us_eligible True.
# ================================================================================================
def _chewy_job(title, loc, path):
    return {"title": title, "locationsText": loc, "externalPath": path,
            "bulletFields": ["R-12345"]}


def test_chewy_remote_wfh_csr_is_kept():
    row = ce._chewy_row(_chewy_job(
        "Customer Care Representative - Work from Home in Texas",
        "USA - TX - Virtual Richardson - DF4",
        "/job/Texas-Work-from-Home/Customer-Care-Representative_R-12345"))
    assert row is not None
    assert row["source"] == "chewy"
    assert row["category"] == "customer_support"
    assert row["us_eligible"] is True
    assert "chewy.wd5.myworkdayjobs.com" in row["apply_url"]


def test_chewy_us_confirmed_from_path_when_location_bare():
    # locationsText carries NO US signal ("Louisville, KY" — no state word/USA); US is confirmed via
    # the state NAME in the externalPath slug (us_from_path).
    row = ce._chewy_row(_chewy_job(
        "Customer Care Representative - Work From Home in Kentucky",
        "Louisville, KY",
        "/job/Kentucky-Work-From-Home/Customer-Care-Representative_R-2"))
    assert row is not None
    assert row["us_eligible"] is True


def test_chewy_onsite_warehouse_is_dropped():
    # No remote signal anywhere → HARD RULE 1 drops it.
    assert ce._chewy_row(_chewy_job(
        "Fulfillment Center Team Member", "USA - PA - Belle Vernon",
        "/job/PA-Belle-Vernon/Fulfillment-Center-Team-Member_R-3")) is None


def test_chewy_senior_remote_is_dropped():
    # Remote + US, but a senior title → HARD RULE 2 (categorize) drops it.
    assert ce._chewy_row(_chewy_job(
        "Senior Manager, Customer Care - Work from Home in Texas",
        "USA - TX - Virtual", "/job/Texas-Work-from-Home/Sr-Manager_R-4")) is None


# ================================================================================================
# Ashby CA fintechs (KOHO / Neo / Wealthsimple / Clearco / Float). ca=True: keep a CA-remote entry
# row, force us_eligible True; drop US-remote, non-remote, and senior rows.
# ================================================================================================
def _ashby_job(title, location, *, workplace="Remote", is_remote=True, secondary=None, jid="a1"):
    return {"id": jid, "title": title, "location": location,
            "workplaceType": workplace, "isRemote": is_remote,
            "secondaryLocations": [{"location": s} for s in (secondary or [])],
            "employmentType": "FullTime", "publishedAt": "2026-09-01T00:00:00Z",
            "jobUrl": f"https://jobs.ashbyhq.com/x/{jid}"}


def _assert_ca_kept(row, source):
    assert row is not None
    assert row["source"] == source
    assert row["us_eligible"] is True                       # forced (keep on NA board)
    assert mh._is_ca_remote(row["location_raw"])            # location carries the Canada signal
    assert mh.categorize(row["title"]) is not None          # entry bucket


def test_koho_ca_remote_csr_is_kept():
    _assert_ca_kept(ce._koho_row(_ashby_job(
        "Customer Support Specialist", "Remote (Canada)")), "koho")


def test_neo_ca_remote_csr_is_kept():
    _assert_ca_kept(ce._neo_row(_ashby_job(
        "Customer Service Representative", "Remote - ON, Canada")), "neo")


def test_wealthsimple_ca_remote_csr_is_kept():
    _assert_ca_kept(ce._wealthsimple_row(_ashby_job(
        "Client Success Associate", "Remote (Canada)")), "wealthsimple")


def test_clearco_ca_remote_csr_is_kept():
    _assert_ca_kept(ce._clearco_row(_ashby_job(
        "Customer Support Associate", "Remote - Toronto, Ontario, Canada")), "clearco")


def test_float_ca_remote_csr_is_kept():
    _assert_ca_kept(ce._float_row(_ashby_job(
        "Customer Support Representative", "Remote (Canada)")), "float")


def test_ashby_ca_secondary_remote_location_is_kept():
    # A hybrid-in-office PRIMARY with a "Remote (Canada)" SECONDARY location qualifies as CA-remote.
    _assert_ca_kept(ce._koho_row(_ashby_job(
        "Customer Support Specialist", "Toronto Headquarters",
        workplace="Hybrid", secondary=["Remote (Canada)"])), "koho")


def test_ashby_ca_us_remote_is_dropped():
    # A US-remote row on a CA connector → dropped (not _is_ca_remote).
    assert ce._koho_row(_ashby_job(
        "Customer Support Specialist", "Remote (US)")) is None


def test_ashby_ca_onsite_is_dropped():
    # HARD RULE 1: an office-only (non-remote) location is dropped even in Canada.
    assert ce._neo_row(_ashby_job(
        "Customer Service Representative", "Calgary, Alberta, Canada",
        workplace="OnSite", is_remote=False)) is None


def test_ashby_ca_senior_is_dropped():
    # HARD RULE 2: a senior/manager CS title is dropped.
    assert ce._wealthsimple_row(_ashby_job(
        "Senior Manager, Client Success", "Remote (Canada)")) is None


# ================================================================================================
# Hootsuite — Greenhouse (CA). ca=True.
# ================================================================================================
def _gh_job(title, loc_name, jid=123):
    return {"id": jid, "title": title, "location": {"name": loc_name},
            "absolute_url": f"https://boards.greenhouse.io/hootsuite/jobs/{jid}",
            "updated_at": "2026-09-01T00:00:00Z"}


def test_hootsuite_ca_remote_csr_is_kept():
    row = ce._hootsuite_row(_gh_job(
        "Bilingual Customer Support Representative", "Remote - Canada"))
    _assert_ca_kept(row, "hootsuite")


def test_hootsuite_us_remote_is_dropped():
    assert ce._hootsuite_row(_gh_job(
        "Customer Support Representative", "Remote - United States")) is None


def test_hootsuite_onsite_is_dropped():
    assert ce._hootsuite_row(_gh_job(
        "Customer Support Representative", "Toronto, Ontario, Canada")) is None


def test_hootsuite_senior_remote_is_dropped():
    assert ce._hootsuite_row(_gh_job(
        "Senior Customer Success Manager", "Remote - Canada")) is None


# ================================================================================================
# Coveo — SmartRecruiters (CA). country='ca', force_eligible=True.
# ================================================================================================
def _sr_job(name, *, country="ca", remote=True, city="Toronto", region="ON", jid="s1"):
    return {"id": jid, "name": name,
            "location": {"country": country, "remote": remote, "city": city, "region": region},
            "releasedDate": "2026-09-01T00:00:00Z"}


def test_coveo_ca_remote_csr_is_kept():
    row = ce._coveo_row(_sr_job("Customer Service Representative"))
    assert row is not None
    assert row["source"] == "coveo"
    assert row["us_eligible"] is True
    assert mh._is_ca_remote(row["location_raw"])
    assert row["category"] == "customer_support"
    assert "jobs.smartrecruiters.com/Coveo/" in row["apply_url"]


def test_coveo_us_country_is_dropped():
    # A US posting on the CA (country='ca') connector → dropped (country mismatch).
    assert ce._coveo_row(_sr_job("Customer Service Representative", country="us")) is None


def test_coveo_onsite_is_dropped():
    assert ce._coveo_row(_sr_job("Customer Service Representative", remote=False)) is None


def test_coveo_senior_is_dropped():
    assert ce._coveo_row(_sr_job("Senior Manager, Customer Support")) is None
