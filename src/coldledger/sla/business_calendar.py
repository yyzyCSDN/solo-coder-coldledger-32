from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.clock import utc
from ..core.intervals import Interval

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)
WEEKDAYS: frozenset[int] = frozenset({MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY})
ALL_DAYS: frozenset[int] = frozenset(range(7))

# Safety bound so an all-holidays calendar can never hang an advance loop.
_MAX_DAY_SCAN = 4000


@dataclass(frozen=True)
class Calendar:
    """Business calendar.

    All wall-clock fields (holidays, open_time/close_time) are interpreted in
    ``zone`` local time; every computation returns/accepts timezone-aware
    datetimes and crosses module boundaries as UTC absolute instants, which is
    the boundary shared with site hours and dispatch plans.
    """

    zone: str
    holidays: frozenset[date]
    open_time: time
    close_time: time
    working_weekdays: frozenset[int] = WEEKDAYS
    version: int = 0

    def __post_init__(self) -> None:
        if self.open_time.tzinfo is not None or self.close_time.tzinfo is not None:
            raise ValueError("open_time/close_time must be naive wall times")
        if self.open_time >= self.close_time:
            raise ValueError("overnight windows are not supported; open_time must be < close_time")
        if not self.working_weekdays <= ALL_DAYS:
            raise ValueError("working_weekdays must be weekday numbers 0..6")
        try:
            ZoneInfo(self.zone)
        except ZoneInfoNotFoundError:
            raise ValueError(f"unknown time zone: {self.zone}")


def is_business_day(cal: Calendar, day: date) -> bool:
    return day.weekday() in cal.working_weekdays and day not in cal.holidays


def day_bounds(cal: Calendar, day: date) -> tuple[datetime, datetime]:
    """Open/close instants (UTC) of one local calendar day.

    ``datetime.combine`` with a zone resolves spring-forward gaps and
    fall-back overlaps deterministically (PEP 495, fold=0); the returned pair
    are absolute instants, so a DST day naturally carries a shorter/longer
    open window.
    """
    z = ZoneInfo(cal.zone)
    opening = datetime.combine(day, cal.open_time, z)
    closing = datetime.combine(day, cal.close_time, z)
    return utc(opening), utc(closing)


def spans_dst_transition(cal: Calendar, day: date) -> bool:
    """True when the open window of ``day`` crosses a DST offset change."""
    z = ZoneInfo(cal.zone)
    opening = datetime.combine(day, cal.open_time, z)
    closing = datetime.combine(day, cal.close_time, z)
    return opening.utcoffset() != closing.utcoffset()


def _iter_local_windows(cal: Calendar, start: datetime, end: datetime):
    """Yield (local_day, opening_utc, closing_utc) for every business day whose
    open window intersects [start, end].

    Enumeration starts one local day before the start's local date because a
    day's UTC window may begin on the previous UTC date (e.g. a 00:30 local
    opening in a +01 zone starts at 23:30Z) and must not be clipped away.
    """
    s, e = utc(start), utc(end)
    if e < s:
        raise ValueError("end before start")
    z = ZoneInfo(cal.zone)
    day = s.astimezone(z).date() - timedelta(days=1)
    last_day = e.astimezone(z).date()
    while day <= last_day:
        if is_business_day(cal, day):
            opening, closing = day_bounds(cal, day)
            if closing > s and opening < e:
                yield day, opening, closing
        day += timedelta(days=1)


def open_windows(cal: Calendar, start: datetime, end: datetime) -> list[Interval]:
    """Open business intervals (UTC) clipped to [start, end].

    Full DST-aware daily windows are intersected with the range; windows
    crossing the UTC midnight boundary are neither split nor dropped.
    """
    s, e = utc(start), utc(end)
    out: list[Interval] = []
    for _, opening, closing in _iter_local_windows(cal, s, e):
        w = Interval(max(s, opening), min(e, closing))
        if w.end > w.start:
            out.append(w)
    return out


def business_minutes_between(cal: Calendar, start: datetime, end: datetime) -> int:
    """Whole open minutes between two instants; closed minutes never count."""
    s, e = utc(start), utc(end)
    seconds = 0.0
    for _, opening, closing in _iter_local_windows(cal, s, e):
        lo, hi = max(s, opening), min(e, closing)
        seconds += (hi - lo).total_seconds()
    return int(seconds // 60)


def add_minutes(cal: Calendar, start: datetime, minutes: int) -> datetime:
    """Advance ``minutes`` of open business time from ``start``.

    Weekends, holidays and overnight closed periods are skipped. Windows are
    consumed in chronological order as absolute instants, so an open window
    crossing the UTC midnight boundary or a DST transition is never cut in
    half. The result is a UTC absolute instant, the boundary shared with site
    hours and dispatch schedules.
    """
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        raise ValueError("minutes must be an integer number of minutes")
    if minutes < 0:
        raise ValueError("negative duration")
    cur = utc(start)
    budget = timedelta(minutes=minutes)
    z = ZoneInfo(cal.zone)
    # Begin one local day before cur: its UTC window may start earlier on the
    # previous UTC date and still contain cur.
    day = cur.astimezone(z).date() - timedelta(days=1)
    scanned = 0
    while budget > timedelta():
        if scanned > _MAX_DAY_SCAN:
            raise ValueError("no reachable business day; check holidays/working_weekdays")
        if is_business_day(cal, day):
            opening, closing = day_bounds(cal, day)
            if closing <= cur:
                pass  # window already (partly) behind us; cur sits past it
            else:
                if cur < opening:
                    cur = opening
                if cur < closing:
                    take = min(budget, closing - cur)
                    cur += take
                    budget -= take
        day += timedelta(days=1)
        scanned += 1
    return cur.astimezone(timezone.utc)
