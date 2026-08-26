import httpx
from contextlib import contextmanager

from backend.tools.company_enrichment import (
    MANDATORY_CAREER_AUDIT,
    _get,
    apply_mandatory_career_audit,
    canonical_domain,
    career_candidates,
    detect_ats,
    enrich_company,
    enrich_database,
    looks_like_career_page,
    public_http_url,
    verify_mandatory_official_site,
)


class _Response:
    def __init__(self, url, text, status_code=200, content_type="text/html"):
        self.url = url
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}


class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, follow_redirects=False):
        self.calls.append(url)
        response = self.responses.get(url, _Response(url, "", 404))
        if isinstance(response, list):
            return response.pop(0)
        return response


def test_canonical_domain():
    assert canonical_domain("https://www.Example.com/about") == "example.com"
    assert canonical_domain("example.com") == "example.com"
    assert canonical_domain("") == ""


def test_career_candidates_prefers_explicit_path_and_anchor_text():
    html = '<a href="/about">About</a><a href="/join">Careers</a><a href="/jobs">Jobs</a>'
    assert career_candidates(html, "https://acme.test/")[:2] == [
        "https://acme.test/jobs",
        "https://acme.test/join",
    ]


def test_career_candidates_rejects_job_article_title():
    html = (
        '<a href="/articles/ai-reshaping-customer-service-jobs">'
        'AI is reshaping customer service jobs</a>'
        '<a href="/about/careers">Careers</a>'
    )
    assert career_candidates(html, "https://acme.test/") == [
        "https://acme.test/about/careers"
    ]


def test_career_page_signal_rejects_article_and_accepts_careers_heading():
    assert not looks_like_career_page(
        "https://acme.test/articles/customer-service-jobs",
        "<title>AI is reshaping customer service jobs</title>",
    )
    assert looks_like_career_page(
        "https://acme.test/work-with-us",
        "<title>Build your career with Acme</title>",
    )
    assert not looks_like_career_page(
        "https://acme.test/careers/summer-festival",
        "<title>Acme summer festival</title>",
    )


def test_detects_supported_ats_urls():
    cases = {
        "https://jobs.ashbyhq.com/acme": ("ashby", "acme"),
        "https://jobs.lever.co/acme-inc/123": ("lever", "acme-inc"),
        "https://acme.wd5.myworkdayjobs.com/External": ("workday", "acme"),
        "https://jobs.smartrecruiters.com/Acme/123": ("smartrecruiters", "Acme"),
        "https://acme.icims.com/jobs/intro": ("icims", "acme"),
        "https://apply.workable.com/acme/jobs": ("workable", "acme"),
        "https://acme.eightfold.ai/careers": ("eightfold", "acme"),
        "https://job-boards.greenhouse.io/acme": ("greenhouse", "acme"),
        "https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX": ("oracle", "acme"),
        "https://acme.successfactors.com/career?company=acme": (
            "successfactors", "acme"),
    }
    for url, expected in cases.items():
        result = detect_ats([url])
        assert (result["ats"], result["ats_slug"]) == expected


def test_detect_ats_preserves_workday_site_and_oracle_site_number():
    workday = detect_ats([
        "https://ghr.wd1.myworkdayjobs.com/en-us/lateral-us/login"
    ])
    assert workday["ats_url"].endswith("/en-us/lateral-us/login")
    oracle = detect_ats([
        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/requisitions"
    ])
    assert "/sites/CX_1001/requisitions" in oracle["ats_url"]


def test_detect_ats_preserves_customer_identity_for_shared_hosts():
    successfactors = detect_ats([
        'href="https://career8.successfactors.com/career?company=jetblueair&amp;site=external"'
    ])
    assert successfactors["ats_slug"] == "jetblueair"
    assert "company=jetblueair" in successfactors["ats_url"]
    eightfold = detect_ats([
        'https://app.eightfold.ai/careers?domain=elcompanies.com'
    ])
    assert eightfold["ats_slug"] == "app:elcompanies.com"
    assert eightfold["ats_url"].endswith("domain=elcompanies.com")


def test_detect_ats_rejects_shared_host_without_customer_identity():
    assert detect_ats(["https://career8.successfactors.com/career"])["ats"] == ""
    assert detect_ats(["https://app.eightfold.ai/careers"])["ats"] == ""


def test_detect_ats_prefers_main_icims_tenant_over_event_portal():
    result = detect_ats([
        "https://events-statefarm.icims.com/jobs/search",
        "https://careers-statefarm.icims.com/jobs/login?loginOnly=1",
    ])
    assert result["ats"] == "icims"
    assert result["ats_slug"] == "careers-statefarm"


def test_enrich_detects_successfactors_rmk_on_official_custom_domain():
    home = "https://foundever.test/"
    careers = "https://jobs.foundever.test/careers"
    client = _Client({
        home: _Response(home, f'<a href="{careers}">Careers</a>'),
        careers: _Response(careers, (
            '<script src="https://performancemanager4.successfactors.com/x.js"></script>'
            '<img src="https://rmkcdn.successfactors.com/site/logo.png">')),
    })
    result = enrich_company({"id": 1, "domain": "foundever.test"}, client)
    assert result["ats"] == "successfactors"
    assert result["ats_slug"] == "jobs.foundever.test"
    assert result["ats_url"] == careers


def test_enriches_from_official_homepage_without_job_catalog():
    home = "https://acme.test/"
    careers = "https://acme.test/careers"
    client = _Client({
        home: _Response(home, '<a href="/careers">Work with us</a>'),
        careers: _Response(careers, '<a href="https://jobs.lever.co/acme">Open roles</a>'),
    })
    result = enrich_company({"legal_name": "Acme", "domain": "www.acme.test"}, client)
    assert result["domain"] == "acme.test"
    assert result["careers_url"] == careers
    assert result["ats"] == "lever"
    assert result["ats_slug"] == "acme"
    assert result["careers_confidence"] == 0.95
    assert result["provenance"]["web_enrichment"]["result"] == "ats_found"
    assert result["provenance"]["web_enrichment"]["ats_url"] == \
        "https://jobs.lever.co/acme"
    assert client.calls == [home, careers]


def test_record_without_domain_does_not_make_requests():
    client = _Client({})
    result = enrich_company({"legal_name": "Unknown"}, client)
    assert result["ats"] == ""
    assert client.calls == []


def test_ssrf_guard_rejects_local_and_private_targets():
    assert not public_http_url("http://127.0.0.1/admin")
    assert not public_http_url("http://169.254.169.254/latest/meta-data")
    assert not public_http_url("http://10.1.2.3/")
    assert not public_http_url("http://localhost:8080/")
    assert public_http_url("https://example.com/careers")


def test_redirect_target_is_checked_before_following():
    home = "https://acme.test/"
    client = _Client({home: _Response(
        home, "", 302, content_type="text/html")})
    client.responses[home].headers["location"] = "http://127.0.0.1/admin"
    assert _get(client, home) is None
    assert client.calls == [home]


def test_transient_status_is_retried(monkeypatch):
    monkeypatch.setattr("backend.tools.company_enrichment.time.sleep", lambda _: None)
    url = "https://acme.test/"
    client = _Client({url: [
        _Response(url, "busy", 503),
        _Response(url, "<html>ok</html>", 200),
    ]})
    assert _get(client, url) is not None
    assert client.calls == [url, url]


def test_real_client_stream_stops_oversized_response(monkeypatch):
    monkeypatch.setattr(
        "backend.tools.company_enrichment.public_http_url", lambda *_args, **_kwargs: True)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/html"}, content=b"x" * (2 * 1024 * 1024 + 1),
        request=request))
    with httpx.Client(transport=transport) as client:
        assert _get(client, "https://oversized.test/") is None


def test_unverified_fallback_is_not_saved_as_careers_url():
    home = "https://acme.test/"
    client = _Client({home: _Response(home, "<html>No links</html>")})
    result = enrich_company({"id": 1, "domain": "acme.test"}, client)
    assert result["careers_url"] == ""
    assert result["provenance"]["web_enrichment"]["result"] == "no_careers_found"


def test_database_batch_updates_selected_domain_rows(monkeypatch):
    from backend.tools import company_discovery_db as db

    home = "https://acme.test/"
    careers = "https://acme.test/jobs"
    client = _Client({
        home: _Response(home, '<a href="/jobs">Jobs</a>'),
        careers: _Response(careers, '<a href="https://jobs.ashbyhq.com/acme">Roles</a>'),
    })
    saved = []
    monkeypatch.setattr(db, "list_enrichment_candidates", lambda **_: [
        {"id": 9, "domain": "acme.test", "provenance": {"source": "sec"}},
    ])
    monkeypatch.setattr(db, "update_enrichment_results",
                        lambda rows: saved.extend(rows) or len(rows))
    monkeypatch.setattr(db, "enrichment_counts", lambda: {
        "domains": 1, "careers": 1, "ats": 1, "attempted": 1,
    })
    result = enrich_database(limit=10, workers=99, min_interval=0,
                             client_factory=lambda: client)
    assert result == {"selected": 1, "updated": 1, "domains": 1,
                      "careers": 1, "ats": 1, "attempted": 1}
    assert saved[0]["id"] == 9
    assert saved[0]["ats"] == "ashby"


def test_database_batch_preserves_completed_rows_when_one_worker_fails(monkeypatch):
    from backend.tools import company_discovery_db as db

    rows = [{"id": 1, "domain": "good.test"}, {"id": 2, "domain": "bad.test"}]
    saved = []
    monkeypatch.setattr(db, "list_enrichment_candidates", lambda **_: rows)
    monkeypatch.setattr(db, "update_enrichment_results",
                        lambda values: saved.extend(values) or len(values))
    monkeypatch.setattr(db, "enrichment_counts", lambda: {
        "domains": 2, "careers": 0, "ats": 0, "attempted": 1,
    })

    def factory():
        class WorkerClient(_Client):
            def get(self, url, follow_redirects=False):
                if "bad.test" in url:
                    raise RuntimeError("unexpected parser failure")
                return _Response(url, "<html>ok</html>")
        return WorkerClient({})

    result = enrich_database(limit=2, workers=2, min_interval=0,
                             client_factory=factory)
    assert result["updated"] == 1
    assert result["errors"] == 1
    assert [row["id"] for row in saved] == [1]


def test_mandatory_audit_has_unique_tenants_and_excludes_event_portal():
    assert len(MANDATORY_CAREER_AUDIT) == 15
    identities = {(row["ats"], row["ats_slug"])
                  for row in MANDATORY_CAREER_AUDIT.values()}
    assert len(identities) == 15
    state_farm = MANDATORY_CAREER_AUDIT["statefarm.com"]
    assert state_farm["ats_slug"] == "careers-statefarm"
    assert "events-statefarm" not in state_farm["ats_url"]
    assert MANDATORY_CAREER_AUDIT["concentrix.com"]["ats_url"].endswith(
        "/external_global")


def test_mandatory_audit_atomically_overwrites_stale_enrichment(monkeypatch):
    from backend.tools import company_discovery_db as db

    class Cursor:
        rowcount = 1

        def __init__(self):
            self.calls = []

        def execute(self, sql, params=()):
            self.calls.append((sql, params))

        def fetchall(self):
            return [(number, source_id) for number, source_id in enumerate(
                sorted(MANDATORY_CAREER_AUDIT), 1)]

    cursor = Cursor()

    @contextmanager
    def fake_cur(_dict_rows=True):
        yield cursor

    monkeypatch.setattr(db, "_cur", fake_cur)
    result = apply_mandatory_career_audit()
    assert result == {"selected": 15, "updated": 15, "careers": 15,
                      "named_ats": 7, "custom_experiences": 8}
    assert len(cursor.calls) == 16
    updates = cursor.calls[1:]
    assert all("careers_url=%s,ats=%s,ats_slug=%s,ats_url=%s" in sql
               and "COALESCE" not in sql for sql, _ in updates)


def test_live_official_site_is_a_separate_identity_factor(monkeypatch):
    from backend.tools import company_discovery_db as db

    class Cursor:
        rowcount = 1

        def __init__(self):
            self.calls = []

        def execute(self, sql, params=()):
            self.calls.append((sql, params))

        def fetchone(self):
            return {"id": 11982, "domain": "jpmorganchase.com",
                    "candidate_domain": "jpmorganchase.com",
                    "trade_name": "JPMorganChase",
                    "legal_name": "JPMorgan Chase & Co."}

    cursors = []

    @contextmanager
    def fake_cur(_dict_rows=True):
        cursor = Cursor()
        cursors.append(cursor)
        yield cursor

    monkeypatch.setattr(db, "_cur", fake_cur)
    home = "https://jpmorganchase.com/"
    client = _Client({home: _Response(
        home, "<title>JPMorganChase | Global Financial Services</title>")})
    result = verify_mandatory_official_site("jpmorganchase.com", client=client)
    assert result["verified"] is True
    master_sql, master_params = cursors[1].calls[0]
    assert "provider'<>'official_site_identity'" in master_sql
    assert "independent_live_official_site" in master_params[0]
    assert '"class": "official_site_identity"' in master_params[0]
    assert "authoritative_first_factor" not in master_params[0]
