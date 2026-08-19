"""Per-profile etalons: each person routes jobs to THEIR OWN résumé variants."""
import json

import pytest

from backend.profiles.store import Profile
from backend.services.tailor import variants


@pytest.fixture(autouse=True)
def _clear_cache():
    variants._load_raw_cached.cache_clear()
    yield
    variants._load_raw_cached.cache_clear()


def _etalon(key, label):
    return {"key": key, "label": label, "best_for": label,
            "resume": {"headline": label, "summary": f"{label} with 12+ years",
                       "experience": [{"company": "RealCo", "title": label,
                                       "bullets": ["Did support work"]}],
                       "skills": [{"group": "Core", "items": ["Zendesk"]}],
                       "certifications": [], "education": []}}


def _profile(pid):
    return Profile(id=pid, full_name="Kate Doe", email="k@x.com", phone="1")


def test_profiles_load_own_etalons(tmp_path, monkeypatch):
    monkeypatch.setattr(variants, "ETALONS_DIR", tmp_path)
    (tmp_path / "kate.json").write_text(
        json.dumps([_etalon("travel-hospitality", "Travel Support")]), encoding="utf-8")
    (tmp_path / "michael.json").write_text(
        json.dumps([_etalon("bpo-voice-qa", "Call Center QA")]), encoding="utf-8")

    key_k, resume_k = variants.variant_for({"title": "Travel Support Agent"}, _profile("kate"))
    assert key_k == "travel-hospitality"
    assert resume_k["personal_info"]["full_name"] == "Kate Doe"

    key_m, _ = variants.variant_for({"title": "Call Center QA Analyst"}, _profile("michael"))
    assert key_m == "bpo-voice-qa"


def test_profile_without_etalons_gets_none(tmp_path, monkeypatch):
    monkeypatch.setattr(variants, "ETALONS_DIR", tmp_path)
    key, resume = variants.variant_for({"title": "Support Agent"}, _profile("nobody"))
    assert key is None and resume is None


def test_list_niches_per_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(variants, "ETALONS_DIR", tmp_path)
    (tmp_path / "kate.json").write_text(json.dumps([_etalon("a", "A")]), encoding="utf-8")
    assert [n["key"] for n in variants.list_niches("kate")] == ["a"]
    assert variants.list_niches("nobody") == []


def test_categorize_with_explicit_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(variants, "ETALONS_DIR", tmp_path)
    (tmp_path / "kate.json").write_text(
        json.dumps([_etalon("travel-hospitality", "Travel Support")]), encoding="utf-8")
    key, score = variants.categorize("Travel Support Agent", "", "kate")
    assert key == "travel-hospitality"
    assert variants.categorize("Travel Support Agent", "", "")[0] is None


def test_etalons_edit_picked_up_without_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(variants, "ETALONS_DIR", tmp_path)
    import os
    f = tmp_path / "kate.json"
    f.write_text(json.dumps([_etalon("a", "A")]), encoding="utf-8")
    assert [n["key"] for n in variants.list_niches("kate")] == ["a"]
    f.write_text(json.dumps([_etalon("b", "B")]), encoding="utf-8")
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 2))  # ensure mtime moves
    assert [n["key"] for n in variants.list_niches("kate")] == ["b"]


def test_unsafe_profile_id_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(variants, "ETALONS_DIR", tmp_path)
    assert variants._load_raw("../profiles") == ()
