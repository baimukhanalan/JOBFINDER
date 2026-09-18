"""mh_ondemand cron-cadence staggering. The «Расписание запуска» preset must NEVER write ONE
identical schedule to every selected mass-hiring lane — that re-created the 2026-09-12 Xvfb `:98`
overload (46 chrome procs, load ~7-10, fills dying with TargetClosedError). Each lane must get its
OWN minute + staggered hours. The crontab write is mocked here — no live crontab is touched. Run:
    PYTHONPATH=. python3 -m pytest backend/tests/test_mh_ondemand.py -q
"""
from collections import Counter

from backend.tools import mh_ondemand as mo


def test_schedule_for_is_staggered_per_lane():
    lanes = list(mo.LANES)
    for preset in ("5x", "3x", "2x", "daily"):
        specs = {l: mo.schedule_for(l, preset) for l in lanes}
        assert all(specs.values()), f"{preset}: every lane has a schedule"
        # distinct MINUTES so two lanes never start on the same minute
        minutes = [s.split()[0] for s in specs.values()]
        assert len(set(minutes)) == len(minutes), f"{preset}: minutes not distinct: {minutes}"
        # ≤2 lanes share any START hour (the anti-overload invariant)
        load = Counter()
        for s in specs.values():
            for h in s.split()[1].split(","):
                load[h] += 1
        assert max(load.values()) <= 2, f"{preset}: >2 lanes share an hour: {load}"
    # 5x mirrors the documented live Maximus phase exactly
    assert mo.schedule_for("maximus", "5x") == "0 0,5,10,15,20 * * *"
    # 'off' / unknown → no schedule (the caller comments the line out)
    assert mo.schedule_for("maximus", "off") is None
    assert mo.schedule_for("maximus", "bogus") is None
    # hourly is every hour but still staggered by distinct MINUTE
    hmins = [mo.schedule_for(l, "hourly").split()[0] for l in lanes]
    assert len(set(hmins)) == len(hmins)


def _mock_crontab(monkeypatch, tmp_path, lines):
    monkeypatch.setattr(mo, "_read_crontab", lambda: list(lines))
    monkeypatch.setattr(mo, "LOG_DIR", tmp_path)
    cap = {}

    def fake_run(argv, **kw):
        cap["argv"], cap["input"] = argv, kw.get("input")

        class R:
            returncode = 0
        return R()
    monkeypatch.setattr(mo.subprocess, "run", fake_run)
    return cap


def test_set_schedule_gives_each_lane_its_own_schedule(tmp_path, monkeypatch):
    # two lane lines that START identical (the un-staggered state) + one unrelated line.
    lines = [
        "0 1,6,11,15,20 * * * cd /x && sg mail -c 'python3 -m backend.tools.mass_hiring_apply_cron'",
        "0 1,6,11,15,20 * * * cd /x && sg mail -c 'python3 -m backend.tools.mass_hiring_apply_tp_cron'",
        "30 4 * * * echo unrelated",
    ]
    cap = _mock_crontab(monkeypatch, tmp_path, lines)
    res = mo.set_schedule(["maximus", "teleperformance"], "5x")
    assert res["ok"] and res["changed"] == 2
    written = cap["input"].splitlines()
    mx = next(l for l in written if l.endswith("mass_hiring_apply_cron'"))
    tp = next(l for l in written if l.endswith("mass_hiring_apply_tp_cron'"))
    # each got ITS OWN documented staggered schedule — NOT one shared string
    assert mx.startswith("0 0,5,10,15,20 * * * ")
    assert tp.startswith("12 1,6,11,16,21 * * * ")
    assert mx.split()[:5] != tp.split()[:5], "lanes must not share a schedule after a preset"
    # the unrelated line is preserved verbatim
    assert "30 4 * * * echo unrelated" in written
    # a backup was written before the overwrite
    assert list(tmp_path.glob("crontab.bak-*"))


def test_set_schedule_all_lanes_stay_spread(tmp_path, monkeypatch):
    # applying the default 5x preset to ALL 6 lanes (the modal's default-checked state) keeps ≤2
    # lanes starting any hour — the 'restore defaults' click can no longer un-stagger them.
    lines = [
        f"0 1,6,11,15,20 * * * cd /x && sg mail -c 'python3 -m backend.tools.{mo._modbase(l)}'"
        for l in mo.LANES
    ]
    cap = _mock_crontab(monkeypatch, tmp_path, lines)
    res = mo.set_schedule(None, "5x")          # None → every lane
    assert res["ok"] and res["changed"] == len(mo.LANES)
    written = cap["input"].splitlines()
    load = Counter()
    for l in written:
        f = l.split()
        for h in f[1].split(","):
            load[h] += 1
    assert max(load.values()) <= 2, f">2 lanes share a start hour: {load}"


def test_set_schedule_off_comments_out(tmp_path, monkeypatch):
    lines = [
        "0 0,5,10,15,20 * * * cd /x && sg mail -c 'python3 -m backend.tools.mass_hiring_apply_cron'",
    ]
    cap = _mock_crontab(monkeypatch, tmp_path, lines)
    res = mo.set_schedule(["maximus"], "off")
    assert res["ok"] and res["changed"] == 1
    assert cap["input"].splitlines()[0].startswith("# 0 0,5,10,15,20")
