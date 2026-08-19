"""Wave F: /assist endpoint — closed-screener resolution + the extension API.

- assist_closed: analyzer rules first, constrained choice for the rest,
  demographics never auto-answered, malformed items never crash
- /assist is token-gated (401) and profile-gated (404 unknown)
- /profile_form returns identity fields only
"""
import pytest

import backend.dashboard_app as dash
from backend.dashboard_app import assist_closed
from backend.profiles.store import Profile
from backend.services.tailor import choices

FORM = {
    "full_name": "Kate Person",
    "email": "kate@example.com",
    "phone": "555-0100",
    "work_authorization": "US Citizen",
    "needs_sponsorship": "No",
    "country": "United States",
}
JOB = {"title": "Support Specialist", "company": "Acme"}


def _no_llm(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("choose_options must not be called")
    monkeypatch.setattr(choices, "choose_options", boom)


@pytest.fixture
def fake_profiles(monkeypatch, tmp_path):
    profs = {
        "michael": Profile(id="michael", full_name="Michael Heck",
                           email="m@example.com", phone="555-0186"),
        "kate": Profile(id="kate", full_name="Kate Person",
                        email="kate@example.com", phone="555-0100"),
    }
    src = tmp_path / "profiles.json"
    src.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(dash, "load_profiles", lambda: profs)
    monkeypatch.setattr(dash, "_source_path", lambda _: src)
    monkeypatch.setattr(dash, "_PROFILES_CACHE", {"mtime": None, "profiles": {}})
    return profs


# --- assist_closed: pure cascade logic -----------------------------------------

def test_rule_answers_work_auth(monkeypatch):
    _no_llm(monkeypatch)  # the rule covers it — the choice engine must stay idle
    res = assist_closed(
        [{"question": "Are you legally authorized to work in the United States?",
          "options": ["Select...", "Yes", "No"]}],
        FORM, {}, JOB)
    assert res == [{"index": 1, "option": "Yes", "source": "rule", "review": False}]


def test_rule_answers_sponsorship_no(monkeypatch):
    _no_llm(monkeypatch)
    res = assist_closed(
        [{"question": "Will you now or in the future require visa sponsorship?",
          "options": ["Yes", "No"]}],
        FORM, {}, JOB)
    assert res == [{"index": 1, "option": "No", "source": "rule", "review": False}]


def test_fallthrough_to_choices(monkeypatch):
    seen = {}

    def fake(questions, facts, job, niche_label=""):
        seen["questions"] = questions
        seen["niche"] = niche_label
        return [{"index": 1, "backed": False}]

    monkeypatch.setattr(choices, "choose_options", fake)
    res = assist_closed(
        [{"question": "Which shift do you prefer?", "options": ["Morning", "Night"]}],
        FORM, {}, JOB, "customer support")
    assert seen["questions"] == [{"question_text": "Which shift do you prefer?",
                                  "options": ["Morning", "Night"]}]
    assert seen["niche"] == "customer support"
    # unbacked choice -> filled but flagged for the human
    assert res == [{"index": 1, "option": "Night", "source": "choice", "review": True}]


def test_rule_match_without_fitting_option_goes_to_choices(monkeypatch):
    # work-auth rule resolves "Yes" but no option matches it -> choice engine's turn
    seen = {}

    def fake(questions, facts, job, niche_label=""):
        seen["n"] = len(questions)
        return [{"index": None, "backed": False}]

    monkeypatch.setattr(choices, "choose_options", fake)
    res = assist_closed(
        [{"question": "Are you authorized to work in the US?",
          "options": ["Maybe", "Unsure"]}],
        FORM, {}, JOB)
    assert seen["n"] == 1
    assert res == [{"index": None, "option": None, "source": "", "review": False}]


def test_unknown_choice_stays_none(monkeypatch):
    monkeypatch.setattr(choices, "choose_options",
                        lambda *a, **k: [{"index": None, "backed": False}])
    res = assist_closed(
        [{"question": "What is your favorite color?", "options": ["Red", "Blue"]}],
        FORM, {}, JOB)
    assert res[0]["index"] is None and res[0]["option"] is None


def test_demographics_skipped_not_sent_to_choices(monkeypatch):
    _no_llm(monkeypatch)  # _skip questions must never reach the choice engine
    res = assist_closed(
        [{"question": "What is your gender?", "options": ["Male", "Female", "Decline"]}],
        FORM, {}, JOB)
    assert res == [{"index": None, "option": None, "source": "skip", "review": False}]


def test_malformed_items_skipped(monkeypatch):
    _no_llm(monkeypatch)
    res = assist_closed(
        [{"question": "", "options": ["Yes", "No"]},          # no question
         {"question": "Only one way out?", "options": ["Ok"]},  # <2 options
         {}],
        FORM, {}, JOB)
    assert all(r["index"] is None for r in res) and len(res) == 3


def test_order_preserved_mixed_sources(monkeypatch):
    monkeypatch.setattr(choices, "choose_options",
                        lambda qs, *a, **k: [{"index": 0, "backed": True}])
    res = assist_closed(
        [{"question": "Which shift do you prefer?", "options": ["Morning", "Night"]},
         {"question": "Are you legally authorized to work in the United States?",
          "options": ["Yes", "No"]}],
        FORM, {}, JOB)
    assert [r["source"] for r in res] == ["choice", "rule"]
    assert res[0] == {"index": 0, "option": "Morning", "source": "choice", "review": False}
    assert res[1]["index"] == 0  # Yes


# --- /assist endpoint -----------------------------------------------------------

def test_assist_401_without_token(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(dash, "ASSIST_TOKEN", "tok")
    r = TestClient(dash.app).post("/assist", json={"open": ["Why us?"]})
    assert r.status_code == 401


def test_assist_unknown_profile_404(fake_profiles, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(dash, "ASSIST_TOKEN", "tok")
    r = TestClient(dash.app).post("/assist", headers={"x-assist-token": "tok"},
                                  json={"profile": "stranger", "open": ["Why us?"]})
    assert r.status_code == 404
    assert r.json() == {"error": "unknown profile"}


def test_assist_full_flow(fake_profiles, monkeypatch):
    """Closed: rule + choice; open: cache hit with a [review] prefix -> stripped
    text + review map. No LLM is touched anywhere."""
    from fastapi.testclient import TestClient
    import backend.answer_cache as answer_cache
    monkeypatch.setattr(dash, "ASSIST_TOKEN", "tok")
    monkeypatch.setattr(choices, "choose_options",
                        lambda qs, *a, **k: [{"index": 0, "backed": True}])
    monkeypatch.setattr(answer_cache, "get_many",
                        lambda qs, company, **kw: {"Why us?": "[review] Because."})
    monkeypatch.setattr(answer_cache, "put_many",
                        lambda *a, **k: pytest.fail("nothing new to cache"))
    r = TestClient(dash.app).post("/assist", headers={"x-assist-token": "tok"}, json={
        "profile": "kate", "company": "Acme", "job_title": "Support",
        "closed": [
            {"question": "Are you legally authorized to work in the United States?",
             "options": ["Yes", "No"]},
            {"question": "Which shift do you prefer?", "options": ["Morning", "Night"]},
        ],
        "open": ["Why us?"],
    })
    assert r.status_code == 200
    d = r.json()
    assert d["closed"][0] == {"index": 0, "option": "Yes", "source": "rule", "review": False}
    assert d["closed"][1] == {"index": 0, "option": "Morning", "source": "choice", "review": False}
    assert d["answers"] == {"Why us?": "Because."}   # wire prefix never leaves the server
    assert d["review"] == {"Why us?": True}
    assert d["counts"] == {"closed": 2, "closed_rule": 1, "closed_choice": 1,
                           "from_cache": 1, "from_llm": 0}


# --- /profile_form endpoint ------------------------------------------------------

def test_profile_form_401(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(dash, "ASSIST_TOKEN", "tok")
    assert TestClient(dash.app).get("/profile_form?profile=kate").status_code == 401


def test_profile_form_unknown_404(fake_profiles, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(dash, "ASSIST_TOKEN", "tok")
    r = TestClient(dash.app).get("/profile_form?profile=stranger",
                                 headers={"x-assist-token": "tok"})
    assert r.status_code == 404


def test_profile_form_identity_only(fake_profiles, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(dash, "ASSIST_TOKEN", "tok")
    r = TestClient(dash.app).get("/profile_form?profile=kate",
                                 headers={"x-assist-token": "tok"})
    assert r.status_code == 200
    d = r.json()
    assert d["full_name"] == "Kate Person"
    assert d["first_name"] == "Kate" and d["last_name"] == "Person"
    assert d["email"] == "kate@example.com" and d["phone"] == "555-0100"
    # never eligibility/salary — those stay rule/facts territory server-side
    assert "work_authorization" not in d and "desired_salary" not in d
    assert "needs_sponsorship" not in d and "resume_path" not in d
