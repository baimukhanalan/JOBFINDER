"""Wave E: multi-user isolation + review-gate UX — the pure logic.

- /draft grounds answers in the REQUESTING profile's identity (never michael's)
- the shared co-pilot page is owner-gated (second user can't clobber a review)
- review_items surface as a badge and survive into the batch queue keys
- auto-submit / human-resolved statuses are terminal for the batch
- the fake sample profile can't touch live postings
- status writes are atomic
"""
import json
import os
import time

import pytest

import backend.dashboard_app as dash
import backend.status_store as status_store
from backend.applier import runner
from backend.applier.batch import _QUEUE_KEYS, _prior_state
from backend.copilot import BUSY_TTL, can_load
from backend.dashboard_app import _badge, _profile_form, _save_status
from backend.profiles.store import Profile, is_sample_profile


def _profile(pid: str, **kw) -> Profile:
    base = dict(id=pid, full_name=f"{pid.title()} Person", email=f"{pid}@example.com",
                phone="555-0100")
    base.update(kw)
    return Profile(**base)


@pytest.fixture
def fake_profiles(monkeypatch, tmp_path):
    """Point dashboard's profile cache at an in-memory set of two people."""
    profs = {
        "michael": _profile("michael", years_experience="14", desired_salary="55000",
                            resume={"summary": "Michael's summary"}),
        "kate": _profile("kate", years_experience="3",
                         resume={"summary": "Kate's summary"}),
    }
    src = tmp_path / "profiles.json"
    src.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(dash, "load_profiles", lambda: profs)
    monkeypatch.setattr(dash, "_source_path", lambda _: src)
    monkeypatch.setattr(dash, "_PROFILES_CACHE", {"mtime": None, "profiles": {}})
    return profs


# --- E1: _profile_form resolves the REQUESTING profile ------------------------

def test_profile_form_returns_own_identity(fake_profiles):
    facts, summary = _profile_form("kate")
    assert facts["full_name"] == "Kate Person"
    assert facts["email"] == "kate@example.com"
    assert facts["years_experience"] == "3"
    assert summary == "Kate's summary"
    # never michael's values under kate's key
    assert "14" not in facts.values() and "55000" not in facts.values()


def test_profile_form_filters_empty_values(fake_profiles):
    facts, _ = _profile_form("kate")
    assert all(isinstance(v, (str, int)) and v for v in facts.values())
    assert "desired_salary" not in facts  # empty on kate


def test_profile_form_unknown_profile_is_empty(fake_profiles):
    assert _profile_form("stranger") == ({}, "")


def test_profiles_cache_refreshes_on_mtime_change(monkeypatch, tmp_path):
    src = tmp_path / "profiles.json"
    src.write_text("[]", encoding="utf-8")
    calls = {"n": 0}
    profs = {"a": _profile("a")}

    def fake_load():
        calls["n"] += 1
        return profs

    monkeypatch.setattr(dash, "load_profiles", fake_load)
    monkeypatch.setattr(dash, "_source_path", lambda _: src)
    monkeypatch.setattr(dash, "_PROFILES_CACHE", {"mtime": None, "profiles": {}})
    assert dash._profiles() is profs
    assert dash._profiles() is profs
    assert calls["n"] == 1  # cached on the same mtime
    os.utime(src, (1, 1))   # file changed -> reload
    dash._profiles()
    assert calls["n"] == 2


def test_draft_unknown_profile_404(fake_profiles, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(dash, "ASSIST_TOKEN", "tok")
    client = TestClient(dash.app)
    r = client.post("/draft", headers={"x-assist-token": "tok"},
                    json={"questions": ["Why us?"], "profile": "stranger"})
    assert r.status_code == 404
    assert r.json() == {"error": "unknown profile"}


# --- E2: co-pilot owner gate --------------------------------------------------

def test_can_load_when_free():
    assert can_load(None, 0.0, "kate", time.time())


def test_can_load_same_owner():
    now = time.time()
    assert can_load("kate", now, "kate", now)


def test_cannot_load_other_owner_recent():
    now = time.time()
    assert not can_load("michael", now - 60, "kate", now)


def test_can_load_other_owner_after_ttl():
    now = time.time()
    assert can_load("michael", now - BUSY_TTL, "kate", now)
    assert can_load("michael", now - BUSY_TTL - 1, "kate", now)


def test_cannot_load_just_under_ttl():
    now = time.time()
    assert not can_load("michael", now - BUSY_TTL + 5, "kate", now)


# --- E5: badge surfaces the review gate ----------------------------------------

def test_badge_needs_review():
    job = {"review_items": [{"question": "Q1"}, {"question": "Q2"}], "unfilled": []}
    assert _badge(job, "") == ("NEEDS REVIEW (2)", "warn")


def test_badge_unfilled_beats_review():
    job = {"review_items": [{"question": "Q"}], "unfilled": ["Cover letter"]}
    label, cls = _badge(job, "")
    assert label.startswith("NEEDS INFO") and cls == "warn"


def test_badge_status_beats_review():
    job = {"review_items": [{"question": "Q"}]}
    assert _badge(job, "submitted") == ("SUBMITTED", "sub")


def test_badge_ready_without_review():
    assert _badge({"unfilled": [], "review_items": []}, "") == ("READY TO SUBMIT", "ready")


# --- E4/E6: batch terminal-state classification + queue keys -------------------

def _write_report(out_dir, jid, **extra):
    d = out_dir / jid
    d.mkdir(parents=True)
    rep = {"apply_url": f"https://jobs.example/{jid}", "job_title": jid,
           "company": "Co", "submitted": False, **extra}
    (d / "report.json").write_text(json.dumps(rep), encoding="utf-8")
    return rep["apply_url"]


def test_prior_state_human_status_is_terminal(tmp_path):
    url = _write_report(tmp_path, "job-a")
    (tmp_path / "status.json").write_text(json.dumps({"job-a": {"status": "rejected"}}))
    pending, done = _prior_state(tmp_path)
    assert url in done and not pending


def test_prior_state_auto_submit_is_terminal(tmp_path):
    url = _write_report(tmp_path, "job-b", submitted=True)
    pending, done = _prior_state(tmp_path)
    assert url in done and not pending


def test_prior_state_pending_carries_review_keys(tmp_path):
    url = _write_report(tmp_path, "job-c",
                        review_items=[{"question": "Q", "answer": "A", "kind": "draft"}],
                        drafted_answers={"Q": "[review] A"})
    pending, done = _prior_state(tmp_path)
    assert not done
    item = pending[url]
    assert item["review_items"] == [{"question": "Q", "answer": "A", "kind": "draft"}]
    for k in ("review_items", "answer_sources", "choice_picks", "drafted_answers"):
        assert k in _QUEUE_KEYS and k in item


# --- E6: the shared status store writes the dashboard status shape -------------
# (used by the extension's /mark_ext when it detects the human's Submit click)

def test_mark_submitted_writes_and_merges(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "OUT_ROOT", tmp_path)
    sf = tmp_path / "kate" / "status.json"
    sf.parent.mkdir(parents=True)
    sf.write_text(json.dumps({"old-job": {"status": "interview"}}), encoding="utf-8")
    status_store.mark("kate", "new-job", "submitted")
    data = json.loads(sf.read_text(encoding="utf-8"))
    assert set(data) == {"old-job", "new-job"}
    assert data["old-job"] == {"status": "interview"}  # pre-ts entry untouched
    assert data["new-job"]["status"] == "submitted"
    assert not list(sf.parent.glob("*.tmp"))


def test_mark_submitted_survives_corrupt_status(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "OUT_ROOT", tmp_path)
    sf = tmp_path / "kate" / "status.json"
    sf.parent.mkdir(parents=True)
    sf.write_text("{corrupt", encoding="utf-8")
    status_store.mark("kate", "job-x", "submitted")
    assert json.loads(sf.read_text())["job-x"]["status"] == "submitted"


# --- E8: sample-profile guard ---------------------------------------------------

def test_sample_by_flag():
    assert is_sample_profile(_profile("anyone", is_sample=True))


def test_sample_by_id():
    assert is_sample_profile(_profile("sample"))


def test_real_profile_is_not_sample():
    assert not is_sample_profile(_profile("michael"))


# --- E7: atomic status save ------------------------------------------------------

def test_save_status_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "PREFILL_ROOT", tmp_path)
    _save_status("kate", {"j1": {"status": "submitted"}})
    f = tmp_path / "kate" / "status.json"
    assert json.loads(f.read_text(encoding="utf-8")) == {"j1": {"status": "submitted"}}
    assert not list(f.parent.glob("*.tmp"))  # tmp file replaced, not left behind
