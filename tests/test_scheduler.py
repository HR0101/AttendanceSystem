import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from src.scheduler import (
    ScheduleError, calendar_intervals, due_course, load_schedule,
    registration_time, save_schedule, schedule_data, validate_schedule,
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

    @staticmethod
    def _raw_schedule(*courses):
        raw = schedule_data(SCHEDULE)
        raw["courses"] = [dict(COURSE, **fields) for fields in courses]
        return raw

    def test_rejects_same_register_time_even_with_different_start_times(self):
        cases = (
            (
                {"course_name": "授業A", "start_time": "13:00", "register_time": "13:15"},
                {"course_name": "授業B", "start_time": "13:10", "register_time": "13:15"},
            ),
            (
                {"course_name": "授業A", "start_time": "13:00"},
                {"course_name": "授業B", "start_time": "13:10", "register_time": "13:05"},
            ),
        )
        for first, second in cases:
            with self.subTest(first=first, second=second):
                with self.assertRaises(ScheduleError) as ctx:
                    validate_schedule(self._raw_schedule(first, second))
                self.assertIn("授業A", str(ctx.exception))
                self.assertIn("授業B", str(ctx.exception))
                self.assertIn(first.get("register_time", "13:05"), str(ctx.exception))

        # 別の範囲指定授業があっても同一登録時刻は衝突する。
        with self.assertRaises(ScheduleError):
            validate_schedule(self._raw_schedule(
                {"course_name": "授業A", "start_time": "13:00", "register_time": "13:15"},
                {"course_name": "授業B", "start_time": "13:10", "register_time": "13:15"},
                {"course_name": "授業C", "weekday": "tue", "start_time": "10:00", "register_time": "10:05", "register_until": "10:15"},
            ))

    def test_rejects_same_start_time_and_weekday_even_if_register_times_differ(self):
        with self.assertRaises(ScheduleError) as ctx:
            validate_schedule(self._raw_schedule(
                {"course_name": "授業A", "start_time": "14:00", "register_time": "14:05"},
                {"course_name": "授業B", "start_time": "14:00", "register_time": "14:50"},
            ))
        self.assertIn("同じ曜日・開始時刻", str(ctx.exception))

    def test_delay_tolerant_window_boundary_25_minutes(self):
        for conflict_time in ("14:20", "14:30"):
            with self.subTest(conflict_time=conflict_time), self.assertRaises(ScheduleError) as ctx:
                validate_schedule(self._raw_schedule(
                    {"course_name": "授業A", "start_time": "14:00", "register_time": "14:05"},
                    {"course_name": "授業B", "start_time": "14:15", "register_time": conflict_time},
                ))
            self.assertIn("授業A", str(ctx.exception))
            self.assertIn("授業B", str(ctx.exception))
            self.assertIn("14:05", str(ctx.exception))
            self.assertIn(conflict_time, str(ctx.exception))

        # 開始5分後の自動計算同士でも25分境界で判定する。
        with self.assertRaises(ScheduleError):
            validate_schedule(self._raw_schedule(
                {"course_name": "授業A", "start_time": "14:00"},
                {"course_name": "授業B", "start_time": "14:25"},
            ))

        validated_fixed = validate_schedule(self._raw_schedule(
            {"course_name": "授業A", "start_time": "14:00", "register_time": "14:05"},
            {"course_name": "授業B", "start_time": "14:15", "register_time": "14:31"},
        ))
        self.assertEqual(len(validated_fixed["courses"]), 2)

        validated_default = validate_schedule(self._raw_schedule(
            {"course_name": "授業A", "start_time": "14:00"},
            {"course_name": "授業B", "start_time": "14:26"},
        ))
        self.assertEqual(len(validated_default["courses"]), 2)

    def test_exact_mode_range_conflicts_and_non_overlapping_boundaries(self):
        range_course = {"course_name": "授業A", "start_time": "14:00", "register_time": "14:05", "register_until": "14:15"}
        for conflicting in (
            {"course_name": "授業B", "start_time": "14:10", "register_time": "14:10"},
            {"course_name": "授業B", "start_time": "14:05", "register_time": "14:05"},
            {"course_name": "授業B", "start_time": "14:15", "register_time": "14:15"},
            {"course_name": "授業B", "start_time": "14:12", "register_time": "14:12", "register_until": "14:20"},
            {"course_name": "授業B", "start_time": "14:15", "register_time": "14:15", "register_until": "14:20"},
        ):
            with self.subTest(conflicting=conflicting), self.assertRaises(ScheduleError) as ctx:
                validate_schedule(self._raw_schedule(range_course, conflicting))
            self.assertIn("授業A", str(ctx.exception))
            self.assertIn("授業B", str(ctx.exception))

        for non_conflicting in (
            {"course_name": "授業B", "start_time": "14:04", "register_time": "14:04"},
            {"course_name": "授業B", "start_time": "14:16", "register_time": "14:16"},
            {"course_name": "授業B", "start_time": "14:16", "register_time": "14:16", "register_until": "14:20"},
        ):
            with self.subTest(non_conflicting=non_conflicting):
                validated = validate_schedule(self._raw_schedule(range_course, non_conflicting))
                self.assertEqual(len(validated["courses"]), 2)

        # exact モードでは固定時刻同士が25分以内でも重複しないが、範囲授業が無効なら衝突する。
        fixed_1 = {"course_name": "授業A", "start_time": "14:00", "register_time": "14:05"}
        fixed_2 = {"course_name": "授業B", "start_time": "14:15", "register_time": "14:20"}
        tue_range = {"course_name": "授業C", "weekday": "tue", "start_time": "10:00", "register_time": "10:05", "register_until": "10:15"}
        self.assertEqual(len(validate_schedule(self._raw_schedule(fixed_1, fixed_2, tue_range))["courses"]), 3)
        with self.assertRaises(ScheduleError):
            validate_schedule(self._raw_schedule(fixed_1, fixed_2, dict(tue_range, enabled=False)))

    def test_week_wraparound_conflicts_and_boundaries(self):
        # sun 23:55 (既定 -> mon 00:00..00:25) と mon 00:10 (00:10..00:35)
        for mon_course in (
            {"course_name": "月曜固定", "weekday": "mon", "start_time": "00:05", "register_time": "00:10"},
            {"course_name": "月曜既定", "weekday": "mon", "start_time": "00:10"},
        ):
            with self.subTest(mon_course=mon_course), self.assertRaises(ScheduleError):
                validate_schedule(self._raw_schedule(
                    {"course_name": "日曜深夜", "weekday": "sun", "start_time": "23:55"},
                    mon_course,
                ))

        # sun 23:50 既定 (日 23:55～月 00:20) の週またぎ区間分割
        with self.assertRaises(ScheduleError) as ctx:
            validate_schedule(self._raw_schedule(
                {"course_name": "日曜深夜", "weekday": "sun", "start_time": "23:50"},
                {"course_name": "月曜深夜", "weekday": "mon", "start_time": "00:10", "register_time": "00:10"},
            ))
        self.assertIn("日曜深夜", str(ctx.exception))
        self.assertIn("月曜深夜", str(ctx.exception))

        # sun 23:58 既定 (mon 00:03) と mon 00:03 は exact モードでも衝突する。
        with self.assertRaises(ScheduleError):
            validate_schedule(self._raw_schedule(
                {"course_name": "日曜深夜", "weekday": "sun", "start_time": "23:58"},
                {"course_name": "月曜深夜", "weekday": "mon", "start_time": "00:03", "register_time": "00:03"},
                {"course_name": "火曜範囲", "weekday": "tue", "start_time": "10:00", "register_time": "10:05", "register_until": "10:15"},
            ))

        # sun 23:35 (+25m = mon 00:00) は mon 00:00 と重なり、sun 23:34 (+25m = sun 23:59) は重ならない。
        with self.assertRaises(ScheduleError):
            validate_schedule(self._raw_schedule(
                {"course_name": "日曜境界", "weekday": "sun", "start_time": "23:30", "register_time": "23:35"},
                {"course_name": "月曜境界", "weekday": "mon", "start_time": "00:00", "register_time": "00:00"},
            ))
        validated = validate_schedule(self._raw_schedule(
            {"course_name": "日曜境界", "weekday": "sun", "start_time": "23:30", "register_time": "23:34"},
            {"course_name": "月曜境界", "weekday": "mon", "start_time": "00:00", "register_time": "00:00"},
        ))
        self.assertEqual(len(validated["courses"]), 2)

        # sun 23:55 (既定 -> mon 00:00..00:25) と mon 00:26 は衝突しない。
        validated_non_overlap = validate_schedule(self._raw_schedule(
            {"course_name": "日曜深夜", "weekday": "sun", "start_time": "23:55"},
            {"course_name": "月曜深夜", "weekday": "mon", "start_time": "00:20", "register_time": "00:26"},
        ))
        self.assertEqual(len(validated_non_overlap["courses"]), 2)

    def test_disabled_conflicting_course_is_allowed_until_enabled_and_save_rejects(self):
        disabled_conflict = self._raw_schedule(
            {"course_name": "授業A", "start_time": "14:00", "register_time": "14:05", "enabled": True},
            {"course_name": "授業B", "start_time": "14:15", "register_time": "14:20", "enabled": False},
        )
        validated = validate_schedule(disabled_conflict)
        self.assertEqual(len(validated["courses"]), 2)

        enabled_conflict = self._raw_schedule(
            {"course_name": "授業A", "start_time": "14:00", "register_time": "14:05", "enabled": True},
            {"course_name": "授業B", "start_time": "14:15", "register_time": "14:20", "enabled": True},
        )
        with self.assertRaises(ScheduleError):
            validate_schedule(enabled_conflict)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedule.json"
            save_schedule(path, disabled_conflict)
            original = path.read_bytes()
            with self.assertRaises(ScheduleError):
                save_schedule(path, enabled_conflict)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(load_schedule(path)["courses"][1]["enabled"])


if __name__ == "__main__":
    unittest.main()
