"""Fact-sheet loader: per-person JSON answering facts for rules and prompts."""
import json

from backend.profiles import facts as facts_mod


def test_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(facts_mod, "FACTS_DIR", tmp_path)
    assert facts_mod.load_facts("nobody") == {}


def test_loads_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(facts_mod, "FACTS_DIR", tmp_path)
    (tmp_path / "kate.json").write_text(json.dumps({"typing_wpm": "70"}), encoding="utf-8")
    assert facts_mod.load_facts("kate") == {"typing_wpm": "70"}


def test_garbage_or_non_dict_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(facts_mod, "FACTS_DIR", tmp_path)
    (tmp_path / "bad.json").write_text("not json", encoding="utf-8")
    (tmp_path / "list.json").write_text("[1,2]", encoding="utf-8")
    assert facts_mod.load_facts("bad") == {}
    assert facts_mod.load_facts("list") == {}


def test_unsafe_profile_id_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(facts_mod, "FACTS_DIR", tmp_path)
    assert facts_mod.load_facts("../profiles") == {}


def test_sample_file_is_valid():
    # the committed sample must always load (it documents the schema)
    data = facts_mod.load_facts("sample")
    assert isinstance(data, dict) and data.get("background_check_ok") == "Yes"
