from backend.tools.company_apply_policy import evaluate, revalidation_hash


def profile(**overrides):
    value = {
        "id": "real_us", "full_name": "Real Person", "email": "real@takhet.com",
        "phone": "+1 212 555 9000", "country": "United States", "state": "NY",
        "work_authorization": "US Citizen", "needs_sponsorship": "No",
        "mailbox": "real@takhet.com", "resume": {
            "summary": "Customer support specialist",
            "experience": [{"title": "Support Agent", "bullets": ["Helped customers"]}],
            "skills": ["Customer support"], "education": [{"degree": "BA"}],
            "personal_info": {"email": "real@takhet.com"},
        },
    }
    value.update(overrides)
    return value


def job(**overrides):
    value = {
        "id": 10, "status": "active", "remote_type": "remote",
        "questions_status": "success", "title": "Customer Support Agent",
        "location_raw": "Remote - United States",
        "description": "Remote customer support. Help customers and resolve issues.",
        "apply_url": "https://jobs.example.test/10/apply", "content_hash": "job-hash",
        "question_set_hash": "q-hash", "questions": [
            {"label": "Why are you interested?", "required": True},
        ],
    }
    value.update(overrides)
    return value


def test_real_region_matched_profile_is_allowed_but_still_requires_approval():
    result = evaluate(job(), profile(), {"availability": "Immediate"}, min_fit=0)
    assert result["allowed"] is True
    assert result["candidate_region"] == "US"
    assert result["review_reasons"] == [
        "human approval is required before opening the live form"]


def test_unknown_or_wrong_geography_fails_closed():
    unknown = evaluate(job(location_raw="Remote", description="Help customers."),
                       profile(), {"x": 1}, min_fit=0)
    assert "job geography is unknown" in unknown["blocking_reasons"]
    canada = evaluate(job(location_raw="Remote - Canada"), profile(), {"x": 1}, min_fit=0)
    assert "candidate region US is not eligible" in canada["blocking_reasons"]


def test_sample_synthetic_missing_facts_and_fake_contact_are_blocked():
    result = evaluate(job(), profile(
        id="sample", is_sample=True, is_synthetic=True,
        phone="+1 212 555-0101", email="x@example.com", mailbox=""), {}, min_fit=0)
    text = " | ".join(result["blocking_reasons"])
    assert "sample profile" in text
    assert "synthetic profile" in text
    assert "reserved-fictional" in text
    assert "candidate facts are missing" in text
    assert "verified recruiter reply route" in text


def test_sensitive_legal_and_demographic_questions_are_always_reviewed():
    result = evaluate(job(questions=[
        {"label": "Disability status"}, {"label": "I certify this is accurate"},
        {"label": "Portfolio URL"},
    ]), profile(), {"x": 1}, min_fit=0)
    assert result["review_reasons"][:2] == [
        "Disability status", "I certify this is accurate"]


def test_incomplete_questions_http_and_low_fit_are_blocked():
    result = evaluate(job(questions_status="failed", apply_url="http://bad.test/apply"),
                      profile(resume={"summary": "Unrelated"}), {"x": 1}, min_fit=100)
    text = " | ".join(result["blocking_reasons"])
    assert "complete application questions" in text
    assert "verified HTTPS" in text
    assert "below 100" in text


def test_revalidation_hash_changes_with_job_questions_profile_or_facts():
    base = revalidation_hash(job(), profile(), {"x": 1})
    assert revalidation_hash(job(content_hash="changed"), profile(), {"x": 1}) != base
    assert revalidation_hash(job(question_set_hash="changed"), profile(), {"x": 1}) != base
    assert revalidation_hash(job(), profile(phone="+1 646 555 9000"), {"x": 1}) != base
    assert revalidation_hash(job(), profile(), {"x": 2}) != base
