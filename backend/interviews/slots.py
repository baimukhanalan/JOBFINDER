"""Pure, stdlib-only slot/grid/conflict logic for the interview scheduler.

**Timezone model (per-person).** Each responsible carries their OWN IANA timezone
(`iv_responsibles.tz`, auto-detected from their browser). Their weekly availability
is wall-clock in THAT timezone. The operator's «Собес» grid is drawn in the OPERATOR's
own timezone. Everything absolute (booking `start_ts`, `booked` intervals) is tz-aware
UTC. Conversions here are done by MATERIALISING each responsible's weekly availability
into absolute UTC intervals for the relevant dates — robust to any offset (incl. :30/:45
zones), to DST where it exists, and to overnight / 24h windows.

No DB, no network, no other project imports. `date.weekday()` is 0=Mon..6=Sun.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = ZoneInfo("UTC")
DEFAULT_TZ = "Asia/Almaty"  # the team's home zone; the fallback for a missing/bad tz

HOUR_START = 0
HOUR_END = 24
DURATION_MIN = 60


def zone(name: str | None) -> ZoneInfo:
    """A ZoneInfo for an IANA name, falling back to DEFAULT_TZ on anything invalid
    (a stale/garbage tz must never crash scheduling)."""
    try:
        return ZoneInfo(name) if name else ZoneInfo(DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def tz_label(name: str | None) -> str:
    """A short human label for a tz — the city part ('Asia/Almaty' -> 'Almaty')."""
    return (name or DEFAULT_TZ).split("/")[-1].replace("_", " ")


def week_dates(monday: date) -> list[date]:
    """The 7 dates of the week starting at `monday` (Mon..Sun)."""
    return [monday + timedelta(days=i) for i in range(7)]


def cell_start_utc(tz: str | None, d: date, hour: int) -> datetime:
    """The UTC instant for a grid cell at LOCAL date `d`, `hour`:00 in timezone `tz`."""
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=zone(tz)).astimezone(UTC)


def to_local(dt: datetime, tz: str | None = DEFAULT_TZ) -> datetime:
    """A UTC/aware instant as wall-clock in timezone `tz`."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(zone(tz))


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """True when half-open interval [a_start, a_end) overlaps [b_start, b_end)."""
    return a_start < b_end and b_start < a_end


def availability_utc_intervals(
    avail_rows: list[dict],
    resp_tz: str | None,
    day_from: date,
    day_to: date,
) -> list[tuple[datetime, datetime]]:
    """Materialise a responsible's weekly availability into absolute UTC intervals for
    each LOCAL (resp_tz) date in [day_from, day_to]. Window kinds per weekday row:
      * start == end -> a full 24h local day;
      * start <  end -> a same-day window [start, end);
      * start >  end -> an overnight window, continuous from local `start` to local
                        `end` the NEXT day.
    A weekday may carry SEVERAL windows (e.g. 06:30–14:00 AND 18:00–01:00) — EVERY enabled
    row for that weekday produces its own interval. Only `enabled` rows produce intervals.
    Pass a ±1 day pad around the range you care about so an overnight window that starts the
    day before is included.
    """
    tz = zone(resp_tz)
    rows_by_dow: dict[int, list[dict]] = {}
    for r in avail_rows:
        if r.get("enabled", True):
            rows_by_dow.setdefault(int(r["dow"]), []).append(r)
    out: list[tuple[datetime, datetime]] = []
    d = day_from
    while d <= day_to:
        base = datetime(d.year, d.month, d.day, 0, 0, tzinfo=tz)  # local midnight
        for row in rows_by_dow.get(d.weekday(), ()):
            s, e = int(row["start_min"]), int(row["end_min"])
            if s == e:
                st, en = base, base + timedelta(days=1)
            elif s < e:
                st, en = base + timedelta(minutes=s), base + timedelta(minutes=e)
            else:
                st, en = base + timedelta(minutes=s), base + timedelta(days=1, minutes=e)
            out.append((st.astimezone(UTC), en.astimezone(UTC)))
        d += timedelta(days=1)
    return out


def is_free(
    intervals_utc: list[tuple[datetime, datetime]],
    booked: list[tuple[datetime, datetime]],
    utc_start: datetime,
    dur: int = DURATION_MIN,
) -> bool:
    """True when [utc_start, utc_start+dur) fits ENTIRELY inside some availability
    interval AND overlaps no booked interval. All times tz-aware UTC."""
    cell_end = utc_start + timedelta(minutes=dur)
    if not any(s <= utc_start and cell_end <= e for s, e in intervals_utc):
        return False
    for b_start, b_end in booked:
        if overlaps(utc_start, cell_end, b_start, b_end):
            return False
    return True


def is_free_at(
    avail_rows: list[dict],
    resp_tz: str | None,
    booked: list[tuple[datetime, datetime]],
    utc_start: datetime,
    dur: int = DURATION_MIN,
) -> bool:
    """Convenience single-slot check for one responsible: materialise the ±1 day of
    availability around `utc_start` (in resp-local dates) and test membership."""
    local_day = to_local(utc_start, resp_tz).date()
    intervals = availability_utc_intervals(
        avail_rows, resp_tz, local_day - timedelta(days=1), local_day + timedelta(days=1))
    return is_free(intervals, booked, utc_start, dur)


def free_grid(
    per_resp: dict[int, tuple[list[dict], str, list[tuple[datetime, datetime]]]],
    monday: date,
    viewer_tz: str | None,
) -> dict[str, list[dict]]:
    """{"YYYY-MM-DD:HH": [{id, tz, local}...]} over the 7-day x HOUR_START..HOUR_END-1
    grid whose axis is `viewer_tz` local dates/hours starting at `monday`. `per_resp`
    maps id -> (avail_rows, resp_tz, booked_utc). Each free entry carries the
    responsible's OWN local "HH:MM" for that slot (so the operator sees the member's
    real time). Caller decorates with names."""
    # materialise each responsible's availability once, padded to cover the viewer week
    span_start = cell_start_utc(viewer_tz, monday, 0)
    span_end = span_start + timedelta(days=7)
    intervals: dict[int, list[tuple[datetime, datetime]]] = {}
    for rid, (avail, rtz, _booked) in per_resp.items():
        lo = to_local(span_start, rtz).date() - timedelta(days=1)
        hi = to_local(span_end, rtz).date() + timedelta(days=1)
        intervals[rid] = availability_utc_intervals(avail, rtz, lo, hi)

    grid: dict[str, list[dict]] = {}
    for d in week_dates(monday):
        for hour in range(HOUR_START, HOUR_END):
            utc = cell_start_utc(viewer_tz, d, hour)
            free = []
            for rid, (_avail, rtz, booked) in per_resp.items():
                if is_free(intervals[rid], booked, utc):
                    free.append({"id": rid, "tz": rtz,
                                 "local": to_local(utc, rtz).strftime("%H:%M")})
            free.sort(key=lambda x: x["id"])
            grid[f"{d.isoformat()}:{hour:02d}"] = free
    return grid
