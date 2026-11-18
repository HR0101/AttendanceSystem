import contextlib
import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from src import config
from src.scheduler import (
    ScheduleError, calendar_intervals, due_course, registration_time,
    save_schedule, schedule_data, schedule_digest, schedule_lock, validate_schedule,
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

    def test_schedule_lock_and_digest_check_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            path = base / "schedule.json"
            self.assertIsNone(schedule_digest(path))
            save_schedule(path, schedule_data(SCHEDULE))
            digest = schedule_digest(path)
            self.assertEqual(len(digest), 64)
            lock_path = base / "schedule.json.lock"
            lock_path.write_text("", encoding="utf-8")
            os.chmod(lock_path, 0o644)
            with schedule_lock(path, exclusive=True):
                self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(BlockingIOError):
                    with schedule_lock(path, exclusive=True, blocking=False):
                        pass
            target = base / "other.json"
            target.write_text("{}", encoding="utf-8")
            path.unlink()
            path.symlink_to(target)
            with self.assertRaises(ScheduleError):
                schedule_digest(path)
            with self.assertRaises(ScheduleError):
                save_schedule(path, schedule_data(SCHEDULE))
            self.assertEqual(target.read_text(encoding="utf-8"), "{}")
            lock_path.unlink()
            lock_path.symlink_to(target)
            with self.assertRaises(ScheduleError):
                with schedule_lock(path, exclusive=True):
                    pass
            lock_path.unlink()
            lock_path.mkdir()
            with self.assertRaises(ScheduleError):
                with schedule_lock(path, exclusive=True):
                    pass
            lock_path.rmdir()
            if hasattr(os, "mkfifo"):
                os.mkfifo(lock_path, 0o600)
                with self.assertRaises(ScheduleError):
                    with schedule_lock(path, exclusive=True):
                        pass

    def test_schedule_lock_reentrancy_and_nonblocking_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "schedule.json"
            with schedule_lock(path, exclusive=True, blocking=True):
                with schedule_lock(path, exclusive=True, blocking=True):
                    with schedule_lock(path, exclusive=False, blocking=True):
                        with self.assertRaises(BlockingIOError):
                            with schedule_lock(path, exclusive=True, blocking=False):
                                pass
            with schedule_lock(path, exclusive=False, blocking=True):
                with schedule_lock(path, exclusive=False, blocking=False):
                    pass
                with self.assertRaises(BlockingIOError):
                    with schedule_lock(path, exclusive=True, blocking=False):
                        pass

    def test_schedule_lock_excludes_independent_flock_and_subprocess(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            path = base / "schedule.json"
            lock_path = base / "schedule.json.lock"

            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                with self.assertRaises(BlockingIOError):
                    with schedule_lock(path, exclusive=True, blocking=False):
                        pass
                with self.assertRaises(BlockingIOError):
                    with schedule_lock(path, exclusive=False, blocking=False):
                        pass
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            self.assertEqual([p.name for p in base.iterdir()], ["schedule.json.lock"])

            with schedule_lock(path, exclusive=True):
                probe = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import fcntl, os, sys; "
                            "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600); "
                            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)"
                        ),
                        str(lock_path),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(probe.returncode, 0)
                self.assertIn("BlockingIOError", probe.stderr)

    def test_schedule_lock_delegates_to_config_private_file_lock_when_present(self):
        calls = []

        @contextlib.contextmanager
        def fake_private_lock(target, *, exclusive=True, blocking=True):
            calls.append((target, exclusive, blocking))
            yield target.with_name(f"{target.name}.lock")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "schedule.json"
            with patch.object(config, "private_file_lock", fake_private_lock, create=True):
                with schedule_lock(path, exclusive=False, blocking=True) as entered:
                    self.assertEqual(entered, path.with_name("schedule.json.lock"))
            self.assertEqual(calls, [(path, False, True)])

            @contextlib.contextmanager
            def symlink_error_lock(target, *, exclusive=True, blocking=True):
                raise OSError("schedule.json.lock にシンボリックリンクは使用できません。")
                yield

            with patch.object(config, "private_file_lock", symlink_error_lock, create=True):
                with self.assertRaises(ScheduleError):
                    with schedule_lock(path, exclusive=True):
                        pass

            @contextlib.contextmanager
            def blocking_error_lock(target, *, exclusive=True, blocking=True):
                raise BlockingIOError("already locked")
                yield

            with patch.object(config, "private_file_lock", blocking_error_lock, create=True):
                with self.assertRaises(BlockingIOError):
                    with schedule_lock(path, exclusive=True, blocking=False):
                        pass

            with patch.object(config, "private_file_lock", fake_private_lock, create=True):
                with self.assertRaises(PermissionError):
                    with schedule_lock(path, exclusive=True):
                        raise PermissionError("body error untouched")


if __name__ == "__main__":
    unittest.main()
