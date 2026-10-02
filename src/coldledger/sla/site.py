"""Site open windows share the calendar zone, by construction.

A Site binds a business calendar to a concrete facility so dispatch and
pickup planning cannot drift onto a different timezone boundary than the
SLA calendar that judges the resulting service level.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from .business_calendar import Calendar, is_open, _open_close
from ..core import clock


@dataclass(frozen=True)
class Site:
    key: str
    calendar: Calendar

    @property
    def zone(self) -> str:
        return self.calendar.zone

    def is_open(self, instant: datetime) -> bool:
        return is_open(self.calendar, instant)

    def open_window(self, day):
        """(open_instant, close_instant) for a local date, DST-safe."""
        op, cl, _, _ = _open_close(self.calendar, day)
        return op, cl


def require_zone(site: Site, zone: str) -> None:
    """Guard used by schedulers: a job/resource zone must equal the site zone."""
    if zone != site.zone:
        raise ValueError(f'zone {zone!r} does not match site {site.key!r} zone {site.zone!r}')


def next_open(site: Site, instant: datetime):
    """First open instant at or after ``instant`` (same clock boundary)."""
    cal = site.calendar
    cur = clock.utc(instant)
    z = clock.zone(cal.zone)
    while True:
        day = cur.astimezone(z).date()
        if cal.is_business_day(day):
            op, cl, _, _ = _open_close(cal, day)
            if cur < op:
                return op
            if cur < cl:
                return cur
        cur, _ = clock.at_local(cal.next_business_day(day), cal.open_time, cal.zone)


def fit_open_window(site: Site, start: datetime, duration: timedelta,
                    hard_end: datetime):
    """Earliest open instant >= ``start`` where ``duration`` fits before close.

    Used by dispatch/pickup/dock schedulers so planned work never lands in
    time the SLA calendar treats as closed. Returns None when no business-day
    window can hold the work before ``hard_end``.
    """
    cal = site.calendar
    cur = clock.utc(start)
    hard_end = clock.utc(hard_end)
    if duration <= timedelta():
        raise ValueError('duration must be positive')
    z = clock.zone(cal.zone)
    while True:
        day = cur.astimezone(z).date()
        if not cal.is_business_day(day):
            cur, _ = clock.at_local(cal.next_business_day(day), cal.open_time, cal.zone)
            continue
        op, cl, _, _ = _open_close(cal, day)
        if cur < op:
            cur = op
        if cur >= cl:
            cur, _ = clock.at_local(cal.next_business_day(day + timedelta(days=1)),
                                    cal.open_time, cal.zone)
            continue
        if cur + duration <= min(cl, hard_end):
            return cur
        # does not fit today; try the next business day
        nxt, _ = clock.at_local(cal.next_business_day(day + timedelta(days=1)),
                                cal.open_time, cal.zone)
        if nxt >= hard_end:
            return None
        cur = nxt

