"""Regression tests for the critical output bugs in the draft generator:
  1. positive work-auth question ("authorized to work WITHOUT sponsorship") -> Yes, not No
  2. "OR" state code leaking into a work-auth text field (the "United States" match)
  3. "Education History" invented by the LLM instead of rendered from the résumé
  4. the résumé PDF dumped into a "Photo" upload field
"""
from backend.tools import catalog_drafts as cd

PROFILE = {
    "id": "test_us_christopher",
    "full_name": "Christopher Bailey",
    "email": "christopher.bailey@takhet.com",
    "phone": "+1 801 422 5397",
    "country": "United States",
    "state": "OR",
    "zip_code": "97201",
    "work_authorization": "U.S. Citizen",
    "needs_sponsorship": "No",
    "linkedin_url": "https://www.linkedin.com/in/christopher-bailey-39",
    "years_experience": 6,
    "resume": {
        "headline": "Product Manager",
        "summary": "Product manager with fintech and marketplace experience.",
        "education": [{"year": "2016", "degree": "BSc Information Systems",
                       "school": "Massachusetts Institute of Technology"}],
        "experience": [{"company": "Amazon", "title": "Product Manager"},
                       {"company": "Cognizant", "title": "Business Analyst"}],
        "skills_grouped": {"core": ["SQL", "Product Strategy", "Analytics"]},
    },
}


# ---- unit: _identity_choice work-authorization polarity -------------------------
def test_positive_auth_without_sponsorship_is_yes():
    # job 1003: "...can you present proof you are authorized to work ... without visa
    # sponsorship?" — a US citizen must answer YES (it merely MENTIONS sponsorship).
    label = ("If hired, can you present proof that you are authorized to work in the "
             "United States or Canada for any employer, and that you are authorized to "
             "work without visa sponsorship?")
    assert cd._identity_choice(label, ["Yes", "No"], PROFILE) == 0  # Yes


def test_require_sponsorship_is_no():
    label = "Do you now or in the future require visa sponsorship to work in the United States?"
    assert cd._identity_choice(label, ["Yes", "No"], PROFILE) == 1  # No


def test_auth_with_no_sponsorship_disclaimer_is_yes():
    # classic: an auth question that explains "we don't offer sponsorship" -> still Yes.
    label = "Are you legally authorized to work in the US? (we do not offer visa sponsorship)"
    assert cd._identity_choice(label, ["Yes", "No"], PROFILE) == 0  # Yes


def test_sponsor_regex_does_not_match_positive_frame():
    assert not cd._SPONSOR_RE.search(
        "authorized to work in the United States without requiring company visa sponsorship")
    assert cd._SPONSOR_RE.search("Do you require visa sponsorship?")


# ---- unit: state regex no longer matches "United States" ------------------------
def test_state_regex_anchored():
    state_rx = next(rx for rx, k in cd._ID_TEXT if k == "state")
    assert not state_rx.search("Are you authorized to work in the United States?")
    assert state_rx.search("State / Province")
    assert state_rx.search("What state do you live in?")


def test_auth_text_is_affirmative():
    txt = cd._auth_text(PROFILE)
    assert txt.lower().startswith("yes")
    assert "United States" in txt and "sponsorship" in txt
    assert txt.strip() != "OR"


# ---- unit: education history rendered from the résumé, never invented -----------
def test_education_history_from_resume():
    val = cd._education_value("history", PROFILE["resume"])
    assert "BSc Information Systems" in val
    assert "Massachusetts Institute of Technology" in val
    assert "Business Administration" not in val


# ---- unit: pick_candidate never sends a US person to a foreign posting ----------
def test_pick_candidate_region_gating():
    roster = {"us1": {"profile": {}}, "ca1": {"profile": {}}, "kz1": {"profile": {}}}
    pools = {"US": ["us1"], "CA": ["ca1"], "OTHER": ["kz1"]}

    def who(regions):
        c = cd.pick_candidate(regions, roster, pools)
        return next((k for k, v in roster.items() if v is c), None)

    assert who(["US"]) == "us1"
    assert who(["CA"]) == "ca1"
    assert who(["US", "CA"]) == "us1"           # US wins
    assert who(["OTHER", "US"]) == "us1"
    assert cd.pick_candidate(["OTHER"], roster, pools) is None   # foreign -> no US default
    assert cd.pick_candidate(["UK"], roster, pools) is None      # no UK pool -> skip
    assert cd.pick_candidate([], roster, pools) is None          # untagged -> skip


# ---- unit: work-auth polarity — "authorized WITHOUT sponsorship" must be Yes ----
def test_auth_without_sponsorship_is_yes():
    prof = {"needs_sponsorship": "No"}
    opts = ["Yes", "No"]
    # the exact job-3573 phrasing: "sponsorship now or in the future" made _SPONSOR_RE fire
    # and invert an authorized citizen to No — the positive "without ... sponsorship" frame wins.
    lab = ("Are you legally authorized to work in the United States without requiring "
           "sponsorship now or in the future?")
    assert opts[cd._identity_choice(lab, opts, prof)] == "Yes"
    # a genuine "do you REQUIRE sponsorship?" still answers No for the same candidate
    assert opts[cd._identity_choice("Will you now or in the future require visa sponsorship?",
                                    opts, prof)] == "No"


# ---- unit: email derivation (first.last@takhet.com from a name) -----------------
def test_derive_email():
    assert cd.derive_email("Steve Jobs") == "steve.jobs@takhet.com"
    assert cd.derive_email("José García") == "jose.garcia@takhet.com"   # accents stripped
    assert cd.derive_email("Zoe O'Brien") == "zoe.obrien@takhet.com"    # apostrophe dropped
    assert cd.derive_email("Jean-Luc Picard") == "jeanluc.picard@takhet.com"
    assert cd.derive_email("Wei") == "wei@takhet.com"                   # single token
    assert cd.derive_email("") == ""                                   # nothing to derive


def test_email_filled_when_profile_email_blank():
    # a candidate whose profile.email isn't set still gets a working address in the form
    assert cd._profile_value("email", {"full_name": "Steve Jobs"}) == "steve.jobs@takhet.com"
    assert cd._profile_value("email", {"full_name": "Steve Jobs", "email": ""}) == "steve.jobs@takhet.com"
    # a real stored email always wins over the derived one
    assert cd._profile_value("email", {"full_name": "Steve Jobs",
                                       "email": "real@takhet.com"}) == "real@takhet.com"


# ---- integration: full generate_draft routing ----------------------------------
def _draft():
    job_row = {
        "title": "Accountant", "company": "Remote", "description": "Remote accounting role.",
        "questions": [
            {"type": "choice", "required": True, "options": ["Yes", "No"],
             "label": ("If hired, can you present proof that you are authorized to work in "
                       "the United States or Canada for any employer, and that you are "
                       "authorized to work without visa sponsorship?")},
            {"type": "choice", "options": ["Yes", "No"],
             "label": "Do you now or in the future require visa sponsorship?"},
            {"type": "textarea",
             "label": ("Are you legally authorized to work in the United States without "
                       "requiring company visa sponsorship?")},
            {"type": "select", "label": "Education History"},
            {"type": "file", "label": "Photo"},
            {"type": "input_text", "label": "State / Province"},
        ],
    }
    d = cd.generate_draft(job_row, {"profile": PROFILE, "facts": {"salary_annual": 200000}},
                          use_ai=False, ideal=False)
    return d["answers"]


def test_generate_draft_fixes():
    answers = _draft()

    def find(prefix):
        return next(a for a in answers if a["label"].startswith(prefix))

    assert find("If hired, can you present")["value"] == "Yes"
    assert find("Do you now or in the future require")["value"] == "No"

    auth_text = find("Are you legally authorized")
    assert auth_text["value"].lower().startswith("yes")
    assert auth_text["value"] != "OR"
    assert auth_text["source"] == "identity"

    edu = find("Education History")
    assert "BSc Information Systems" in edu["value"]
    assert "Business Administration" not in edu["value"]
    assert edu["source"] == "identity"

    photo = find("Photo")
    assert photo["status"] == "human"
    assert photo["value"] == ""

    state = find("State / Province")
    assert state["value"] == "OR"   # a real state field still fills correctly
