import httpx

from backend.tools.company_enrichment import (
    _get,
    canonical_domain,
    career_candidates,
    detect_ats,
    enrich_company,
    enrich_database,
    public_http_url,
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


def test_career_candidates_prefers_anchor_text():
    html = '<a href="/about">About</a><a href="/join">Careers</a><a href="/jobs">Jobs</a>'
    assert career_candidates(html, "https://acme.test/")[:2] == [
        "https://acme.test/join",
        "https://acme.test/jobs",
    ]


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
            "successfactors", "acme.successfactors.com"),
    }
    for url, expected in cases.items():
        result = detect_ats([url])
        assert (result["ats"], result["ats_slug"]) == expected


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
