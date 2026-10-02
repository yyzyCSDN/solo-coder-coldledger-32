from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..core import clock
from ..sla.site import Site, require_zone, fit_open_window


@dataclass(frozen=True)
class CourierJob:
    key: str
    earliest: datetime
    latest: datetime
    duration: timedelta
    zone: str
    priority: int = 0
    setup_group: str = "default"

    def validate(self) -> None:
        if self.duration <= timedelta():
            raise ValueError("duration must be positive")
        if self.latest <= self.earliest:
            raise ValueError("invalid scheduling window")


@dataclass(frozen=True)
class Courier:
    key: str
    zone: str
    available_from: datetime
    available_until: datetime
    setup_minutes: int = 0


@dataclass(frozen=True)
class Assignment:
    item_key: str
    resource_key: str
    start: datetime
    end: datetime


def build(items, resources, *, site: Site | None = None):
    jobs = list(items)
    rs = list(resources)
    free = {r.key: clock.utc(r.available_from) for r in rs}
    previous_group: dict[str, str] = {}
    result: list[Assignment] = []
    for job in sorted(jobs, key=lambda j: (-j.priority, clock.utc(j.latest), clock.utc(j.earliest), j.key)):
        job.validate()
        if site is not None:
            require_zone(site, job.zone)
        choices = []
        for r in rs:
            if r.zone != job.zone:
                continue
            if site is not None:
                require_zone(site, r.zone)
            setup = timedelta(minutes=r.setup_minutes if previous_group.get(r.key) not in (None, job.setup_group) else 0)
            start = max(clock.utc(job.earliest), free[r.key] + setup, clock.utc(r.available_from))
            end = start + job.duration
            hard_end = min(clock.utc(job.latest), clock.utc(r.available_until))
            if site is not None:
                # SLA calendar and dispatch plan share the site open window.
                fitted = fit_open_window(site, start, job.duration, hard_end)
                if fitted is None:
                    continue
                start, end = fitted, fitted + job.duration
            if end <= hard_end:
                idle = (start - free[r.key]).total_seconds()
                choices.append((idle, start, r.key, end))
        if not choices:
            raise ValueError(f"cannot schedule {job.key}")
        _, start, rk, end = min(choices, key=lambda x: (x[0], x[1], x[2]))
        result.append(Assignment(job.key, rk, start, end))
        free[rk] = end
        previous_group[rk] = job.setup_group
    return result


def conflicts(assignments: Iterable[Assignment]) -> list[tuple[str, str]]:
    by_resource: dict[str, list[Assignment]] = {}
    for a in assignments:
        by_resource.setdefault(a.resource_key, []).append(a)
    out = []
    for rows in by_resource.values():
        rows.sort(key=lambda x: x.start)
        for a, b in zip(rows, rows[1:]):
            if b.start < a.end:
                out.append((a.item_key, b.item_key))
    return out


def utilization(assignments: Iterable[Assignment], resources: Iterable[Courier]) -> dict[str, float]:
    rows = list(assignments)
    result = {}
    for r in resources:
        window = (_utc(r.available_until) - _utc(r.available_from)).total_seconds()
        used = sum((a.end - a.start).total_seconds() for a in rows if a.resource_key == r.key)
        result[r.key] = 0.0 if window <= 0 else min(1.0, used / window)
    return result


def shift(assignments: Iterable[Assignment], item_key: str, delta: timedelta) -> list[Assignment]:
    out = []
    for a in assignments:
        if a.item_key == item_key:
            out.append(Assignment(a.item_key, a.resource_key, a.start + delta, a.end + delta))
        else:
            out.append(a)
    if conflicts(out):
        raise ValueError("shift creates conflict")
    return out
