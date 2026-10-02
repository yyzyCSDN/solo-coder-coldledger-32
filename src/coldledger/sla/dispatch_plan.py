from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..core.clock import utc
from ..core.intervals import Interval, merge
from .business_calendar import add_minutes
from .site_hours import Site, open_windows, opening_at_or_after


@dataclass(frozen=True)
class PlannedJob:
    """Scheduling window derived from the same business calendar the SLA uses.

    ``earliest``/``latest`` are UTC absolute instants, matching the boundary
    convention of the transport/dispatch schedulers.
    """

    key: str
    site_key: str
    earliest: datetime
    latest: datetime
    duration: timedelta

    def validate(self) -> None:
        if self.duration <= timedelta():
            raise ValueError("duration must be positive")
        if self.latest <= self.earliest:
            raise ValueError("invalid scheduling window")


@dataclass(frozen=True)
class Violation:
    item_key: str
    site_key: str
    outside: Interval          # the part of the assignment outside open hours
    reason: str


def planning_window(site: Site, key: str, ready_at: datetime, duration: timedelta,
                    sla_minutes: int, leeway_minutes: int = 0) -> PlannedJob:
    """Build the job window shared by SLA promises and dispatch schedules.

    The latest permissible completion is the SLA deadline (business minutes on
    the site calendar); ``leeway_minutes`` is business time granted on top.
    """
    if duration <= timedelta():
        raise ValueError("duration must be positive")
    # Snap to the first open instant so the earliest edge never points into
    # closed time (which would force the scheduler into an infeasible plan).
    earliest = opening_at_or_after(site, utc(ready_at))
    deadline = add_minutes(site.calendar, earliest,
                           int(sla_minutes) + int(leeway_minutes))
    job = PlannedJob(key, site.key, earliest, deadline, duration)
    if job.duration > job.latest - job.earliest:
        raise ValueError(f"{key}: SLA window shorter than job duration")
    return job


def _covered(interval: Interval, windows) -> bool:
    """True iff ``interval`` is fully covered by open windows."""
    cursor = interval.start
    for w in sorted(windows, key=lambda x: x.start):
        if w.end <= cursor:
            continue
        if w.start > cursor:
            return False
        cursor = max(cursor, w.end)
        if cursor >= interval.end:
            return True
    return cursor >= interval.end


def check_assignments(site: Site, assignments) -> list[Violation]:
    """Return assignments (duck-typed item_key/start/end) running in closed time."""
    out: list[Violation] = []
    for a in assignments:
        interval = Interval(utc(a.start), utc(a.end))
        if interval.end <= interval.start:
            raise ValueError(f"non-positive assignment interval for {a.item_key}")
        windows = merge(open_windows(site, interval.start, interval.end))
        if not _covered(interval, windows):
            cursor = interval.start
            for w in windows:
                if w.end <= cursor:
                    continue
                if w.start > cursor:
                    out.append(Violation(a.item_key, site.key,
                                         Interval(cursor, min(w.start, interval.end)),
                                         "scheduled outside site open hours"))
                cursor = max(cursor, w.end)
                if cursor >= interval.end:
                    break
            if cursor < interval.end:
                out.append(Violation(a.item_key, site.key,
                                     Interval(cursor, interval.end),
                                     "scheduled outside site open hours"))
    return out


def assert_within_hours(site: Site, assignments) -> None:
    violations = check_assignments(site, assignments)
    if violations:
        v = violations[0]
        raise ValueError(
            f"{v.item_key} {v.reason} ({v.outside.start.isoformat()}..{v.outside.end.isoformat()})"
        )
