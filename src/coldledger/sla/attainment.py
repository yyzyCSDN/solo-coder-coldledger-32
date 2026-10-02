from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime

from ..core.clock import utc
from .business_calendar import Calendar, business_minutes_between


@dataclass(frozen=True)
class Commitment:
    """An SLA promise. ``due_at`` is a UTC absolute instant produced by the
    same business calendar used by the dispatch plan."""

    key: str
    site_key: str
    opened_at: datetime
    due_at: datetime


@dataclass(frozen=True)
class ServiceResult:
    key: str
    on_time: bool
    elapsed_business_minutes: int
    delay_business_minutes: int
    completed_at: datetime


def evaluate(cal: Calendar, commitment: Commitment, completed_at: datetime) -> ServiceResult:
    """Assess a completed promise using business minutes only.

    Delay is measured in open minutes past the deadline, so weekends,
    holidays and overnight closed periods never inflate (or shrink) the
    service-level result.
    """
    done = utc(completed_at)
    opened = utc(commitment.opened_at)
    due = utc(commitment.due_at)
    if done < opened:
        raise ValueError("completed before opened")
    elapsed = business_minutes_between(cal, opened, done)
    delay = business_minutes_between(cal, due, done) if done > due else 0
    return ServiceResult(commitment.key, done <= due, elapsed, delay, done)


def remaining_business_minutes(cal: Calendar, commitment: Commitment, now: datetime) -> int:
    at = utc(now)
    due = utc(commitment.due_at)
    if at >= due:
        return 0
    return business_minutes_between(cal, at, due)
