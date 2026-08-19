"""Tests for the apply pipeline: profile load -> tailor -> render -> prefill invariants.

Run: PYTHONPATH=. python3 -m pytest backend/tests/test_pipeline.py -q

These are pure-logic tests: no network, no browser, no API key. Async checks use
asyncio.run() so we don't depend on pytest-asyncio.
"""
import asyncio

import pytest

from backend.applier.runner import prefill_application
from backend.profiles.store import Profile, get_profile, load_profiles
from backend.services.tailor.render import render_text
from backend.services.tailor.tailor import tailor_resume

SAMPLE_JD = (
    "We are hiring a Customer Support Specialist for our SaaS platform. "
    "You'll handle Tier 1 and Tier 2 troubleshooting over email, live chat, and phone "
    "using Zendesk and Intercom. Strong written communication, escalation management, "
    "knowledge base ownership, and SLA / CSAT focus are required. API debugging with "
    "Postman and basic SQL are a plus. Fully remote."
)


def _base_resume() -> dict:
    return get_profile("sample").resume


def _base_companies(resume: dict) -> set[str]:
    return {e.get("company", "").lower() for e in resume.get("experience", [])}


def _base_bullets(resume: dict) -> set[str]:
    bullets: set[str] = set()
    for e in resume.get("experience", []):
        bullets.update(e.get("bullets", []))
    return bullets


def _tailored() -> dict:
    return tailor_resume(
        _base_resume(),
        job_title="Customer Support Specialist",
        job_company="Acme SaaS",
        job_description=SAMPLE_JD,
    )


# 1) load_profiles()/get_profile("sample") -> Profile with non-empty resume.
def test_get_profile_sample_has_resume():
    profiles = load_profiles()
    assert "sample" in profiles, f"expected 'sample' in {sorted(profiles)}"

    profile = get_profile("sample")
    assert isinstance(profile, Profile)
    assert profile.id == "sample"
    assert profile.full_name, "profile must have a name"

    resume = profile.resume
    assert isinstance(resume, dict)
    assert resume, "resume must be a non-empty dict"
    assert resume.get("experience"), "resume must have experience entries"


# 2) NO-FABRICATION: companies are a subset of base companies; match_score is int 0..100;
#    kept bullets are a subset of base bullets.
def test_tailor_no_fabrication():
    base = _base_resume()
    base_companies = _base_companies(base)
    base_bullets = _base_bullets(base)
    assert base_companies, "base resume must list companies for this test to mean anything"
    assert base_bullets, "base resume must have bullets"

    tailored = _tailored()

    # Every tailored company must be one of the base companies (no invented employers).
    for entry in tailored.get("experience", []):
        company = entry.get("company", "").lower()
        assert company in base_companies, (
            f"tailored company {company!r} not in base companies {sorted(base_companies)}"
        )

    # match_score is an int in [0, 100].
    score = tailored.get("match_score")
    assert isinstance(score, int), f"match_score must be int, got {type(score)}"
    assert 0 <= score <= 100, f"match_score out of range: {score}"

    # Kept bullets are a subset of the base bullets (none invented / reworded by default path).
    kept_bullets: set[str] = set()
    for entry in tailored.get("experience", []):
        kept_bullets.update(entry.get("bullets", []))
    assert kept_bullets, "tailored resume must keep at least some bullets"
    assert kept_bullets <= base_bullets, (
        f"tailored introduced bullets not in base: {sorted(kept_bullets - base_bullets)}"
    )


# 3) render_text(tailored) contains the candidate name and "EXPERIENCE".
def test_render_text_contains_name_and_experience():
    profile = get_profile("sample")
    tailored = _tailored()

    text = render_text(tailored)
    assert isinstance(text, str) and text.strip()

    # Name is rendered uppercased in the header.
    assert profile.full_name.upper() in text, (
        f"candidate name {profile.full_name!r} missing from rendered text"
    )
    assert "EXPERIENCE" in text, "rendered text must contain the EXPERIENCE section heading"


# 4) NO AUTO-SUBMIT: the engine pre-fills and STOPS. There is deliberately no code
#    path that clicks the final Submit — a human always reviews and submits. This
#    guards against anyone silently re-introducing an auto-submit path.
def test_no_auto_submit_path():
    import inspect

    from backend.applier.strategies.base import ApplyStrategy, GenericStrategy
    # prefill_application exposes no submit toggle
    assert "submit" not in inspect.signature(prefill_application).parameters
    # no strategy can click the final Submit button
    assert not hasattr(ApplyStrategy, "submit_form")
    assert not hasattr(GenericStrategy, "submit_form")


# 5) ANSWER CACHE: new answers are stored, repeated questions are served from
#    cache, and a cached answer never leaks another company's name.
def test_answer_cache_roundtrip_company_portable(tmp_path, monkeypatch):
    from backend import answer_cache
    monkeypatch.setattr(answer_cache, "DB_PATH", str(tmp_path / "cache.db"))

    q = "Why do you want to work at Acme?"
    answer_cache.put_many({q: "I admire Acme's support-first culture."}, company="Acme")

    # same question, different company -> cache hit, answer personalized
    q2 = "Why do you want to work at Zapier?"
    got = answer_cache.get_many([q2], company="Zapier")
    assert got, "normalized question should hit the cache across companies"
    assert "Acme" not in got[q2], f"cached answer leaked the original company: {got[q2]!r}"
    assert "Zapier" in got[q2]

    # unseen question -> miss (would go to the LLM and be written back)
    assert answer_cache.get_many(["What is your WPM typing speed?"], company="Zapier") == {}

    stats = answer_cache.stats()
    assert stats["cached_questions"] == 1 and stats["cache_hits_served"] >= 1
