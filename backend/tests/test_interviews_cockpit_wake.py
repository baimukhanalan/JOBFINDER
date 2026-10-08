"""Cockpit Telegram-confirm + Mac auto-wake — PURE parts (no DB, no network, no ssh).

    PYTHONPATH=. python3 -m pytest backend/tests/test_interviews_cockpit_wake.py -q
"""
from datetime import datetime, timedelta, timezone

from backend.interviews import cockpit_wake as cw


def _now():
    return datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- callback data / confirm key
def test_callback_data_roundtrip():
    iv = {"id": 123}
    assert cw.callback_data("yes", iv) == "ck:y:123"
    assert cw.callback_data("no", iv) == "ck:n:123"
    assert cw.parse_callback_data("ck:y:123") == ("yes", 123)
    assert cw.parse_callback_data("ck:n:123") == ("no", 123)


def test_parse_callback_rejects_garbage():
    for bad in ("", "x", "ck:y", "ck:z:1", "ck:y:abc", "other:y:1", None, "ck:y:1:2"):
        assert cw.parse_callback_data(bad) is None


def test_confirm_key_uses_id_and_start_date():
    iv = {"id": 7, "start_ts": datetime(2026, 10, 9, 18, 30, tzinfo=timezone.utc)}
    assert cw.confirm_key(iv) == "7:2026-10-09"
    assert cw.confirm_key({"id": 7, "start_ts": None}) == "7:"       # no date → re-ask when booked


# ---------------------------------------------------------------- wake decision + timing
def test_wake_decision():
    assert cw.wake_decision(True) == "pmset"
    assert cw.wake_decision(False) == "caffeinate"


def test_pmset_wake_delta_leads_the_start_and_floors():
    now = _now()
    start = now + timedelta(minutes=40)
    # wake 6 min before start → ~34 min = 2040s
    assert cw.pmset_wake_delta_secs(now, start, lead_min=6) == 40 * 60 - 6 * 60
    # a near/past start floors at 60s so the schedule is always in the future
    assert cw.pmset_wake_delta_secs(now, now + timedelta(minutes=2), lead_min=6) == 60


def test_caffeinate_secs_covers_window_and_caps():
    now = _now()
    start = now + timedelta(minutes=40)
    secs = cw.caffeinate_secs(now, start, lead_min=6, session_min=75)
    # from (start-lead) now..through start+session ≈ 40 + 75 = 115 min
    assert secs == (40 - 6) * 60 + (6 + 75) * 60 == 115 * 60
    assert cw.caffeinate_secs(now, now - timedelta(hours=1)) >= 300           # floor
    assert cw.caffeinate_secs(now, now + timedelta(days=5)) == 4 * 3600       # cap


def test_should_ask_window():
    now = _now()
    assert cw.should_ask(now, now + timedelta(minutes=40)) is True            # inside
    assert cw.should_ask(now, now + timedelta(minutes=90)) is False           # too far
    assert cw.should_ask(now, now + timedelta(minutes=3)) is False            # too close
    assert cw.should_ask(now, None) is False                                  # no time


# ---------------------------------------------------------------- sidecar dedupe (tmp-pathed)
def test_ask_and_confirm_sidecars(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "_ASK_SENT", str(tmp_path / "ask.json"))
    monkeypatch.setattr(cw, "_CONFIRMS", str(tmp_path / "confirm.json"))
    k = "123:2026-10-09"
    assert cw.ask_already_sent(k) is False
    cw.mark_ask_sent(k)
    assert cw.ask_already_sent(k) is True                                     # survives reload
    assert cw.confirmed_yes(k) is False
    cw.record_confirm(k, "yes")
    assert cw.confirmed_yes(k) is True
    cw.record_confirm(k, "no")
    assert cw.confirmed_yes(k) is False                                       # overwrite
