import json

import httpx
import pytest

from backend.tools import company_job_sources as src


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_greenhouse_fetches_full_jd_and_every_question_for_remote_only():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "/jobs/101" in request.url.path:
            return httpx.Response(200, json={
                "id": 101, "title": "Remote Support", "location": {"name": "Remote - US"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
                "content": "&lt;h2&gt;Requirements&lt;/h2&gt;&lt;p&gt;Clear writing&lt;/p&gt;",
                "questions": [
                    {"id": 1, "label": "Can you work weekends?", "required": True,
                     "fields": [{"name": "question_1", "type": "single_select",
                                 "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
                    {"id": 2, "label": "Explain", "required": False,
                     "fields": [{"name": "question_2", "label": "Detailed answer",
                                 "type": "textarea", "required": True}]},
                ],
                "compliance": [{"type": "eeoc", "questions": [
                    {"id": 3, "label": "Voluntary disclosure", "required": False,
                     "fields": [{"name": "question_3", "type": "single_select",
                                 "values": [{"label": "Decline", "value": "decline"}]}]}
                ]}],
            })
        return httpx.Response(200, json={"jobs": [
            {"id": 101, "title": "Remote Support", "location": {"name": "Remote - US"},
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
             "content": "&lt;h2&gt;Requirements&lt;/h2&gt;&lt;p&gt;Clear writing&lt;/p&gt;"},
            {"id": 102, "title": "Office Support", "location": {"name": "New York, NY"},
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/102", "content": "<p>Great role</p>"},
        ]})

    jobs = src.fetch_remote_jobs("greenhouse", "acme", 77, client=_client(handler))
    assert len(jobs) == 1
    job = jobs[0]
    assert job["source_job_id"] == "101"
    assert job["company_id"] == 77
    assert job["description"] == "Requirements\nClear writing"
    assert job["requirements"] == "Clear writing"
    assert job["question_count"] == 3
    assert job["questions_state"] == "available"
    assert job["questions"][0]["fields"][0]["values"][1]["label"] == "No"
    assert job["questions"][1]["fields"][0]["label"] == "Detailed answer"
    assert job["questions"][1]["fields"][0]["required"] is True
    assert job["questions"][2]["group"] == "compliance.questions"
    assert job["raw_payload"]["detail"]["questions"][1]["label"] == "Explain"
    assert not any("/jobs/102" in url for url in calls)


def test_greenhouse_detail_failure_marks_result_incomplete_for_closure(monkeypatch):
    monkeypatch.setattr(src.time, "sleep", lambda _: None)

    def handler(request):
        if "/jobs/101" in request.url.path:
            return httpx.Response(503)
        return httpx.Response(200, json={"jobs": [{
            "id": 101, "title": "Remote Support", "location": {"name": "Remote - US"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
            "content": "<p>Complete board JD</p>",
        }]})

    result = src.fetch_remote_jobs(
        "greenhouse", "acme", 7, client=_client(handler), retries=1)
    assert result.complete is False
    assert result.errors and "job 101 detail" in result.errors[0]


def test_greenhouse_excludes_hybrid_even_when_title_mentions_remote():
    payload = {"jobs": [{
        "id": 1, "title": "Remote-friendly Analyst", "location": {"name": "Hybrid - Boston"},
        "content": "<p>This is a fully remote role.</p>", "absolute_url": "https://x/1",
    }]}
    assert src.parse_greenhouse_jobs(payload, 1, "acme") == []


def test_lever_normalizes_named_sections_salary_and_dates():
    payload = [{
        "id": "lev-1", "text": "Customer Support", "workplaceType": "remote",
        "categories": {"location": "United States", "team": "CX", "commitment": "Full-time"},
        "description": "<p>Help customers.</p>",
        "lists": [
            {"text": "Requirements", "content": "<ul><li>Good writing</li></ul>"},
            {"text": "Benefits", "content": "<ul><li>Health plan</li></ul>"},
        ],
        "salaryRange": {"min": 50000, "max": 70000, "currency": "USD", "interval": "year"},
        "applyUrl": "https://jobs.lever.co/acme/lev-1/apply",
        "hostedUrl": "https://jobs.lever.co/acme/lev-1", "createdAt": 1710000000000,
    }]
    job = src.parse_lever_jobs(payload, "company-1", "acme")[0]
    assert job["requirements"] == "Good writing"
    assert job["benefits"] == "Health plan"
    assert (job["salary_min"], job["salary_max"], job["currency"]) == (50000, 70000, "USD")
    assert job["employment_type"] == "Full-time"
    assert job["posted_at"] == 1710000000000


def test_lever_rejects_ambiguous_and_hybrid_jobs():
    jobs = [
        {"id": "a", "text": "Engineer", "categories": {"location": "US"}, "description": "Remote collaboration."},
        {"id": "b", "text": "Engineer", "workplaceType": "hybrid", "categories": {"location": "Remote / NYC"}},
    ]
    assert src.parse_lever_jobs(jobs, 1, "acme") == []


@pytest.mark.parametrize("ats", ["icims", "oracle", "successfactors", "eightfold", "custom"])
def test_structured_html_connectors_keep_remote_full_jd(ats):
    payload = json.dumps({
        "@context": "https://schema.org", "@type": "JobPosting",
        "identifier": {"value": "REQ-7"}, "title": "Remote Support Specialist",
        "jobLocationType": "TELECOMMUTE", "employmentType": "FULL_TIME",
        "description": "<h2>Requirements</h2><p>Clear writing</p>",
        "url": "https://careers.acme.test/jobs/REQ-7", "datePosted": "2026-08-25",
    })
    html = f'<script type="application/ld+json">{payload}</script>'
    jobs = src.parse_structured_html_jobs(
        html, 9, "acme", ats=ats, page_url="https://careers.acme.test/jobs/REQ-7")
    assert len(jobs) == 1
    assert jobs[0]["source"] == ats
    assert jobs[0]["source_job_id"] == "REQ-7"
    assert jobs[0]["requirements"] == "Clear writing"
    assert jobs[0]["questions_state"] == "not_available"


def test_structured_html_connectors_reject_onsite_job():
    payload = json.dumps({
        "@type": "JobPosting", "identifier": "REQ-8", "title": "Office Support",
        "jobLocation": {"address": {"addressLocality": "Dallas", "addressRegion": "TX"}},
        "description": "<p>Work from our office.</p>",
    })
    html = f'<script type="application/ld+json">{payload}</script>'
    assert src.parse_structured_html_jobs(
        html, 9, "acme", ats="custom", page_url="https://acme.test/jobs/REQ-8") == []


def test_icims_public_board_uses_iframe_pages_and_full_details(monkeypatch):
    monkeypatch.setattr(
        "backend.tools.company_enrichment.public_http_url", lambda *_args, **_kwargs: True)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/jobs/search":
            if request.url.params.get("pr") == "1":
                return httpx.Response(200, text='''
                  <a href="/jobs/102/office-agent/job?in_iframe=1">Office Agent</a>
                ''', headers={"content-type": "text/html"})
            return httpx.Response(200, text='''
              <a href="/jobs/101/remote-support/job?in_iframe=1">Remote Support</a>
              <a href="/jobs/search?pr=1&amp;ss=1&amp;in_iframe=1">Next</a>
            ''', headers={"content-type": "text/html"})
        if "/jobs/101/" in request.url.path:
            return httpx.Response(200, text='''
              <html><head><meta property="og:title" content="Remote Support Specialist"></head>
              <body><h1>Remote Support Specialist</h1><div>Location:</div>
              <div>Remote, United States</div><h2>Overview</h2>
              <p>This is a 100% remote position helping customers.</p></body></html>
            ''', headers={"content-type": "text/html"})
        return httpx.Response(200, text='''
          <h1>Office Agent</h1><div>Location:</div><div>Dallas, TX</div>
          <p>This is an in-office position.</p>
        ''', headers={"content-type": "text/html"})

    result = src.fetch_remote_jobs(
        "icims", "careers-acme", 12,
        ats_url="https://careers-acme.icims.com/jobs", client=_client(handler))
    assert result.complete is True
    assert [job["source_job_id"] for job in result] == ["101"]
    assert result[0]["location_raw"] == "Remote, United States"
    assert any("in_iframe=1" in url and "/jobs/search" in url for url in calls)
    assert any("pr=1" in url for url in calls)


def test_icims_event_portal_is_never_authoritative_for_closures(monkeypatch):
    monkeypatch.setattr(
        "backend.tools.company_enrichment.public_http_url", lambda *_args, **_kwargs: True)
    client = _client(lambda request: httpx.Response(
        200, text="<p>0 jobs found</p>", headers={"content-type": "text/html"}))
    result = src.fetch_remote_jobs(
        "icims", "events-acme", 12,
        ats_url="https://events-acme.icims.com/jobs", client=client)
    assert result == []
    assert result.complete is False
    assert "not an authoritative full job inventory" in result.errors[0]


def test_successfactors_rmk_uses_branded_search_and_job_paths(monkeypatch):
    monkeypatch.setattr(
        "backend.tools.company_enrichment.public_http_url", lambda *_args, **_kwargs: True)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/search/":
            return httpx.Response(200, text='''
              <a href="/job/Remote-Support-US-00000/1409884000/">Remote Support</a>
            ''', headers={"content-type": "text/html"})
        return httpx.Response(200, text='''
          <html><head><meta property="og:title" content="Remote Support Analyst"></head>
          <body><h1>Remote Support Analyst</h1><div>Location:</div>
          <div>Remote - United States</div><p>This is a fully remote role.</p></body></html>
        ''', headers={"content-type": "text/html"})

    result = src.fetch_remote_jobs(
        "successfactors", "jetblueair", 13,
        ats_url="https://careers.acme.test/index", client=_client(handler))
    assert result.complete is True
    assert [job["source_job_id"] for job in result] == ["1409884000"]
    assert calls[0].startswith("https://careers.acme.test/search/")
    assert "sortColumn=referencedate" in calls[0]


def test_successfactors_pagination_cannot_escape_the_board_path(monkeypatch):
    monkeypatch.setattr(
        "backend.tools.company_enrichment.public_http_url", lambda *_args, **_kwargs: True)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/search/":
            return httpx.Response(200, text='''
              <a href="/job/Remote-Support-US/101/">Remote Support</a>
              <a href="/unrelated?startrow=999">Unrelated pagination-like link</a>
            ''', headers={"content-type": "text/html"})
        return httpx.Response(200, text='''
          <h1>Remote Support</h1><div>Location:</div><div>Remote - United States</div>
          <p>This is a fully remote role.</p>
        ''', headers={"content-type": "text/html"})

    result = src.fetch_remote_jobs(
        "successfactors", "acme", 13,
        ats_url="https://careers.acme.test/index", client=_client(handler))
    assert result.complete is True
    assert len(result) == 1
    assert not any("/unrelated" in url for url in calls)


def test_icims_pending_pagination_at_cap_is_incomplete(monkeypatch):
    monkeypatch.setattr(
        "backend.tools.company_enrichment.public_http_url", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(src, "MAX_PUBLIC_BOARD_PAGES", 1)
    client = _client(lambda request: httpx.Response(
        200, text='<a href="/jobs/search?pr=2&amp;ss=1">Next</a>',
        headers={"content-type": "text/html"}))
    result = src.fetch_remote_jobs(
        "icims", "careers-acme", 12,
        ats_url="https://careers-acme.icims.com/jobs", client=client)
    assert result.complete is False
    assert any("pagination exceeded" in error for error in result.errors)


def test_successfactors_detail_truncation_is_incomplete(monkeypatch):
    monkeypatch.setattr(
        "backend.tools.company_enrichment.public_http_url", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(src, "MAX_PUBLIC_JOB_DETAILS", 1)

    def handler(request):
        if request.url.path == "/search/":
            return httpx.Response(200, text='''
              <a href="/job/Remote-One-US/101/">One</a>
              <a href="/job/Remote-Two-US/102/">Two</a>
            ''', headers={"content-type": "text/html"})
        return httpx.Response(200, text='''
          <h1>Remote Support</h1><div>Location:</div><div>Remote - United States</div>
          <p>This is a fully remote role.</p>
        ''', headers={"content-type": "text/html"})

    result = src.fetch_remote_jobs(
        "successfactors", "acme", 13,
        ats_url="https://careers.acme.test/index", client=_client(handler))
    assert result.complete is False
    assert len(result) == 1
    assert any("details exceeded bounded limit" in error for error in result.errors)


@pytest.mark.parametrize(("ats", "ats_url"), [
    ("icims", "https://careers-acme.icims.com/jobs"),
    ("successfactors", "https://careers.acme.test/index"),
])
def test_public_board_unparseable_detail_is_incomplete(monkeypatch, ats, ats_url):
    monkeypatch.setattr(
        "backend.tools.company_enrichment.public_http_url", lambda *_args, **_kwargs: True)

    def handler(request):
        if request.url.path in {"/jobs/search", "/search/"}:
            link = ("/jobs/101/remote/job" if ats == "icims"
                    else "/job/Remote-Support-US/101/")
            return httpx.Response(200, text=f'<a href="{link}">Remote Support</a>',
                                  headers={"content-type": "text/html"})
        return httpx.Response(200, text=(
            "<html><title>Access Denied</title><body>Access denied. "
            "Verify you are human.</body></html>"),
                              headers={"content-type": "text/html"})

    result = src.fetch_remote_jobs(
        ats, "acme", 13, ats_url=ats_url, client=_client(handler))
    assert result == []
    assert result.complete is False
    assert any("unparseable job detail" in error for error in result.errors)


def test_eightfold_without_token_is_incomplete_and_makes_no_request(monkeypatch):
    monkeypatch.delenv("EIGHTFOLD_API_TOKEN", raising=False)
    calls = []
    client = _client(lambda request: calls.append(str(request.url)) or httpx.Response(500))
    result = src.fetch_remote_jobs(
        "eightfold", "citi:citi.com", 14,
        ats_url="https://citi.eightfold.ai/careers", client=client)
    assert result == []
    assert result.complete is False
    assert "requires EIGHTFOLD_API_TOKEN" in result.errors[0]
    assert calls == []


def test_eightfold_authenticated_positions_are_remote_filtered(monkeypatch):
    monkeypatch.delenv("EIGHTFOLD_API_TOKEN", raising=False)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert request.headers["authorization"] == "Bearer secret-test-token"
        return httpx.Response(200, json={"data": [
            {"positionId": "EF-1", "name": "Remote Support", "isRemote": True,
             "locations": [{"name": "United States"}],
             "jobDescription": "Help customers from home.",
             "atsData": {"applyUrl": "https://citi.eightfold.ai/careers/job/EF-1"}},
            {"positionId": "EF-2", "name": "Office Support",
             "locations": [{"name": "New York, NY"}],
             "jobDescription": "This is an in-office role."},
        ]})

    result = src.fetch_remote_jobs(
        "eightfold", "citi:citi.com", 14,
        ats_url="https://citi.eightfold.ai/careers", client=_client(handler),
        eightfold_token="secret-test-token")
    assert result.complete is True
    assert [job["source_job_id"] for job in result] == ["EF-1"]
    assert result[0]["apply_url"].endswith("/EF-1")
    assert "/api/v2/core/positions?" in calls[0]


def test_oracle_parser_keeps_remote_detail_and_secondary_locations():
    details = [{
        "Id": "77", "Title": "Remote Service Agent", "Category": "Support",
        "ExternalDescriptionStr": "<h2>Requirements</h2><p>Clear writing</p>",
        "PrimaryLocation": "Remote, United States", "JobSchedule": "Full time",
        "_listing": {"WorkplaceType": "Remote", "secondaryLocations": [
            {"Name": "Remote, Canada"},
        ]},
    }]
    job = src.parse_oracle_jobs(
        details, 4, "acme", public_base_url=
        "https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1")[0]
    assert job["source_job_id"] == "77"
    assert job["requirements"] == "Clear writing"
    assert job["locations"] == ["Remote, United States", "Remote, Canada"]


def test_oracle_parser_combines_available_jd_fields_and_keeps_qualifications_separate():
    details = [{
        "Id": "78", "Title": "Remote Claims Specialist",
        "WorkplaceType": "Remote", "PrimaryLocation": "United States",
        "ShortDescriptionStr": "<p>Help customers resolve claims.</p>",
        "ExternalDescriptionStr": "<p>Help customers resolve claims.</p>",
        "ExternalResponsibilitiesStr": "<p>Review and document each claim.</p>",
        "ExternalQualificationsStr": "<ul><li>Two years of claims experience</li></ul>",
        "CorporateDescriptionStr": "<p>We serve customers nationwide.</p>",
    }]

    job = src.parse_oracle_jobs(
        details, 4, "acme", public_base_url=
        "https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1")[0]

    assert job["description"].count("Help customers resolve claims.") == 1
    assert "Review and document each claim." in job["description"]
    assert "We serve customers nationwide." in job["description"]
    assert "Two years of claims experience" not in job["description"]
    assert job["requirements"] == "Two years of claims experience"


def test_oracle_parser_uses_short_description_and_does_not_invent_empty_text():
    short_only = [{
        "Id": "79", "Title": "Service Agent", "WorkplaceType": "Remote",
        "ShortDescriptionStr": "<p>Support customers from home.</p>",
    }]
    empty = [{
        "Id": "80", "Title": "Remote Service Agent", "WorkplaceType": "Remote",
    }]

    short_job = src.parse_oracle_jobs(
        short_only, 4, "acme", public_base_url="https://example.test/jobs")[0]
    empty_job = src.parse_oracle_jobs(
        empty, 4, "acme", public_base_url="https://example.test/jobs")[0]

    assert short_job["description"] == "Support customers from home."
    assert empty_job["description"] == ""
    assert empty_job["description_html"] == ""
    assert empty_job["requirements"] == ""


def test_workday_context_skips_lowercase_locale_before_site():
    cxs, public, site = src._workday_context(
        "ghr", "https://ghr.wd1.myworkdayjobs.com/en-us/lateral-us/login")
    assert site == "lateral-us"
    assert cxs.endswith("/wday/cxs/ghr/lateral-us")
    assert public.endswith("/lateral-us")


def test_ashby_keeps_maximum_fields_and_raw_payload():
    payload = {"jobs": [{
        "id": "ash-1", "title": "Data Entry", "isRemote": True, "workplaceType": "Remote",
        "location": "USA", "department": "Operations", "employmentType": "Contract",
        "descriptionHtml": "<h2>Benefits</h2><p>Flexible time</p>",
        "jobUrl": "https://jobs.ashbyhq.com/acme/ash-1", "applyUrl": "https://jobs.ashbyhq.com/acme/ash-1/application",
        "publishedAt": "2026-08-01T00:00:00Z",
        "compensation": {"minValue": 20, "maxValue": 25, "currencyCode": "USD", "unit": "hour"},
        "applicationFormQuestions": [{"id": "q1", "title": "Timezone?"}],
        "unknownFutureField": {"preserved": True},
    }]}
    job = src.parse_ashby_jobs(payload, 2, "acme")[0]
    assert job["benefits"] == "Flexible time"
    assert job["salary_interval"] == "hour"
    assert job["question_count"] == 1
    assert job["questions_state"] == "available"
    assert job["raw_payload"]["unknownFutureField"] == {"preserved": True}


def test_workable_requires_real_remote_signal_and_keeps_location():
    payload = {"jobs": [
        {"shortcode": "YES", "title": "Support", "telecommuting": True,
         "location": {"location_str": "United States"}, "description": "<p>Support users</p>",
         "url": "https://apply.workable.com/acme/j/YES/"},
        {"shortcode": "NO", "title": "Support", "telecommuting": False,
         "location": {"location_str": "Austin, TX"}, "description": "<p>Support users</p>"},
    ]}
    jobs = src.parse_workable_jobs(payload, 3, "acme")
    assert [j["source_job_id"] for j in jobs] == ["YES"]
    assert jobs[0]["location_raw"] == "United States"


def test_smartrecruiters_fetches_all_pages_and_full_remote_detail_only():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith("/postings"):
            offset = int(request.url.params["offset"])
            rows = [{"id": "remote-1", "name": "Support", "location": {"remote": True}}] \
                if offset == 0 else [{"id": "office-1", "name": "Office", "location": {"city": "Boston"}}]
            return httpx.Response(200, json={"totalFound": 2, "content": rows})
        if request.url.path.endswith("/remote-1"):
            return httpx.Response(200, json={
                "id": "remote-1", "name": "Remote Support",
                "location": {"remote": True, "country": "us", "region": "US", "city": "Remote"},
                "department": {"label": "Customer Care"},
                "typeOfEmployment": {"label": "Full-time"},
                "releasedDate": "2026-08-20T12:00:00Z",
                "ref": "https://jobs.smartrecruiters.com/Acme/remote-1",
                "jobAd": {"sections": {
                    "jobDescription": {"title": "Job Description", "text": "<p>Help customers.</p>"},
                    "qualifications": {"title": "Qualifications", "text": "<ul><li>Clear writing</li></ul>"},
                    "additionalInformation": {"title": "Benefits", "text": "<p>Health plan</p>"},
                }},
                "futureField": {"keep": True},
            })
        return httpx.Response(200, json={
            "id": "office-1", "name": "Office", "location": {"city": "Boston"},
            "jobAd": {"sections": {"jobDescription": {"text": "<p>On-site role.</p>"}}},
        })

    jobs = src.fetch_remote_jobs("smartrecruiters", "Acme", 8, client=_client(handler))
    assert len(jobs) == 1
    job = jobs[0]
    assert job["source_job_id"] == "remote-1"
    assert job["description"] == "Job Description\nHelp customers.\nQualifications\nClear writing\nBenefits\nHealth plan"
    assert job["requirements"] == "Clear writing"
    assert job["benefits"] == "Health plan"
    assert job["employment_type"] == "Full-time"
    assert job["questions_state"] == "not_available"
    assert job["raw_payload"]["futureField"] == {"keep": True}
    assert len([url for url in calls if url.endswith("/postings/remote-1")]) == 1
    assert len([url for url in calls if "/postings?" in url]) == 2


def test_workday_uses_exact_cxs_site_paginates_and_fetches_full_detail():
    calls = []

    def handler(request):
        calls.append((request.method, str(request.url)))
        if request.url.path.endswith("/jobs"):
            assert request.method == "POST"
            body = json.loads(request.content)
            assert body["appliedFacets"] == {}
            assert body["searchText"] == "remote"
            row = ({"title": "Support", "externalPath": "/job/US/Support_R1", "locationsText": "Remote"}
                   if body["offset"] == 0 else
                   {"title": "Office", "externalPath": "/job/US/Office_R2", "locationsText": "Austin"})
            return httpx.Response(200, json={"total": 2, "jobPostings": [row]})
        if request.url.path.endswith("Support_R1"):
            return httpx.Response(200, json={"jobPostingInfo": {
                "id": "R1", "jobReqId": "R1", "title": "Customer Support",
                "jobDescription": "<h2>Requirements</h2><p>Clear writing</p><h2>Benefits</h2><p>Medical</p>",
                "location": "Remote - United States", "additionalLocations": ["Remote - Canada"],
                "jobFamily": "Customer Care", "timeType": "Full time",
                "startDate": "2026-08-01", "postedOn": "Posted 2 Days Ago",
            }, "unmodeled": {"preserved": True}})
        return httpx.Response(200, json={"jobPostingInfo": {
            "id": "R2", "title": "Office", "jobDescription": "<p>In-office role.</p>",
            "location": "Austin, TX",
        }})

    jobs = src.fetch_remote_jobs(
        "workday", "acme", 9, ats_url="https://acme.wd5.myworkdayjobs.com/en-US/External",
        client=_client(handler),
    )
    assert len(jobs) == 1
    job = jobs[0]
    assert job["source_job_id"] == "R1"
    assert job["requirements"] == "Clear writing"
    assert job["benefits"] == "Medical"
    assert job["locations"] == ["Remote - United States", "Remote - Canada"]
    assert job["job_url"] == "https://acme.wd5.myworkdayjobs.com/External/job/US/Support_R1"
    assert job["questions_state"] == "not_available"
    assert job["posted_at"] == "2026-08-01"
    assert job["raw_payload"]["unmodeled"] == {"preserved": True}
    assert len([url for method, url in calls if method == "POST" and url.endswith("/jobs")]) == 2


def test_workday_repeated_page_is_rejected_instead_of_looping():
    def handler(request):
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={
                "total": 999,
                "jobPostings": [{"title": "Support", "externalPath": "/job/R1"}],
            })
        return httpx.Response(200, json={"jobPostingInfo": {}})

    with pytest.raises(src.JobSourceError, match="made no progress"):
        src.fetch_remote_jobs(
            "workday", "acme", 9,
            ats_url="https://acme.wd5.myworkdayjobs.com/External",
            client=_client(handler),
        )


def test_workday_requires_verified_career_url():
    client = _client(lambda request: httpx.Response(500))
    with pytest.raises(ValueError, match="requires ats_url"):
        src.fetch_remote_jobs("workday", "acme", client=client)
    with pytest.raises(ValueError, match="public myworkdayjobs"):
        src.fetch_remote_jobs("workday", "acme", ats_url="https://example.com/jobs", client=client)
    assert src._workday_context(
        "ignored", "https://acme.wd5.myworkdayjobs.com/wday/cxs/tenant/Site"
    )[:2] == (
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/tenant/Site",
        "https://acme.wd5.myworkdayjobs.com/Site",
    )


@pytest.mark.parametrize("ats,path", [
    ("lever", "/v0/postings/acme"),
    ("ashby", "/posting-api/job-board/acme"),
    ("workable", "/api/v1/widget/accounts/acme"),
])
def test_public_fetch_endpoints_and_user_supplied_client(ats, path):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        assert request.headers["user-agent"] == src.USER_AGENT
        if ats == "lever":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"jobs": []})

    assert src.fetch_remote_jobs(ats, "acme", 1, client=_client(handler)) == []
    assert path in seen[0]


def test_retry_is_bounded_and_retries_503(monkeypatch):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(src.time, "sleep", lambda _: None)
    assert src.fetch_remote_jobs("lever", "acme", 1, client=_client(handler), retries=3) == []
    assert count == 3


def test_non_retryable_http_error_and_bad_input():
    client = _client(lambda request: httpx.Response(404))
    with pytest.raises(src.JobSourceError):
        src.fetch_remote_jobs("lever", "missing", 1, client=client)
    with pytest.raises(ValueError):
        src.fetch_remote_jobs("unknown", "x", 1, client=client)
    with pytest.raises(ValueError):
        src.fetch_remote_jobs("lever", "", 1, client=client)


def test_jd_only_remote_must_be_strong_and_conflict_free():
    strong = {"jobs": [{"id": "1", "title": "Agent", "location": "US",
                         "descriptionHtml": "<p>This is a 100% remote position.</p>"}]}
    ambiguous = {"jobs": [{"id": "2", "title": "Agent", "location": "US",
                            "descriptionHtml": "<p>We use remote collaboration tools.</p>"}]}
    conflict = {"jobs": [{"id": "3", "title": "Agent", "location": "US",
                           "descriptionHtml": "<p>Fully remote with a hybrid schedule.</p>"}]}
    assert len(src.parse_ashby_jobs(strong, 1, "x")) == 1
    assert src.parse_ashby_jobs(ambiguous, 1, "x") == []
    assert src.parse_ashby_jobs(conflict, 1, "x") == []


def test_parenthetical_remote_is_strong_but_remote_domain_title_is_not():
    remote = {"jobs": [{
        "id": "1", "title": "Customer Service Agent (Remote)", "location": "United States",
        "descriptionHtml": "<p>Help customers.</p>",
    }]}
    domain = {"jobs": [{
        "id": "2", "title": "Remote Sensing Engineer", "location": "Denver, CO",
        "descriptionHtml": "<p>Satellite imagery role in our office.</p>",
    }]}
    assert len(src.parse_ashby_jobs(remote, 1, "x")) == 1
    assert src.parse_ashby_jobs(domain, 1, "x") == []
