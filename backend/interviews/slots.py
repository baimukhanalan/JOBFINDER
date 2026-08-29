"""Pure, stdlib-only slot/grid/conflict logic for the interview scheduler.

No DB, no network, no other project imports. **Wall-clock times (availability
windows, the grid axis, the `date`+`hour` passed to `is_free`) are LOCAL time —
Almaty (UTC+5, single Kazakhstan zone) — because the whole team is there and thinks
in local time.** Absolute instants (booking `start_ts`, the `booked` intervals) stay
tz-aware UTC. `cell_start_utc` is the one bridge: it takes a LOCAL date+hour and
returns the UTC instant. Weekday convention is Python's `date.weekday()` (0=Mon..6=Sun),
applied to the LOCAL date.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")
LOCAL_TZ = ZoneInfo("Asia/Almaty")  # UTC+5, no DST — the team's single timezone

HOUR_START = 0
HOUR_END = 24
DURATION_MIN = 60


def to_local(dt: datetime) -> datetime:
    """A UTC/aware instant as LOCAL (Almaty) wall-clock time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(LOCAL_TZ)


def week_dates(monday: date) -> list[date]:
    """The 7 dates of the week starting at `monday` (Mon..Sun)."""
    return [monday + timedelta(days=i) for i in range(7)]


def cell_start_utc(d: date, hour: int) -> datetime:
    """The tz-aware UTC instant for the grid cell on LOCAL (Almaty) date `d` at
    `hour`:00 local time. This is where local wall-clock crosses into UTC."""
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=LOCAL_TZ).astimezone(UTC)


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """True when half-open interval [a_start, a_end) overlaps [b_start, b_end)."""
    return a_start < b_end and b_start < a_end


def _covers_same_day(start_min: int, end_min: int, a: int, b: int) -> bool:
    """Does a weekday window's OWN-DAY part cover the cell [a, b) (minutes-in-day)?
    Three window kinds are supported so every day/night/all-day window works:
      * start == end  -> a full 24h window (covers the whole day, no wrap);
      * start <  end  -> an ordinary same-day window [start, end);
      * start >  end  -> an overnight window; its own-day part is [start, 24:00)
                         (the [0, end) tail belongs to the NEXT day — see _covers_wrap).
    """
    if start_min == end_min:
        return True
    if start_min < end_min:
        return start_min <= a and b <= end_min
    return start_min <= a  # overnight forward part; b <= 1440 always for a day cell


def _covers_wrap(prev_start_min: int, prev_end_min: int, a: int, b: int) -> bool:
    """Does the PREVIOUS weekday's overnight window wrap into this day's early cell
    [a, b)? Only an overnight window (start > end) wraps, covering [0, end) here."""
    if prev_start_min <= prev_end_min:  # normal or 24h -> no spill into the next day
        return False
    return b <= prev_end_min  # a >= 0 always; cell must fit inside [0, prev_end)


def is_free(
    avail_rows: list[dict],
    booked: list[tuple[datetime, datetime]],
    d: date,
    hour: int,
) -> bool:
    """True when the responsible's availability covers this hour's cell AND no
    booked interval overlaps it. Availability may be an overnight window (end <=
    start), so a cell can be covered by THIS weekday's window or by the PREVIOUS
    weekday's overnight window wrapping past midnight.
    """
    dow = d.weekday()
    a = hour * 60
    b = a + DURATION_MIN

    def _row(x):
        r = next((r for r in avail_rows if r.get("dow") == x), None)
        return r if (r and r.get("enabled")) else None

    today = _row(dow)
    covered = bool(today and _covers_same_day(today["start_min"], today["end_min"], a, b))
    if not covered:
        prev = _row((dow - 1) % 7)
        covered = bool(prev and _covers_wrap(prev["start_min"], prev["end_min"], a, b))
    if not covered:
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
