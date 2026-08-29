from contextlib import contextmanager

import httpx

from backend.tools import custom_board_recovery as recovery


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _record(url="https://careers.example.test/jobs"):
    return {"id": 7, "canonical_name": "Acme", "careers_url": url,
            "ats": "custom", "ats_slug": "careersexampletest", "ats_url": url}


def test_robots_disallow_fails_closed_without_fetching_board():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="User-agent: *\nDisallow: /jobs")

    with _client(handler) as client:
        result = recovery.recover_record(_record(), client=client, resolve_dns=False)
    assert result["status"] == "incomplete"
    assert result["pages_fetched"] == 0
    assert calls == ["https://careers.example.test/robots.txt"]


def test_static_jsonld_recovers_supported_ats_without_fetching_external_host():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text='''<script type="application/ld+json">{
          "@type":"Organization","sameAs":"https://jobs.lever.co/acme"
        }</script>''')

    with _client(handler) as client:
        result = recovery.recover_record(_record(), client=client, resolve_dns=False)
    assert result["status"] == "recovered"
    assert result["ats"] == "lever"
    assert result["ats_slug"] == "acme"
    assert result["evidence"]["evidence_type"] == "json_ld"
    assert all("lever.co" not in url for url in calls)


def test_same_origin_jobs_sitemap_can_supply_external_ats_evidence():
    def handler(request):
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=(
                "User-agent: *\nAllow: /\n"
                "Sitemap: https://careers.example.test/jobs-sitemap.xml"))
        if path == "/jobs":
            return httpx.Response(200, text="<html><body>Careers</body></html>")
        if path == "/jobs-sitemap.xml":
            return httpx.Response(200, text=(
                "<urlset><url><loc>https://careers.example.test/jobs/123</loc>"
                "</url></urlset>"))
        if path == "/jobs/123":
            return httpx.Response(200, text=(
                '<a href="https://boards.greenhouse.io/acme/jobs/123">Apply</a>'))
        return httpx.Response(404)

    with _client(handler) as client:
        result = recovery.recover_record(_record(), client=client, resolve_dns=False)
    assert result["status"] == "recovered"
    assert result["ats"] == "greenhouse"
    assert result["ats_slug"] == "acme"
    assert result["sitemaps_fetched"] == 1
    assert result["pages_fetched"] == 2


def test_challenge_page_remains_incomplete():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text="Verify you are human - CAPTCHA")

    with _client(handler) as client:
        result = recovery.recover_record(_record(), client=client, resolve_dns=False)
    assert result["status"] == "incomplete"
    assert "challenge page detected" in result["errors"]


def test_apply_updates_only_custom_verified_active_row_with_evidence(monkeypatch):
    calls = []

    class Cursor:
        rowcount = 1

        def executemany(self, sql, values):
            calls.append((sql, values))

    @contextmanager
    def fake_cur(dict_rows=True):
        yield Cursor()

    monkeypatch.setattr(recovery.company_db, "_cur", fake_cur)
    updated = recovery.save_recovered([{
        "company_id": 7, "status": "recovered", "ats": "lever",
        "ats_slug": "acme", "ats_url": "https://jobs.lever.co/acme",
        "evidence": {"evidence_type": "external_link"},
    }])
    assert updated == 1
    sql, values = calls[0]
    assert "m.in_target_population" in sql and "m.domain_verified" in sql
    assert "lower(c.ats)='custom'" in sql
    assert "custom_board_recovery" in sql
    assert values[0][:3] == ("lever", "acme", "https://jobs.lever.co/acme")


def test_revalidation_persists_only_matching_stored_identity(monkeypatch):
    calls = []

    class Cursor:
        rowcount = 1

        def executemany(self, sql, values):
            calls.append((sql, values))

    @contextmanager
    def fake_cur(dict_rows=True):
        yield Cursor()

    monkeypatch.setattr(recovery.company_db, "_cur", fake_cur)
    updated = recovery.save_revalidated([{
        "company_id": 7, "status": "recovered", "ats": "Workday",
        "ats_slug": "Vizient", "evidence": {"page_url": "https://example.test/careers"},
    }])
    assert updated == 1
    sql, values = calls[0]
    assert "lower(c.ats)=%s AND c.ats_slug=%s" in sql
    assert values[0][2:] == ("workday", "vizient")
