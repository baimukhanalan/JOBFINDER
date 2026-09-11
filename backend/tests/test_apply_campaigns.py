"""apply_campaigns store + pacing + target resolution — pure, no DB/network. Run:
    PYTHONPATH=. python3 -m pytest backend/tests/test_apply_campaigns.py -q
"""
from backend.tools import apply_campaigns as ac


def _use_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "_PATH", tmp_path / "apply_campaigns.json")
    monkeypatch.setattr(ac, "_EVENTS_PATH", tmp_path / "apply_campaign_events.json")
    # captcha-solver switch OFF by default (a leaked env must not flip the captcha-ATS filter)
    monkeypatch.delenv("CAMPAIGN_SOLVE_CAPTCHA", raising=False)
    # keep create() DB-free + order-preserving: the cursor/resolve tests assert exact job_ids
    # order and don't care about company interleave (unit-tested separately). Tests that DO
    # exercise the interleave re-set this after calling _use_tmp.
    monkeypatch.setattr(ac, "interleave_by_company", lambda ids, jobs_by_ids=None: list(ids))


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
    """A jobs_by_ids stub: {id: row} from a list of rows (rows may carry dead=True or an explicit
    `ats`). A row WITHOUT `ats` defaults to a submittable one (greenhouse) so the cursor-mechanics
    tests aren't filtered out by the captcha-walled-ATS skip; the ATS-filter test sets `ats` itself."""
    keep = lambda ids: set(int(i) for i in ids)

    def stub(ids):
        out = {}
        for r in rows:
            if int(r["id"]) in keep(ids):
                row = dict(r)
                row.setdefault("ats", "greenhouse")
                out[int(r["id"])] = row
        return out
    return stub


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
    # the cursor round-robins the FULL selection across runs. Here nothing LANDS (attempted, 0
    # confirmed), so no job drops out of the rotation and it is a clean cycle; a landing then drops.
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2, 3], per_day=2)
    rows = [{"id": 1}, {"id": 2}, {"id": 3}]
    c = ac.list_campaigns()[0]
    got = ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows))
    assert got == [1, 2]                       # run 1: from cursor 0
    ac.note_run(1, [], "2026-09-09", attempted=got)   # attempted; nothing landed → budget intact
    c = ac.list_campaigns()[0]
    assert c["cursor"] == 2 and c["runs_today"] == 0
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows)) == [3, 1]   # wraps round-robin
    # a LANDED job (confirmed submit) drops out of the rotation from now on
    ac.note_run(1, [3], "2026-09-10", attempted=[3])
    c = ac.list_campaigns()[0]
    assert c["confirmed_jobids"] == [3]
    out = ac.resolve_targets(c, "2026-09-10", jobs_by_ids=_alive(rows))
    assert 3 not in out and set(out) <= {1, 2}   # only the un-landed jobs keep rotating


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
    # resolve and note_run must index the SAME list: with job 2 dead the rotation walks 1,3,1,3 —
    # never a repeat of 1 while 3 starves (the old dead-filtered index vs full-list modulo bug).
    # Nothing LANDS here (attempted, 0 confirmed) so no job drops out of the rotation.
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2, 3], per_day=1)
    rows = [{"id": 1}, {"id": 2, "dead": True}, {"id": 3}]
    c = ac.list_campaigns()[0]
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows)) == [1]   # cursor 0 → job 1
    ac.note_run(1, [], "2026-09-09", attempted=[1])       # attempted 1 (not landed) → cursor after 1
    assert ac.list_campaigns()[0]["cursor"] == 1
    c = ac.list_campaigns()[0]
    # next run skips the dead 2 and lands on 3 (not a repeat of 1)
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows)) == [3]
    ac.note_run(1, [], "2026-09-09", attempted=[3])       # cursor after job 3 (position 2) → wraps to 0
    assert ac.list_campaigns()[0]["cursor"] == 0
    c = ac.list_campaigns()[0]
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows)) == [1]   # back round to 1
    # an attempted job that is no longer in the selection leaves the cursor alone
    ac.note_run(1, [], "2026-09-09", attempted=[999])
    assert ac.list_campaigns()[0]["cursor"] == 0


def test_fill_counts_as_done_only_for_a_real_submit():
    assert ac.fill_counts_as_done({"state": "done", "submit": {"confirmed": True}})
    assert ac.fill_counts_as_done({"state": "done"})                       # no submit phase at all
    # pressed but never confirmed (the emailed code never came) or rejected by the ATS -> not counted
    assert not ac.fill_counts_as_done({"state": "done", "submit": {"clicked": True, "confirmed": False}})
    assert not ac.fill_counts_as_done({"state": "done", "submit": {"clicked": True, "confirmed": True,
                                                                     "blocked": "flagged as possible spam"}})
    # filled but never submitted, a dead posting, an error, still running -> not an application
    assert not ac.fill_counts_as_done({"state": "done", "submit": {"clicked": False, "reason": "incomplete"}})
    assert not ac.fill_counts_as_done({"state": "done", "submit": {"clicked": False, "reason": "no_form"}})
    assert not ac.fill_counts_as_done({"state": "error", "error": "timeout"})
    assert not ac.fill_counts_as_done({"state": "running"})
    assert not ac.fill_counts_as_done({}) and not ac.fill_counts_as_done(None)
    assert ac.fill_is_dead_posting({"state": "done", "submit": {"reason": "no_form"}})
    assert not ac.fill_is_dead_posting({"state": "done", "submit": {"reason": "incomplete"}})


def test_note_run_cursor_moves_past_the_last_attempted_not_only_done(tmp_path, monkeypatch):
    # the first live run: job A dead (no_form), B and C attempted; only C submitted -> budget 1,
    # cursor after C (a dead A doesn't pin the rotation; it is marked dead by the cron anyway)
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2, 3, 4], per_day=3)
    ac.note_run(1, [3], "2026-09-09", attempted=[1, 2, 3])
    c = ac.list_campaigns()[0]
    assert c["runs_today"] == 1 and c["applied_jobids"] == [3] and c["cursor"] == 3
    assert c["confirmed_jobids"] == [3]        # job 3 landed → excluded, but it's already behind the cursor
    got = ac.resolve_targets(c, "2026-09-09", jobs_by_ids=lambda ids: {i: {"id": i, "ats": "ashby"} for i in ids})
    assert got == [4, 1]


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


def test_jobs_ignores_search_exclusions_but_honors_confirmed(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2], per_day=2)
    rows = [{"id": 1}, {"id": 2}]
    c = ac.list_campaigns()[0]
    # the search-kind exclusions (a GLOBAL `submitted` set, `list_jobs`) do NOT apply to a jobs pick
    # — a job the owner chose is attempted even if some OTHER campaign submitted it globally.
    got = ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows), submitted={1, 2},
                             list_jobs=lambda **k: [])
    assert got == [1, 2]
    # but THIS campaign's own LANDED jobs (confirmed_jobids) ARE excluded from the rotation
    ac.note_run(1, [1], "2026-09-09", attempted=[1, 2])   # job 1 landed for this campaign
    c = ac.list_campaigns()[0]
    assert c["confirmed_jobids"] == [1]
    got2 = ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows), submitted={1, 2},
                              list_jobs=lambda **k: [])
    assert 1 not in got2 and got2 == [2]


def test_jobs_inactive_or_empty_selection_resolves_nothing(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2], per_day=2)
    ac.set_active(1, False)
    c = ac.list_campaigns()[0]
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive([{"id": 1}, {"id": 2}])) == []
    # a hand-edited row with no selection is skipped, not a crash (the cron just logs "nothing")
    c = dict(c, active=True, job_ids=[])
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive([])) == []


# ---- (1) confirmed-exclusion · (2) attempt cap · (3) captcha-ATS filter (for `jobs` kind) --------

def test_confirmed_job_is_not_reserved(tmp_path, monkeypatch):
    # (1) a job the campaign already LANDED (a confirmed submit note_run's first arg carries) is
    # never re-served, while the un-landed selected jobs keep rotating.
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2, 3], per_day=2)
    rows = [{"id": 1}, {"id": 2}, {"id": 3}]
    c = ac.list_campaigns()[0]
    ac.note_run(1, [2], "2026-09-09", attempted=[1, 2])   # job 2 landed; 1 attempted-not-landed
    c = ac.list_campaigns()[0]
    assert c["confirmed_jobids"] == [2]
    out = ac.resolve_targets(c, "2026-09-10", jobs_by_ids=_alive(rows))
    assert 2 not in out and set(out) <= {1, 3}            # 2 confirmed → skipped; 1 & 3 still rotate
    # once ALL selected jobs are landed, nothing resolves even though the daily budget is available
    saved = ac._load(); saved[0]["confirmed_jobids"] = [1, 2, 3]; ac._save(saved)
    c = ac.list_campaigns()[0]
    assert ac.remaining_today(c, "2026-09-12") == 2       # budget IS free on a fresh day
    assert ac.resolve_targets(c, "2026-09-12", jobs_by_ids=_alive(rows)) == []   # but all landed


def test_attempt_cap_stops_a_zero_confirm_day(tmp_path, monkeypatch):
    # (2) per_day is a CONFIRMED cap; a 0-yield day would otherwise fire unlimited fills. The
    # per-day ATTEMPT ceiling (per_day × CAMPAIGN_MAX_ATTEMPTS_FACTOR) stops it.
    _use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(ac, "CAMPAIGN_MAX_ATTEMPTS_FACTOR", 2)     # cap = per_day × 2
    _jobs_campaign([1, 2, 3, 4, 5, 6], per_day=3)                  # attempt ceiling = 3 × 2 = 6
    rows = [{"id": i} for i in range(1, 7)]
    c = ac.list_campaigns()[0]
    assert ac.remaining_today(c, "2026-09-09") == 3               # min(3 - 0, 6 - 0)
    ac.note_run(1, [], "2026-09-09", attempted=[1, 2, 3])         # 3 attempts, 0 confirmed
    c = ac.list_campaigns()[0]
    assert c["attempts_today"] == 3 and c["runs_today"] == 0
    assert ac.remaining_today(c, "2026-09-09") == 3               # min(3, 6 - 3) — attempts now bind
    ac.note_run(1, [], "2026-09-09", attempted=[4, 5, 6])         # 6 attempts total, still 0 confirmed
    c = ac.list_campaigns()[0]
    assert c["attempts_today"] == 6
    assert ac.remaining_today(c, "2026-09-09") == 0              # min(3, 6 - 6) = 0 — the cap stops the day
    assert ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows)) == []
    assert ac.remaining_today(c, "2026-09-10") == 3             # a new day clears the attempt ledger


def test_resolve_jobs_skips_captcha_ats_unless_solver(tmp_path, monkeypatch):
    # (3) by default only the auto-submittable ATSes (greenhouse/ashby) are targeted; the
    # captcha-walled lever/workable are attempted only with CAMPAIGN_SOLVE_CAPTCHA=1.
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2, 3], per_day=3)
    rows = [{"id": 1, "ats": "greenhouse"}, {"id": 2, "ats": "lever"}, {"id": 3, "ats": "ashby"}]
    c = ac.list_campaigns()[0]
    got = ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows))
    assert 2 not in got and set(got) == {1, 3}               # lever (captcha-walled) skipped by default
    # flip the switch on (a solver + clean egress live) → every selected ATS is attempted
    monkeypatch.setenv("CAMPAIGN_SOLVE_CAPTCHA", "1")
    got2 = ac.resolve_targets(c, "2026-09-09", jobs_by_ids=_alive(rows))
    assert got2 == [1, 2, 3]
    # the helper mirrors the env (also honours the module-level default when unset)
    monkeypatch.delenv("CAMPAIGN_SOLVE_CAPTCHA", raising=False)
    assert ac._solve_captcha_on() is False
    monkeypatch.setenv("CAMPAIGN_SOLVE_CAPTCHA", "yes")
    assert ac._solve_captcha_on() is True


def test_next_identity_fresh_mailbox_per_application(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    _jobs_campaign([1, 2], per_day=2, name="Dana Erlan")
    c = ac.list_campaigns()[0]
    assert c["email_mode"] == "per_apply" and c["seq"] == 0 and 120 <= c["seq_base"] <= 9000
    taken = {f"dana.erlan{c['seq_base'] + 1}@takhet.com"}       # pretend the CRM already has it
    e1, p1 = ac.next_identity(1, exists=lambda e: e in taken)
    e2, p2 = ac.next_identity(1, exists=lambda e: e in taken)
    assert e1.startswith("dana.erlan") and e1.endswith("@takhet.com") and e1 not in taken
    assert e2 != e1 and p2 != p1 and p1.startswith("demo_camp1_danaerlan_")
    assert ac.list_campaigns()[0]["seq"] == 3                   # 1 skipped (taken) + 2 issued
    # a campaign pinned to one mailbox keeps it
    ac.list_campaigns()  # noqa
    rows = ac._load(); rows[0]["email_mode"] = "fixed"; ac._save(rows)
    assert ac.next_identity(1, exists=lambda e: False) == (c["email"], c["pid"])


# ---- per-application JOURNAL (events store + outcome derivation + log backfill) ------------------
def test_outcome_from_fill_state():
    assert ac.outcome_from_fill_state({"state": "done", "submit": {"confirmed": True}})[0] == "confirmed"
    assert ac.outcome_from_fill_state({"state": "done", "submit": {"reason": "no_form"}})[0] == "dead"
    assert ac.outcome_from_fill_state(
        {"state": "done", "submit": {"clicked": True, "blocked": "couldn't submit — flagged as possible spam"}})[0] == "spam"
    assert ac.outcome_from_fill_state(
        {"state": "done", "submit": {"clicked": True, "blocked": "needs correction"}})[0] == "needs_correction"
    assert ac.outcome_from_fill_state({"state": "done", "submit": {"clicked": True}})[0] == "clicked"
    assert ac.outcome_from_fill_state({"state": "error", "error": "boom"})[0] == "error"


def test_log_event_append_cap_and_summary(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(ac, "_EVENTS_CAP", 5)
    for i in range(8):
        ac.log_event(1, 1000 + i, company="Acme", title="T", mailbox="a@b.com",
                     outcome="confirmed" if i % 2 == 0 else "spam", ts=f"2026-09-10 07:0{i}:00")
    evs = ac.list_events(1)
    assert len(evs) == 5                       # capped at 5, newest first
    assert evs[0]["job_id"] == 1007 and evs[-1]["job_id"] == 1003
    s = ac.event_summary(1)
    assert s["total"] == 5 and s["confirmed"] + s["spam"] == 5


def test_list_events_filters_by_cid(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    ac.log_event(1, 10, outcome="confirmed", ts="2026-09-10 01:00:00")
    ac.log_event(2, 20, outcome="dead", ts="2026-09-10 02:00:00")
    assert [e["job_id"] for e in ac.list_events(1)] == [10]
    assert [e["job_id"] for e in ac.list_events(2)] == [20]
    assert len(ac.list_events()) == 2          # cid=None → all


_SAMPLE_LOG = """\
2026-09-10 07:08:02,233 campaign 1 job 20437 as Dana Erlan <dana.erlan628@takhet.com>
2026-09-10 07:09:46,869 campaign 1 job 20437 -> done submit=clicked CONFIRMED
2026-09-10 07:09:46,992 campaign 1 job 20442 as Dana Erlan <dana.erlan629@takhet.com>
2026-09-10 07:11:24,756 campaign 1 job 20442 -> done submit=no_form
2026-09-10 01:10:03,097 campaign 1 job 20039 -> done submit=clicked blocked=couldn't submit your
2026-09-10 13:09:41,487 campaign 1 job 20451 -> done submit=clicked blocked=needs correction
"""


def test_backfill_events_from_log_and_dedup(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    jbi = lambda ids: {20437: {"company": "Supabase", "title": "Eng"}}
    added = ac.backfill_events_from_log(text=_SAMPLE_LOG, jobs_by_ids=jbi)
    assert added == 4
    outs = {(e["job_id"]): e["outcome"] for e in ac.list_events(1)}
    assert outs == {20437: "confirmed", 20442: "dead", 20039: "spam", 20451: "needs_correction"}
    ev = next(e for e in ac.list_events(1) if e["job_id"] == 20437)
    assert ev["company"] == "Supabase" and ev["mailbox"] == "dana.erlan628@takhet.com"
    # idempotent: a second run adds nothing (dedup on ts+cid+job)
    assert ac.backfill_events_from_log(text=_SAMPLE_LOG, jobs_by_ids=jbi) == 0
    assert len(ac.list_events(1)) == 4


# ---- parallel lane: interleave-by-company + create wiring -----------------------------------------
def test_interleave_by_company_round_robins_and_keeps_all():
    jbi = lambda ids: {1: {"company": "A"}, 2: {"company": "A"}, 3: {"company": "B"},
                       4: {"company": "C"}, 5: {"company": "B"}, 6: {"company": "A"}}
    got = ac.interleave_by_company([1, 2, 3, 4, 5, 6], jobs_by_ids=jbi)
    assert got == [1, 3, 4, 2, 5, 6]                       # A,B,C · A,B · A
    assert sorted(got) == [1, 2, 3, 4, 5, 6]               # keeps every id exactly once
    assert ac.interleave_by_company([], jobs_by_ids=jbi) == []
    # a DB miss degrades to the original order (all ids in one '?' bucket)
    assert ac.interleave_by_company([3, 1, 2], jobs_by_ids=lambda ids: {}) == [3, 1, 2]
    # stable: same input → same output
    assert ac.interleave_by_company([1, 2, 3, 4, 5, 6], jobs_by_ids=jbi) == got


def test_create_jobs_interleaves_by_company(tmp_path, monkeypatch):
    _use_tmp(tmp_path, monkeypatch)
    calls = {}

    def spy(job_ids, jobs_by_ids=None):
        calls["ids"] = list(job_ids)
        return [job_ids[-1]] + list(job_ids[:-1])          # a visible reordering
    monkeypatch.setattr(ac, "interleave_by_company", spy)
    c = _jobs_campaign([10, 11, 12], per_day=3)
    assert calls["ids"] == [10, 11, 12]                    # create ran it on the parsed selection
    assert c["job_ids"] == [12, 10, 11] and c["cursor"] == 0
