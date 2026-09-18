"""Breezy HR is the 5th supported no-account ATS collector.

Its public board JSON (https://<slug>.breezy.hr/json) is a flat list of positions
with an explicit ``is_remote`` flag + structured location, but NO job description
(only a posted ``salary`` string). These tests pin the adapter's mapping, the salary
-> comp path, and the slug regex — all without network."""
from backend.applier import ats_boards, boards
from backend.tools import catalog_collector as cc


# A trimmed real-shape sample (from the live vetsez / bettersource boards, 2026-09).
_SAMPLE = [
    {   # US remote-within-location, posted salary
        "id": "18df3ec23bf901",
        "name": "Appian Integration Developer (Remote Opportunity)",
        "url": "https://vetsez.breezy.hr/p/18df3ec23bf901-appian-integration-developer",
        "type": {"id": "fullTime", "name": "Full-Time"},
        "location": {"country": {"name": "United States", "id": "US"},
                     "state": {"id": "FL", "name": "Florida"}, "city": "Tampa",
                     "is_remote": True, "remote_details": {"value": "remote-location"},
                     "name": "Tampa, FL"},
        "department": None,
        "salary": "$115,000 – $145,000 / year",
    },
    {   # worldwide anywhere-remote, no salary, has a department
        "id": "2d861561c6f2",
        "name": "Appointment Setter",
        "url": "https://bettersource.breezy.hr/p/2d861561c6f2-appointment-setter",
        "type": {"id": "fullTime", "name": "Full-Time"},
        "location": {"is_remote": True, "remote_details": {"value": "remote"},
                     "name": "Worldwide"},
        "department": "Sales & Support",
        "salary": "",
    },
    {   # on-site (not remote), and a name composed from parts (no pre-joined 'name')
        "id": "onsite-3",
        "name": "Office Manager",
        "url": "https://acme.breezy.hr/p/onsite-3-office-manager",
        "location": {"country": {"name": "United States", "id": "US"},
                     "state": {"id": "TX", "name": "Texas"}, "city": "Austin",
                     "is_remote": False},
        "department": None,
        "salary": "",
    },
]


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _patch_http(monkeypatch, data):
    monkeypatch.setattr(ats_boards.httpx, "get", lambda url, timeout=0: _Resp(data))


def test_fetch_breezy_maps_shape(monkeypatch):
    _patch_http(monkeypatch, _SAMPLE)
    jobs = ats_boards.fetch_board("breezy", "vetsez")
    assert len(jobs) == 3

    j0 = jobs[0]
    assert j0["id"] == "18df3ec23bf901"
    assert j0["title"].startswith("Appian")
    assert j0["applyUrl"] == "https://vetsez.breezy.hr/p/18df3ec23bf901-appian-integration-developer"
    assert j0["jobUrl"] == j0["applyUrl"]
    assert j0["isRemote"] is True and j0["workplaceType"] == "Remote"
    assert j0["location"] == "Tampa, FL"
    assert "$115,000" in j0["descriptionPlain"]      # salary surfaced for comp
    assert "$115,000" in j0["descriptionHtml"]

    # worldwide anywhere-remote
    assert jobs[1]["isRemote"] is True and jobs[1]["location"] == "Worldwide"
    assert jobs[1]["department"] == "Sales & Support"
    assert jobs[1]["descriptionPlain"] == "" and jobs[1]["descriptionHtml"] == ""

    # on-site + a location composed from city/state/country (no pre-joined name)
    assert jobs[2]["isRemote"] is False and jobs[2]["workplaceType"] == "OnSite"
    assert jobs[2]["location"] == "Austin, Texas, United States"


def test_fetch_breezy_non_list_is_empty(monkeypatch):
    # an unknown slug 302s to an HTML shell -> httpx.json() is a dict, not a list
    _patch_http(monkeypatch, {"error": "not found"})
    assert ats_boards.fetch_board("breezy", "nope") == []


def test_collect_board_breezy(monkeypatch):
    """collect_board honors remote_only, keeps distinct ids, and parses the salary
    into the posted comp + tags the worldwide row as KZ-eligible (open_anywhere)."""
    _patch_http(monkeypatch, _SAMPLE)
    rows = cc.collect_board("breezy", "vetsez", "VetsEZ", remote_only=True)

    # the on-site row is dropped; the two remote ids are kept, not collapsed
    ext = sorted(r["external_id"] for r in rows)
    assert ext == ["18df3ec23bf901", "2d861561c6f2"]

    r0 = next(r for r in rows if r["external_id"] == "18df3ec23bf901")
    assert r0["ats"] == "breezy" and r0["company"] == "VetsEZ" and r0["company_key"] == "vetsez"
    assert r0["is_remote"] is True
    assert r0["url"].startswith("https://vetsez.breezy.hr/p/")
    # posted salary -> comp
    assert r0["comp_min"] == 115000 and r0["comp_max"] == 145000
    assert r0["comp_currency"] == "USD" and r0["comp_source"] == "rule"

    # worldwide anywhere-remote is open to a remote applicant from any country
    rw = next(r for r in rows if r["external_id"] == "2d861561c6f2")
    assert rw["open_anywhere"] is True


def test_breezy_registered():
    assert "breezy" in ats_boards.SUPPORTED
    assert "breezy" in ats_boards._FETCHERS
    assert "breezy" in boards._FETCHERS
    assert "breezy" in boards._ATS_BASE
    assert "breezy" in cc._slugs()            # collector builds a breezy bucket


def test_breezy_slug_regex_mines_apply_url():
    # a *.breezy.hr apply URL mined from an aggregator yields the subdomain slug
    assert boards._url_slug("https://vetsez.breezy.hr/p/18df3ec23bf901-x") == "vetsez"
    assert boards._url_slug("https://20four7va.breezy.hr/") == "20four7va"
    assert boards._url_slug("https://powered-by-search.breezy.hr/p/abc-role") == "powered-by-search"
    # infra subdomains are skipped, not treated as a company slug
    assert boards._url_slug("https://assets-cdn.breezy.hr/x.js") == ""
    # doesn't cross-match a different ATS's URL
    assert boards._url_slug("https://boards.greenhouse.io/acme/jobs/1") == "acme"
