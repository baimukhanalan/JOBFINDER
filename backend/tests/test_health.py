"""Health tab — cron-lane status, the crontab parser, the cron→RU words helper and the bounded
parallel gather. Pure (tmp log dir, no DB/network). Run:
    PYTHONPATH=. python3 -m pytest backend/tests/test_health.py -q
"""
import os
import time

from backend.tools import health


def _lane(tmp_path, monkeypatch, text, age_h, max_h=8):
    monkeypatch.setattr(health, "_LOGS", str(tmp_path))
    monkeypatch.setattr(health, "_CRONS", [("Lane", "lane.log", max_h)])
    monkeypatch.setattr(health, "_crontab_text", lambda: "")     # hermetic: no live crontab
    p = tmp_path / "lane.log"
    p.write_text(text)
    ts = time.time() - age_h * 3600
    os.utime(p, (ts, ts))
    return health.cron_lanes()[0]


def test_fresh_success_is_ok(tmp_path, monkeypatch):
    r = _lane(tmp_path, monkeypatch, "FINISHED ok=5 errors=0\n", age_h=1)
    assert r["status"] == "ok"


def test_error_tail_is_down(tmp_path, monkeypatch):
    r = _lane(tmp_path, monkeypatch, "Traceback (most recent call last):\nKeyError: 'x'\n", age_h=1)
    assert r["status"] == "down" and "ОШИБКА" in r["detail"]


def test_recovered_run_after_old_traceback_is_ok(tmp_path, monkeypatch):
    # Yesterday's traceback still sits right under today's `collect:`/`stats:` summary — the lane
    # must read as alive (the mass-hiring collect stayed red for this after it had recovered).
    text = ("Traceback (most recent call last):\n"
            "psycopg2.InterfaceError: connection already closed\n"
            "collect: {'remotive': {'collected': 1}}\n"
            "stats: {'total': 668, 'active': 209}\n")
    r = _lane(tmp_path, monkeypatch, text, age_h=1)
    assert r["status"] == "ok"


def test_error_after_summary_is_still_down(tmp_path, monkeypatch):
    text = "stats: {'total': 1}\nTraceback (most recent call last):\nKeyError: 'x'\n"
    r = _lane(tmp_path, monkeypatch, text, age_h=1)
    assert r["status"] == "down"


def test_stale_within_2x_cadence_is_warn(tmp_path, monkeypatch):
    r = _lane(tmp_path, monkeypatch, "FINISHED ok=5 errors=0\n", age_h=10, max_h=8)
    assert r["status"] == "warn" and "STALE" in r["detail"]


def test_silent_past_2x_cadence_is_down(tmp_path, monkeypatch):
    # A hung cron writes no error line at all (the 2026-09 DB-lock outage) — silence past 2× the
    # lane's cadence must go RED so the alert fires, not sit as a benign yellow "stale".
    r = _lane(tmp_path, monkeypatch, "FINISHED ok=5 errors=0\n", age_h=17, max_h=8)
    assert r["status"] == "down" and "ЗАВИС" in r["detail"]


def test_missing_log_is_warn(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "_LOGS", str(tmp_path))
    monkeypatch.setattr(health, "_CRONS", [("Lane", "none.log", 8)])
    monkeypatch.setattr(health, "_crontab_text", lambda: "")
    assert health.cron_lanes()[0]["status"] == "warn"


# ---- cron expression → RU words + cadence --------------------------------------------------------
def test_cron_human_words():
    assert health.cron_human("*/20 * * * *") == "каждые 20 мин"
    assert health.cron_human("30 */6 * * *") == "каждые 6 ч (в :30)"
    assert health.cron_human("30 5 * * *") == "ежедневно в 05:30"
    assert health.cron_human("0 1,6,11,15,20 * * *") == "5×/день: 01:00, 06:00, 11:00, 15:00, 20:00"
    assert health.cron_human("0 7 * * 0") == "еженедельно (вс) в 07:00"
    assert health.cron_human("* * * * *") == "каждую минуту"
    # unknown shapes fall back to the raw expression, never raise
    assert health.cron_human("5 4 1 * *") == "5 4 1 * *"
    assert health.cron_human("garbage") == "garbage"


def test_cron_cadence_hours():
    assert abs(health.cron_cadence_hours("*/20 * * * *") - 1 / 3) < 1e-9
    assert health.cron_cadence_hours("0 1,6,11,15,20 * * *") == 5.0       # longest gap 20:00→01:00
    assert health.cron_cadence_hours("30 5 * * *") == 24.0
    assert health.cron_cadence_hours("0 7 * * 0") == 168.0
    assert health.cron_cadence_hours("30 */6 * * *") == 6.0
    assert health.cron_cadence_hours("5 4 1 * *") == 24.0 * 31             # dom-restricted → ~monthly
    assert health.cron_cadence_hours("nonsense") is None


# ---- crontab parser ------------------------------------------------------------------------------
_SAMPLE_CRONTAB = """
# other project — must be ignored
0 3 * * * /home/projects/toqsankz/backend/backup.sh
# a COMMENTED jobfinder line must be ignored too
#30 4 * * * cd /home/projects/jobfinder && python3 -m backend.tools.old >> /home/projects/jobfinder/logs/old.log 2>&1
30 5 * * * cd /home/projects/jobfinder && /usr/bin/python3 -m backend.tools.catalog_collector >> /home/projects/jobfinder/logs/catalog.log 2>&1
15 6 * * * cd /home/projects/jobfinder && /usr/bin/python3 -m backend.tools.catalog_collector --backfill-regions >> /home/projects/jobfinder/logs/regions.log 2>&1
0 1,6,11,15,20 * * * DISPLAY=:98 sg mail -c '/usr/bin/python3 /home/projects/jobfinder/backend/tools/mass_hiring_apply_cron.py --workers 4' >> /home/projects/jobfinder/logs/mh_apply.log 2>&1
*/20 * * * * /usr/bin/flock -n /home/projects/jobfinder/logs/harvest_amcat.lock -c 'DISPLAY=:98 sg mail -c "cd /home/projects/jobfinder && PYTHONPATH=. python3 -m backend.tools.harvest_runner --platform amcat --limit 1"' >> /home/projects/jobfinder/logs/harvest_cron.log 2>&1
*/2 * * * * cd /home/projects/jobfinder && sg mail -c '/usr/bin/python3 -m backend.tools.mail_sink --poll' >> /home/projects/jobfinder/logs/mailpoll.log 2>&1
"""


def test_parse_crontab_sample():
    entries = health.parse_crontab(_SAMPLE_CRONTAB, root="/home/projects/jobfinder")
    by_log = {e["log"]: e for e in entries}
    assert set(by_log) == {"catalog.log", "regions.log", "mh_apply.log", "harvest_cron.log", "mailpoll.log"}
    assert by_log["catalog.log"]["module"] == "catalog_collector" and by_log["catalog.log"]["flag"] == ""
    assert by_log["regions.log"]["flag"] == "--backfill-regions"
    assert by_log["mh_apply.log"]["module"] == "mass_hiring_apply_cron"      # a .py script, no -m
    assert by_log["mh_apply.log"]["human"].startswith("5×/день")
    assert by_log["harvest_cron.log"]["module"] == "harvest_runner"           # nested inside flock -c '…'
    assert by_log["harvest_cron.log"]["sched"] == "*/20 * * * *"
    assert by_log["mailpoll.log"]["flag"] == "--poll" and by_log["mailpoll.log"]["cadence_h"] < 0.1


def test_untracked_crontab_line_is_listed(tmp_path, monkeypatch):
    # A crontab line whose log isn't in _CRONS must still show up (cadence derived from its schedule).
    monkeypatch.setattr(health, "_LOGS", str(tmp_path))
    monkeypatch.setattr(health, "_CRONS", [("Lane", "lane.log", 8)])
    monkeypatch.setattr(health, "_crontab_text", lambda: _SAMPLE_CRONTAB)
    (tmp_path / "lane.log").write_text("FINISHED errors=0\n")
    p = tmp_path / "mailpoll.log"
    p.write_text("Poll: +0 new\n")
    rows = health.cron_lanes()
    names = [r["name"] for r in rows]
    assert names[0] == "Lane"
    extra = [r for r in rows if r["name"].startswith("mail_sink --poll")]
    assert extra and "не в списке" in extra[0]["name"] and extra[0]["sched"] == "каждые 2 мин"
    assert extra[0]["status"] == "ok"                                        # fresh log, short cadence
    # the tracked lane has no crontab line in this sample → flagged
    assert rows[0]["status"] == "warn" and "нет строки в crontab" in rows[0]["detail"]


def test_tracked_lane_with_crontab_line_gets_schedule(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "_LOGS", str(tmp_path))
    monkeypatch.setattr(health, "_CRONS", [("Каталог", "catalog.log", 30)])
    monkeypatch.setattr(health, "_crontab_text", lambda: _SAMPLE_CRONTAB)
    (tmp_path / "catalog.log").write_text("DONE. catalog counts -> {'total': 1}\n")
    r = health.cron_lanes()[0]
    assert r["status"] == "ok" and r["sched"] == "ежедневно в 05:30"


# ---- bounded parallel gather ---------------------------------------------------------------------
def test_gather_bounds_a_hanging_probe(monkeypatch):
    def hang():
        time.sleep(30)
        return {"name": "never", "status": "ok", "detail": ""}

    def quick():
        return [{"name": "q", "status": "down", "detail": "x"}, {"name": "i", "status": "info", "detail": ""}]

    def boom():
        raise RuntimeError("kaput")

    monkeypatch.setattr(health, "_GROUPS", [
        ("g1", "G1", [("hanging", hang), ("quick", quick)]),
        ("g2", "G2", [("boom", boom)]),
    ])
    t0 = time.monotonic()
    d = health.gather(timeout=1.0)
    assert time.monotonic() - t0 < 4.0
    rows = {r["name"]: r for s in d["sections"] for r in s["rows"]}
    assert rows["hanging"]["status"] == "warn" and "нет ответа" in rows["hanging"]["detail"]
    assert rows["boom"]["status"] == "warn" and "kaput" in rows["boom"]["detail"]
    assert d["overall"] == "down" and d["counts"] == {"ok": 0, "warn": 2, "down": 1}   # info not counted
    assert d["g1"] and d["g2"] and "sections" in d and "elapsed" in d
    assert [s["status"] for s in d["sections"]] == ["down", "warn"]


def test_plural_and_restart_growth():
    assert health._plural(1, "сбой", "сбоя", "сбоев") == "1 сбой"
    assert health._plural(3, "сбой", "сбоя", "сбоев") == "3 сбоя"
    assert health._plural(11, "сбой", "сбоя", "сбоев") == "11 сбоев"
    st = {}
    now = 1_000_000.0
    assert health._restart_growth("svc", 5, now, st) == 0
    assert health._restart_growth("svc", 6, now + 600, st) == 1
    assert health._restart_growth("svc", 8, now + 1200, st) == 3
    assert health._restart_growth("svc", 8, now + 7200, st) == 0         # old samples pruned (>1h)
