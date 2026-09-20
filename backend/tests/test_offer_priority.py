"""Offer/interview-efficiency layer: high-pay ordering + multi-candidate-per-position with a
stop-on-interview/offer condition, composed WITHIN the per-company velocity cap. All pure/
injectable — no DB, mail, network, and disk only via tmp_path."""
import json
import os

import pytest

from backend.tools import offer_priority as op


# ---- knobs ------------------------------------------------------------------------------------
def test_apply_order_defaults_to_none(monkeypatch):
    monkeypatch.delenv("APPLY_ORDER", raising=False)
    assert op.apply_order() == "none"   # owner 2026-09-20: no high-pay accent by default


@pytest.mark.parametrize("val,expect", [
    ("pay_asc", "pay_asc"), ("none", "none"), ("as_is", "as_is"),
    ("PAY_DESC", "pay_desc"), ("garbage", "none"),
])
def test_apply_order_knob(monkeypatch, val, expect):
    monkeypatch.setenv("APPLY_ORDER", val)
    assert op.apply_order() == expect


def test_candidates_per_position_knob(monkeypatch):
    monkeypatch.delenv("APPLY_CANDIDATES_PER_POSITION", raising=False)
    assert op.candidates_per_position() == 2                 # default K
    monkeypatch.setenv("APPLY_CANDIDATES_PER_POSITION", "3")
    assert op.candidates_per_position() == 3
    monkeypatch.setenv("APPLY_CANDIDATES_PER_POSITION", "0")  # clamps to >= 1
    assert op.candidates_per_position() == 1
    monkeypatch.setenv("APPLY_CANDIDATES_PER_POSITION", "999")
    assert op.candidates_per_position() == 8                  # clamps to <= 8
    monkeypatch.setenv("APPLY_CANDIDATES_PER_POSITION", "x")
    assert op.candidates_per_position() == 2                  # bad value -> default


def test_mh_candidates_default_unlimited(monkeypatch):
    monkeypatch.delenv("MH_CANDIDATES_PER_POSITION", raising=False)
    assert op.mh_candidates_per_position() == 0               # unlimited by default
    monkeypatch.setenv("MH_CANDIDATES_PER_POSITION", "5")
    assert op.mh_candidates_per_position() == 5


def test_lane_priority_favours_bpo():
    assert op.lane_priority("maximus") > op.lane_priority("catalog")
    assert op.lane_priority("teleperformance") > 0
    assert op.lane_priority("greenhouse") == 0                # catalog/tech lane = no fast weight
    assert op.lane_priority("") == 0


# ---- pay keys ---------------------------------------------------------------------------------
def test_pay_key_catalog_takes_highest_disclosed_figure():
    assert op.pay_key_catalog({"comp_min": 120000, "comp_max": 180000}) == 180000
    # estimated total beats a lower posted base
    assert op.pay_key_catalog({"comp_min": 90000, "est_total_max": 250000}) == 250000
    assert op.pay_key_catalog({"est_base_min": 60000}) == 60000
    assert op.pay_key_catalog({}) == 0.0
    assert op.pay_key_catalog(None) == 0.0
    assert op.pay_key_catalog({"comp_max": None, "comp_min": "bad"}) == 0.0


def test_pay_key_masshiring_posted_beats_estimate():
    hp_posted = op.pay_key_masshiring({}, hourly_pay=lambda r: (20.0, 30.0, False))
    hp_est = op.pay_key_masshiring({}, hourly_pay=lambda r: (20.0, 30.0, True))
    assert hp_posted == 30.0
    assert hp_est == pytest.approx(29.99)          # estimate gets a tiny penalty
    assert hp_posted > hp_est
    assert op.pay_key_masshiring({}, hourly_pay=lambda r: None) == 0.0
    assert op.pay_key_masshiring({}, hourly_pay=lambda r: (_ for _ in ()).throw(RuntimeError())) == 0.0


def test_order_by_pay_desc_and_asc_and_none():
    rows = {1: {"comp_max": 100000}, 2: {"comp_max": 300000}, 3: {"comp_max": 50000}}
    key = lambda j: op.pay_key_catalog(rows[j])
    assert op.order_by_pay([1, 2, 3], key, order="pay_desc") == [2, 1, 3]
    assert op.order_by_pay([1, 2, 3], key, order="pay_asc") == [3, 1, 2]
    assert op.order_by_pay([1, 2, 3], key, order="none") == [1, 2, 3]     # stable, unchanged


def test_order_by_pay_is_stable_for_ties():
    key = lambda j: 0.0
    assert op.order_by_pay([5, 4, 3, 2, 1], key, order="pay_desc") == [5, 4, 3, 2, 1]


# ---- stop-on-response -------------------------------------------------------------------------
@pytest.mark.parametrize("stage,landed", [
    ("interview", True), ("offer", True), ("OFFER", True), ("  Interview ", True),
    ("action_needed", False), ("assessment_done", False), ("ack", False),
    ("rejection", False), ("other", False), (None, False), ("", False),
])
def test_is_landed(stage, landed):
    assert op.is_landed(stage) is landed


def test_landed_positions():
    stages = {1: "interview", 2: "ack", 3: "offer", 4: None, 5: "assessment_done"}
    got = op.landed_positions([1, 2, 3, 4, 5], lambda j: stages.get(j))
    assert got == {1, 3}


def test_landed_positions_swallows_errors():
    def boom(j):
        if j == 2:
            raise RuntimeError("mail down")
        return "interview" if j == 1 else None
    assert op.landed_positions([1, 2, 3], boom) == {1}       # error id treated as un-landed


def test_remaining_candidates():
    assert op.remaining_candidates(0, None, 2) == 2
    assert op.remaining_candidates(1, None, 2) == 1
    assert op.remaining_candidates(2, None, 2) == 0
    assert op.remaining_candidates(5, None, 2) == 0          # never negative
    assert op.remaining_candidates(0, "interview", 2) == 0   # STOP on interview
    assert op.remaining_candidates(0, "offer", 5) == 0       # STOP on offer
    assert op.remaining_candidates(3, "ack", 2) == 0         # ack is not a stop, but K reached
    # k <= 0 == unlimited lifetime budget (the volume-lane mode)
    assert op.remaining_candidates(100, None, 0) == op._UNLIMITED
    assert op.remaining_candidates(100, "interview", 0) == 0  # stop still wins over unlimited


# ---- plan_positions: multi-candidate + stop + order + cap -------------------------------------
def test_plan_positions_emits_k_copies_high_pay_first():
    pay = {1: 100000, 2: 300000, 3: 50000}
    plan = op.plan_positions([1, 2, 3], pay_key_of=lambda j: pay[j], k=2, order="pay_desc")
    # 2 copies each, highest-paying first (explicit pay_desc — default is now 'none')
    assert plan == [2, 2, 1, 1, 3, 3]


def test_plan_positions_stops_landed_and_respects_applied_count():
    pay = {1: 100000, 2: 300000, 3: 50000}
    stages = {2: "interview"}                    # 2 already landed -> no more
    applied = {1: 1}                             # 1 already got 1 persona -> 1 more (K=2)
    plan = op.plan_positions([1, 2, 3], pay_key_of=lambda j: pay[j],
                             furthest_stage_of=lambda j: stages.get(j),
                             applied_count_of=lambda j: applied.get(j, 0), k=2)
    assert plan == [1, 3, 3]                      # 2 dropped (landed); 1 gets 1; 3 gets 2


def test_plan_positions_per_position_cap_limits_copies():
    plan = op.plan_positions([1, 2], pay_key_of=lambda j: -j, k=0, per_position_cap=3)
    # k=0 (unlimited lifetime) but per_position_cap=3 caps each run; pay -j so 1 before 2
    assert plan == [1, 1, 1, 2, 2, 2]


def test_plan_positions_dedupes_input():
    plan = op.plan_positions([1, 1, 2], pay_key_of=lambda j: 0.0, k=1)
    assert sorted(plan) == [1, 2]


def test_plan_positions_runs_flat_plan_through_velocity_guard():
    # a stub guard that keeps only the first 2 of any flat plan (models a company budget of 2)
    def guard(plan):
        return plan[:2], {}
    plan = op.plan_positions([1, 2, 3], pay_key_of=lambda j: {1: 9, 2: 8, 3: 7}[j],
                             k=3, velocity_guard=guard)
    assert plan == [1, 1]                         # ordered [1,1,1,2,2,2,3,3,3] -> trimmed to 2


def test_plan_positions_guard_error_leaves_plan_uncapped():
    def guard(plan):
        raise RuntimeError("db down")
    plan = op.plan_positions([1], pay_key_of=lambda j: 0.0, k=2, velocity_guard=guard)
    assert plan == [1, 1]                         # guard failure never drops the lane's jobs


# ---- velocity-cap interaction with the REAL company_velocity.guard ----------------------------
def test_multi_candidate_lives_within_the_real_company_cap():
    """K copies of positions at ONE company are trimmed by the real per-company cap to that
    company's remaining daily budget — the redundancy raises odds WITHIN the cap, not past it."""
    from backend.tools import company_velocity as cv
    NOW = 1_800_000_000.0
    # jobs 1,2 belong to company 'acme'; job 3 to 'globex'. No prior fills on disk.
    mapping = {1: "acme", 2: "acme", 3: "globex"}
    jobs_by_ids = lambda ids: {int(j): {"id": int(j), "company_key": mapping[int(j)]}
                               for j in ids if int(j) in mapping}
    company_jobids = lambda ck: [j for j, c in mapping.items() if c == ck]

    def guard(plan):
        return cv.guard(plan, jobs_by_ids=jobs_by_ids, company_jobids=company_jobids,
                        hits={}, now=NOW, per_day=2, per_week=6)

    # K=3 copies each of jobs 1,2,3 = 9 attempts, but EACH company's daily cap is 2.
    plan = op.plan_positions([1, 2, 3], pay_key_of=lambda j: {1: 9, 2: 8, 3: 7}[j],
                             k=3, velocity_guard=guard)
    assert plan.count(3) == 2                       # globex trimmed to its 2/day budget
    assert plan.count(1) + plan.count(2) == 2       # acme (jobs 1+2) also trimmed to 2/day total
    assert len(plan) == 4                           # 9 requested -> 4 kept across the two caps


# ---- personas_by_jobid / applied_counts / furthest_stage_by_jobid (disk join) -----------------
def _write_persona(root, demo, jobid, email):
    d = root / demo / str(jobid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "persona.json").write_text(json.dumps({"profile": {"email": email}}), encoding="utf-8")
    return d


def test_personas_by_jobid_scans_prefill_tree(tmp_path):
    root = tmp_path / "prefill"
    _write_persona(root, "demo_a1", 11, "a1@takhet.com")
    _write_persona(root, "demo_b2", 11, "b2@takhet.com")     # 2nd persona on the same position
    _write_persona(root, "demo_c3", "mh_50", "c3@takhet.com")  # mass-hiring namespace
    got = op.personas_by_jobid([11, "mh_50", 99], prefill_root=root)
    assert sorted(got["11"]) == ["a1@takhet.com", "b2@takhet.com"]
    assert got["mh_50"] == ["c3@takhet.com"]
    assert "99" not in got                                   # no dir -> absent


def test_personas_by_jobid_missing_root_returns_empty(tmp_path):
    assert op.personas_by_jobid([1, 2], prefill_root=tmp_path / "does_not_exist") == {}


def test_applied_counts(tmp_path):
    root = tmp_path / "prefill"
    _write_persona(root, "demo_a1", 11, "a1@x")
    _write_persona(root, "demo_b2", 11, "b2@x")
    _write_persona(root, "demo_c3", 22, "c3@x")
    got = op.applied_counts([11, 22, 33], prefill_root=root)
    assert got == {"11": 2, "22": 1}


def test_furthest_stage_by_jobid_picks_best_across_personas(tmp_path):
    root = tmp_path / "prefill"
    _write_persona(root, "demo_a1", 11, "a1@x")
    _write_persona(root, "demo_b2", 11, "b2@x")              # this one reached interview
    _write_persona(root, "demo_c3", 22, "c3@x")
    stage_by_mailbox = lambda emails: {"a1@x": "ack", "b2@x": "interview", "c3@x": "rejection"}
    got = op.furthest_stage_by_jobid([11, 22], prefill_root=root,
                                     stage_by_mailbox=stage_by_mailbox)
    assert got["11"] == "interview"                          # best of ack + interview
    assert got["22"] == "rejection"


def test_landed_check_closure(tmp_path):
    root = tmp_path / "prefill"
    _write_persona(root, "demo_a1", 11, "a1@x")
    _write_persona(root, "demo_c3", 22, "c3@x")
    stage_by_mailbox = lambda emails: {"a1@x": "offer", "c3@x": "ack"}
    check = op.landed_check([11, 22], prefill_root=root, stage_by_mailbox=stage_by_mailbox)
    assert op.is_landed(check(11)) is True
    assert op.is_landed(check(22)) is False
    assert check(999) is None


# ---- plan_mh_batch live wrapper ---------------------------------------------------------------
def test_plan_mh_batch_orders_by_pay_and_stops_landed(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLY_ORDER", "pay_desc")   # exercise ordering (default is now 'none')
    root = tmp_path / "prefill"
    # job 22 already reached interview -> dropped; jobs 11 & 33 open
    _write_persona(root, "demo_x", "mh_22", "x@x")
    rows = {11: {"salary_min": 15, "salary_max": 18},
            22: {"salary_min": 25, "salary_max": 40},
            33: {"salary_min": 20, "salary_max": 30}}
    stage_by_mailbox = lambda emails: {"x@x": "interview"}
    plan = op.plan_mh_batch([11, 22, 33], rounds=2, rows_by_id=rows, prefill_root=root,
                            stage_by_mailbox=stage_by_mailbox, k=0)
    # 22 stopped (interview); remaining ordered high-pay-first: 33 ($30) before 11 ($18); rounds=2
    assert plan == [33, 33, 11, 11]


def test_plan_mh_batch_worktree_no_uploads_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLY_ORDER", "pay_desc")   # exercise ordering (default is now 'none')
    rows = {11: {"salary_min": 15}, 22: {"salary_min": 25}}
    # no prefill tree -> nothing landed, applied=0 -> each id gets `rounds` copies (order by pay)
    plan = op.plan_mh_batch([11, 22], rounds=2, rows_by_id=rows,
                            prefill_root=tmp_path / "absent", stage_by_mailbox=lambda e: {}, k=0)
    assert sorted(plan) == [11, 11, 22, 22]
    assert plan[:2] == [22, 22]                              # higher pay first


def test_plan_mh_batch_lifetime_cap(tmp_path):
    root = tmp_path / "prefill"
    _write_persona(root, "demo_a", "mh_11", "a@x")           # 11 already has 1 persona
    rows = {11: {"salary_min": 20}}
    # K=2 lifetime, 1 already applied, rounds=5 -> only 1 more this run
    plan = op.plan_mh_batch([11], rounds=5, rows_by_id=rows, prefill_root=root,
                            stage_by_mailbox=lambda e: {}, k=2)
    assert plan == [11]


def test_plan_mh_batch_empty():
    assert op.plan_mh_batch([], rounds=3) == []


def test_catalog_pay_floor(monkeypatch):
    monkeypatch.setenv("APPLY_MIN_MONTHLY_USD", "5000")   # $60k/yr floor
    assert op.passes_catalog_floor({"est_total_max": 72000}) is True    # $6k/mo — passes
    assert op.passes_catalog_floor({"est_total_max": 48000}) is False   # $4k/mo — below
    assert op.passes_catalog_floor({}) is True                          # no comp — kept (benefit of doubt)
    monkeypatch.setenv("APPLY_MIN_MONTHLY_USD", "0")
    assert op.passes_catalog_floor({"est_total_max": 12000}) is True    # floor off — passes
