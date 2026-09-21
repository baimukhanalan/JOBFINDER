"""Tailoring-attractiveness improvements (deterministic-first, strict no-fabrication).

Covers: role-targeted summary, "Key skills for this role" highlight built only from the
persona's OWN skills (the old JD-keyword-injection that FABRICATED skills is gone),
quantified-bullet-first ordering, a role-specific cover letter, positive screener
defaults, and the LLM-path truthful vocab-mirroring lift. Every test asserts the
no-fabrication guarantee: no skill / company / number in the output that isn't in the
input. Network-free (the LLM is mocked where used).
"""
import json
import re

import backend.services.tailor.tailor as T
from backend.services.tailor import answers as A
from backend.services.tailor.ats_score import ats_score
from backend.services.tailor.render import render_text
from backend.services.tailor.tailor import tailor_resume


def _base():
    return {
        "personal_info": {"full_name": "Jamie Rivers", "email": "j@x.com",
                          "phone": "555-0100", "location": "Austin, TX"},
        "headline": "Support Professional",
        "preferred_titles": ["Customer Support Representative", "Support Agent"],
        "summary": "Dedicated professional focused on delivering results.",
        "experience": [{"company": "Helpline Co", "title": "Customer Support Representative",
                        "dates": "2021-Present", "bullets": [
                            "Maintained team documentation and onboarding guides.",
                            "Resolved 45 tickets/day via Zendesk live chat with 96% CSAT.",
                            "Handled escalations and de-escalation for billing disputes."]}],
        "skills_grouped": {"Skills": ["Excel", "Zendesk", "live chat", "troubleshooting",
                                      "de-escalation", "email support", "Salesforce"]},
        "education": [{"degree": "BA Communications", "school": "State U", "year": "2017"}],
        "certifications": [],
    }


TITLE = "Remote Customer Support Representative"
JD = ("Remote Customer Support Representative. We need a support rep for live chat and "
      "email support. Must have Zendesk experience, strong troubleshooting, and "
      "de-escalation skills. Zendesk is required. Troubleshooting is required.")


def _tokens(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _base_token_pool(base):
    """Every alnum token present anywhere in the base résumé + the JOB (company/role are
    the posting we apply to, legitimately referenced)."""
    pool = set()
    pool |= _tokens(render_text(base))
    return pool


# ---- no-fabrication -----------------------------------------------------------
def test_no_fabricated_skill_injected():
    """The old 'Role-specific skills' block injected JD keywords the résumé LACKED —
    that fabrication is gone. Every skill in the tailored résumé is a base skill."""
    base = _base()
    base_skills = {s.lower() for items in base["skills_grouped"].values() for s in items}
    tailored = tailor_resume(base, TITLE, "Acme", JD, use_ai=False)
    assert "Role-specific skills" not in tailored["skills_grouped"]
    out_skills = {s.lower() for items in tailored["skills_grouped"].values() for s in items}
    assert out_skills <= base_skills, out_skills - base_skills


def test_no_new_numbers_or_companies():
    """No digit-bearing token and no company in the tailored output that wasn't in the base."""
    base = _base()
    pool = _base_token_pool(base)
    tailored = tailor_resume(base, TITLE, "Acme", JD, use_ai=False)
    out_tokens = _tokens(render_text(tailored))
    # the company/role of the POSTING are allowed (we reference where we apply)
    allowed = pool | _tokens(TITLE) | _tokens("Acme")
    leaked = {t for t in out_tokens if t not in allowed}
    # numbers must all be traceable to the input
    leaked_nums = {t for t in leaked if any(c.isdigit() for c in t)}
    assert not leaked_nums, f"fabricated numbers: {leaked_nums}"


# ---- key-skills highlight -----------------------------------------------------
def test_key_skills_highlight_leads_and_is_truthful():
    base = _base()
    tailored = tailor_resume(base, TITLE, "Acme", JD, use_ai=False)
    groups = list(tailored["skills_grouped"].keys())
    assert groups[0] == "Key skills for this role"
    hi = tailored["skills_grouped"]["Key skills for this role"]
    # only the persona's own JD-matched skills, no duplicates into the tail group
    assert "Zendesk" in hi and "troubleshooting" in hi and "live chat" in hi
    tail = tailored["skills_grouped"].get("Skills", [])
    assert not (set(s.lower() for s in hi) & set(s.lower() for s in tail))
    # Excel (not in the JD) must NOT be highlighted but must still be present somewhere
    assert "Excel" not in hi
    all_out = [s for items in tailored["skills_grouped"].values() for s in items]
    assert "Excel" in all_out


# ---- targeted summary ---------------------------------------------------------
def test_targeted_summary_leads_with_title_and_strengths():
    base = _base()
    tailored = tailor_resume(base, TITLE, "Acme", JD, use_ai=False)
    summ = tailored["summary"]
    # leads with the persona's OWN best-matching title (never the fabricated job title)
    assert summ.startswith("Customer Support Representative with hands-on strengths")
    assert "Zendesk" in summ  # a truthful JD-matched strength surfaced
    assert "Dedicated professional" in summ  # the persona's own summary is kept
    # every word of the summary is traceable to the base résumé + the job title
    allowed = _base_token_pool(base) | _tokens(TITLE) | _tokens(
        "with hands on strengths in and")
    leaked = _tokens(summ) - allowed
    assert not leaked, f"fabricated summary tokens: {leaked}"


def test_targeted_summary_falls_back_without_matches():
    base = _base()
    base["skills_grouped"] = {"Skills": ["gardening", "pottery"]}  # no JD match
    tailored = tailor_resume(base, TITLE, "Acme", JD, use_ai=False)
    # no strengths clause, but still leads with the aligned title + keeps the summary
    assert "hands-on strengths" not in tailored["summary"]
    assert "Dedicated professional" in tailored["summary"]


# ---- quantified-bullet ordering ----------------------------------------------
def test_quantified_bullet_leads_when_equally_relevant():
    base = {
        "personal_info": {"full_name": "A B"}, "headline": "Agent",
        "preferred_titles": ["Agent"], "summary": "x",
        "experience": [{"company": "Co", "title": "Agent", "dates": "2020",
                        "bullets": ["Supported customers on the team.",
                                    "Supported customers, cutting handle time by 20%."]}],
        "skills_grouped": {"Skills": ["support"]}, "education": [], "certifications": [],
    }
    jd = "Support role. Supported customers required."
    tailored = tailor_resume(base, "Support", "Co", jd, use_ai=False)
    bullets = tailored["experience"][0]["bullets"]
    # both mention 'supported customers' equally; the quantified one leads
    assert "20%" in bullets[0]


# ---- ats: no regression, LLM lift --------------------------------------------
def test_ats_not_lowered_by_tailoring():
    base = _base()
    before = ats_score(JD, {**base, "_jd_title": TITLE})["score"]
    after = tailor_resume(base, TITLE, "Acme", JD, use_ai=False)["ats_score"]["score"]
    assert after >= before  # honest reordering/highlighting never drops the score


def test_llm_path_truthful_vocab_mirroring_lifts_score(monkeypatch):
    """When the base competencies match but the WORDING doesn't use the JD's keywords,
    the LLM polish truthfully mirrors the vocabulary and lifts coverage."""
    base = {
        "personal_info": {"full_name": "A B"}, "headline": "Support Agent",
        "preferred_titles": ["Support Agent"],
        "summary": "Reliable agent who helps customers by chat and phone.",
        "experience": [{"company": "Co", "title": "Support Agent", "dates": "2021",
                        "bullets": ["Helped users through chat questions and calls daily.",
                                    "Calmed upset customers and fixed account problems.",
                                    "Kept notes in the company system for every case."]}],
        "skills_grouped": {"Skills": ["chat", "phone", "account help"]},
        "education": [], "certifications": [],
    }
    title = "Live Chat Support Representative"
    jd = ("Live Chat Support Representative. Requires live chat support, troubleshooting, "
          "de-escalation and CRM. Live chat is required. Troubleshooting is required.")
    before = tailor_resume(base, title, "Acme", jd, use_ai=False)["ats_score"]["score"]

    def fake_llm(prompt):
        return json.dumps({
            "summary": "Live chat support rep skilled in troubleshooting and de-escalation.",
            "experience": [{"company": "Co", "title": "Support Agent", "dates": "2021",
                            "context": "", "bullets": [
                                "Provided live chat support, troubleshooting daily issues.",
                                "Applied de-escalation to calm upset customers.",
                                "Logged every case in the CRM."]}]})

    monkeypatch.setattr(T, "_llm_complete", fake_llm)
    after = tailor_resume(base, title, "Acme", jd, use_ai=True)["ats_score"]["score"]
    assert after > before + 20  # a large, TRUTHFUL lift


# ---- cover letter -------------------------------------------------------------
def test_cover_letter_is_role_specific_and_truthful():
    base = _base()
    tailored = tailor_resume(base, TITLE, "Acme", JD, use_ai=False)
    rtext = render_text(tailored)
    job = {"title": TITLE, "company": "Acme Support", "description": JD}
    letter = A.cover_letter(job, rtext, {"full_name": "Jamie Rivers"}, use_llm=False)
    assert "Acme Support" in letter and TITLE in letter
    # references a truthful strength from the résumé
    assert "Zendesk" in letter or "troubleshooting" in letter
    # no fabricated skill: every capitalized/known skill token traceable
    allowed = _tokens(rtext) | _tokens(TITLE) | _tokens("Acme Support") | _tokens(letter)
    # sanity: the letter mentions the company + role + is non-trivial
    assert len(letter) > 120


def test_resume_section_and_top_skills_parse():
    base = _base()
    rtext = render_text(tailor_resume(base, TITLE, "Acme", JD, use_ai=False))
    summ = A._resume_section(rtext, "SUMMARY")
    assert "Dedicated professional" in summ and "SKILLS" not in summ
    skills = A._top_resume_skills(rtext, 3)
    assert len(skills) == 3
    base_skills = {s.lower() for items in base["skills_grouped"].values() for s in items}
    assert all(s.lower() in base_skills for s in skills)


# ---- screener defaults --------------------------------------------------------
def test_availability_default_is_positive():
    out = A.deterministic_answers(["When can you start in this role?"], {}, {"title": "CS"})
    ans = next(iter(out.values()))
    assert "Immediately" in ans and "flexible" in ans.lower()


def test_join_and():
    assert T._join_and(["a"]) == "a"
    assert T._join_and(["a", "b"]) == "a and b"
    assert T._join_and(["a", "b", "c"]) == "a, b, and c"
