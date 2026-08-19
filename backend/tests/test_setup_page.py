"""Friend-onboarding editor: GET /setup (+ per-profile editors) and POST /setup/save.

All paths are monkeypatched to tmp_path: profiles.json via
backend.profiles.store.REAL_PROFILES (the module reads it at call time), facts via
backend.profiles.facts.FACTS_DIR, etalons via dash.ETALONS_DIR.
"""
import json

import pytest
from fastapi.testclient import TestClient

import backend.dashboard_app as dash
import backend.profiles.facts as facts_lib
import backend.profiles.store as profile_store

KATE = {"id": "kate", "full_name": "Kate Person", "email": "kate@example.com",
        "phone": "555-0100", "mailbox": "kate@mail.test"}
SAMPLE_FACTS = {"notice_period": "Immediately", "languages": ["English"],
                "typing_wpm": "65"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    profiles_file = tmp_path / "profiles.json"
    facts_dir = tmp_path / "facts"
    etalons_dir = tmp_path / "etalons"
    facts_dir.mkdir()
    etalons_dir.mkdir()
    monkeypatch.setattr(profile_store, "REAL_PROFILES", profiles_file)
    monkeypatch.setattr(facts_lib, "FACTS_DIR", facts_dir)
    monkeypatch.setattr(dash, "ETALONS_DIR", etalons_dir)
    monkeypatch.setattr(dash, "_PROFILES_CACHE", {"mtime": None, "profiles": {}})
    profiles_file.write_text(json.dumps([KATE]), encoding="utf-8")
    (facts_dir / "sample.json").write_text(json.dumps(SAMPLE_FACTS), encoding="utf-8")
    (etalons_dir / "kate.json").write_text(json.dumps(
        [{"key": "n1", "resume": {}}, {"key": "n2", "resume": {}}]), encoding="utf-8")

    class Env:
        pass

    e = Env()
    e.profiles_file, e.facts_dir, e.etalons_dir = profiles_file, facts_dir, etalons_dir
    e.client = TestClient(dash.app)
    return e


# --- GET /setup -------------------------------------------------------------------

def test_index_lists_profiles_with_badges(env):
    (env.facts_dir / "kate.json").write_text("{}", encoding="utf-8")
    r = env.client.get("/setup")
    assert r.status_code == 200
    assert "kate" in r.text and "Kate Person" in r.text
    assert "/setup?profile=kate" in r.text          # edit link
    assert "facts ✓" in r.text
    assert "etalons ✓ 2 niches" in r.text
    assert "mailbox ✓" in r.text
    assert "New profile" in r.text                  # new-id form


def test_index_badges_when_missing(env):
    r = env.client.get("/setup")  # kate has no facts file in this state
    assert "facts ✗" in r.text


def test_editor_prefills_existing_entry_and_facts(env):
    (env.facts_dir / "kate.json").write_text(json.dumps({"typing_wpm": "80"}),
                                             encoding="utf-8")
    r = env.client.get("/setup?profile=kate")
    assert r.status_code == 200
    assert "Kate Person" in r.text                  # profiles.json entry
    assert "&quot;80&quot;" in r.text               # own facts, not the sample
    assert "notice_period" not in r.text
    assert "Answers used for screener questions — edit truthfully for this person" in r.text


def test_editor_prefills_facts_template_when_missing(env):
    r = env.client.get("/setup?profile=newguy")     # fresh id: both templates
    assert r.status_code == 200
    assert "notice_period" in r.text                # sample.json as facts template
    assert "typing_wpm" in r.text
    assert "&quot;id&quot;: &quot;newguy&quot;" in r.text  # minimal profile template
    assert "full_name" in r.text
    assert "✗ not set" in r.text                    # etalons read-only status
    assert str(env.etalons_dir / "newguy.json") in r.text  # file path shown


def test_saved_flag_shows_note(env):
    r = env.client.get("/setup?profile=kate&saved=1")
    assert "Saved." in r.text


# --- POST /setup/save: facts --------------------------------------------------------

def test_save_facts_writes_file(env):
    facts = {"typing_wpm": "70", "languages": ["English", "French"]}
    r = env.client.post("/setup/save",
                        data={"profile": "kate", "kind": "facts",
                              "body": json.dumps(facts)},
                        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup?profile=kate&saved=1"
    assert json.loads((env.facts_dir / "kate.json").read_text()) == facts


def test_save_invalid_json_preserves_input(env):
    bad = '{"oops": '
    r = env.client.post("/setup/save",
                        data={"profile": "kate", "kind": "facts", "body": bad})
    assert r.status_code == 400
    assert "Invalid JSON" in r.text and "Expecting value" in r.text  # decoder message
    assert "&quot;oops&quot;: " in r.text          # submitted text back in the textarea
    assert not (env.facts_dir / "kate.json").exists()


def test_save_facts_non_dict_rejected(env):
    r = env.client.post("/setup/save",
                        data={"profile": "kate", "kind": "facts", "body": "[1, 2]"})
    assert r.status_code == 400
    assert "JSON object" in r.text
    assert not (env.facts_dir / "kate.json").exists()


# --- POST /setup/save: profile -------------------------------------------------------

def test_save_profile_unknown_key_shows_from_dict_error(env):
    entry = {**KATE, "bogus": 1}
    r = env.client.post("/setup/save",
                        data={"profile": "kate", "kind": "profile",
                              "body": json.dumps(entry)})
    assert r.status_code == 400
    assert "unknown keys" in r.text and "bogus" in r.text
    assert json.loads(env.profiles_file.read_text()) == [KATE]  # untouched


def test_save_profile_id_mismatch_rejected(env):
    entry = {**KATE, "id": "someoneelse"}
    r = env.client.post("/setup/save",
                        data={"profile": "kate", "kind": "profile",
                              "body": json.dumps(entry)})
    assert r.status_code == 400
    assert json.loads(env.profiles_file.read_text()) == [KATE]


def test_save_profile_replaces_by_id_no_duplicate(env):
    updated = {**KATE, "full_name": "Kate Updated"}
    r = env.client.post("/setup/save",
                        data={"profile": "kate", "kind": "profile",
                              "body": json.dumps(updated)},
                        follow_redirects=False)
    assert r.status_code == 303
    entries = json.loads(env.profiles_file.read_text())
    assert entries == [updated]                    # replaced in place, not appended


def test_save_profile_new_id_appends(env):
    new = {"id": "newguy", "full_name": "New Guy", "email": "n@example.com",
           "phone": "555-0101"}
    r = env.client.post("/setup/save",
                        data={"profile": "newguy", "kind": "profile",
                              "body": json.dumps(new)},
                        follow_redirects=False)
    assert r.status_code == 303
    entries = json.loads(env.profiles_file.read_text())
    assert entries == [KATE, new]


def test_save_profile_invalidates_profiles_cache(env):
    assert "kate" in dash._profiles()
    new = {"id": "newguy", "full_name": "New Guy", "email": "n@example.com",
           "phone": "555-0101"}
    env.client.post("/setup/save",
                    data={"profile": "newguy", "kind": "profile",
                          "body": json.dumps(new)}, follow_redirects=False)
    assert "newguy" in dash._profiles()            # cache re-read after the save


# --- hardening -----------------------------------------------------------------------

def test_traversal_id_rejected(env):
    r = env.client.post("/setup/save",
                        data={"profile": "../evil", "kind": "facts", "body": "{}"})
    assert r.status_code == 400
    # nothing written anywhere: no remapped "evil.json" either
    assert list(env.facts_dir.glob("*.json")) == [env.facts_dir / "sample.json"]
    assert not (env.facts_dir.parent / "evil.json").exists()


def test_bad_kind_rejected(env):
    r = env.client.post("/setup/save",
                        data={"profile": "kate", "kind": "etalons", "body": "{}"})
    assert r.status_code == 400
