"""Mass-hiring lanes MUST run the résumé through the SAME attractiveness engine as /catalog.

Owner directive 2026-09-23 («движок привлекательности обязательно при подаче тоже»): every
mass-hiring auto-apply uploads a role-TAILORED résumé, not a generic one — role-targeted summary +
the persona's OWN JD-matched-skills group + JD-relevance ordering, STRICTLY no-fabrication. The row
carries no JD body, so `mass_hiring_apply._jd_proxy` supplies a truthful title+category descriptor
as the tailor's JD *input* (nothing is added to the résumé). Everything is guarded: a tailor/LLM
failure falls back to the persona's untailored base résumé — never empty, never raises.

Network-free: the tailor's LLM call is monkeypatched to raise, so the deterministic path runs.
Mirrors test_tailor_attractiveness.py's no-fabrication assertions.
"""
import re

import pytest

import backend.services.tailor.tailor as T
from backend.services.tailor.render import render_text
from backend.tools import mass_hiring_apply as mha


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """Force the deterministic tailoring path (no network) for every test here."""
    def _boom(*a, **k):
        raise RuntimeError("LLM disabled in test")
    monkeypatch.setattr(T, "_llm_complete", _boom)


def _cand():
    """A synthetic-persona shape (profile.resume) like synth_persona builds — CSR skills."""
    return {
        "profile": {
            "id": "demo_test_cand1", "full_name": "Jamie Rivers", "country": "United States",
            "resume": {
                "personal_info": {"name": "Jamie Rivers", "full_name": "Jamie Rivers",
                                  "email": "j@takhet.com", "phone": "+1 (415) 555-0100",
                                  "location": "Austin, TX"},
                "headline": "Support Professional",
                "preferred_titles": ["Customer Support Representative"],
                "summary": "Dedicated professional focused on delivering results.",
                "experience": [{"company": "Helpline Co", "title": "Customer Support Representative",
                                "dates": "2021-Present", "bullets": [
                                    "Maintained team documentation and onboarding guides.",
                                    "Resolved 45 tickets/day via Zendesk live chat with 96% CSAT.",
                                    "Handled escalations and de-escalation for billing disputes."]}],
                "skills_grouped": {"Skills": ["Zendesk", "live chat", "troubleshooting",
                                              "de-escalation", "email support", "communication",
                                              "CRM", "Salesforce"]},
                "education": [{"degree": "BA Communications", "school": "State U", "year": "2017"}],
                "certifications": [],
            },
        },
        "facts": {"languages": ["English"]},
    }


ROW = {"id": 42, "title": "Remote Customer Service Representative",
       "company": "Acme Support Co", "company_key": "acme",
       "category": "customer_support", "location_raw": "Remote, United States",
       "apply_url": "https://acme.avature.net/careers/apply/1"}


def _tokens(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


# ---- JD proxy: truthful, no fabricated persona data --------------------------
def test_jd_proxy_uses_title_company_category_only():
    job = mha._job_from_row(ROW)
    proxy = mha._jd_proxy(job)
    assert "Remote Customer Service Representative" in proxy
    assert "Acme Support Co" in proxy
    # category descriptor surfaced (customer_support lexicon) so matched-skills can fire
    assert "customer service" in proxy.lower()
    # the proxy is a ROLE descriptor — it must not carry the persona's identity
    assert "Jamie" not in proxy and "555" not in proxy


def test_job_from_row_carries_category():
    job = mha._job_from_row(ROW)
    assert job["category"] == "customer_support"
    assert job["description"] == ""  # tailored_draft supplies the proxy


# ---- résumé is TAILORED to the role (not generic) ----------------------------
def test_resume_is_role_targeted_not_generic():
    cand = _cand()
    d = mha.tailored_draft(mha._job_from_row(ROW), cand)
    tailored = d["resume"]
    # 1) role-targeted headline aligns to the posting's title
    assert "Customer" in (tailored.get("headline") or "")
    # 2) the persona's OWN JD-matched skills lead the SKILLS section
    grp = tailored.get("skills_grouped", {})
    assert "Key skills for this role" in grp, grp
    matched = grp["Key skills for this role"]
    assert matched, "matched-skills group must be non-empty when the JD proxy has real signal"
    # at least one genuine CSR skill from the base résumé surfaced
    assert any(s.lower() in {"troubleshooting", "de-escalation", "communication", "crm"}
               for s in matched)
    # 3) targeted summary leads with the aligned role
    assert "Customer" in (tailored.get("summary") or "")


# ---- strict no-fabrication ---------------------------------------------------
def test_no_fabricated_skill_company_or_number():
    cand = _cand()
    base = cand["profile"]["resume"]
    base_pool = _tokens(render_text(base))
    d = mha.tailored_draft(mha._job_from_row(ROW), cand)
    tailored = d["resume"]

    # every skill in the tailored résumé is a base skill (no injected JD keyword)
    base_skills = {s.lower() for items in base["skills_grouped"].values() for s in items}
    for items in tailored.get("skills_grouped", {}).values():
        for s in items:
            assert s.lower() in base_skills, f"fabricated skill: {s}"

    # every number in the tailored output already appears in the base résumé
    base_nums = set(re.findall(r"\d+", render_text(base)))
    for n in re.findall(r"\d+", render_text(tailored)):
        assert n in base_nums, f"fabricated number: {n}"

    # no experience company invented
    base_cos = {e["company"].lower() for e in base["experience"]}
    for e in tailored.get("experience", []):
        assert e.get("company", "").lower() in base_cos


# ---- guard: tailoring failure falls back to the base résumé, never breaks ----
def test_tailor_failure_falls_back_to_base_resume(monkeypatch):
    cand = _cand()

    def _raise(*a, **k):
        raise RuntimeError("generate_draft blew up")
    monkeypatch.setattr(mha.catalog_drafts, "generate_draft", _raise)

    d = mha.tailored_draft(mha._job_from_row(ROW), cand)   # must NOT raise
    assert d.get("_tailor_failed") is True
    assert d["resume"] == cand["profile"]["resume"]        # base résumé, not empty
    pdf = mha.resume_pdf_bytes(d, cand)
    assert pdf and pdf[:4] == b"%PDF"                        # a real PDF, never empty bytes


def test_resume_pdf_bytes_falls_back_when_draft_resume_empty():
    cand = _cand()
    pdf = mha.resume_pdf_bytes({"resume": {}}, cand)         # empty draft résumé
    assert pdf and pdf[:4] == b"%PDF"                         # base résumé rendered instead


def test_tailored_resume_renders_pdf():
    cand = _cand()
    d = mha.tailored_draft(mha._job_from_row(ROW), cand)
    pdf = mha.resume_pdf_bytes(d, cand)
    assert pdf and pdf[:4] == b"%PDF"
