from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..core.clock import utc
from .business_calendar import (Calendar, add_minutes, business_minutes_between,
                                day_bounds, is_business_day, spans_dst_transition)
from .attainment import Commitment

# Reason codes are part of the public, auditable contract.
FREEZE_HISTORICAL = "freeze:promise-already-completed"
FREEZE_PAST_DEADLINE = "freeze:deadline-already-passed"
FREEZE_IMMATERIAL = "freeze:calendar-change-does-not-affect-deadline"
FREEZE_MORE_TIME = "freeze:change-adds-business-time"
REPLAN_HOLIDAY = "replan:holiday-update-removes-business-time"
REPLAN_WORKWEEK = "replan:workweek-change-removes-business-time"
REPLAN_HOURS = "replan:open-hours-shortened"
REPLAN_DST = "replan:dst-transition-window-shifts"
REPLAN_ZONE = "replan:timezone-boundary-changed"


@dataclass(frozen=True)
class CalendarRevision:
    """A calendar update announced at ``announced_at`` (e.g. a newly published
    holiday or a zone database correction)."""

    before: Calendar
    after: Calendar
    announced_at: datetime


@dataclass(frozen=True)
class ReplanDecision:
    commitment_key: str
    action: str                 # "freeze" | "replan"
    reason: str
    old_due_at: datetime
    new_due_at: datetime | None
    sla_minutes: int
    affected_days: tuple
    explainable: bool = True


def _open_seconds(cal: Calendar, day) -> float:
    opening, closing = day_bounds(cal, day)
    return (closing - opening).total_seconds()


def affected_days(rev: CalendarRevision, end_at: datetime,
                  start_at: datetime | None = None) -> list:
    """Local dates (in the old calendar's zone) whose UTC open windows differ
    between the two revisions, between start_at and end_at."""
    lo = utc(start_at) if start_at is not None else utc(rev.announced_at)
    hi = utc(end_at)
    if hi < lo:
        return []
    z = ZoneInfo(rev.before.zone)
    day = lo.astimezone(z).date()
    last_day = hi.astimezone(z).date()
    out = []
    while day <= last_day:
        before_open = is_business_day(rev.before, day)
        after_open = is_business_day(rev.after, day)
        if before_open != after_open:
            out.append(day)
        elif before_open and after_open and day_bounds(rev.before, day) != day_bounds(rev.after, day):
            out.append(day)
        day += timedelta(days=1)
    return out


def _classify(rev: CalendarRevision, day) -> str:
    """Why a single changed day affects a promise whose window contains it.

    Tightening/shifting reasons force a replan; ``FREEZE_MORE_TIME`` marks a
    day that only gained availability (the existing plan stays feasible).
    """
    before_open = is_business_day(rev.before, day)
    after_open = is_business_day(rev.after, day)
    if before_open and not after_open:
        if day in rev.after.holidays and day not in rev.before.holidays:
            return REPLAN_HOLIDAY
        return REPLAN_WORKWEEK
    if not before_open and after_open:
        return FREEZE_MORE_TIME
    if rev.before.zone != rev.after.zone:
        return REPLAN_ZONE
    if (spans_dst_transition(rev.before, day) or spans_dst_transition(rev.after, day)):
        # Both open, but the UTC open boundary moves across the offset change,
        # so an existing UTC-fixed assignment may now sit outside open hours.
        return REPLAN_DST
    if _open_seconds(rev.after, day) < _open_seconds(rev.before, day):
        return REPLAN_HOURS
    return FREEZE_MORE_TIME


def _sla_business_minutes(cal: Calendar, commitment: Commitment) -> int:
    # Recover the promised budget; the stored deadline was produced by
    # add_minutes on the same calendar.
    return business_minutes_between(cal, commitment.opened_at, commitment.due_at)


def decide(rev: CalendarRevision, commitment: Commitment,
           completed_at: datetime | None = None) -> ReplanDecision:
    """Choose, with a machine-readable reason, whether the promised result is
    frozen on the old calendar or the dispatch plan is rebuilt on the new one.

    Deterministic rule order (first match wins):
      1. promise already completed     -> freeze (historical fact)
      2. deadline passed at announcement -> freeze (too late to replan)
      3. changed day in the promised window tightens or shifts open time
         (new holiday/non-working day, shortened hours, DST shift, zone
         change)                                     -> replan with reason
      4. changed day only adds availability          -> freeze (favourable)
      5. change does not touch the promised window   -> freeze (immaterial)
    """
    announced = utc(rev.announced_at)
    old_due = utc(commitment.due_at)
    opened = utc(commitment.opened_at)
    key = commitment.key
    sla_minutes = _sla_business_minutes(rev.before, commitment)

    if completed_at is not None:
        return ReplanDecision(key, "freeze", FREEZE_HISTORICAL, old_due, None,
                              sla_minutes, ())
    if old_due <= announced:
        return ReplanDecision(key, "freeze", FREEZE_PAST_DEADLINE, old_due, None,
                              sla_minutes, ())

    new_due = add_minutes(rev.after, opened, sla_minutes)
    # Only changed days whose open window overlaps the promised window
    # (through the earlier of the two deadlines) can invalidate a frozen
    # result or an existing dispatch plan.
    window_end = min(old_due, new_due)
    scan_end = max(old_due, new_due) + timedelta(days=1)
    days = []
    for d in affected_days(rev, scan_end, start_at=opened):
        b0, b1 = day_bounds(rev.before, d)
        a0, a1 = day_bounds(rev.after, d)
        lo, hi = min(b0, a0), max(b1, a1)
        if hi > opened and lo < window_end:
            days.append(d)
    days = tuple(days)

    if not days:
        return ReplanDecision(key, "freeze", FREEZE_IMMATERIAL,
                              old_due, old_due, sla_minutes, ())
    # First tightener (chronologically) gives the reason; pure additions are
    # harmless because every previously open instant remains schedulable.
    for d in days:
        reason = _classify(rev, d)
        if reason != FREEZE_MORE_TIME:
            return ReplanDecision(key, "replan", reason, old_due, new_due,
                                  sla_minutes, days)
    return ReplanDecision(key, "freeze", FREEZE_MORE_TIME, old_due, new_due,
                          sla_minutes, days)


def apply_decision(decision: ReplanDecision, commitment: Commitment) -> Commitment:
    """Return the commitment to dispatch against: unchanged on freeze, new
    deadline (same promised budget) on replan."""
    if decision.action == "freeze" or decision.new_due_at is None:
        return commitment
    return Commitment(commitment.key, commitment.site_key,
                      commitment.opened_at, decision.new_due_at)


def record_decision(trail, decision: ReplanDecision, actor: str = "calendar-service"):
    """Append a hash-chained audit entry (audit.trail.AuditTrail-compatible)."""
    subject = f"commitment:{decision.commitment_key}"
    note = f"{decision.action}:{decision.reason}"
    return trail.append(actor, note, subject)
