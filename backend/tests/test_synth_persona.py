"""Unit tests for the synthetic demo-candidate generator (no network — the LLM path is
forced to fall back). Verifies region->country mapping, a valid profile/résumé shape, a
derived @takhet.com email, and that the persona is flagged synthetic."""
from backend.tools import synth_persona as sp


def test_region_of():
    assert sp._region_of({"regions": ["US"]}) == "US"
    assert sp._region_of({"regions": ["US", "CA"]}) == "US"      # US wins
    assert sp._region_of({"regions": ["CA"]}) == "CA"
    assert sp._region_of({"regions": ["OTHER"]}) == "KZ"          # rest-of-world
    assert sp._region_of({"regions": ["UK"]}) == "KZ"
    assert sp._region_of({"regions": []}) == "KZ"                 # untagged
    assert sp._region_of({}) == "KZ"


def _job(region):
    return {"title": "Data Analyst", "company": "Salmon",
            "description": "Remote data analyst role.", "regions": [region]}


def test_fallback_persona_builds_valid_candidate():
    for region, country in (("US", "United States"), ("CA", "Canada"), ("OTHER", "Kazakhstan")):
        job = _job(region)
        r = sp._region_of(job)
        raw = sp._fallback_persona(job, r)
        cand = sp._build_candidate(raw, r, job)
        p = cand["profile"]
        assert p["full_name"]                                    # got a name
        assert p["country"] == country                           # region-appropriate
        assert p["email"].endswith("@takhet.com")                # working demo mailbox
        assert p["email"] == sp.derive_email(p["full_name"])     # email derived from name
        assert p["is_synthetic"] is True                         # flagged demo persona
        assert p["resume"]["experience"]                         # has experience to tailor
        assert p["resume"]["personal_info"]["email"] == p["email"]


def test_synth_persona_uses_fallback_without_llm(monkeypatch):
    # force the LLM path to fail -> deterministic fallback still yields a full candidate
    monkeypatch.setattr(sp, "_llm_persona", lambda job, region: None)
    cand = sp.synth_persona(_job("OTHER"))
    p = cand["profile"]
    assert p["country"] == "Kazakhstan"
    assert p["email"].endswith("@takhet.com")
    assert p["id"].startswith("demo_kz_")
    assert p["work_authorization"] == "Kazakhstan Citizen"


def test_synth_persona_honours_llm_when_present(monkeypatch):
    monkeypatch.setattr(sp, "_llm_persona", lambda job, region: {
        "full_name": "Steve Jobs", "city": "Almaty", "years_experience": 8,
        "summary": "s", "experience": [{"company": "X", "title": "Y", "bullets": ["b"]}],
        "education": [{"degree": "BSc CS", "school": "U", "field": "CS", "year": "2016"}],
        "skills": ["python", "sql"]})
    cand = sp.synth_persona(_job("OTHER"))
    assert cand["profile"]["full_name"] == "Steve Jobs"
    assert cand["profile"]["email"] == "steve.jobs@takhet.com"
