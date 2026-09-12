"""Per-company velocity guard: the shared cap that makes '149 fills on one company' impossible."""
import os
import time

import pytest

from backend.tools import company_velocity as cv

NOW = 1_800_000_000.0


def _prefill(tmp_path, hits):
    """Create uploads/prefill/<demo>/<jobid>/ dirs with the given (jobid, age_seconds) hits."""
    root = tmp_path / "prefill"
    for i, (jid, age) in enumerate(hits):
        d = root / f"demo_p{i}" / str(jid)
        d.mkdir(parents=True)
        os.utime(d, (NOW - age, NOW - age))
    return root


def _rows(mapping):
    """jobs_by_ids stub: {jobid: {"company_key": ck}}."""
    def jobs_by_ids(ids):
        return {int(j): {"id": int(j), "company_key": mapping[int(j)]} for j in ids if int(j) in mapping}
    return jobs_by_ids


def _cj(mapping):
    """company_jobids stub: company_key -> all its jobids (from the same mapping)."""
    def company_jobids(ck):
        return [j for j, c in mapping.items() if c == ck]
    return company_jobids


def test_prefill_hits_indexes_jobid_dirs(tmp_path):
    root = _prefill(tmp_path, [(11, 10), (11, 20), (22, 30)])
    hits = cv.prefill_hits(root)
    assert sorted(hits) == [11, 22] and len(hits[11]) == 2 and len(hits[22]) == 1


def test_recent_counts_rolling_windows(tmp_path):
    # salmon: 2 hits today, 1 hit 3 days ago, 1 hit 10 days ago (outside the week)
    root = _prefill(tmp_path, [(1, 100), (2, 3600), (3, 3 * 86400), (4, 10 * 86400)])
    m = {1: "salmon", 2: "salmon", 3: "salmon", 4: "salmon"}
    c = cv.recent_counts({"salmon"}, company_jobids=_cj(m), hits=cv.prefill_hits(root), now=NOW)
    assert c["salmon"] == {"day": 2, "week": 3}


def test_guard_drops_over_cap_company_keeps_others(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_CAP_PER_DAY", "2")
    monkeypatch.setenv("COMPANY_CAP_PER_WEEK", "6")
    # salmon already has 2 hits today -> over the daily cap; acme has none
    root = _prefill(tmp_path, [(1, 100), (2, 200)])
    m = {1: "salmon", 2: "salmon", 3: "salmon", 4: "acme", 5: "acme"}
    kept, held = cv.guard([3, 4, 5], jobs_by_ids=_rows(m), company_jobids=_cj(m),
                          hits=cv.prefill_hits(root), now=NOW)
    assert kept == [4, 5]
    assert held == {"salmon": 1}


def test_guard_limits_one_batch_to_remaining_budget(tmp_path, monkeypatch):
    """A single bulk run must not fire 10 at one tenant: budget = per_day - hits today."""
    monkeypatch.setenv("COMPANY_CAP_PER_DAY", "2")
    monkeypatch.setenv("COMPANY_CAP_PER_WEEK", "6")
    root = _prefill(tmp_path, [])                       # no prior hits
    m = {i: "salmon" for i in range(1, 11)}
    kept, held = cv.guard(list(range(1, 11)), jobs_by_ids=_rows(m), company_jobids=_cj(m),
                          hits=cv.prefill_hits(root), now=NOW)
    assert kept == [1, 2] and held == {"salmon": 8}


def test_weekly_cap_binds_when_daily_has_room(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_CAP_PER_DAY", "2")
    monkeypatch.setenv("COMPANY_CAP_PER_WEEK", "6")
    # 6 hits spread over the week, none today -> daily room 2, weekly room 0 -> blocked
    root = _prefill(tmp_path, [(i, (i + 1) * 86400 - 10) for i in range(1, 7)])
    m = {i: "salmon" for i in range(1, 8)}
    kept, held = cv.guard([7], jobs_by_ids=_rows(m), company_jobids=_cj(m),
                          hits=cv.prefill_hits(root), now=NOW)
    assert kept == [] and held == {"salmon": 1}


def test_unknown_company_never_blocked(tmp_path):
    root = _prefill(tmp_path, [])
    kept, held = cv.guard([99], jobs_by_ids=_rows({}), company_jobids=_cj({}),
                          hits=cv.prefill_hits(root), now=NOW)
    assert kept == [99] and held == {}


def test_cap_off_escape_hatch(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_CAP_OFF", "1")
    root = _prefill(tmp_path, [(1, 1), (2, 2), (3, 3)])
    m = {i: "salmon" for i in range(1, 5)}
    kept, held = cv.guard([4], jobs_by_ids=_rows(m), company_jobids=_cj(m),
                          hits=cv.prefill_hits(root), now=NOW)
    assert kept == [4] and held == {}


def test_guard_error_lets_jobs_through(monkeypatch):
    def boom(ids):
        raise RuntimeError("db down")
    kept, held = cv.guard([1, 2], jobs_by_ids=boom, now=NOW)
    assert kept == [1, 2] and held == {}


def test_campaign_resolve_applies_velocity_guard(tmp_path, monkeypatch):
    """resolve_targets (jobs kind) passes its candidates through the guard: an over-cap company
    is trimmed and the run still fills from the others."""
    from backend.tools import apply_campaigns as ac
    monkeypatch.setattr(ac, "_DATA", str(tmp_path), raising=False)
    monkeypatch.setattr(ac, "PATH", str(tmp_path / "apply_campaigns.json"), raising=False)
    rows = {i: {"id": i, "ats": "ashby", "company_key": ("salmon" if i <= 3 else "acme"),
                "dead": False} for i in range(1, 7)}
    camp = {"id": 1, "name": "Dana", "target_kind": "jobs", "job_ids": list(range(1, 7)),
            "per_day": 4, "active": True, "cursor": 0, "confirmed_jobids": [],
            "quarantine_jobids": [], "applied_jobids": []}
    seen = {}

    def guard(ids, **kw):
        seen["in"] = list(ids)
        return [i for i in ids if rows[i]["company_key"] != "salmon"], {"salmon": 3}

    out = ac.resolve_targets(camp, "2026-09-13", jobs_by_ids=lambda ids: {i: rows[i] for i in ids if i in rows},
                             velocity_guard=guard)
    assert seen["in"], "guard was consulted"
    assert out and all(rows[j]["company_key"] == "acme" for j in out)
    assert len(out) <= 4
