"""'[review]' is a WIRE format (cache / drafted_answers only): it must be
converted to metadata before touching a live field or a human-facing surface,
and the flag must survive a co-pilot reload (known_answers replay).

Also covers the review dashboard's home page: ready-first tabs, junk page
types hidden, card anchors, and the profile reality gate (an undeliverable
profile gets a banner and loses every apply affordance)."""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

import backend.dashboard_app as dash
import backend.profiles.store as profile_store
from backend.applier.strategies.base import review_from_known, strip_review
from backend.dashboard_app import _split_review


# --- strip_review: wire prefix -> (text, flagged) ---------------------------

def test_strip_with_prefix_and_space():
    assert strip_review("[review] Needs a human eye") == ("Needs a human eye", True)


def test_strip_prefix_without_trailing_space():
    assert strip_review("[review]Needs a human eye") == ("Needs a human eye", True)


def test_strip_prefix_with_extra_whitespace():
    assert strip_review("[review]   padded") == ("padded", True)


def test_unprefixed_string_passes_through_unchanged():
    assert strip_review("Plain answer") == ("Plain answer", False)


def test_prefix_mid_string_is_not_a_flag():
    s = "I will [review] the docs"
    assert strip_review(s) == (s, False)


def test_non_string_passthrough():
    assert strip_review(None) == (None, False)
    assert strip_review(42) == (42, False)
    assert strip_review(["[review] x"]) == (["[review] x"], False)


# --- review_from_known: reload must not erase the review gate ----------------

def test_rescan_flags_and_strips_review_entries():
    known = {
        "Why do you want to work here?": "[review] Because of the mission",
        "Years of experience?": "5",
        "Pick one": 3,  # non-string -> ignored
    }
    assert review_from_known(known) == [
        {"question": "Why do you want to work here?",
         "answer": "Because of the mission", "kind": "draft"},
    ]


def test_rescan_ignores_unflagged_and_handles_empty():
    assert review_from_known({"Q": "fine"}) == []
    assert review_from_known({}) == []
    assert review_from_known(None) == []


def test_rescan_preserves_order_of_multiple_flags():
    known = {"Q1": "[review] a", "Q2": "ok", "Q3": "[review]b"}
    items = review_from_known(known)
    assert [it["question"] for it in items] == ["Q1", "Q3"]
    assert [it["answer"] for it in items] == ["a", "b"]


# --- /draft metadata: answers stripped, review map emitted -------------------

def test_split_review_strips_and_maps():
    clean, review = _split_review({"Q1": "[review] x", "Q2": "fine"})
    assert clean == {"Q1": "x", "Q2": "fine"}
    assert review == {"Q1": True}


def test_split_review_no_flags():
    clean, review = _split_review({"Q": "answer"})
    assert clean == {"Q": "answer"} and review == {}


def test_split_review_empty():
    assert _split_review({}) == ({}, {})


# --- home page: tabs, anchors, stats, profile reality gate -------------------

# vera passes the reality gate; kate is undeliverable by construction
# (reserved-fictional 555-01xx phone + placeholder email domain).
VERA = {"id": "vera", "full_name": "Vera Person", "email": "vera@realmail.com",
        "phone": "(512) 209-4417"}
KATE = {"id": "kate", "full_name": "Kate Person", "email": "kate@example.com",
        "phone": "555-0134"}


def _report(company: str, url: str, page_type: str = "application_form",
            filled: int = 5, failed: int = 0, **extra) -> dict:
    return {"job_title": f"{company} Support", "company": company, "apply_url": url,
            "page_type": page_type, "filled": filled, "failed": failed, **extra}


@pytest.fixture
def env(monkeypatch, tmp_path):
    prefill = tmp_path / "prefill"
    inbox = tmp_path / "inbox"
    prefill.mkdir()
    inbox.mkdir()
    profiles_file = tmp_path / "profiles.json"
    profiles_file.write_text(json.dumps([VERA, KATE]), encoding="utf-8")
    monkeypatch.setattr(profile_store, "REAL_PROFILES", profiles_file)
    monkeypatch.setattr(dash, "PREFILL_ROOT", prefill)
    monkeypatch.setattr(dash, "INBOX_DIR", inbox)
    monkeypatch.setattr(dash, "_PROFILES_CACHE", {"mtime": None, "profiles": {}})

    class Env:
        client = TestClient(dash.app)

        def add(self, profile: str, jid: str, rep: dict) -> None:
            d = prefill / profile / jid
            d.mkdir(parents=True)
            (d / "report.json").write_text(json.dumps(rep), encoding="utf-8")

    e = Env()
    e.prefill = prefill
    return e


def _panes(html: str) -> tuple[str, str]:
    ready = html.split("id='pane-ready'")[1].split("id='pane-info'")[0]
    info = html.split("id='pane-info'")[1]
    return ready, info


def test_ready_tab_holds_only_clean_filled_forms(env):
    env.add("vera", "acme-1", _report("AcmeCo", "https://x.test/1"))
    env.add("vera", "beta-2", _report("BetaCo", "https://x.test/2", filled=0))
    env.add("vera", "gamma-3", _report("GammaCo", "https://x.test/3", failed=2))
    html = env.client.get("/queue?profile=vera").text
    assert "Ready (1)" in html and "Needs info (2)" in html
    ready, info = _panes(html)
    assert "AcmeCo" in ready and "AcmeCo" not in info
    assert "BetaCo" in info and "GammaCo" in info


def test_junk_page_types_in_neither_tab_only_counted(env):
    env.add("vera", "acme-1", _report("AcmeCo", "https://x.test/1"))
    env.add("vera", "dead-2", _report("DeadCo", "https://x.test/2", page_type="expired"))
    env.add("vera", "list-3", _report("ListCo", "https://x.test/3", page_type="job_listing"))
    env.add("vera", "mist-4", _report("MistCo", "https://x.test/4", page_type="unknown"))
    html = env.client.get("/queue?profile=vera").text
    for co in ("DeadCo", "ListCo", "MistCo"):
        assert co not in html
    assert "<b>3</b><span>skipped: no form</span>" in html
    assert "Ready (1)" in html and "Needs info (0)" in html


def test_terminal_status_never_in_ready_tab(env):
    env.add("vera", "acme-1", _report("AcmeCo", "https://x.test/1"))
    (env.prefill / "vera" / "status.json").write_text(
        json.dumps({"acme-1": {"status": "submitted"}}), encoding="utf-8")
    html = env.client.get("/queue?profile=vera").text
    assert "Ready (0)" in html
    ready, info = _panes(html)
    assert "AcmeCo" in info and "AcmeCo" not in ready


def test_card_anchor_present_for_deep_links(env):
    env.add("vera", "acme-1", _report("AcmeCo", "https://x.test/1"))
    html = env.client.get("/queue?profile=vera").text
    assert "id='job-acme-1'" in html


def test_valid_profile_has_apply_affordances_no_banner(env):
    env.add("vera", "acme-1", _report("AcmeCo", "https://x.test/1"))
    html = env.client.get("/queue?profile=vera").text
    assert "Applications are paused" not in html
    assert "Apply (1-click)" in html
    assert "Open form" in html
    assert "Open in co-pilot" in html and "copilotLoad('acme-1'" in html
    assert "/copilot/load" in html  # the co-pilot POST target


def test_invalid_profile_banner_and_no_apply_affordances(env):
    env.add("kate", "acme-1", _report("AcmeCo", "https://x.test/1"))
    html = env.client.get("/queue?profile=kate").text
    assert "Applications are paused" in html
    assert "reserved-fictional" in html         # the phone problem, spelled out
    assert "placeholder" in html                # the email problem
    assert "/setup?profile=kate" in html        # the fix link
    assert "Apply (1-click)" not in html
    assert "Open form" not in html
    assert "Open in co-pilot" not in html
    # viewing and bookkeeping stay available
    assert "Résumé PDF" in html
    assert "mark submitted" in html


def test_unknown_profile_is_blocked_too(env):
    env.add("ghost", "acme-1", _report("AcmeCo", "https://x.test/1"))
    html = env.client.get("/queue?profile=ghost").text
    assert "Applications are paused" in html
    assert "Apply (1-click)" not in html


def test_last_review_never_without_status_file(env):
    env.add("vera", "acme-1", _report("AcmeCo", "https://x.test/1"))
    html = env.client.get("/queue?profile=vera").text
    assert "<b>never</b><span>last review</span>" in html
    assert "stat alert" not in html


def test_last_review_stale_goes_red(env):
    env.add("vera", "acme-1", _report("AcmeCo", "https://x.test/1"))
    st = env.prefill / "vera" / "status.json"
    st.write_text(json.dumps({"old-jid": {"status": "submitted"}}), encoding="utf-8")
    stale = time.time() - 4 * 86400
    os.utime(st, (stale, stale))
    html = env.client.get("/queue?profile=vera").text
    assert "<b>4d ago</b><span>last review</span>" in html
    assert "class='stat alert'" in html


def test_last_review_fresh_not_red(env):
    env.add("vera", "acme-1", _report("AcmeCo", "https://x.test/1"))
    (env.prefill / "vera" / "status.json").write_text(
        json.dumps({"old-jid": {"status": "submitted"}}), encoding="utf-8")
    html = env.client.get("/queue?profile=vera").text
    assert "<b>today</b><span>last review</span>" in html
    assert "stat alert" not in html
