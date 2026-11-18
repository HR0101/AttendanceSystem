import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime
from pathlib import Path

from src.config import private_file_lock
from src.scheduler import (
    ScheduleError, calendar_intervals, due_course, load_schedule,
    registration_time, save_schedule, schedule_data, update_schedule, validate_schedule,
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

    def test_schedule_normalizes_fullwidth_and_four_digit_rooms(self):
        raw = schedule_data(SCHEDULE)
        raw["courses"][0]["room"] = "７３０１"
        validated = validate_schedule(raw)
        self.assertEqual(validated["courses"][0]["room"], "731")
        self.assertEqual(validated["courses"][0]["route"], "7301")
        raw["courses"][0]["room"] = "６４２"
        validated = validate_schedule(raw)
        self.assertEqual(validated["courses"][0]["room"], "642")
        self.assertEqual(validated["courses"][0]["route"], "642")
        for bad_room in ("6402", "7312", None):
            with self.subTest(bad_room=bad_room), self.assertRaises(ScheduleError):
                raw["courses"][0]["room"] = bad_room
                validate_schedule(raw)

    def test_load_schedule_tightens_permissions_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = base / "schedule.json"
            path.write_text(json.dumps(schedule_data(SCHEDULE)), encoding="utf-8")
            path.chmod(0o644)

            loaded = load_schedule(path)
            self.assertEqual(len(loaded["courses"]), 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            sym = base / "sym-schedule.json"
            sym.symlink_to(path)
            with self.assertRaisesRegex(ScheduleError, "シンボリックリンク"):
                load_schedule(sym)
            with self.assertRaisesRegex(ScheduleError, "シンボリックリンク"):
                save_schedule(sym, schedule_data(SCHEDULE))

            broken = base / "broken-schedule.json"
            broken.symlink_to(base / "missing.json")
            with self.assertRaisesRegex(ScheduleError, "シンボリックリンク"):
                load_schedule(broken)

    def test_update_schedule_serializes_concurrent_threads_and_subprocesses(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = base / "schedule.json"
            save_schedule(path, schedule_data(SCHEDULE))

            days = ("tue", "wed", "thu", "fri")

            def append_course(day):
                def mutator(current):
                    current["courses"].append({
                        "weekday": day, "start_time": "10:00", "room": "642",
                        "course_name": f"授業-{day}", "enabled": True,
                    })
                    return current
                update_schedule(path, mutator)

            threads = [threading.Thread(target=append_course, args=(day,)) for day in days]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            self.assertEqual({c["weekday"] for c in load_schedule(path)["courses"]}, {"mon", *days})

            # 別プロセスと update_schedule の排他ロック直列化を検証する。
            script = "\n".join([
                "import sys",
                "from pathlib import Path",
                "from src.config import private_file_lock",
                "from src.scheduler import update_schedule",
                "p = Path(sys.argv[1])",
                "def add_sat(cur):",
                "    cur['courses'].append({'weekday': 'sat', 'start_time': '09:00', 'room': '731', 'course_name': '土曜授業', 'enabled': True})",
                "    return cur",
                "with private_file_lock(p):",
                "    print('LOCKED', flush=True)",
                "    sys.stdin.read()",
                "    update_schedule(p, add_sat)",
            ])
            proc = subprocess.Popen(
                [sys.executable, "-c", script, str(path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=str(Path(__file__).resolve().parents[1]),
            )
            try:
                line = proc.stdout.readline().strip()
                if line != "LOCKED":
                    self.fail(f"unexpected lock output {line!r}: {proc.stderr.read()}")
                done = threading.Event()
                errors = []

                def add_sun():
                    try:
                        def mutator(cur):
                            cur["courses"].append({
                                "weekday": "sun", "start_time": "11:00", "room": "642",
                                "course_name": "日曜授業", "enabled": True,
                            })
                            return cur
                        update_schedule(path, mutator)
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        done.set()

                thread = threading.Thread(target=add_sun)
                thread.start()
                self.assertFalse(done.wait(timeout=0.05))
                proc.stdin.close()
                thread.join(timeout=5)
                self.assertEqual(proc.wait(timeout=5), 0, proc.stderr.read())
                self.assertEqual(errors, [])
                self.assertEqual(
                    {c["weekday"] for c in load_schedule(path)["courses"]},
                    {"mon", *days, "sat", "sun"},
                )
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream and not stream.closed:
                        stream.close()
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)

    def test_schedule_lock_file_conflicts_with_flock_and_allows_reentrant_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = base / "schedule.json"
            lock_path = base / "schedule.json.lock"
            save_schedule(path, schedule_data(SCHEDULE))

            # 独立した flock 呼び出しが schedule.json.lock 上で非ブロッキング取得と競合することを確認する。
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                with self.assertRaises(BlockingIOError):
                    with private_file_lock(path, exclusive=True, blocking=False):
                        pass
                with self.assertRaises(BlockingIOError):
                    with private_file_lock(path, exclusive=False, blocking=False):
                        pass
                fcntl.flock(fd, fcntl.LOCK_SH)
                with private_file_lock(path, exclusive=False, blocking=False):
                    pass
                with self.assertRaises(BlockingIOError):
                    with private_file_lock(path, exclusive=True, blocking=False):
                        pass
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

            # 同一スレッド内の再入（update_schedule -> save_schedule）は成功し、blocking=False は拒否される。
            def add_course(day: str):
                def mutator(current):
                    current["courses"].append({
                        "weekday": day, "start_time": "10:00", "room": "642",
                        "course_name": f"授業-{day}", "enabled": True,
                    })
                    return current
                return mutator

            with private_file_lock(path):
                update_schedule(path, add_course("tue"))
                with self.assertRaises(BlockingIOError):
                    with private_file_lock(path, exclusive=True, blocking=False):
                        pass
                with self.assertRaises(BlockingIOError):
                    with private_file_lock(path, exclusive=False, blocking=False):
                        pass

            # 共有ロックから update_schedule（排他ロック昇格）へのネストもデッドロックせず完了する。
            with private_file_lock(path, exclusive=False):
                self.assertEqual(len(load_schedule(path)["courses"]), 2)
                update_schedule(path, add_course("wed"))
            self.assertEqual({c["weekday"] for c in load_schedule(path)["courses"]}, {"mon", "tue", "wed"})

            # 別プロセスが schedule.json.lock を保持している間、blocking=False は BlockingIOError を送出する。
            script = "\n".join([
                "import sys",
                "from pathlib import Path",
                "from src.config import private_file_lock",
                "with private_file_lock(Path(sys.argv[1]), exclusive=True):",
                "    print('LOCKED', flush=True)",
                "    sys.stdin.read()",
            ])
            proc = subprocess.Popen(
                [sys.executable, "-c", script, str(path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=str(Path(__file__).resolve().parents[1]),
            )
            try:
                line = proc.stdout.readline().strip()
                if line != "LOCKED":
                    self.fail(f"unexpected lock output {line!r}: {proc.stderr.read()}")
                with self.assertRaises(BlockingIOError):
                    with private_file_lock(path, exclusive=True, blocking=False):
                        pass
                with self.assertRaises(BlockingIOError):
                    with private_file_lock(path, exclusive=False, blocking=False):
                        pass
                proc.stdin.close()
                self.assertEqual(proc.wait(timeout=5), 0, proc.stderr.read())
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream and not stream.closed:
                        stream.close()
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)

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
