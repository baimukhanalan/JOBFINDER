"""Pure slot/interval/conflict logic for the interview scheduler (per-timezone)."""
from datetime import date, datetime, timedelta, timezone

import backend.interviews.slots as slots

UTC = timezone.utc


def _monday():
    d = date(2026, 8, 31)
    assert d.weekday() == 0  # sanity: this fixture date really is a Monday
    return d


def _utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def test_zone_and_label_fallback():
    assert slots.zone("Asia/Almaty").key == "Asia/Almaty"
    assert slots.zone("Not/AZone").key == slots.DEFAULT_TZ  # bad name -> default
    assert slots.zone("").key == slots.DEFAULT_TZ
    assert slots.tz_label("America/New_York") == "New York"
    assert slots.tz_label("Asia/Almaty") == "Almaty"


def test_cell_start_utc_uses_the_given_zone():
    d = _monday()
    assert slots.cell_start_utc("Asia/Almaty", d, 9) == _utc(2026, 8, 31, 4)   # 09:00 +5
    assert slots.cell_start_utc("America/New_York", d, 9) == _utc(2026, 8, 31, 13)  # 09:00 EDT (-4)


def test_intervals_same_day_window():
    av = [{"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True}]
    iv = slots.availability_utc_intervals(av, "Asia/Almaty", _monday(), _monday())
    assert iv == [(_utc(2026, 8, 31, 4), _utc(2026, 8, 31, 12))]  # 09:00-17:00 Almaty


def test_intervals_overnight_is_continuous_across_midnight():
    av = [{"dow": 0, "start_min": 21 * 60, "end_min": 2 * 60, "enabled": True}]  # 21:00->02:00
    iv = slots.availability_utc_intervals(av, "Asia/Almaty", _monday(), _monday())
    # 21:00 Almaty Mon = 16:00 UTC Mon; 02:00 Almaty Tue = 21:00 UTC Mon
    assert iv == [(_utc(2026, 8, 31, 16), _utc(2026, 8, 31, 21))]


def test_intervals_24h_spans_local_day():
    av = [{"dow": 0, "start_min": 0, "end_min": 0, "enabled": True}]
    iv = slots.availability_utc_intervals(av, "Asia/Almaty", _monday(), _monday())
    assert iv == [(_utc(2026, 8, 30, 19), _utc(2026, 8, 31, 19))]  # Almaty Mon 00:00..Tue 00:00


def test_intervals_skip_disabled():
    av = [{"dow": 0, "start_min": 0, "end_min": 0, "enabled": False}]
    assert slots.availability_utc_intervals(av, "Asia/Almaty", _monday(), _monday()) == []


def test_is_free_membership_and_booking():
    iv = [(_utc(2026, 8, 31, 4), _utc(2026, 8, 31, 12))]
    assert slots.is_free(iv, [], _utc(2026, 8, 31, 4))       # first hour
    assert slots.is_free(iv, [], _utc(2026, 8, 31, 11))      # last hour fits [11,12)
    assert not slots.is_free(iv, [], _utc(2026, 8, 31, 12))  # would end past the window
    assert not slots.is_free(iv, [], _utc(2026, 8, 31, 3))   # before the window
    booked = [(_utc(2026, 8, 31, 5), _utc(2026, 8, 31, 6))]
    assert not slots.is_free(iv, booked, _utc(2026, 8, 31, 5))
    assert slots.is_free(iv, booked, _utc(2026, 8, 31, 6))


def test_is_free_at_respects_responsible_timezone():
    av = [{"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True}]
    # Almaty responsible: free at 09:00 Almaty == 04:00 UTC, not at 08:00 Almaty
    assert slots.is_free_at(av, "Asia/Almaty", [], _utc(2026, 8, 31, 4))
    assert not slots.is_free_at(av, "Asia/Almaty", [], _utc(2026, 8, 31, 3))
    # New York responsible with the SAME wall-clock window is free at a different instant
    assert slots.is_free_at(av, "America/New_York", [], _utc(2026, 8, 31, 13))  # 09:00 EDT
    assert not slots.is_free_at(av, "America/New_York", [], _utc(2026, 8, 31, 4))


def test_free_grid_shows_each_members_local_time():
    monday = _monday()
    almaty = [{"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True}]
    ny = [{"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True}]
    per_resp = {
        1: (almaty, "Asia/Almaty", []),
        2: (ny, "America/New_York", []),
    }
    # viewer (operator) is in Almaty
    grid = slots.free_grid(per_resp, monday, "Asia/Almaty")
    assert len(grid) == 7 * (slots.HOUR_END - slots.HOUR_START)

    # Almaty 10:00 cell: the Almaty member (id 1) is free, shown at their own 10:00
    c10 = grid[f"{monday.isoformat()}:10"]
    assert {"id": 1, "tz": "Asia/Almaty", "local": "10:00"} in c10
    # the NY member is NOT free then (Almaty 10:00 == 05:00 UTC == NY 01:00)
    assert not any(x["id"] == 2 for x in c10)

    # Almaty 18:00 cell == 13:00 UTC == NY 09:00: the NY member IS free, shown as 09:00
    c18 = grid[f"{monday.isoformat()}:18"]
    ny_entry = next(x for x in c18 if x["id"] == 2)
    assert ny_entry["local"] == "09:00" and ny_entry["tz"] == "America/New_York"


def test_free_grid_respects_booking():
    monday = _monday()
    av = [{"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True}]
    busy = [(slots.cell_start_utc("Asia/Almaty", monday, 10),
             slots.cell_start_utc("Asia/Almaty", monday, 10) + timedelta(minutes=60))]
    per_resp = {1: (av, "Asia/Almaty", []), 2: (av, "Asia/Almaty", busy)}
    grid = slots.free_grid(per_resp, monday, "Asia/Almaty")
    ids10 = [x["id"] for x in grid[f"{monday.isoformat()}:10"]]
    assert ids10 == [1]  # 2 is booked at 10:00
    assert [x["id"] for x in grid[f"{monday.isoformat()}:09"]] == [1, 2]
