from backend.tools.company_enrichment import (
    canonical_domain,
    career_candidates,
    detect_ats,
    enrich_company,
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
        return self.responses.get(url, _Response(url, "", 404))


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
