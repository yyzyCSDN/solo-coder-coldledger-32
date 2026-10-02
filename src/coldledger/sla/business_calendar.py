"""Business calendar and SLA time math.

Single time-boundary contract shared with site windows and dispatch plans:
- every instant entering the module is converted through core.clock (UTC);
- local calendar dates, weekdays and open/close walls come from the same
  zoneinfo boundary;
- DST gaps and ambiguous wall times are resolved through explicit policies
  instead of silent fold=0 guesses, and every boundary jump is recorded on
  the returned trace so frozen vs. recomputed results stay explainable.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from ..core import clock

# weekday(): Monday=0 ... Sunday=6
WEEKEND = frozenset({5, 6})


@dataclass(frozen=True)
class Calendar:
    zone: str
    holidays: frozenset
    open_time: time
    close_time: time
    weekend: frozenset = WEEKEND

    def __post_init__(self):
        if not self.zone:
            raise ValueError('zone required')
        try:
            ZoneInfo(self.zone)  # fail fast on an unknown zone
        except Exception as exc:
            raise ValueError(f'unknown zone {self.zone!r}') from exc
        if not isinstance(self.holidays, frozenset):
            object.__setattr__(self, 'holidays', frozenset(self.holidays))
        for d in self.holidays:
            if not isinstance(d, date):
                raise ValueError('holidays must contain dates')
        if not (self.open_time.tzinfo is None and self.close_time.tzinfo is None):
            raise ValueError('open/close must be naive wall times')
        if self.close_time <= self.open_time:
            raise ValueError('overnight windows are not supported; split into same-day segments')
        if not self.weekend <= frozenset(range(7)):
            raise ValueError('invalid weekday set')

    def is_business_day(self, day: date) -> bool:
        return day.weekday() not in self.weekend and day not in self.holidays

    def next_business_day(self, day: date) -> date:
        cur = day
        while not self.is_business_day(cur):
            cur += timedelta(days=1)
        return cur

    def why_closed(self, day: date) -> str | None:
        if day in self.holidays:
            return 'holiday'
        if day.weekday() in self.weekend:
            return 'weekend'
        return None


@dataclass(frozen=True)
class CalendarTrace:
    """Explainable record of how an SLA instant was derived."""
    zone: str
    start: datetime
    result: datetime
    business_minutes: int
    events: tuple = field(default_factory=tuple)

    def reasons(self):
        return tuple(e['reason'] for e in self.events)


def _event(reason, day=None, **extra):
    e = {'reason': reason}
    if day is not None:
        e['date'] = day.isoformat()
    e.update(extra)
    return e


def _open_close(cal: Calendar, day: date):
    """Open (after a spring gap) and close (last instant before a spring gap)."""
    op, op_kind = clock.at_local(day, cal.open_time, cal.zone,
                                 gap_policy=clock.SHIFT_FORWARD,
                                 fold_policy=clock.FOLD_EARLY)
    cl, cl_kind = clock.at_local(day, cal.close_time, cal.zone,
                                 gap_policy=clock.SHIFT_BACKWARD,
                                 fold_policy=clock.FOLD_LATE)
    return op, cl, op_kind, cl_kind


def _skip_events(cal: Calendar, from_day: date, to_day: date):
    """One event per closed day crossed (weekend/holiday), in order."""
    events = []
    d = from_day
    while d < to_day:
        reason = cal.why_closed(d)
        if reason is not None:
            events.append(_event('skip_' + reason, d, next_open=to_day.isoformat()))
        d += timedelta(days=1)
    return events


def add_minutes(cal: Calendar, start: datetime, minutes: int, return_trace: bool = False):
    """Advance ``minutes`` *business* minutes from ``start``; closed time never counts.

    Business minutes are real elapsed minutes while the site is open:
    weekends/holidays and daily closed intervals are skipped; a spring-forward
    gap contributes no minutes (those minutes never existed); a fall-back
    repeated hour contributes its real minutes. Returns a UTC datetime, or a
    CalendarTrace when ``return_trace`` is True.
    """
    if minutes < 0:
        raise ValueError('negative duration')
    if not isinstance(minutes, int):
        raise ValueError('whole minutes required')
    events = []
    requested = minutes
    cur = clock.utc(start)
    z = ZoneInfo(cal.zone)

    while True:
        day = cur.astimezone(z).date()
        closed = cal.why_closed(day)
        if closed is not None:
            nxt = cal.next_business_day(day)
            events.append(_event('skip_' + closed, day, next_open=nxt.isoformat()))
            events.extend(_skip_events(cal, day + timedelta(days=1), nxt))
            cur, _ = clock.at_local(nxt, cal.open_time, cal.zone)
            continue
        op, cl, op_kind, cl_kind = _open_close(cal, day)
        if cur < op:
            events.append(_event('skip_closed', day, detail='before open'))
            cur = op
            continue
        if cur >= cl:
            nxt = cal.next_business_day(day + timedelta(days=1))
            events.append(_event('after_close', day, next_open=nxt.isoformat()))
            events.extend(_skip_events(cal, day + timedelta(days=1), nxt))
            cur, _ = clock.at_local(nxt, cal.open_time, cal.zone)
            continue
        if op_kind == 'gap':
            events.append(_event('dst_gap', day, boundary='open'))
        if cl_kind == 'gap':
            events.append(_event('dst_gap', day, boundary='close'))
        take = min(minutes, _segment_wall_minutes(cur, cl, z))
        before = cur
        cur = cur + timedelta(minutes=take)
        minutes -= take
        # detect a spring-forward gap crossed inside this step (wall jumps
        # forward, unlike a fall-back where the wall repeats)
        trans = _transition_after(before, z, cur)
        if trans is not None:
            wall = trans.astimezone(z).replace(tzinfo=None)
            prev = (trans - timedelta(minutes=1)).astimezone(z).replace(tzinfo=None)
            if wall > prev + timedelta(minutes=1):
                events.append(_event('dst_gap', day))
            else:
                events.append(_event('dst_fold', day))
        if minutes == 0:
            break
        nxt = cal.next_business_day(day + timedelta(days=1))
        events.append(_event('day_exhausted', day, next_open=nxt.isoformat()))
        events.extend(_skip_events(cal, day + timedelta(days=1), nxt))
        cur, _ = clock.at_local(nxt, cal.open_time, cal.zone)

    trace = CalendarTrace(cal.zone, clock.utc(start), cur, requested, tuple(events))
    return trace if return_trace else cur


def _wall_minutes(a, b, z):
    """Elapsed minutes between two instants on the same offset side."""
    if a.utcoffset() != b.utcoffset():
        raise ValueError('transition inside segment')
    return int((b - a).total_seconds() // 60)


def _transition_after(instant, z, limit):
    """First instant > ``instant`` where the local wall does not tick +1 minute.

    Catches spring-forward (wall jumps over the gap) and fall-back (the wall
    repeats 01:00). ``limit`` bounds the per-minute scan (at most one day).
    """
    cur = instant + timedelta(minutes=1)
    expected = instant.astimezone(z).replace(tzinfo=None) + timedelta(minutes=1)
    while cur <= limit:
        wall = cur.astimezone(z).replace(tzinfo=None)
        if wall != expected:
            return cur
        expected = wall + timedelta(minutes=1)
        cur += timedelta(minutes=1)
    return None


def _segment_wall_minutes(a, b, z):
    """Real elapsed open minutes in [a,b); valid inside one business window.

    Splitting at the transition and summing same-offset pieces makes a
    spring-forward gap count zero while a fall-back repeat counts once.
    """
    total = 0
    piece_start, stop = a, b
    trans = _transition_after(piece_start, z, stop)
    while trans is not None:
        total += _wall_minutes(piece_start, trans, z)
        piece_start = trans
        trans = _transition_after(piece_start, z, stop)
    return total + _wall_minutes(piece_start, stop, z)


def business_minutes_between(cal: Calendar, start: datetime, end: datetime,
                             return_trace: bool = False):
    """Count open-for-business minutes inside [start, end). Closed minutes = 0.

    Mirrors add_minutes using the same boundaries, so a deadline and the
    minutes consumed to reach it can never disagree.
    """
    s, e = clock.utc(start), clock.utc(end)
    if e < s:
        raise ValueError('end before start')
    z = ZoneInfo(cal.zone)
    cur, total, events = s, 0, []
    while cur < e:
        day = cur.astimezone(z).date()
        closed = cal.why_closed(day)
        if closed is not None:
            nxt = cal.next_business_day(day)
            events.append(_event('skip_' + closed, day, next_open=nxt.isoformat()))
            events.extend(_skip_events(cal, day + timedelta(days=1), nxt))
            cur, _ = clock.at_local(nxt, cal.open_time, cal.zone)
            continue
        op, cl, op_kind, cl_kind = _open_close(cal, day)
        if cur < op:
            cur = op
            continue
        if cur >= cl:
            nxt = cal.next_business_day(day + timedelta(days=1))
            events.extend(_skip_events(cal, day + timedelta(days=1), nxt))
            cur, _ = clock.at_local(nxt, cal.open_time, cal.zone)
            continue
        if op_kind == 'gap':
            events.append(_event('dst_gap', day, boundary='open'))
        if cl_kind == 'gap':
            events.append(_event('dst_gap', day, boundary='close'))
        seg_end = min(cl, e)
        total += _segment_wall_minutes(cur, seg_end, z)
        cur = seg_end if seg_end == e else cl
    if return_trace:
        return total, tuple(events)
    return total


def is_open(cal: Calendar, instant: datetime) -> bool:
    v = clock.utc(instant)
    day = v.astimezone(ZoneInfo(cal.zone)).date()
    if not cal.is_business_day(day):
        return False
    op, cl, _, _ = _open_close(cal, day)
    return op <= v < cl
