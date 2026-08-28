"""Pure, stdlib-only slot/grid/conflict logic for the interview scheduler.

No DB, no network, no other project imports — every time here is GMT/UTC.
Weekday convention is Python's `date.weekday()` (0=Mon..6=Sun).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")

HOUR_START = 8
HOUR_END = 20
DURATION_MIN = 60


def week_dates(monday: date) -> list[date]:
    """The 7 dates of the week starting at `monday` (Mon..Sun)."""
    return [monday + timedelta(days=i) for i in range(7)]


def cell_start_utc(d: date, hour: int) -> datetime:
    """The tz-aware UTC datetime for the grid cell on date `d` at `hour`:00 GMT."""
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=UTC)


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """True when half-open interval [a_start, a_end) overlaps [b_start, b_end)."""
    return a_start < b_end and b_start < a_end


def is_free(
    avail_rows: list[dict],
    booked: list[tuple[datetime, datetime]],
    d: date,
    hour: int,
) -> bool:
    """True when the responsible's weekday row covers this hour's cell AND no
    booked interval overlaps [cell_start_utc, cell_start_utc + DURATION_MIN).
    """
    dow = d.weekday()
    cell_start_min = hour * 60
    cell_end_min = cell_start_min + DURATION_MIN

    row = next((r for r in avail_rows if r.get("dow") == dow), None)
    if row is None or not row.get("enabled"):
        return False
    if not (row["start_min"] <= cell_start_min and cell_end_min <= row["end_min"]):
        return False

    cell_start = cell_start_utc(d, hour)
    cell_end = cell_start + timedelta(minutes=DURATION_MIN)
    for b_start, b_end in booked:
        if overlaps(cell_start, cell_end, b_start, b_end):
            return False
    return True


def free_grid(
    per_resp: dict[int, tuple[list[dict], list[tuple[datetime, datetime]]]],
    monday: date,
) -> dict[str, list[int]]:
    """{"YYYY-MM-DD:HH": [sorted free responsible ids]} for every cell of the
    7-day x HOUR_START..HOUR_END-1 grid starting at `monday`.
    """
    grid: dict[str, list[int]] = {}
    for d in week_dates(monday):
        for hour in range(HOUR_START, HOUR_END):
            key = f"{d.isoformat()}:{hour:02d}"
            grid[key] = sorted(
                resp_id
                for resp_id, (avail_rows, booked) in per_resp.items()
                if is_free(avail_rows, booked, d, hour)
            )
    return grid
