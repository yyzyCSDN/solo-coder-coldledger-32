"""Freeze-vs-recompute decisions when calendars change.

When a holiday is added, opening hours change, DST rules are updated in the
tz database or an assignment crosses midnight, previously produced SLA
deadlines and dispatch plans must either be kept (frozen) or recomputed.
The policy is explicit and every decision carries a human-readable reason.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from .business_calendar import Calendar, add_minutes


class Policy(str, Enum):
    FREEZE = 'freeze'        # keep the historical result once work started
    RECOMPUTE = 'recompute'  # always derive from the current calendar
    ADAPTIVE = 'adaptive'    # freeze completed/started, recompute open work


# Why a calendar revision changed the boundary.
def calendar_changes(old: Calendar, new: Calendar) -> tuple[str, ...]:
    changes = []
    if old.zone != new.zone:
        changes.append('zone')
    if old.open_time != new.open_time or old.close_time != new.close_time:
        changes.append('open_hours')
    if old.holidays != new.holidays:
        added = sorted(old.holidays ^ new.holidays, key=lambda d: d.isoformat())
        changes.append('holidays:' + ','.join(d.isoformat() for d in added))
    if old.weekend != new.weekend:
        changes.append('weekend')
    return tuple(changes)


@dataclass(frozen=True)
class JobState:
    """What happened to the piece of work the result belongs to."""
    key: str
    opened_at: datetime
    started: bool = False       # courier/dock work has begun
    completed: bool = False    # result is historical
    assigned: bool = False      # a dispatch plan exists but work not started


@dataclass(frozen=True)
class ReplanDecision:
    job_key: str
    action: str                 # 'freeze' | 'recompute'
    old_due: datetime
    new_due: datetime
    reasons: tuple = field(default_factory=tuple)
    detail: str = ''

    @property
    def changed(self) -> bool:
        return self.new_due != self.old_due

    def explain(self) -> str:
        verb = 'FREEZE old result' if self.action == 'freeze' else 'RECOMPUTE schedule'
        why = '; '.join(self.reasons) if self.reasons else 'no calendar change'
        shift = ''
        if self.changed:
            delta = self.new_due - self.old_due
            shift = f'; deadline moves {delta}'
        return f'{self.job_key}: {verb} ({why}); due {self.old_due.isoformat()} -> {self.new_due.isoformat()}{shift}; {self.detail}'


def decide(policy: Policy, state: JobState, old_calendar: Calendar,
           new_calendar: Calendar, response_minutes: int) -> ReplanDecision:
    old_due = add_minutes(old_calendar, state.opened_at, response_minutes)
    new_due = add_minutes(new_calendar, state.opened_at, response_minutes)
    changes = calendar_changes(old_calendar, new_calendar)

    if policy is Policy.FREEZE:
        return ReplanDecision(state.key, 'freeze', old_due, new_due, changes,
                              'policy=freeze: historical results are authoritative')
    if policy is Policy.RECOMPUTE:
        return ReplanDecision(state.key, 'recompute', old_due, new_due, changes,
                              'policy=recompute: current calendar is authoritative')

    # ADAPTIVE
    if state.completed:
        return ReplanDecision(state.key, 'freeze', old_due, new_due, changes,
                              'adaptive: work completed; historical result frozen')
    if state.started:
        return ReplanDecision(state.key, 'freeze', old_due, new_due, changes,
                              'adaptive: work in progress; replanning would break custody')
    if new_due == old_due:
        return ReplanDecision(state.key, 'freeze', old_due, new_due, changes,
                              'adaptive: revision does not move the deadline')
    if state.assigned:
        return ReplanDecision(state.key, 'recompute', old_due, new_due, changes,
                              'adaptive: assigned but not started; plan is rebuilt before dispatch')
    return ReplanDecision(state.key, 'recompute', old_due, new_due, changes,
                          'adaptive: work not yet assigned; fresh plan on the new calendar')
