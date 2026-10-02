import unittest
from datetime import datetime, timezone, timedelta, date, time

from coldledger.core import clock
from coldledger.sla import (
    Calendar, Site, Agreement, evaluate, add_minutes,
    business_minutes_between, is_open,
)
from coldledger.sla.reevaluation import Policy, JobState, decide, calendar_changes
from coldledger.dispatch.courier_schedule import CourierJob, Courier, build as courier_build
from coldledger.transport.pickup_schedule import PickupJob, Vehicle, build as pickup_build
from coldledger.warehouse.dock_schedule import DockJob, Dock, build as dock_build

UTC = timezone.utc
BERLIN = 'Europe/Berlin'
NY = 'America/New_York'


class BusinessCalendarTests(unittest.TestCase):
    def setUp(self):
        # Mon-Fri 09:00-18:00 Berlin, Monday 2026-10-12 is a holiday
        self.cal = Calendar(BERLIN, frozenset({date(2026, 10, 12)}), time(9), time(18))
        self.site = Site('BER-HUB', self.cal)
        self.fri = datetime(2026, 10, 9, 15, tzinfo=UTC)  # Friday 17:00 CEST

    def test_weekend_minutes_never_count(self):
        due = add_minutes(self.cal, self.fri, 60)
        # only 60 open minutes left on Friday -> 18:00 sharp
        self.assertEqual(due.astimezone(clock.zone(BERLIN)).replace(tzinfo=None),
                         datetime(2026, 10, 9, 18, 0))
        used = business_minutes_between(
            self.cal, self.fri, datetime(2026, 10, 11, 22, tzinfo=UTC))
        self.assertEqual(used, 60)  # Saturday/Sunday contribute zero
        self.assertFalse(is_open(self.cal, datetime(2026, 10, 10, 12, tzinfo=UTC)))

    def test_sla_skips_weekend_and_holiday_with_trace(self):
        tr = add_minutes(self.cal, self.fri, 120, return_trace=True)
        local = tr.result.astimezone(clock.zone(BERLIN)).replace(tzinfo=None)
        # 60 minutes Friday + 60 minutes Tuesday (Monday holiday) = Tue 10:00
        self.assertEqual(local, datetime(2026, 10, 13, 10, 0))
        self.assertEqual(tr.reasons(),
                         ('day_exhausted', 'skip_weekend', 'skip_weekend', 'skip_holiday'))
        self.assertEqual(tr.business_minutes, 120)

    def test_round_trip_add_then_measure_agrees(self):
        due = add_minutes(self.cal, self.fri, 200)
        self.assertEqual(business_minutes_between(self.cal, self.fri, due), 200)

    def test_holiday_update_moves_deadline(self):
        new_cal = Calendar(BERLIN, frozenset({date(2026, 10, 12), date(2026, 10, 13)}),
                           time(9), time(18))
        old_due = add_minutes(self.cal, self.fri, 120)
        new_due = add_minutes(new_cal, self.fri, 120)
        self.assertEqual(new_due - old_due, timedelta(days=1))
        self.assertTrue(any('holidays' in c for c in calendar_changes(self.cal, new_cal)))

    def test_invalid_calendars_rejected(self):
        with self.assertRaises(ValueError):
            Calendar(BERLIN, frozenset(), time(18), time(9))  # overnight
        with self.assertRaises(ValueError):
            Calendar('Not/AZone', frozenset(), time(9), time(18))


class SharedTimezoneBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.cal = Calendar(BERLIN, frozenset({date(2026, 10, 12)}), time(9), time(18))
        self.site = Site('BER-HUB', self.cal)
        self.fri = datetime(2026, 10, 9, 15, tzinfo=UTC)
        self.window_end = datetime(2026, 10, 21, tzinfo=UTC)

    def _resource(self):
        span = (datetime(2026, 10, 9, tzinfo=UTC), self.window_end)
        return Courier('R', BERLIN, *span), Vehicle('R', BERLIN, *span), Dock('R', BERLIN, *span)

    def test_all_three_schedulers_share_open_window(self):
        couriers, vehicles, docks = self._resource()
        cases = (
            (courier_build, CourierJob('J', self.fri, self.window_end, timedelta(minutes=30), BERLIN), couriers),
            (pickup_build, PickupJob('J', self.fri, self.window_end, timedelta(minutes=30), BERLIN), vehicles),
            (dock_build, DockJob('J', self.fri, self.window_end, timedelta(minutes=30), BERLIN), docks),
        )
        for build, job, resource in cases:
            a = build([job], [resource], site=self.site)[0]
            self.assertTrue(self.site.is_open(a.start))
            self.assertLessEqual(a.start.astimezone(clock.zone(BERLIN)).replace(tzinfo=None),
                                 datetime(2026, 10, 9, 17, 30))

    def test_job_that_overflows_close_is_moved_to_next_business_day(self):
        job = CourierJob('J', self.fri, self.window_end, timedelta(minutes=90), BERLIN)
        a = courier_build([job], [self._resource()[0]], site=self.site)[0]
        self.assertEqual(a.start.astimezone(clock.zone(BERLIN)).replace(tzinfo=None),
                         datetime(2026, 10, 13, 9, 0))  # past weekend AND holiday

    def test_sla_verdict_matches_dispatch_plan(self):
        job = CourierJob('J', self.fri, self.window_end, timedelta(minutes=90), BERLIN)
        a = courier_build([job], [self._resource()[0]], site=self.site)[0]
        ok = evaluate(Agreement('A', self.cal, 150), self.fri, a.end)
        breach = evaluate(Agreement('A', self.cal, 149), self.fri, a.end)
        self.assertTrue(ok.on_time)
        self.assertEqual(ok.business_minutes_used, 150)
        self.assertFalse(breach.on_time)

    def test_zone_mismatch_is_rejected(self):
        job = CourierJob('J', self.fri, self.window_end, timedelta(minutes=30), 'America/New_York')
        with self.assertRaises(ValueError):
            courier_build([job], [self._resource()[0]], site=self.site)

    def test_end_equal_to_latest_is_feasible(self):
        # regression: courier build used '<' while pickup/dock used '<='
        j = CourierJob('J', datetime(2026, 10, 9, 8, tzinfo=UTC),
                       datetime(2026, 10, 9, 8, 30, tzinfo=UTC), timedelta(minutes=30), BERLIN)
        r = Courier('R', BERLIN, datetime(2026, 10, 9, 8, tzinfo=UTC),
                    datetime(2026, 10, 9, 8, 30, tzinfo=UTC))
        a = courier_build([j], [r])[0]
        self.assertEqual(a.end, datetime(2026, 10, 9, 8, 30, tzinfo=UTC))


class DaylightSavingTests(unittest.TestCase):
    def test_localize_classifies_gap_ambiguous_normal(self):
        # America/New_York springs forward 2026-03-08 02:00 -> 03:00
        gap = datetime(2026, 3, 8, 2, 30)
        forward, kind = clock.localize(gap, NY, gap_policy=clock.SHIFT_FORWARD)
        self.assertEqual(kind, 'gap')
        self.assertEqual(forward.astimezone(clock.zone(NY)).replace(tzinfo=None),
                         datetime(2026, 3, 8, 3, 30))
        backward, _ = clock.localize(gap, NY, gap_policy=clock.SHIFT_BACKWARD)
        self.assertEqual(backward.astimezone(clock.zone(NY)).replace(tzinfo=None),
                         datetime(2026, 3, 8, 1, 30))
        # falls back 2026-11-01 02:00; 01:30 happens twice
        late, kind = clock.localize(datetime(2026, 11, 1, 1, 30), NY,
                                    fold_policy=clock.FOLD_LATE)
        self.assertEqual(kind, 'ambiguous')
        self.assertEqual(late, datetime(2026, 11, 1, 6, 30, tzinfo=UTC))  # EST
        early, _ = clock.localize(datetime(2026, 11, 1, 1, 30), NY,
                                  fold_policy=clock.FOLD_EARLY)
        self.assertEqual(early, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))  # EDT
        self.assertEqual(clock.localize(datetime(2026, 7, 1, 12), NY)[1], 'normal')

    def test_sla_crosses_spring_forward_weekend(self):
        cal = Calendar(NY, frozenset(), time(9), time(17))
        fri = datetime(2026, 3, 6, 21, tzinfo=UTC)  # Friday 16:00 EST
        tr = add_minutes(cal, fri, 120, return_trace=True)
        local = tr.result.astimezone(clock.zone(NY))
        self.assertEqual(local.replace(tzinfo=None), datetime(2026, 3, 9, 10, 0))
        self.assertEqual(local.utcoffset(), timedelta(hours=-4))  # EDT after transition
        self.assertEqual(tr.reasons(), ('day_exhausted', 'skip_weekend', 'skip_weekend'))

    def test_gap_minutes_inside_business_window_are_not_charged(self):
        cal = Calendar(NY, frozenset(), time(1), time(4), weekend=frozenset())
        t0 = datetime(2026, 3, 8, 6, tzinfo=UTC)  # 01:00 EST Sunday (workable here)
        tr = add_minutes(cal, t0, 60, return_trace=True)
        self.assertEqual(tr.result.astimezone(clock.zone(NY)).replace(tzinfo=None),
                         datetime(2026, 3, 8, 3, 0))
        self.assertIn('dst_gap', tr.reasons())
        due = add_minutes(cal, t0, 120)
        self.assertEqual(business_minutes_between(cal, t0, due), 120)
        full = business_minutes_between(cal, t0, datetime(2026, 3, 8, 8, tzinfo=UTC))
        self.assertEqual(full, 120)  # 01:00-02:00 + 03:00-04:00

    def test_fall_back_hour_counts_once(self):
        cal = Calendar(NY, frozenset(), time(1), time(4), weekend=frozenset())
        t0 = datetime(2026, 11, 1, 5, tzinfo=UTC)   # 01:00 EDT (first)
        t1 = datetime(2026, 11, 1, 9, tzinfo=UTC)   # 04:00 EST
        self.assertEqual(business_minutes_between(cal, t0, t1), 240)


class ReevaluationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.old = Calendar(BERLIN, frozenset({date(2026, 10, 12)}), time(9), time(18))
        self.new = Calendar(BERLIN, frozenset({date(2026, 10, 12), date(2026, 10, 13)}),
                            time(9), time(18))
        self.fri = datetime(2026, 10, 9, 15, tzinfo=UTC)

    def test_explicit_policies(self):
        self.assertEqual(decide(Policy.FREEZE, JobState('a', self.fri),
                                self.old, self.new, 120).action, 'freeze')
        self.assertEqual(decide(Policy.RECOMPUTE, JobState('a', self.fri, completed=True),
                                self.old, self.new, 120).action, 'recompute')

    def test_adaptive_freezes_history_recomputes_open_work(self):
        frozen = decide(Policy.ADAPTIVE, JobState('a', self.fri, completed=True),
                        self.old, self.new, 120)
        started = decide(Policy.ADAPTIVE, JobState('b', self.fri, started=True),
                         self.old, self.new, 120)
        assigned = decide(Policy.ADAPTIVE, JobState('c', self.fri, assigned=True),
                          self.old, self.new, 120)
        fresh = decide(Policy.ADAPTIVE, JobState('d', self.fri),
                       self.old, self.new, 120)
        self.assertEqual([d.action for d in (frozen, started, assigned, fresh)],
                         ['freeze', 'freeze', 'recompute', 'recompute'])
        self.assertTrue(frozen.changed)  # explain text names the moved deadline
        self.assertIn('holidays', ';'.join(frozen.reasons))

    def test_adaptive_freezes_when_revision_is_neutral(self):
        d = decide(Policy.ADAPTIVE, JobState('a', self.fri, assigned=True),
                   self.old, self.old, 120)
        self.assertEqual(d.action, 'freeze')
        self.assertFalse(d.changed)

    def test_explanation_is_human_readable(self):
        d = decide(Policy.ADAPTIVE, JobState('c', self.fri, assigned=True),
                   self.old, self.new, 120)
        text = d.explain()
        self.assertIn('RECOMPUTE', text)
        self.assertIn('holidays', text)
        self.assertIn('1 day', text)


if __name__ == '__main__':
    unittest.main()
