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


# ---- `jobs` kind: an owner-picked SET of catalog ids walked round-robin by a cursor -------------

def _alive(rows):
    """A jobs_by_ids stub: {id: row} from a list of rows (rows may carry dead=True)."""
    return lambda ids: {int(r["id"]): r for r in rows if int(r["id"]) in set(int(i) for i in ids)}


def _jobs_campaign(job_ids, per_day, today="2026-09-09", name="J Set"):
    return ac.create(name=name, target_kind="jobs", job_ids=job_ids, per_day=per_day, today=today)


def test_create_jobs_requires_ids(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    for bad in (None, [], "", " , "):
        try:
            ac.create(name="J", target_kind="jobs", job_ids=bad, today="2026-09-09")
            assert False, f"should have raised for {bad!r}"
        except ValueError as exc:
            assert "job_ids" in str(exc)
    # a non-numeric token is an error, never silently dropped from the selection
    try:
        ac.create(name="J", target_kind="jobs", job_ids="1,abc", today="2026-09-09")
        assert False, "should have raised"
    except ValueError:
        pass
    assert ac.list_campaigns() == []
    # the other kinds are unchanged: still no job_ids needed, still a name required
    try:
        ac.create(name="", target_kind="search", q="us", today="2026-09-09")
        assert False, "search kind must still require a name"
    except ValueError:
        pass


def test_create_jobs_dedups_and_sets_cursor(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    c = _jobs_campaign([3, "3", 1, 3, 2, 1], per_day=9)
    assert c["target_kind"] == "jobs"
    assert c["job_ids"] == [3, 1, 2]          # de-duplicated, ORDER PRESERVED, ints
    assert c["cursor"] == 0
    assert c["per_day"] == 9                  # a free number (clamped 1..100), not a 1-5 picker
    assert _jobs_campaign([1], per_day=500)["per_day"] == 100 and _jobs_campaign([1], per_day=0)["per_day"] == 1
    assert c["email"].endswith("@takhet.com") and c["pid"].startswith("demo_camp1")
    # the form-encoded "1,2,3" the /catalog sheet POSTs is accepted too
    c2 = ac.create(name="J2", target_kind="jobs", job_ids="1, 2,2 3", today="2026-09-09")
    assert c2["job_ids"] == [1, 2, 3] and c2["cursor"] == 0
    # the persisted row carries the selection + cursor
    saved = {r["id"]: r for r in ac.list_campaigns()}
    assert saved[1]["job_ids"] == [3, 1, 2] and saved[1]["cursor"] == 0
    # other kinds do NOT grow the new keys
    s = ac.create(name="S", target_kind="search", q="us", today="2026-09-09")
    assert "job_ids" not in s and "cursor" not in s


def test_jobs_empty_name_is_generated(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    seen = {}

    def fake_gen(ids, gender):
        seen["ids"], seen["gender"] = list(ids), gender
        return "Gen Person"

    # (a) the module hook (what the live create() uses) — monkeypatched, so no DB / persona banks
    monkeypatch.setattr(ac, "_generated_name", fake_gen)
    c = ac.create(name="", target_kind="jobs", job_ids=[5, 6], gender="female", today="2026-09-09")
    assert c["name"] == "Gen Person" and seen == {"ids": [5, 6], "gender": "female"}
    assert c["email"].startswith("gen.person") and c["email"].endswith("@takhet.com")
    assert c["pid"] == "demo_camp1_genperson"
    # (b) an explicit `name_for` callable wins over the module hook
    c2 = ac.create(name="  ", target_kind="jobs", job_ids=[7], today="2026-09-09",
                   name_for=lambda ids, g: "Other Name")
    assert c2["name"] == "Other Name"
    # (c) a typed name is used verbatim — the generator is NOT consulted
    seen.clear()
    c3 = ac.create(name="Typed Name", target_kind="jobs", job_ids=[8], today="2026-09-09")
    assert c3["name"] == "Typed Name" and seen == {}
    # (d) a generator that yields nothing still fails closed
    try:
        ac.create(name="", target_kind="jobs", job_ids=[9], today="2026-09-09",
                  name_for=lambda ids, g: "")
        assert False, "should have raised"
    except ValueError:
        pass


def test_generated_name_falls_back_to_us_without_db(monkeypatch):
    """`_generated_name` itself: a failing catalog lookup → 'United States' bank, gender default
    'male'; never raises out of the DB path."""
    from backend.tools import catalog_db, synth_persona
    calls = {}
    monkeypatch.setattr(catalog_db, "jobs_by_ids", lambda ids: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(synth_persona, "_pick_name",
                        lambda country, gender="either": calls.setdefault("args", (country, gender)) and "X Y")
    assert ac._generated_name([1, 2], "") == "X Y"
    assert calls["args"] == ("United States", "male")
    # a found row routes through _country_of
    calls.clear()
    monkeypatch.setattr(catalog_db, "jobs_by_ids", lambda ids: {1: {"id": 1, "location": "Toronto, Canada"}})
    assert ac._generated_name([1, 2], "female") == "X Y"
    assert calls["args"] == ("Canada", "female")


def test_resolve_jobs_cycles_with_cursor(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2, 3], per_day=2)
    rows = [{"id": 1}, {"id": 2}, {"id": 3}]
    c = ac.list_campaigns()[0]
    got = ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows))
    assert got == [1, 2]                       # run 1: from cursor 0
    ac.note_run(1, got, "2026-09-09")
    c = ac.list_campaigns()[0]
    assert c["cursor"] == 2 and c["runs_today"] == 2
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows)) == []   # budget spent
    got2 = ac.resolve_targets(c, "2026-09-10", jobs_by_ids=_alive(rows))
    assert got2 == [3, 1]                      # run 2: wraps round-robin
    ac.note_run(1, got2, "2026-09-10")
    c = ac.list_campaigns()[0]
    assert c["cursor"] == 1                    # (2 + 2) % 3
    assert ac.resolve_targets(c, "2026-09-11", jobs_by_ids=_alive(rows)) == [2, 3]


def test_resolve_jobs_per_day_over_selection_repeats_within_day(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2, 3], per_day=5)
    rows = [{"id": 1}, {"id": 2}, {"id": 3}]
    c = ac.list_campaigns()[0]
    got = ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows))
    assert got == [1, 2, 3, 1, 2]              # a day may repeat a job when per_day > len(job_ids)
    ac.note_run(1, got, "2026-09-09")
    assert ac.list_campaigns()[0]["cursor"] == 2   # (0 + 5) % 3


def test_jobs_cursor_advances_only_by_done(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2], per_day=2)
    rows = [{"id": 1}, {"id": 2}]
    c = ac.list_campaigns()[0]
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows)) == [1, 2]
    ac.note_run(1, [1], "2026-09-09")          # job 2's fill failed → the cron reports only [1]
    c = ac.list_campaigns()[0]
    assert c["cursor"] == 1 and c["runs_today"] == 1
    # the same day's remaining budget (1) re-targets job 2, not job 1 again
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows)) == [2]


def test_jobs_cursor_walks_full_list_past_a_dead_gap(tmp_path, monkeypatch):
    # resolve and note_run must index the SAME list: with job 2 dead the rotation is 1,3,1,3 —
    # never a repeat of 1 while 3 starves (the old dead-filtered index vs full-list modulo bug).
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2, 3], per_day=2)
    rows = [{"id": 1}, {"id": 2, "dead": True}, {"id": 3}]
    c = ac.list_campaigns()[0]
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows)) == [1, 3]
    ac.note_run(1, [1, 3], "2026-09-09")
    assert ac.list_campaigns()[0]["cursor"] == 0          # after job 3 (position 2) → wraps to 0
    c = ac.list_campaigns()[0]
    assert ac.resolve_targets(c, "2026-09-10", jobs_by_ids=_alive(rows)) == [1, 3]
    ac.note_run(1, [1], "2026-09-10")                      # job 3 failed → cursor sits after job 1
    c = ac.list_campaigns()[0]
    assert c["cursor"] == 1
    assert ac.resolve_targets(c, "2026-09-10", jobs_by_ids=_alive(rows)) == [3]   # 2 is dead → 3
    # a done job that is no longer in the selection leaves the cursor alone
    ac.note_run(1, [999], "2026-09-10")
    assert ac.list_campaigns()[0]["cursor"] == 1


def test_fill_counts_as_done_only_for_a_completed_fill():
    assert ac.fill_counts_as_done({"state": "done", "submit": {"confirmed": True}})
    assert ac.fill_counts_as_done({"state": "done"})
    assert not ac.fill_counts_as_done({"state": "error", "error": "timeout"})
    assert not ac.fill_counts_as_done({"state": "running"})
    assert not ac.fill_counts_as_done({}) and not ac.fill_counts_as_done(None)


def test_jobs_skips_dead_ids_without_advancing(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2, 3], per_day=2)
    rows = [{"id": 1}, {"id": 2, "dead": True}, {"id": 3}]
    c = ac.list_campaigns()[0]
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows)) == [1, 3]   # 2 is dead
    # the selection is NOT edited — a dead posting can come back
    assert ac.list_campaigns()[0]["job_ids"] == [1, 2, 3]
    assert ac.list_campaigns()[0]["cursor"] == 0                    # resolve never moves the cursor
    # a row MISSING from the catalog is skipped the same way
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive([{"id": 1}, {"id": 2}])) == [1, 2]
    # every id dead → nothing this run (no crash)
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=lambda ids: {}) == []
    # a broken lookup treats every id as alive (the fill reports a dead page itself)
    def boom(ids):
        raise RuntimeError("db down")
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=boom) == [1, 2]
    # once 2 is alive again it is simply back in the rotation
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive([{"id": 1}, {"id": 2}, {"id": 3}])) == [1, 2]


def test_jobs_ignores_applied_exclusion(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2], per_day=2)
    rows = [{"id": 1}, {"id": 2}]
    ac.note_run(1, [1, 2], "2026-09-08")       # already applied to both on an earlier day
    c = ac.list_campaigns()[0]
    assert sorted(c["applied_jobids"]) == [1, 2]
    # search-kind exclusions (applied / globally submitted) do NOT apply — the cycle revisits them
    got = ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows), submitted={1, 2},
                             list_jobs=lambda **k: [])
    assert got == [1, 2]


def test_jobs_inactive_or_empty_selection_resolves_nothing(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2], per_day=2)
    ac.set_active(1, False)
    c = ac.list_campaigns()[0]
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive([{"id": 1}, {"id": 2}])) == []
    # a hand-edited row with no selection is skipped, not a crash (the cron just logs "nothing")
    c = dict(c, active=True, job_ids=[])
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive([])) == []
