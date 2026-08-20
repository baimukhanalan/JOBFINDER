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


# --- per-candidate application cap (ATS policy: <=5 per company per 180 days) -----
def _acme_roles(n):
    return [{"apply_url": f"https://acme/{i}", "title": "Data Analyst",
             "family": "data", "company": "Acme"} for i in range(n)]


def test_cap_skips_candidate_already_at_limit():
    # alice already has 5 Acme applications -> she is at the cap and draws no new roles.
    plan = batch.assign_round_robin(_acme_roles(4), {"data": ["alice"]}, {}, k=1,
                                    app_counts={("alice", "acme"): 5}, cap=5)
    assert "alice" not in plan


def test_cap_limits_assignments_within_one_run():
    # One candidate, ten Acme roles, no prior history: she may take at most 5 this run.
    plan = batch.assign_round_robin(_acme_roles(10), {"data": ["alice"]}, {}, k=1,
                                    app_counts={}, cap=5)
    assert len(plan.get("alice", set())) == 5


def test_cap_is_per_company_not_global():
    # 4 Acme (alice at cap there) + 2 Globex roles; alice still eligible at Globex.
    roles = _acme_roles(2) + [
        {"apply_url": "https://globex/1", "title": "Data Analyst", "family": "data",
         "company": "Globex"}]
    plan = batch.assign_round_robin(roles, {"data": ["alice"]}, {}, k=1,
                                    app_counts={("alice", "acme"): 5}, cap=5)
    assert plan.get("alice") == {"https://globex/1"}  # only the Globex role


def test_cap_disabled_when_zero():
    plan = batch.assign_round_robin(_acme_roles(3), {"data": ["alice"]}, {}, k=1,
                                    app_counts={("alice", "acme"): 99}, cap=0)
    assert len(plan.get("alice", set())) == 3


def test_application_count_windows_gating_and_company(tmp_path, monkeypatch):
    import json as _json
    import os as _os
    import time as _time
    root = tmp_path / "prefill"
    monkeypatch.setattr(batch, "OUT_ROOT", root)

    def _rep(pid, jid, company, gated=False, age_days=0):
        d = root / pid / jid
        d.mkdir(parents=True, exist_ok=True)
        p = d / "report.json"
        p.write_text(_json.dumps({"company": company, "gated_out": gated}))
        if age_days:
            old = _time.time() - age_days * 86400
            _os.utime(p, (old, old))

    _rep("alice", "j1", "Acme")
    _rep("alice", "j2", "Acme")
    _rep("alice", "j3", "Acme", gated=True)     # gated_out -> not an application
    _rep("alice", "j4", "Acme", age_days=200)   # outside the 180-day window
    _rep("alice", "j5", "Globex")               # different employer
    assert batch.application_count("alice", "Acme") == 2
    assert batch.at_application_cap("alice", "Acme", cap=2) is True
    assert batch.at_application_cap("alice", "Acme", cap=3) is False
    assert batch.application_count("alice", "Globex") == 1
    # aggregate scan agrees
    counts = batch.application_counts()
    assert counts[("alice", "acme")] == 2 and counts[("alice", "globex")] == 1
