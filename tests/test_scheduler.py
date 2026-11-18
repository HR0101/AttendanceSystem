import unittest
from datetime import date, datetime, timezone

from src.scheduler import (
    ScheduleError, calendar_intervals, due_course, next_scheduled_run,
    registration_time, schedule_data, validate_schedule,
)


COURSE = {
    "weekday": "mon", "start_time": "14:00", "room": "731",
    "route": "7301", "course_name": "テスト授業",
}
SCHEDULE = {
    "active_from": date(2026, 9, 18),
    "active_until": date(2026, 12, 21),
    "blackout_dates": frozenset({date(2026, 11, 23)}),
    "courses": [COURSE],
}


class SchedulerTests(unittest.TestCase):
    def test_runs_five_minutes_after_start_until_thirty_minutes(self):
        self.assertIsNone(due_course(SCHEDULE, datetime(2026, 10, 5, 14, 4)))
        self.assertEqual(due_course(SCHEDULE, datetime(2026, 10, 5, 14, 5)), COURSE)
        self.assertEqual(due_course(SCHEDULE, datetime(2026, 10, 5, 14, 30)), COURSE)
        self.assertIsNone(due_course(SCHEDULE, datetime(2026, 10, 5, 14, 31)))

    def test_rejects_blackout_and_outside_semester(self):
        self.assertIsNone(due_course(SCHEDULE, datetime(2026, 11, 23, 14, 5)))
        self.assertIsNone(due_course(SCHEDULE, datetime(2026, 12, 28, 14, 5)))

    def test_launchd_weekday_mapping(self):
        self.assertEqual(calendar_intervals([COURSE]), [{"Weekday": 1, "Hour": 14, "Minute": 5}])

    def test_disabled_courses_never_run_or_create_calendar_entries(self):
        disabled = dict(COURSE, enabled=False)
        schedule = dict(SCHEDULE, courses=[disabled])
        self.assertIsNone(due_course(schedule, datetime(2026, 10, 5, 14, 5)))
        self.assertEqual(calendar_intervals([disabled]), [])

    def test_fixed_registration_time_is_independent_of_course_start(self):
        course = dict(COURSE, register_time="14:02")
        schedule = dict(SCHEDULE, courses=[course])
        self.assertEqual(calendar_intervals([course]), [{"Weekday": 1, "Hour": 14, "Minute": 2}])
        self.assertIsNone(due_course(schedule, datetime(2026, 10, 5, 14, 1)))
        self.assertEqual(due_course(schedule, datetime(2026, 10, 5, 14, 2)), course)
        self.assertEqual(course["start_time"], "14:00")

    def test_range_chooses_one_stable_minute_per_occurrence(self):
        course = dict(COURSE, register_time="14:05", register_until="14:15")
        schedule = dict(SCHEDULE, courses=[course])
        times = set()
        for day in (5, 12, 19, 26):
            chosen = registration_time(course, date(2026, 10, day))
            self.assertEqual(chosen, registration_time(course, date(2026, 10, day)))
            self.assertTrue("14:05" <= chosen <= "14:15")
            times.add(chosen)
            matches = [minute for minute in range(60) if due_course(schedule, datetime(2026, 10, day, 14, minute))]
            self.assertEqual(matches, [int(chosen[3:])])
        self.assertGreater(len(times), 1)
        self.assertEqual(calendar_intervals([course]), [{"Weekday": 1, "Hour": 14, "Minute": m} for m in range(5, 16)])

    def test_single_minute_range_and_late_fixed_course_do_not_repeat(self):
        course = dict(COURSE, register_time="14:05", register_until="14:05")
        fixed = dict(COURSE, start_time="13:00", register_time="13:59")
        schedule = dict(SCHEDULE, courses=[fixed, course])
        self.assertEqual(due_course(schedule, datetime(2026, 10, 5, 14, 5)), course)
        self.assertIsNone(due_course(schedule, datetime(2026, 10, 5, 14, 6)))
        self.assertEqual(registration_time(course, date(2026, 10, 5)), "14:05")

    def test_invalid_registration_times_and_ranges_are_rejected(self):
        for fields in ({"register_time": "24:00"}, {"register_time": "1:00"},
                       {"register_time": None}, {"register_until": "14:15"},
                       {"register_time": "14:15", "register_until": "14:05"},
                       {"register_time": "23:50", "register_until": "00:10"}):
            with self.subTest(fields=fields), self.assertRaises(ScheduleError):
                raw = schedule_data(SCHEDULE)
                raw["courses"][0].update(fields)
                validate_schedule(raw)

    def test_range_respects_holidays_and_disabled_courses(self):
        course = dict(COURSE, register_time="14:05", register_until="14:15")
        schedule = dict(SCHEDULE, courses=[course])
        for minute in range(5, 16):
            self.assertIsNone(due_course(schedule, datetime(2026, 11, 23, 14, minute)))
        course["enabled"] = False
        self.assertEqual(calendar_intervals([course]), [])

    def test_default_midnight_rollover_uses_course_date(self):
        course = dict(COURSE, weekday="sun", start_time="23:58")
        schedule = dict(SCHEDULE, active_from=date(2026, 10, 4), active_until=date(2026, 10, 4), courses=[course])
        self.assertEqual(calendar_intervals([course]), [{"Weekday": 1, "Hour": 0, "Minute": 3}])
        self.assertEqual(due_course(schedule, datetime(2026, 10, 5, 0, 3)), course)
        schedule["blackout_dates"] = frozenset({date(2026, 10, 4)})
        self.assertIsNone(due_course(schedule, datetime(2026, 10, 5, 0, 3)))

    def test_next_scheduled_run_picks_earliest_enabled_course(self):
        disabled = dict(COURSE, weekday="mon", start_time="09:00", course_name="無効", enabled=False)
        wednesday = dict(COURSE, weekday="wed", start_time="10:00", course_name="水曜授業", enabled=True)
        schedule = dict(SCHEDULE, courses=[disabled, COURSE, wednesday])
        self.assertEqual(
            next_scheduled_run(schedule, datetime(2026, 10, 5, 14, 4)),
            (datetime(2026, 10, 5, 14, 5), COURSE),
        )
        self.assertEqual(
            next_scheduled_run(schedule, datetime(2026, 10, 5, 14, 5)),
            (datetime(2026, 10, 5, 14, 5), COURSE),
        )
        self.assertEqual(
            next_scheduled_run(schedule, datetime(2026, 10, 5, 14, 6)),
            (datetime(2026, 10, 7, 10, 5), wednesday),
        )

    def test_next_scheduled_run_respects_period_and_blackout_dates(self):
        self.assertEqual(
            next_scheduled_run(SCHEDULE, datetime(2026, 9, 1, 9, 0)),
            (datetime(2026, 9, 21, 14, 5), COURSE),
        )
        # 2026-11-23 は休講日のため翌週の 2026-11-30 を返す。
        self.assertEqual(
            next_scheduled_run(SCHEDULE, datetime(2026, 11, 20, 9, 0)),
            (datetime(2026, 11, 30, 14, 5), COURSE),
        )
        self.assertIsNone(next_scheduled_run(SCHEDULE, datetime(2026, 12, 21, 14, 6)))
        self.assertIsNone(next_scheduled_run(SCHEDULE, datetime(2026, 12, 22, 9, 0)))
        disabled_only = dict(SCHEDULE, courses=[dict(COURSE, enabled=False)])
        self.assertIsNone(next_scheduled_run(disabled_only, datetime(2026, 10, 5, 10, 0)))
        all_blackouts = dict(
            SCHEDULE,
            active_from=date(2026, 10, 5),
            active_until=date(2026, 10, 12),
            blackout_dates=frozenset({date(2026, 10, 5), date(2026, 10, 12)}),
        )
        self.assertIsNone(next_scheduled_run(all_blackouts, datetime(2026, 10, 1, 9, 0)))

    def test_next_scheduled_run_handles_range_rollover_and_timezone(self):
        ranged = dict(COURSE, register_time="14:05", register_until="14:15")
        first_time = registration_time(ranged, date(2026, 10, 5))
        second_time = registration_time(ranged, date(2026, 10, 12))
        first_hour, first_min = map(int, first_time.split(":"))
        second_hour, second_min = map(int, second_time.split(":"))
        ranged_schedule = dict(SCHEDULE, courses=[ranged])
        self.assertEqual(
            next_scheduled_run(ranged_schedule, datetime(2026, 10, 5, first_hour, first_min)),
            (datetime(2026, 10, 5, first_hour, first_min), ranged),
        )
        self.assertEqual(
            next_scheduled_run(ranged_schedule, datetime(2026, 10, 5, first_hour, first_min, 1)),
            (datetime(2026, 10, 12, second_hour, second_min), ranged),
        )
        rollover_course = dict(COURSE, weekday="sun", start_time="23:58")
        rollover_schedule = dict(SCHEDULE, active_from=date(2026, 10, 4), active_until=date(2026, 10, 4), courses=[rollover_course])
        self.assertEqual(
            next_scheduled_run(rollover_schedule, datetime(2026, 10, 5, 0, 1)),
            (datetime(2026, 10, 5, 0, 3), rollover_course),
        )
        aware = next_scheduled_run(SCHEDULE, datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc))
        self.assertEqual(aware, (datetime(2026, 10, 5, 14, 5, tzinfo=timezone.utc), COURSE))


if __name__ == "__main__":
    unittest.main()
