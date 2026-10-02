from __future__ import annotations
from dataclasses import dataclass

from ..core.clock import utc
from .business_calendar import (Calendar, day_bounds, is_business_day,
                                open_windows as _calendar_windows)
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Site:
    """Physical site whose open hours are defined by one business calendar.

    A site pins the zone used both for SLA business-time arithmetic and for
    dispatch scheduling windows, so the two can never disagree about where a
    local day starts and ends.
    """

    key: str
    calendar: Calendar


def open_windows(site: Site, start: datetime, end: datetime):
    return _calendar_windows(site.calendar, start, end)


def is_open_at(site: Site, instant: datetime) -> bool:
    at = utc(instant)
    return any(w.start <= at < w.end for w in open_windows(site, at, at + timedelta(minutes=1)))


def opening_at_or_after(site: Site, instant: datetime) -> datetime:
    """First UTC instant the site is open at/after ``instant`` (instant itself
    counts if the site is already open)."""
    at = utc(instant)
    if is_open_at(site, at):
        return at
    cal = site.calendar
    z = ZoneInfo(cal.zone)
    day = at.astimezone(z).date()
    for _ in range(4000):
        if is_business_day(cal, day):
            opening, _ = day_bounds(cal, day)
            if opening >= at:
                return opening
        day += timedelta(days=1)
    raise ValueError("no reachable opening; check holidays/working_weekdays")
