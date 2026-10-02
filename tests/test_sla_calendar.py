import unittest
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo

from coldledger.sla.business_calendar import (
    Calendar, WEEKDAYS, add_minutes, business_minutes_between,
    open_windows, is_business_day, day_bounds, spans_dst_transition,
)
from coldledger.sla.site_hours import Site, is_open_at, opening_at_or_after
from coldledger.sla.dispatch_plan import (
    planning_window, check_assignments, PlannedJob,
)
from coldledger.sla.attainment import Commitment, evaluate, remaining_business_minutes
from coldledger.sla.replan import (
    CalendarRevision, decide, apply_decision, affected_days, record_decision,
    FREEZE_HISTORICAL, FREEZE_PAST_DEADLINE, FREEZE_IMMATERIAL,
    FREEZE_MORE_TIME, REPLAN_HOLIDAY, REPLAN_WORKWEEK, REPLAN_HOURS,
    REPLAN_DST, REPLAN_ZONE,
)
from coldledger.transport.pickup_schedule import (
    PickupJob, Vehicle, build as build_pickups,
)
from coldledger.audit.trail import AuditTrail

UTC = timezone.utc
BERLIN = "Europe/Berlin"
NYC = "America/New_York"
SHANGHAI = "Asia/Shanghai"

OPEN_8_18 = (time(8), time(18))   # 600 open minutes/day


def berlin(holidays=frozenset(), weekdays=WEEKDAYS, version=0):
    o, c = OPEN_8_18
    return Calendar(BERLIN, frozenset(holidays), o, c, frozenset(weekdays), version)


def dt(y, m, d, hh, mm=0, z=UTC):
    return datetime(y, m, d, hh, mm, tzinfo=z)


class BusinessCalendarTests(unittest.TestCase):
    def test_add_minutes_skips_full_weekend(self):
        cal = berlin()
        # Thursday 2026-10-01 10:00Z (12:00 local) + 600 open min
        # = Thu 8h + Fri 2h -> Friday 10:00Z (12:00 local).
        end = add_minutes(cal, dt(2026, 10, 1, 10, 0, UTC), 600)
        self.assertEqual(end, dt(2026, 10, 2, 10, 0, UTC))
        # One more minute lands... still Friday 10:01; exhaust Friday instead:
        # + 1080 from Thu 10:00Z = Thu 480 + Fri 480 + 120 on Monday 08:00 local.
        end = add_minutes(cal, dt(2026, 10, 1, 10, 0, UTC), 1080)
        self.assertEqual(end, dt(2026, 10, 5, 8, 0, UTC))
        # Nothing scheduled on the weekend at all.
        self.assertEqual(business_minutes_between(
            cal, dt(2026, 10, 2, 16, 0, UTC), dt(2026, 10, 5, 6, 0, UTC)), 0)
        # Friday 16:30Z (closed) + 1 minute -> Monday 06:01Z, not Saturday.
        end = add_minutes(cal, dt(2026, 10, 2, 16, 30, UTC), 1)
        self.assertEqual(end, dt(2026, 10, 5, 6, 1, UTC))

    def test_add_minutes_skips_holiday_chain_and_weekend(self):
        # Friday 2026-10-02 is a holiday; 03/04 are the weekend.
        cal = berlin(holidays={date(2026, 10, 2)})
        end = add_minutes(cal, dt(2026, 10, 1, 14, 0, UTC), 600)
        # Thu 14:00Z = 16:00 local -> 120 min Thu + 480 on Monday -> Mon 16:00 local
        self.assertEqual(end, dt(2026, 10, 5, 14, 0, UTC))

    def test_start_before_opening_snaps_to_open(self):
        cal = berlin()
        end = add_minutes(cal, dt(2026, 10, 1, 4, 0, UTC), 60)  # 06:00 local, closed
        self.assertEqual(end, dt(2026, 10, 1, 7, 0, UTC))       # 09:00 local

    def test_naive_start_rejected(self):
        cal = berlin()
        with self.assertRaises(ValueError):
            add_minutes(cal, datetime(2026, 10, 1, 16), 60)

    def test_dst_windows_spanning_transition_have_150_and_270_minutes(self):
        # 00:30..04:00 local windows actually contain the 02:00/03:00 change.
        # Query around the local day (the window crosses the UTC midnight
        # boundary: e.g. 23:30Z previous day .. 02:00Z).
        cal = Calendar(BERLIN, frozenset(), time(0, 30), time(4),
                       frozenset(range(7)))
        spring = business_minutes_between(cal, dt(2026, 3, 28, 22, 0, UTC),
                                          dt(2026, 3, 29, 4, 0, UTC))
        fall = business_minutes_between(cal, dt(2026, 10, 24, 22, 0, UTC),
                                        dt(2026, 10, 25, 4, 0, UTC))
        self.assertEqual(spring, 150)   # 23:30Z..02:00Z (gap skipped)
        self.assertEqual(fall, 270)     # 22:30Z..03:00Z (repeated hour)
        self.assertTrue(spans_dst_transition(cal, date(2026, 3, 29)))
        self.assertTrue(spans_dst_transition(cal, date(2026, 10, 25)))

    def test_business_day_window_outside_transition_is_600_minutes(self):
        # Sanity: a daytime 08:00-18:00 window never contains the 02:00 change.
        cal7 = berlin(weekdays=frozenset(range(7)))
        self.assertFalse(spans_dst_transition(cal7, date(2026, 3, 29)))
        self.assertEqual(business_minutes_between(
            cal7, dt(2026, 3, 29, 0, 0, UTC), dt(2026, 3, 30, 0, 0, UTC)), 600)

    def test_add_minutes_across_spring_forward_is_absolute(self):
        cal = Calendar(BERLIN, frozenset(), time(0, 30), time(4),
                       frozenset(range(7)))
        # Friday 2026-03-27 window 23:30Z..03:00Z = 210 absolute minutes.
        # Saturday spring-forward window is only 150 minutes; 210 + 150 = 360
        # lands exactly at Saturday's 02:00Z close (04:00 local after the gap).
        end = add_minutes(cal, dt(2026, 3, 27, 23, 30, UTC), 360)
        self.assertEqual(end, dt(2026, 3, 29, 2, 0, UTC))
        # One more minute spills past the shortened window to Sunday 22:31Z.
        end = add_minutes(cal, dt(2026, 3, 27, 23, 30, UTC), 361)
        self.assertEqual(end, dt(2026, 3, 29, 22, 31, UTC))
        # Crossing fall-back: the 270-minute window fully counts.
        end = add_minutes(cal, dt(2026, 10, 24, 22, 30, UTC), 270)
        self.assertEqual(end, dt(2026, 10, 25, 3, 0, UTC))

    def test_day_bounds_dst_and_holidays(self):
        cal = Calendar(BERLIN, frozenset(), time(0, 30), time(4),
                       frozenset(range(7)))
        o, c = day_bounds(cal, date(2026, 3, 29))
        self.assertEqual((o, c), (dt(2026, 3, 28, 23, 30, UTC),
                                  dt(2026, 3, 29, 2, 0, UTC)))
        self.assertFalse(is_business_day(berlin(), date(2026, 10, 3)))  # Saturday

    def test_unreachable_calendar_raises(self):
        cal = berlin(weekdays=frozenset())  # no business days at all
        with self.assertRaises(ValueError):
            add_minutes(cal, dt(2026, 10, 1, 12, 0, UTC), 10)


class SiteHoursTests(unittest.TestCase):
    def setUp(self):
        self.site = Site("BER-DC", berlin())

    def test_open_and_closed(self):
        self.assertTrue(is_open_at(self.site, dt(2026, 10, 1, 8, 0, UTC)))    # Thu 10 local
        self.assertTrue(is_open_at(self.site, dt(2026, 10, 2, 14, 0, UTC)))   # Fri 16 local
        self.assertFalse(is_open_at(self.site, dt(2026, 10, 2, 16, 30, UTC))) # after close
        self.assertFalse(is_open_at(self.site, dt(2026, 10, 3, 12, 0, UTC)))  # Saturday
        self.assertFalse(is_open_at(self.site, dt(2026, 10, 4, 12, 0, UTC)))  # Sunday

    def test_opening_at_or_after(self):
        # Friday 15:30Z is 17:30 local (open until 18:00), so already open.
        self.assertEqual(opening_at_or_after(self.site, dt(2026, 10, 2, 15, 30, UTC)),
                         dt(2026, 10, 2, 15, 30, UTC))
        # 16:30Z Friday is after close; next opening is Monday 08:00 local.
        self.assertEqual(opening_at_or_after(self.site, dt(2026, 10, 2, 16, 30, UTC)),
                         dt(2026, 10, 5, 6, 0, UTC))

    def test_holiday_site_closed(self):
        site = Site("BER-DC", berlin(holidays={date(2026, 10, 2)}))
        self.assertFalse(is_open_at(site, dt(2026, 10, 2, 10, 0, UTC)))


class DispatchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.site = Site("BER-DC", berlin())

    def test_planning_window_deadline_matches_sla_clock(self):
        # Ready Friday 2026-10-02 14:00Z (16:00 local); 120 business min end
        # exactly at Friday 16:00Z close; 121 spills to Monday 06:01Z.
        job = planning_window(self.site, "J1", dt(2026, 10, 2, 14, 0, UTC),
                              timedelta(minutes=30), 120)
        self.assertEqual(job.latest, dt(2026, 10, 2, 16, 0, UTC))
        job2 = planning_window(self.site, "J2", dt(2026, 10, 2, 14, 0, UTC),
                               timedelta(minutes=30), 121)
        self.assertEqual(job2.latest, dt(2026, 10, 5, 6, 1, UTC))
        self.assertIsInstance(job, PlannedJob)

    def test_plan_inside_hours_passes_shared_boundary_check(self):
        job = planning_window(self.site, "P1",
                              dt(2026, 10, 1, 6, 0, UTC), timedelta(minutes=30), 120)
        vehicle = Vehicle("V1", BERLIN, dt(2026, 10, 1, 0, 0, UTC),
                          dt(2026, 10, 2, 23, 0, UTC))
        pjob = PickupJob(job.key, job.earliest, job.latest,
                         job.duration, zone=BERLIN)
        assignments = build_pickups([pjob], [vehicle])
        self.assertEqual(check_assignments(self.site, assignments), [])
        a = assignments[0]
        self.assertTrue(dt(2026, 10, 1, 6, 0, UTC) <= a.start < dt(2026, 10, 1, 16, 0, UTC))

    def test_plan_landing_on_weekend_flagged(self):
        # Earliest Friday 15:30Z, 60-min job cannot finish by 16:00Z; the
        # scheduler's own window is wide enough that without the calendar it
        # would silently place work in the weekend.
        wide = PickupJob("P2", dt(2026, 10, 2, 15, 30, UTC),
                         dt(2026, 10, 6, 0, 0, UTC), timedelta(minutes=60), zone=BERLIN)
        vehicle = Vehicle("V2", BERLIN, dt(2026, 10, 1, 0, 0, UTC),
                          dt(2026, 10, 7, 0, 0, UTC))
        assignments = build_pickups([wide], [vehicle])
        violations = check_assignments(self.site, assignments)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].item_key, "P2")
        self.assertIn("outside site open hours", violations[0].reason)


class AttainmentTests(unittest.TestCase):
    def test_delay_counts_business_minutes_only(self):
        cal = berlin()
        opened = dt(2026, 10, 1, 6, 0, UTC)  # Thu 08:00 local
        due = add_minutes(cal, opened, 1200)  # Thu+Fri full days -> Fri 16:00Z
        self.assertEqual(due, dt(2026, 10, 2, 16, 0, UTC))
        commitment = Commitment("S1", "BER-DC", opened, due)
        # Completed Monday 10:00Z: the whole weekend counts for zero delay;
        # only Monday 06:00Z..10:00Z does.
        result = evaluate(cal, commitment, dt(2026, 10, 5, 10, 0, UTC))
        self.assertFalse(result.on_time)
        self.assertEqual(result.delay_business_minutes, 240)
        # On time Friday afternoon.
        ok = evaluate(cal, commitment, dt(2026, 10, 2, 15, 0, UTC))
        self.assertTrue(ok.on_time)
        self.assertEqual(ok.delay_business_minutes, 0)

    def test_remaining_budget(self):
        cal = berlin()
        commitment = Commitment("S2", "BER-DC", dt(2026, 10, 2, 14, 0, UTC),
                                dt(2026, 10, 2, 16, 0, UTC))
        self.assertEqual(remaining_business_minutes(cal, commitment,
                                                     dt(2026, 10, 2, 15, 0, UTC)), 60)
        self.assertEqual(remaining_business_minutes(cal, commitment,
                                                     dt(2026, 10, 3, 12, 0, UTC)), 0)


class ReplanPolicyTests(unittest.TestCase):
    def setUp(self):
        self.cal = berlin()
        # Promise opened Thu 2026-10-01 08:00Z (10:00 local), 600 business min
        # -> due Fri 08:00Z (10:00 local).
        self.opened = dt(2026, 10, 1, 8, 0, UTC)
        self.due = add_minutes(self.cal, self.opened, 600)
        self.commitment = Commitment("C1", "BER-DC", self.opened, self.due)
        self.announced = dt(2026, 9, 25, 10, 0, UTC)

    def test_completed_promise_is_frozen(self):
        after = berlin(holidays={date(2026, 10, 2)})
        rev = CalendarRevision(self.cal, after, self.announced)
        d = decide(rev, self.commitment, completed_at=dt(2026, 10, 2, 7, 0, UTC))
        self.assertEqual((d.action, d.reason), ("freeze", FREEZE_HISTORICAL))
        self.assertIsNone(d.new_due_at)
        self.assertIs(apply_decision(d, self.commitment), self.commitment)

    def test_new_holiday_in_window_forces_replan(self):
        after = berlin(holidays={date(2026, 10, 2)})
        rev = CalendarRevision(self.cal, after, self.announced)
        d = decide(rev, self.commitment)
        self.assertEqual(d.action, "replan")
        self.assertEqual(d.reason, REPLAN_HOLIDAY)
        self.assertIn(date(2026, 10, 2), d.affected_days)
        # Same 600-minute budget: Thu 480 + 120 on Monday 10:00 local.
        self.assertEqual(d.new_due_at, dt(2026, 10, 5, 8, 0, UTC))
        moved = apply_decision(d, self.commitment)
        self.assertEqual(moved.due_at, d.new_due_at)

    def test_holiday_after_window_is_immaterial(self):
        after = berlin(holidays={date(2026, 10, 5)})
        rev = CalendarRevision(self.cal, after, self.announced)
        d = decide(rev, self.commitment)
        self.assertEqual((d.action, d.reason), ("freeze", FREEZE_IMMATERIAL))

    def test_announcement_after_deadline_freezes(self):
        after = berlin(holidays={date(2026, 10, 2)})
        rev = CalendarRevision(self.cal, after, dt(2026, 10, 2, 12, 0, UTC))
        d = decide(rev, self.commitment)
        self.assertEqual((d.action, d.reason), ("freeze", FREEZE_PAST_DEADLINE))

    def test_removed_holiday_adds_time_freezes(self):
        before = berlin(holidays={date(2026, 10, 5)})
        opened = dt(2026, 10, 1, 8, 0, UTC)
        due = add_minutes(before, opened, 1200)   # Thu 600 + Fri 600
        commitment = Commitment("C2", "BER-DC", opened, due)
        rev = CalendarRevision(before, berlin(), self.announced)
        d = decide(rev, commitment)
        self.assertEqual((d.action, d.reason), ("freeze", FREEZE_MORE_TIME))
        self.assertIsNotNone(d.new_due_at)
        self.assertLess(d.new_due_at, d.old_due_at)

    def test_shortened_hours_force_replan(self):
        after = Calendar(BERLIN, frozenset(), time(8), time(12),
                         WEEKDAYS, version=1)
        rev = CalendarRevision(self.cal, after, self.announced)
        d = decide(rev, self.commitment)
        self.assertEqual(d.action, "replan")
        self.assertEqual(d.reason, REPLAN_HOURS)

    def test_workweek_removal_forces_replan(self):
        after = berlin(weekdays=frozenset({0, 1, 2, 3}))  # Friday gone
        rev = CalendarRevision(self.cal, after, self.announced)
        d = decide(rev, self.commitment)
        self.assertEqual((d.action, d.reason), ("replan", REPLAN_WORKWEEK))

    def test_dst_boundary_shift_forces_replan(self):
        # Same wall-clock 08:00-18:00 window anchored in different zones.
        # A promise spanning 2026-03-29 in Berlin (06:00Z..15:00Z on the
        # spring-forward Sunday, 7-day calendar) vs Shanghai (no DST,
        # 00:00Z..10:00Z): the UTC boundary moves.
        before = Calendar(BERLIN, frozenset(), time(8), time(18),
                          frozenset(range(7)), version=0)
        after = Calendar(SHANGHAI, frozenset(), time(8), time(18),
                         frozenset(range(7)), version=1)
        opened = dt(2026, 3, 27, 6, 0, UTC)
        due = add_minutes(before, opened, 1300)
        commitment = Commitment("C3", "BER-DC", opened, due)
        rev = CalendarRevision(before, after, dt(2026, 3, 20, 0, 0, UTC))
        days = affected_days(rev, due + timedelta(days=1), start_at=opened)
        self.assertIn(date(2026, 3, 29), days)
        d = decide(rev, commitment)
        self.assertEqual(d.action, "replan")
        self.assertIn(d.reason, (REPLAN_DST, REPLAN_ZONE))

    def test_audit_entry_is_recorded_and_verifiable(self):
        after = berlin(holidays={date(2026, 10, 2)})
        rev = CalendarRevision(self.cal, after, self.announced)
        d = decide(rev, self.commitment)
        trail = AuditTrail()
        record_decision(trail, d, actor="ops")
        self.assertTrue(trail.verify())
        self.assertIn("replan", trail.records[0].action)
        self.assertEqual(trail.records[0].subject, "commitment:C1")


if __name__ == "__main__":
    unittest.main()
