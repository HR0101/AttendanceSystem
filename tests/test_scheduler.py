import unittest
from datetime import date, datetime

from src.scheduler import calendar_intervals, due_course


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


if __name__ == "__main__":
    unittest.main()
