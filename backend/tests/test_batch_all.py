"""--profile all: batch over every READY profile (multi-profile batch, B1/B2).

Pure / monkeypatched — no browser, no LLM, no network, no real profiles.json.
"""
import argparse
import asyncio

import pytest

import backend.applier.batch as batch
from backend.apply_cli import _guard_sample
from backend.profiles.store import Profile


def _profile(pid: str, **kw) -> Profile:
    base = dict(id=pid, full_name=f"{pid.title()} Person",
                email=f"{pid}@example.com", phone="555-0100")
    base.update(kw)
    return Profile(**base)


@pytest.fixture
def three_profiles(monkeypatch, tmp_path):
    """michael + kate (real, with facts/etalons files) + the fake sample profile."""
    profs = {
        "michael": _profile("michael"),
        "kate": _profile("kate"),
        "sample": _profile("sample", is_sample=True),
    }
    monkeypatch.setattr(batch, "load_profiles", lambda: profs)
    facts, etalons = tmp_path / "facts", tmp_path / "etalons"
    facts.mkdir()
    etalons.mkdir()
    fams = {"kate": "data", "michael": "qa", "sample": "data"}
    for pid in profs:  # sample gets files too — must be skipped by is_sample alone
        (facts / f"{pid}.json").write_text(
            f'{{"role_family": "{fams[pid]}"}}', encoding="utf-8")
        (etalons / f"{pid}.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(batch, "FACTS_DIR", facts)
    monkeypatch.setattr(batch, "ETALONS_DIR", etalons)
    # Feature C assignment I/O — mocked so tests stay offline and touch no real files.
    monkeypatch.setattr(batch, "_online_roles", lambda: [
        {"apply_url": "https://x/data", "title": "Data Analyst", "family": "data"},
        {"apply_url": "https://x/qa", "title": "QA Engineer", "family": "qa"}])
    monkeypatch.setattr(batch, "_load_assignments", lambda: {})
    monkeypatch.setattr(batch, "_record_assignment", lambda *a, **k: None)
    return profs


def _stub(calls, fail_for=()):
    async def stub(profile_id="sample", **kwargs):
        calls.append((profile_id, kwargs))
        if profile_id in fail_for:
            raise RuntimeError(f"{profile_id} exploded")
        return {"summary": {"profile": profile_id, "new_prefilled": 3,
                            "queue_size": 7}, "items": []}
    return stub


# --- B1: batch_prefill_all ------------------------------------------------------

def test_all_skips_sample_runs_ready_in_order(three_profiles, monkeypatch):
    calls = []
    monkeypatch.setattr(batch, "batch_prefill", _stub(calls))
    res = asyncio.run(batch.batch_prefill_all())
    assert res["skipped"] == ["sample"]
    assert res["ready"] == ["kate", "michael"]          # deterministic (sorted)
    assert [c[0] for c in calls] == ["kate", "michael"]
    assert res["profiles"]["michael"]["new_prefilled"] == 3


def test_all_per_profile_error_does_not_abort(three_profiles, monkeypatch):
    calls = []
    monkeypatch.setattr(batch, "batch_prefill", _stub(calls, fail_for={"kate"}))
    res = asyncio.run(batch.batch_prefill_all())
    # kate raised first (sorted order) yet michael still ran
    assert [c[0] for c in calls] == ["kate", "michael"]
    assert res["profiles"]["kate"] == {"error": "kate exploded"}
    assert res["profiles"]["michael"]["queue_size"] == 7
    assert res["ready"] == ["kate", "michael"]          # attempted counts as ready


def test_all_forwards_kwargs_to_batch_prefill(three_profiles, monkeypatch):
    calls = []
    monkeypatch.setattr(batch, "batch_prefill", _stub(calls))
    asyncio.run(batch.batch_prefill_all(limit=5, draft=True, source="db"))
    # `limit` is superseded by the per-profile assignment size; draft/source forward
    # through, and each call carries the profile's assigned URLs.
    assert calls
    for _, kw in calls:
        assert kw["draft"] is True and kw["source"] == "db"
        assert isinstance(kw["supplied_jobs"], list) and kw["supplied_jobs"]
        assert kw["limit"] == len(kw["supplied_jobs"])


def test_all_skips_profile_missing_facts_etalons_optional(three_profiles, monkeypatch):
    (batch.FACTS_DIR / "kate.json").unlink()       # kate: no fact sheet -> skipped
    (batch.ETALONS_DIR / "michael.json").unlink()  # michael: no etalons -> still ready
    calls = []
    monkeypatch.setattr(batch, "batch_prefill", _stub(calls))
    res = asyncio.run(batch.batch_prefill_all())
    assert res["ready"] == ["michael"]             # etalons optional; facts is the gate
    assert "kate" in res["skipped"] and "sample" in res["skipped"]
    assert [c[0] for c in calls] == ["michael"]


# --- B2: CLI guard lets --profile all through for --batch -------------------------

def test_guard_sample_passes_profile_all_with_batch():
    ap = argparse.ArgumentParser()
    a = argparse.Namespace(profile="all", allow_sample=False, batch=True)
    _guard_sample(ap, a)  # must not exit — per-profile check runs inside batch_prefill_all


def test_guard_sample_profile_all_without_batch_is_usage_error(monkeypatch):
    def boom(_pid):
        raise KeyError("profile not found: 'all'")
    monkeypatch.setattr("backend.apply_cli.get_profile", boom)
    ap = argparse.ArgumentParser()
    a = argparse.Namespace(profile="all", allow_sample=False, batch=False)
    with pytest.raises(SystemExit):
        _guard_sample(ap, a)


def test_guard_sample_still_blocks_the_sample_profile(monkeypatch):
    monkeypatch.setattr("backend.apply_cli.get_profile",
                        lambda _pid: _profile("sample", is_sample=True))
    ap = argparse.ArgumentParser()
    a = argparse.Namespace(profile="sample", allow_sample=False, batch=True)
    with pytest.raises(SystemExit):
        _guard_sample(ap, a)


# --- eligibility: submitted-only application cap + nationality rule -------------
def _roles(company, n, family="data"):
    return [{"apply_url": f"https://{company}/{i}", "title": "Data Analyst",
             "family": family, "company": company.title()} for i in range(n)]


def test_cap_skips_candidate_already_at_limit():
    # alice already submitted 5 to OpenAI -> at the cap, draws no new roles.
    plan = batch.assign_round_robin(_roles("openai", 4), {"data": ["alice"]}, {}, k=1,
                                    app_counts={("alice", "openai"): 5}, caps={"openai": 5})
    assert "alice" not in plan


def test_cap_limits_assignments_within_one_run():
    # One candidate, ten OpenAI roles, no prior submits: at most 5 assigned this run.
    plan = batch.assign_round_robin(_roles("openai", 10), {"data": ["alice"]}, {}, k=1,
                                    app_counts={}, caps={"openai": 5})
    assert len(plan.get("alice", set())) == 5


def test_cap_is_per_company():
    # alice at cap for OpenAI is still eligible at an uncapped company.
    roles = _roles("openai", 2) + _roles("cohere", 1)
    plan = batch.assign_round_robin(roles, {"data": ["alice"]}, {}, k=1,
                                    app_counts={("alice", "openai"): 5}, caps={"openai": 5})
    assert plan.get("alice") == {"https://cohere/0"}


def test_no_cap_when_company_uncapped():
    plan = batch.assign_round_robin(_roles("cohere", 3), {"data": ["alice"]}, {}, k=1,
                                    app_counts={("alice", "cohere"): 99}, caps={})
    assert len(plan.get("alice", set())) == 3


def test_nationality_kz_only_to_salmon():
    pool = {"data": ["gen_kz_01_a", "gen_us_02_b", "gen_ca_03_c"]}
    # Salmon accepts ONLY Kazakhstani personas.
    salmon = batch.assign_round_robin(
        [{"apply_url": "https://salmon/1", "title": "Data Analyst", "family": "data",
          "company": "Salmon"}], pool, {}, k=3, allowed_countries={"salmon": {"kz"}})
    assert salmon.get("gen_kz_01_a") == {"https://salmon/1"}
    assert "gen_us_02_b" not in salmon and "gen_ca_03_c" not in salmon
    # OpenAI (any non-Salmon) accepts ONLY US/CA personas.
    openai = batch.assign_round_robin(
        [{"apply_url": "https://openai/1", "title": "Data Analyst", "family": "data",
          "company": "OpenAI"}], pool, {}, k=3, allowed_countries={"openai": {"us", "ca"}})
    assert "gen_kz_01_a" not in openai
    assert "gen_us_02_b" in openai and "gen_ca_03_c" in openai


def test_country_helpers_from_registry(monkeypatch):
    monkeypatch.setattr(batch, "load_targets", lambda enabled_only=True: [
        {"key": "salmon", "company": "Salmon", "countries": ["kz"]},
        {"key": "openai", "company": "OpenAI", "apply_cap": 5}])
    assert batch._company_countries("Salmon") == {"kz"}
    assert batch._company_countries("OpenAI") == {"us", "ca"}   # default: non-Salmon -> US/CA
    assert batch.country_allowed("gen_kz_01_x", "Salmon") is True
    assert batch.country_allowed("gen_kz_01_x", "OpenAI") is False
    assert batch.country_allowed("gen_us_01_y", "OpenAI") is True
    assert batch.country_allowed("gen_us_01_y", "Salmon") is False
    assert batch.country_allowed("michael_sample", "OpenAI") is True  # unknown country -> allowed
    assert batch._company_cap("OpenAI") == 5 and batch._company_cap("Cohere") == 0


def test_application_count_submitted_only(tmp_path, monkeypatch):
    import json as _json
    root = tmp_path / "prefill"
    monkeypatch.setattr(batch, "OUT_ROOT", root)
    statuses = {("alice", "j1"): "submitted", ("alice", "j2"): "interview",
                ("alice", "j3"): "pending", ("alice", "j4"): "submitted"}
    monkeypatch.setattr(batch, "_status_rec",
                        lambda pid, jid, cache: {"status": statuses.get((pid, jid), ""), "ts": ""})

    def _rep(pid, jid, company):
        d = root / pid / jid
        d.mkdir(parents=True, exist_ok=True)
        (d / "report.json").write_text(_json.dumps({"company": company}))

    _rep("alice", "j1", "OpenAI"); _rep("alice", "j2", "OpenAI")   # submitted + interview -> 2
    _rep("alice", "j3", "OpenAI"); _rep("alice", "j4", "Cohere")   # pending excluded; Cohere sep.
    assert batch.application_count("alice", "OpenAI") == 2          # only actually-sent count
    assert batch.application_count("alice", "Cohere") == 1
    assert batch.at_application_cap("alice", "OpenAI", cap=2) is True
    assert batch.at_application_cap("alice", "OpenAI", cap=3) is False
    counts = batch.application_counts()
    assert counts[("alice", "openai")] == 2 and counts[("alice", "cohere")] == 1
