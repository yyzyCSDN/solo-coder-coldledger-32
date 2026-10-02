"""SLA agreements: deadlines and on-time verdicts on one time boundary."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from .business_calendar import Calendar, CalendarTrace, add_minutes, business_minutes_between
from ..core import clock


@dataclass(frozen=True)
class Agreement:
    key: str
    calendar: Calendar
    response_minutes: int

    def deadline(self, opened_at: datetime, return_trace: bool = False):
        return add_minutes(self.calendar, opened_at, self.response_minutes,
                           return_trace=return_trace)


@dataclass(frozen=True)
class Timeliness:
    agreement_key: str
    opened_at: datetime
    due_at: datetime
    completed_at: datetime | None
    on_time: bool
    business_minutes_used: int
    business_minutes_remaining: int
    trace: CalendarTrace | None = None

    def explain(self) -> str:
        status = 'on-time' if self.on_time else 'BREACH'
        return (f'{self.agreement_key}: {status}; due {self.due_at.isoformat()} '
                f'in zone {self.trace.zone if self.trace else "?"}; '
                f'used {self.business_minutes_used} of '
                f'{self.business_minutes_used + self.business_minutes_remaining} '
                f'business minutes; boundary events: {list(self.trace.reasons()) if self.trace else []}')


def evaluate(agreement: Agreement, opened_at: datetime,
             completed_at: datetime | None = None, *, as_of: datetime | None = None,
             trace: bool = False) -> Timeliness:
    """Judge an SLA. Closed/weekend/holiday minutes never count against it."""
    due_t = agreement.deadline(opened_at, return_trace=True)
    due = due_t.result
    ref = clock.utc(completed_at) if completed_at is not None else clock.utc(as_of)
    used = business_minutes_between(agreement.calendar, opened_at, ref)
    on_time = ref <= due
    remaining = max(0, agreement.response_minutes - used)
    return Timeliness(agreement.key, clock.utc(opened_at), due,
                      ref if completed_at is not None else None,
                      on_time, used, remaining if on_time else 0,
                      due_t if trace else None)
