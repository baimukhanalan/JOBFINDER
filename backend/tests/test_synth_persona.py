"""Unit tests for the synthetic demo-candidate generator (no network — the LLM path is
forced to fall back). Verifies country-from-location, a valid profile/résumé shape, a
derived @takhet.com email, a street address, and that the persona is flagged synthetic."""
import re

from backend.tools import synth_persona as sp


def test_country_of_from_location():
    assert sp._country_of({"location": "Remote U.S.", "regions": None}) == "United States"
    assert sp._country_of({"location": "Amsterdam, Netherlands"}) == "Netherlands"
    assert sp._country_of({"location": "United Kingdom (Remote)"}) == "United Kingdom"
    assert sp._country_of({"location": "Brazil (Remote)"}) == "Brazil"
    assert sp._country_of({"location": "Tbilisi, Georgia"}) == "Georgia"
    assert sp._country_of({"location": "Toronto, ON, Canada"}) == "Canada"


def test_country_of_multi_country():
    # rule: several countries -> Kazakhstan if it's one of them, else the FIRST in the text
    assert sp._country_of({"location": "Kazakhstan; Kyrgyzstan"}) == "Kazakhstan"
    assert sp._country_of({"location": "Georgia; Kazakhstan; Poland"}) == "Kazakhstan"
    assert sp._country_of({"location": "Serbia; Kazakhstan"}) == "Kazakhstan"
    assert sp._country_of({"location": "Kyrgyzstan; Uzbekistan"}) == "Kyrgyzstan"   # first, no KZ
    assert sp._country_of({"location": "Georgia; Poland"}) == "Georgia"
    assert sp._country_of({"location": "United States; Canada"}) == "United States"


def test_country_of_falls_back_to_region_then_kz():
    assert sp._country_of({"location": "Remote", "regions": ["US"]}) == "United States"
    assert sp._country_of({"location": "", "regions": ["CA"]}) == "Canada"
    assert sp._country_of({"location": "Remote", "regions": ["UK"]}) == "United Kingdom"
    assert sp._country_of({"location": "Remote", "regions": ["OTHER"]}) == "Kazakhstan"
    assert sp._country_of({"regions": []}) == "Kazakhstan"


def _job(location, region=None):
    return {"title": "Data Analyst", "company": "Acme", "description": "Remote data role.",
            "location": location, "regions": ([region] if region else None)}


def test_fallback_persona_builds_valid_candidate():
    for loc, country, cit in (("Remote U.S.", "United States", "U.S. Citizen"),
                              ("Amsterdam, Netherlands", "Netherlands", "Netherlands Citizen"),
                              ("Tbilisi, Georgia", "Georgia", "Georgia Citizen")):
        job = _job(loc)
        c = sp._country_of(job)
        cand = sp._build_candidate(sp._fallback_persona(job, c), c, job)
        p = cand["profile"]
        assert p["full_name"]
        assert p["country"] == country
        assert p["work_authorization"] == cit
        assert p["email"].endswith("@takhet.com")
        # demo email = first.last<NUM>@takhet.com (numeric suffix for a unique mailbox)
        base = sp.derive_email(p["full_name"])[: -len("@takhet.com")]
        assert re.match(rf"^{re.escape(base)}\d+@takhet\.com$", p["email"])
        assert p["is_synthetic"] is True
        assert p["street_address"]                               # required-address gap fixed
        assert p["resume"]["experience"]
        assert "," not in p["city"]                              # city is just the city


def test_synth_persona_uses_fallback_without_llm(monkeypatch):
    monkeypatch.setattr(sp, "_llm_persona", lambda job, country, name="": None)
    cand = sp.synth_persona(_job("Brazil (Remote)"))
    p = cand["profile"]
    assert p["country"] == "Brazil"
    assert p["work_authorization"] == "Brazil Citizen"
    assert p["email"].endswith("@takhet.com")
    assert p["street_address"]


def test_synth_persona_honours_llm_content_but_our_name(monkeypatch):
    # The résumé CONTENT comes from the LLM, but the NAME is OUR history-avoided pick — the
    # stateless local model otherwise collapses to the same handful of names on every fill,
    # and must never be trusted to avoid a famous name either ("Steve Jobs" below).
    monkeypatch.setattr(sp, "_llm_persona", lambda job, country, name="": {
        "full_name": "Steve Jobs", "city": "London", "street_address": "1 King St",
        "years_experience": 8, "summary": "s",
        "experience": [{"company": "X", "title": "Y", "bullets": ["b"]}],
        "education": [{"degree": "BSc CS", "school": "U", "field": "CS", "year": "2016"}],
        "skills": ["python", "sql"]})
    cand = sp.synth_persona(_job("London, United Kingdom"))
    p = cand["profile"]
    assert p["full_name"] != "Steve Jobs"                 # our pick overrides the LLM name
    first, last = p["full_name"].split(" ", 1)
    uk_first, uk_last = sp._NAMES["United Kingdom"]
    assert first in uk_first and last in uk_last          # from the UK bank
    assert p["email"].endswith("@takhet.com") and re.search(r"\d+@", p["email"])  # numeric suffix
    assert p["id"].startswith("demo_")
    assert p["resume"]["experience"][0]["company"] == "X"  # LLM content honoured
    assert p["country"] == "United Kingdom"
    assert p["work_authorization"] == "British Citizen"


def test_synth_persona_names_do_not_repeat(monkeypatch, tmp_path):
    # Consecutive fills for one country must not keep showing the same person.
    monkeypatch.setattr(sp, "_llm_persona", lambda job, country, name="": None)
    monkeypatch.setattr(sp, "_USED_NAMES_PATH", str(tmp_path / "used.json"))
    names = [sp.synth_persona(_job("Austin, TX, United States"))["profile"]["full_name"]
             for _ in range(30)]
    assert len(set(names)) == 30                           # all distinct across 30 fills
