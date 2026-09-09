"""Health tab cron-lane status — pure (tmp log dir, no DB/network). Run:
    PYTHONPATH=. python3 -m pytest backend/tests/test_health.py -q
"""
import os
import time

from backend.tools import health


def _lane(tmp_path, monkeypatch, text, age_h, max_h=8):
    monkeypatch.setattr(health, "_LOGS", str(tmp_path))
    monkeypatch.setattr(health, "_CRONS", [("Lane", "lane.log", max_h)])
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
    assert health.cron_lanes()[0]["status"] == "warn"
