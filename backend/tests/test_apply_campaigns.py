"""apply_campaigns store + pacing + target resolution — pure, no DB/network. Run:
    PYTHONPATH=. python3 -m pytest backend/tests/test_apply_campaigns.py -q
"""
from backend.tools import apply_campaigns as ac


def _use_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "_PATH", tmp_path / "apply_campaigns.json")


def test_create_list_toggle_delete(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    c = ac.create(name="Alex Doe", target_kind="search", q="Kazakhstan", per_day=3, today="2026-09-09")
    assert c["id"] == 1 and c["name"] == "Alex Doe" and c["per_day"] == 3
    assert c["email"].endswith("@takhet.com") and c["pid"].startswith("demo_camp1")
    assert c["active"] is True
    assert len(ac.list_campaigns()) == 1
    assert ac.set_active(1, False) and ac.list_campaigns()[0]["active"] is False
    assert ac.delete(1) and ac.list_campaigns() == []


def test_create_requires_job_id_for_job_kind(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    try:
        ac.create(name="X", target_kind="job", today="2026-09-09")
        assert False, "should have raised"
    except ValueError:
        pass


def test_per_day_pacing_and_daily_reset(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    c = ac.create(name="Y", target_kind="search", q="us", per_day=3, today="2026-09-09")
    assert ac.remaining_today(c, "2026-09-09") == 3
    ac.note_run(1, [10, 11], "2026-09-09")
    assert ac.remaining_today(ac.list_campaigns()[0], "2026-09-09") == 1
    # a new day resets the budget
    assert ac.remaining_today(ac.list_campaigns()[0], "2026-09-10") == 3


def test_resolve_job_campaign_applies_per_day_times(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    ac.create(name="Z", target_kind="job", job_id=555, per_day=4, today="2026-09-09")
    c = ac.list_campaigns()[0]
    got = ac.resolve_targets(c, "2026-09-09", list_jobs=lambda **k: [], submitted=set())
    assert got == [555, 555, 555, 555]     # N/day on the one job (owner-requested)


def test_resolve_search_excludes_applied_and_submitted(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    ac.create(name="S", target_kind="search", q="Kazakhstan", per_day=2, today="2026-09-09")
    ac.note_run(1, [100], "2026-09-09")     # already applied 100 (and used 1 of 2 today)
    c = ac.list_campaigns()[0]
    rows = [{"id": 100, "ats": "greenhouse"}, {"id": 101, "ats": "ashby"},
            {"id": 102, "ats": "greenhouse"}, {"id": 103, "ats": "greenhouse"}]
    got = ac.resolve_targets(c, "2026-09-09", list_jobs=lambda **k: rows, submitted={102})
    # remaining today = 1; 100 already applied, 102 submitted -> first fresh is 101
    assert got == [101]


def test_resolve_search_skips_non_auto_submittable_ats(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    ac.create(name="A", target_kind="search", q="us", per_day=2, today="2026-09-09")
    c = ac.list_campaigns()[0]
    rows = [{"id": 1, "ats": "lever"}, {"id": 2, "ats": "workable"},
            {"id": 3, "ats": "greenhouse"}, {"id": 4, "ats": "ashby"}]
    got = ac.resolve_targets(c, "2026-09-09", list_jobs=lambda **k: rows, submitted=set())
    # lever/workable (live-captcha, can't auto-submit) skipped -> only greenhouse/ashby
    assert got == [3, 4]


def test_inactive_campaign_resolves_nothing(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    ac.create(name="Q", target_kind="job", job_id=7, per_day=2, today="2026-09-09")
    ac.set_active(1, False)
    c = ac.list_campaigns()[0]
    assert ac.resolve_targets(c, "2026-09-09", list_jobs=lambda **k: [], submitted=set()) == []
