from backend.tools.employer_domain_verifier import (
    RequestLimiter, _country_compatible_domain, _meaningful_domain_overlap,
    audit_search_domains, discover_search_domains, verify_record,
)


class Response:
    def __init__(self, url, text, status=200):
        self.url = url
        self.text = text
        self.status_code = status
        self.headers = {"content-type": "text/html"}


class Client:
    def __init__(self, responses):
        self.responses = responses

    def get(self, url, follow_redirects=False):
        return self.responses.get(url, Response(url, "", 404))


def test_second_factor_requires_live_brand_identity_and_discovers_ats():
    home = "https://example.com/"
    careers = "https://example.com/careers"
    client = Client({
        home: Response(home, '<title>Example Corporation</title><a href="/careers">Careers</a>'),
        careers: Response(careers, '<a href="https://jobs.lever.co/example">Jobs</a>'),
    })
    result = verify_record({
        "company_id": 7, "candidate_domain": "example.com",
        "brand_name": "Example", "legal_name": "Example Corporation",
    }, client=client, limiter=RequestLimiter(0))
    assert result["domain"] == "example.com"
    assert result["ats"] == "lever"
    assert result["careers_url"] == careers
    assert result["domain_evidence"][0]["class"] == "official_site_identity"


def test_second_factor_rejects_unrelated_homepage():
    client = Client({"https://example.com/": Response(
        "https://example.com/", "<title>Unrelated municipal portal</title>")})
    assert verify_record({
        "company_id": 7, "candidate_domain": "example.com",
        "brand_name": "Acme Logistics", "legal_name": "Acme Logistics, Inc.",
    }, client=client, limiter=RequestLimiter(0)) is None


def test_meaningful_domain_overlap_ignores_legal_suffixes():
    row = {"legal_name": "Example Services, Inc.", "brand_name": "Example Services"}
    assert _meaningful_domain_overlap(row, "example.com") == 1.0
    assert _meaningful_domain_overlap(row, "services.com") == 0
    assert _meaningful_domain_overlap(
        {"brand_name": "Johnson Controls"}, "johnsoncontrols.com") == 1.0
    assert _meaningful_domain_overlap(
        {"brand_name": "AutoZone Parts"}, "google.com") == 0


def test_us_employer_rejects_foreign_country_domain_but_allows_genericized_cc_tld():
    row = {"country": "US"}
    assert not _country_compatible_domain(row, "sodexo.fi")
    assert _country_compatible_domain(row, "example.com")
    assert _country_compatible_domain(row, "startup.ai")


def test_legacy_search_entrypoint_delegates_to_provisional_tier(monkeypatch):
    from backend.tools import employer_provisional_domain
    captured = {}
    monkeypatch.setattr(
        employer_provisional_domain, "run",
        lambda **kwargs: captured.update(kwargs) or {"accepted": 2, "updated": 2})
    result = discover_search_domains(limit=17, workers=3, min_interval=0.7)
    assert result == {"accepted": 2, "updated": 2}
    assert captured == {"limit": 17, "workers": 3,
                        "min_interval": 0.7, "dry_run": False}


def test_audit_requires_structured_link_and_official_site(monkeypatch):
    monkeypatch.setattr(
        "backend.tools.employer_domain_verifier.master_db.list_all_verified_domains",
        lambda: [{
            "company_id": 17, "brand_name": "Example", "legal_name": "Example, Inc.",
            "trade_name": "Example", "country": "US", "domain": "example.com",
            "domain_evidence": [{
                "class": "official_site_identity", "url": "https://example.com/",
            }],
        }],
    )
    captured = {}
    monkeypatch.setattr(
        "backend.tools.employer_domain_verifier.master_db.quarantine_domain_ids",
        lambda ids, reason: captured.update(ids=ids, reason=reason) or len(ids),
    )

    assert audit_search_domains() == {"checked": 1, "rejected": 1}
    assert captured["ids"] == [17]


def test_audit_accepts_two_independent_domain_factors(monkeypatch):
    monkeypatch.setattr(
        "backend.tools.employer_domain_verifier.master_db.list_all_verified_domains",
        lambda: [{
            "company_id": 18, "brand_name": "Example", "legal_name": "Example, Inc.",
            "trade_name": "Example", "country": "US", "domain": "example.com",
            "domain_evidence": [
                {"class": "structured_corporate_source", "candidate_domain": "example.com"},
                {"class": "official_site_identity", "url": "https://example.com/"},
            ],
        }],
    )
    monkeypatch.setattr(
        "backend.tools.employer_domain_verifier.master_db.quarantine_domain_ids",
        lambda ids, reason: len(ids),
    )

    assert audit_search_domains() == {"checked": 1, "rejected": 0}
