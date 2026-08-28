"""Pure slot/grid/conflict logic for the interview scheduler (GMT, stdlib only)."""
from datetime import date, datetime, timedelta, timezone

import backend.interviews.slots as slots


def _monday():
    d = date(2026, 8, 31)
    assert d.weekday() == 0  # sanity: this fixture date really is a Monday
    return d


def test_cell_start_utc_is_utc():
    d = _monday()
    dt = slots.cell_start_utc(d, 9)
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)
    assert (dt.year, dt.month, dt.day) == (d.year, d.month, d.day)
    assert dt.hour == 9 and dt.minute == 0


def test_overlaps():
    base = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
    hour = timedelta(hours=1)

    # identical intervals overlap
    assert slots.overlaps(base, base + hour, base, base + hour)
    # partial overlap
    assert slots.overlaps(
        base, base + hour, base + timedelta(minutes=30), base + hour + timedelta(minutes=30)
    )
    # touching edges (half-open intervals) do not overlap
    assert not slots.overlaps(base, base + hour, base + hour, base + 2 * hour)
    # fully disjoint
    assert not slots.overlaps(base, base + hour, base + 2 * hour, base + 3 * hour)
    # containment
    assert slots.overlaps(base, base + 3 * hour, base + hour, base + 2 * hour)


def test_is_free_respects_window():
    d = _monday()
    avail = [{"dow": d.weekday(), "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True}]
    booked = []

    assert not slots.is_free(avail, booked, d, 8)  # cell starts before the window opens
    assert not slots.is_free(avail, booked, d, 17)  # cell would end exactly at the window close
    assert slots.is_free(avail, booked, d, 9)  # first free hour
    assert slots.is_free(avail, booked, d, 16)  # last free hour


def test_is_free_blocks_on_booking():
    d = _monday()
    avail = [{"dow": d.weekday(), "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True}]
    cell_start = slots.cell_start_utc(d, 10)
    booked = [(cell_start, cell_start + timedelta(minutes=slots.DURATION_MIN))]

    assert not slots.is_free(avail, booked, d, 10)
    assert slots.is_free(avail, booked, d, 11)  # neighboring cell unaffected


def test_free_grid_lists_only_free_responsibles():
    monday = _monday()
    avail = [{"dow": monday.weekday(), "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True}]
    cell10 = slots.cell_start_utc(monday, 10)
    booked_busy = [(cell10, cell10 + timedelta(minutes=slots.DURATION_MIN))]

    per_resp = {
        1: (avail, []),
        2: (avail, booked_busy),
    }
    grid = slots.free_grid(per_resp, monday)

    assert len(grid) == 7 * (slots.HOUR_END - slots.HOUR_START)
    assert grid[f"{monday.isoformat()}:09"] == [1, 2]
    assert grid[f"{monday.isoformat()}:10"] == [1]  # 2 is booked at this cell
    assert grid[f"{monday.isoformat()}:08"] == []  # outside both windows

    tuesday = monday + timedelta(days=1)
    assert grid[f"{tuesday.isoformat()}:09"] == []  # no availability row for this dow
