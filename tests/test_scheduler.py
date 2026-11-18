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

import copy
import fcntl
import io
import json
import logging
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import main
import main as app_main
from src import updater as u
from src.config import private_file_lock
from src.credentials import CredentialError
from src.scheduler import (
    LABEL, agent_status, format_schedule_diagnostics, remove_agent, schedule_diagnostics,
    ScheduleError, _claim_path, _run_key, calendar_intervals, claim_scheduled_run,
    due_course, due_scheduled_course, install_agent, load_schedule, next_scheduled_run,
    registration_time, save_schedule, schedule_data, update_schedule, validate_schedule,
)
from src.verification import AttendanceError


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

    def test_due_course_grace_minutes_supports_exact_boundary_without_breaking_default(self):
        course = dict(COURSE, register_time="14:05", register_until="14:05")
        schedule = dict(SCHEDULE, courses=[course])
        self.assertIsNone(due_course(schedule, datetime(2026, 10, 5, 14, 4), grace_minutes=2))
        self.assertEqual(due_course(schedule, datetime(2026, 10, 5, 14, 5), grace_minutes=2), course)
        self.assertEqual(due_course(schedule, datetime(2026, 10, 5, 14, 6), grace_minutes=2), course)
        self.assertEqual(due_course(schedule, datetime(2026, 10, 5, 14, 7), grace_minutes=2), course)
        self.assertIsNone(due_course(schedule, datetime(2026, 10, 5, 14, 8), grace_minutes=2))
        self.assertIsNone(due_course(schedule, datetime(2026, 10, 5, 14, 6)))


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


class ScheduledExecutionAndLaunchAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "app"
        self.base.mkdir()
        (self.base / "att").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (self.base / "att").chmod(0o755)
        self.plist_path = self.root / "LaunchAgents" / "local.attendance.tool.plist"

    def test_minute_boundary_crossing_registers_once_and_skips_same_day_repeats(self):
        for offset in (1, 2):
            with self.subTest(elapsed=offset):
                base = self.root / f"case-{offset}"
                base.mkdir()
                course = dict(COURSE, register_time="14:05", register_until="14:15")
                schedule = dict(SCHEDULE, courses=[course])
                day = date(2026, 10, 5)
                chosen_hour, chosen_minute = map(int, registration_time(course, day).split(":"))
                chosen_dt = datetime(2026, 10, 5, chosen_hour, chosen_minute, 59)

                # HH:MM:59 の起動が処理到達時に HH:(MM+offset):00 を跨いだケースを再現する。
                arrived_dt = chosen_dt + timedelta(minutes=offset)
                self.assertEqual(due_scheduled_course(base, schedule, arrived_dt), course)

                # 同じ日の以降の分・同じ猶予範囲内の再実行はすべてスキップされる。
                self.assertIsNone(due_scheduled_course(base, schedule, chosen_dt + timedelta(minutes=offset + 1)))
                for minute in range(5, 16):
                    self.assertIsNone(due_scheduled_course(base, schedule, datetime(2026, 10, 5, 14, minute)))

                # 翌週の同じ授業日は別日付なので再び実行対象になる。
                next_day = date(2026, 10, 12)
                nh, nm = map(int, registration_time(course, next_day).split(":"))
                self.assertEqual(due_scheduled_course(base, schedule, datetime(2026, 10, 12, nh, nm)), course)

                runs_path = base / "logs" / "schedule-runs.json"
                self.assertEqual((base / "logs").stat().st_mode & 0o777, 0o700)
                self.assertEqual(runs_path.stat().st_mode & 0o777, 0o600)
                runs = json.loads(runs_path.read_text(encoding="utf-8"))["runs"]
                self.assertEqual(set(runs), {"mon|14:00|2026-10-05", "mon|14:00|2026-10-12"})

    def test_midnight_boundary_crossing_for_range_uses_course_date_and_deduplicates(self):
        course = dict(COURSE, weekday="sun", start_time="23:50", register_time="23:59", register_until="23:59")
        schedule = dict(SCHEDULE, active_from=date(2026, 10, 4), active_until=date(2026, 10, 4), courses=[course])
        self.assertEqual(due_scheduled_course(self.base, schedule, datetime(2026, 10, 5, 0, 0)), course)
        self.assertIsNone(due_scheduled_course(self.base, schedule, datetime(2026, 10, 5, 0, 1)))

    def test_adjacent_courses_within_grace_window_do_not_conflict_after_first_runs(self):
        first = dict(COURSE, start_time="14:00", register_time="14:05", register_until="14:05")
        second = dict(COURSE, start_time="14:01", course_name="次授業", register_time="14:06", register_until="14:06")
        schedule = dict(SCHEDULE, courses=[first, second])
        self.assertEqual(due_scheduled_course(self.base, schedule, datetime(2026, 10, 5, 14, 5)), first)
        self.assertEqual(due_scheduled_course(self.base, schedule, datetime(2026, 10, 5, 14, 6)), second)
        self.assertIsNone(due_scheduled_course(self.base, schedule, datetime(2026, 10, 5, 14, 7)))

    def test_genuine_overlap_still_raises_without_recording_receipt(self):
        first = dict(COURSE, start_time="14:00", register_time="14:05", register_until="14:05")
        second = dict(COURSE, start_time="14:01", course_name="重複授業", register_time="14:05", register_until="14:05")
        schedule = dict(SCHEDULE, courses=[first, second])
        with self.assertRaises(ScheduleError):
            due_scheduled_course(self.base, schedule, datetime(2026, 10, 5, 14, 5))
        self.assertFalse((self.base / "logs" / "schedule-runs.json").exists())

    def test_receipt_prunes_old_entries_recovers_corrupt_json_and_rejects_symlinks(self):
        log_dir = self.base / "logs"
        log_dir.mkdir(mode=0o700)
        runs_path = log_dir / "schedule-runs.json"
        runs_path.write_text(json.dumps({
            "runs": {
                "mon|14:00|2026-09-14": {"weekday": "mon", "start_time": "14:00", "course_date": "2026-09-14"},
                "mon|14:00|2026-09-28": {"weekday": "mon", "start_time": "14:00", "course_date": "2026-09-28"},
            }
        }), encoding="utf-8")
        course = dict(COURSE, register_time="14:05", register_until="14:05")
        schedule = dict(SCHEDULE, courses=[course])
        self.assertEqual(due_scheduled_course(self.base, schedule, datetime(2026, 10, 5, 14, 5)), course)
        kept = json.loads(runs_path.read_text(encoding="utf-8"))["runs"]
        self.assertNotIn("mon|14:00|2026-09-14", kept)
        self.assertIn("mon|14:00|2026-09-28", kept)
        self.assertIn("mon|14:00|2026-10-05", kept)

        # 破損JSONでも停止せず再構築できる。
        runs_path.write_text("{broken", encoding="utf-8")
        self.assertEqual(due_scheduled_course(self.base, schedule, datetime(2026, 10, 12, 14, 5)), course)
        self.assertIn("mon|14:00|2026-10-12", json.loads(runs_path.read_text(encoding="utf-8"))["runs"])

        # シンボリックリンクは拒否し、リンク先を上書きしない。
        outside = self.root / "outside.json"
        outside.write_text('{"keep": true}', encoding="utf-8")
        runs_path.unlink()
        runs_path.symlink_to(outside)
        with self.assertRaises(ScheduleError):
            due_scheduled_course(self.base, schedule, datetime(2026, 10, 19, 14, 5))
        self.assertEqual(outside.read_text(encoding="utf-8"), '{"keep": true}')

    def _cleanup_logger(self):
        logger = logging.getLogger("attendance_tool")
        for handler in list(logger.handlers):
            handler.close()
        logger.handlers.clear()

    def _run_scheduled_main(
        self,
        argv: list[str],
        schedule: dict,
        config: dict,
        now: datetime,
        *,
        base: Path | None = None,
        run_attendance_mock=None,
        get_credentials_mock=None,
        open_page_mock=None,
        setup_logging_mock=None,
    ) -> int:
        target_base = base or self.base

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        def default_credentials(cfg, *args, **kwargs):
            cfg["_active_username"] = cfg.get("default_user", "test")
            return "TEST001", "dummy"

        self.addCleanup(self._cleanup_logger)
        cred_patch = (
            patch.object(app_main, "get_credentials", get_credentials_mock)
            if get_credentials_mock is not None
            else patch.object(app_main, "get_credentials", side_effect=default_credentials)
        )
        page_patch = (
            patch("src.browser.open_page", open_page_mock)
            if open_page_mock is not None
            else patch("src.browser.open_page", return_value=nullcontext(Mock()))
        )
        attend_patch = (
            patch("src.attendance.run_attendance", run_attendance_mock)
            if run_attendance_mock is not None
            else patch("src.attendance.run_attendance", return_value="出席登録成功")
        )
        log_patch = (
            patch.object(app_main, "setup_logging", setup_logging_mock)
            if setup_logging_mock is not None
            else nullcontext()
        )
        with patch.object(app_main, "__file__", str(target_base / "main.py")), \
                patch.object(app_main, "datetime", FixedDatetime), \
                patch.object(app_main, "load_config", side_effect=lambda *_: copy.deepcopy(config)), \
                patch.object(app_main, "load_schedule", return_value=schedule), \
                cred_patch, page_patch, attend_patch, log_patch, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = app_main.main(argv)
        if setup_logging_mock is None:
            self._cleanup_logger()
        return code

    def test_main_scheduled_uses_due_scheduled_course_for_boundary_and_dedup(self):
        course = dict(COURSE, register_time="14:05", register_until="14:05")
        schedule = dict(SCHEDULE, courses=[course])
        config = {"users": [{"username": "test", "student_id": "TEST001"}], "default_user": "test", "dry_run": False}

        attend = Mock(return_value="出席登録成功")
        self.assertEqual(
            self._run_scheduled_main(["--scheduled"], schedule, config, datetime(2026, 10, 5, 14, 6), run_attendance_mock=attend),
            0,
        )
        attend.assert_called_once()
        self.assertEqual(
            self._run_scheduled_main(["--scheduled"], schedule, config, datetime(2026, 10, 5, 14, 7), run_attendance_mock=attend),
            0,
        )
        attend.assert_called_once()

    def test_scheduled_check_and_config_dry_run_do_not_consume_live_run(self):
        course = dict(COURSE, register_time="14:05", register_until="14:05")
        schedule = dict(SCHEDULE, courses=[course])
        now = datetime(2026, 10, 5, 14, 5)
        observed_dry_runs = []

        def fake_attend(page, cfg, *args, **kwargs):
            observed_dry_runs.append(cfg.get("dry_run"))
            return "未出席（確認のみ）" if cfg.get("dry_run") else "出席登録成功"

        # 1. --scheduled --check (CLI指定) は記録を消費しない。
        live_config = {"users": [{"username": "test", "student_id": "TEST001"}], "default_user": "test", "dry_run": False}
        self.assertEqual(
            self._run_scheduled_main(["--scheduled", "--check"], schedule, live_config, now, run_attendance_mock=Mock(side_effect=fake_attend)),
            0,
        )
        # 2. config["dry_run"] == True の --scheduled も記録を消費しない。
        dry_config = dict(live_config, dry_run=True)
        self.assertEqual(
            self._run_scheduled_main(["--scheduled"], schedule, dry_config, now, run_attendance_mock=Mock(side_effect=fake_attend)),
            0,
        )
        self.assertEqual(observed_dry_runs, [True, True])
        self.assertFalse((self.base / "logs" / "schedule-runs.json").exists())

        # 3. 直後の本番 --scheduled は実行され、記録が確定して以降は抑止される。
        live_attend = Mock(side_effect=fake_attend)
        self.assertEqual(
            self._run_scheduled_main(["--scheduled"], schedule, live_config, now, run_attendance_mock=live_attend),
            0,
        )
        self.assertEqual(observed_dry_runs, [True, True, False])
        self.assertTrue((self.base / "logs" / "schedule-runs.json").is_file())
        self.assertEqual(
            self._run_scheduled_main(["--scheduled"], schedule, live_config, now + timedelta(minutes=1), run_attendance_mock=live_attend),
            0,
        )
        live_attend.assert_called_once()

    def test_scheduled_pre_submit_and_non_consuming_failures_allow_retry(self):
        class TimeoutError(Exception):
            pass

        course = dict(COURSE, register_time="14:05", register_until="14:05")
        schedule = dict(SCHEDULE, courses=[course])
        config = {"users": [{"username": "test", "student_id": "TEST001"}], "default_user": "test", "dry_run": False}
        now = datetime(2026, 10, 5, 14, 5)

        # 認証エラー
        self.assertEqual(
            self._run_scheduled_main(
                ["--scheduled"], schedule, config, now,
                get_credentials_mock=Mock(side_effect=CredentialError("Keychain missing")),
                run_attendance_mock=Mock(),
            ),
            1,
        )
        # ブラウザ起動失敗
        self.assertEqual(
            self._run_scheduled_main(
                ["--scheduled"], schedule, config, now,
                open_page_mock=Mock(side_effect=RuntimeError("browser failed")),
                run_attendance_mock=Mock(),
            ),
            1,
        )
        # 授業確認エラー・タイムアウト
        for exc in (AttendanceError("errorModal"), TimeoutError("timed out")):
            self.assertEqual(
                self._run_scheduled_main(
                    ["--scheduled"], schedule, config, now,
                    run_attendance_mock=Mock(side_effect=exc),
                ),
                1,
            )
        # 受付対象なし・確認のみ結果
        for non_consuming in ("出席受付対象なし（受付時間外の可能性・出席操作なし）", "未出席（確認のみ）"):
            self.assertEqual(
                self._run_scheduled_main(
                    ["--scheduled"], schedule, config, now,
                    run_attendance_mock=Mock(return_value=non_consuming),
                ),
                0,
            )
        self.assertFalse((self.base / "logs" / "schedule-runs.json").exists())

        # その後の再試行で登録成功したら当日分が確定する。
        attend = Mock(return_value="出席登録成功")
        self.assertEqual(
            self._run_scheduled_main(["--scheduled"], schedule, config, now + timedelta(minutes=1), run_attendance_mock=attend),
            0,
        )
        attend.assert_called_once()
        self.assertTrue((self.base / "logs" / "schedule-runs.json").is_file())
        self.assertEqual(
            self._run_scheduled_main(["--scheduled"], schedule, config, now + timedelta(minutes=2), run_attendance_mock=attend),
            0,
        )
        attend.assert_called_once()

    def test_scheduled_non_completed_outcomes_do_not_consume_slot_and_allow_retry(self):
        course = dict(COURSE, register_time="14:05", register_until="14:05")
        schedule = dict(SCHEDULE, courses=[course])
        config = {"users": [{"username": "test", "student_id": "TEST001"}], "default_user": "test", "dry_run": False}
        now = datetime(2026, 10, 5, 14, 5)
        expected_key = _run_key(course["weekday"], course["start_time"], now.date())
        non_completed_outcomes = (
            "履修登録なし（出席操作なし）",
            "履修登録なし・出席済み表示（仮登録の可能性、要確認）",
            "履修登録なし（登録後に警告を検出・要確認）",
            "出席受付対象なし（受付時間外の可能性・出席操作なし）",
            "未出席（確認のみ）",
        )

        for idx, outcome in enumerate(non_completed_outcomes):
            with self.subTest(outcome=outcome):
                case_base = self.root / f"non-completed-{idx}"
                case_base.mkdir()
                attend = Mock(side_effect=[outcome, "出席登録成功"])

                self.assertEqual(
                    self._run_scheduled_main(
                        ["--scheduled"], schedule, config, now,
                        base=case_base, run_attendance_mock=attend,
                    ),
                    0,
                )
                self.assertEqual(attend.call_count, 1)
                self.assertFalse((case_base / "logs" / "schedule-runs.json").exists())
                self.assertEqual(list((case_base / "logs").glob(".schedule-claim-*.lock")), [])
                self.assertEqual(due_scheduled_course(case_base, schedule, now, record=False), course)

                # 状態修正後に受付枠・猶予枠内で再試行すると登録を実行し、成功時に実行記録を作成する。
                retry_now = now + timedelta(minutes=1)
                self.assertEqual(
                    self._run_scheduled_main(
                        ["--scheduled"], schedule, config, retry_now,
                        base=case_base, run_attendance_mock=attend,
                    ),
                    0,
                )
                self.assertEqual(attend.call_count, 2)
                runs_path = case_base / "logs" / "schedule-runs.json"
                self.assertTrue(runs_path.is_file())
                self.assertIn(expected_key, json.loads(runs_path.read_text(encoding="utf-8"))["runs"])
                self.assertEqual(list((case_base / "logs").glob(".schedule-claim-*.lock")), [])
                self.assertIsNone(due_scheduled_course(case_base, schedule, retry_now, record=False))

                # 確定後は同じ猶予枠内で再実行されない。
                self.assertEqual(
                    self._run_scheduled_main(
                        ["--scheduled"], schedule, config, now + timedelta(minutes=2),
                        base=case_base, run_attendance_mock=attend,
                    ),
                    0,
                )
                self.assertEqual(attend.call_count, 2)

    def test_scheduled_unenrolled_then_user_fix_retry_succeeds(self):
        course = dict(COURSE, register_time="14:05", register_until="14:05")
        schedule = dict(SCHEDULE, courses=[course])
        now = datetime(2026, 10, 5, 14, 5)
        config_a = {
            "users": [
                {"username": "A", "student_id": "A1"},
                {"username": "B", "student_id": "B2"},
            ],
            "default_user": "A",
            "dry_run": False,
        }
        config_b = dict(config_a, default_user="B")
        seen_student_ids = []

        def fake_credentials(cfg, *args, **kwargs):
            active = cfg["default_user"]
            cfg["_active_username"] = active
            student_id = next(user["student_id"] for user in cfg["users"] if user["username"] == active)
            return student_id, "dummy"

        def fake_attend(page, cfg, student_id, *args, **kwargs):
            seen_student_ids.append(student_id)
            return "履修登録なし（出席操作なし）" if student_id == "A1" else "出席登録成功"

        # 既定ユーザーAで未履修になっても当日枠を消費しない。
        self.assertEqual(
            self._run_scheduled_main(
                ["--scheduled"], schedule, config_a, now,
                get_credentials_mock=Mock(side_effect=fake_credentials),
                run_attendance_mock=Mock(side_effect=fake_attend),
            ),
            0,
        )
        self.assertEqual(seen_student_ids, ["A1"])
        self.assertFalse((self.base / "logs" / "schedule-runs.json").exists())
        self.assertEqual(list((self.base / "logs").glob(".schedule-claim-*.lock")), [])

        # 既定ユーザーをBへ変更して受付枠内に再試行すると登録成功し、以降同日は再実行されない。
        self.assertEqual(
            self._run_scheduled_main(
                ["--scheduled"], schedule, config_b, now + timedelta(minutes=1),
                get_credentials_mock=Mock(side_effect=fake_credentials),
                run_attendance_mock=Mock(side_effect=fake_attend),
            ),
            0,
        )
        self.assertEqual(seen_student_ids, ["A1", "B2"])
        self.assertTrue((self.base / "logs" / "schedule-runs.json").is_file())
        self.assertEqual(list((self.base / "logs").glob(".schedule-claim-*.lock")), [])

        self.assertEqual(
            self._run_scheduled_main(
                ["--scheduled"], schedule, config_b, now + timedelta(minutes=2),
                get_credentials_mock=Mock(side_effect=fake_credentials),
                run_attendance_mock=Mock(side_effect=fake_attend),
            ),
            0,
        )
        self.assertEqual(seen_student_ids, ["A1", "B2"])

    def test_scheduled_confirmed_completion_outcomes_consume_daily_slot(self):
        course = dict(COURSE, register_time="14:05", register_until="14:05")
        schedule = dict(SCHEDULE, courses=[course])
        config = {"users": [{"username": "test", "student_id": "TEST001"}], "default_user": "test", "dry_run": False}
        now = datetime(2026, 10, 5, 14, 5)
        expected_key = _run_key(course["weekday"], course["start_time"], now.date())

        for idx, outcome in enumerate(("出席済み", "出席登録成功")):
            with self.subTest(outcome=outcome):
                case_base = self.root / f"confirmed-{idx}"
                case_base.mkdir()
                attend = Mock(return_value=outcome)

                # --check 時は「出席済み」が返っても当日枠を消費しない。
                self.assertEqual(
                    self._run_scheduled_main(
                        ["--scheduled", "--check"], schedule, config, now,
                        base=case_base, run_attendance_mock=attend,
                    ),
                    0,
                )
                self.assertEqual(attend.call_count, 1)
                self.assertFalse((case_base / "logs" / "schedule-runs.json").exists())

                # 本番実行で完了結果が返ると当日記録を確定し、同日の猶予枠内再実行を抑止する。
                self.assertEqual(
                    self._run_scheduled_main(
                        ["--scheduled"], schedule, config, now,
                        base=case_base, run_attendance_mock=attend,
                    ),
                    0,
                )
                self.assertEqual(attend.call_count, 2)
                runs_path = case_base / "logs" / "schedule-runs.json"
                self.assertTrue(runs_path.is_file())
                self.assertIn(expected_key, json.loads(runs_path.read_text(encoding="utf-8"))["runs"])
                self.assertEqual(list((case_base / "logs").glob(".schedule-claim-*.lock")), [])
                self.assertIsNone(due_scheduled_course(case_base, schedule, now + timedelta(minutes=1), record=False))

                self.assertEqual(
                    self._run_scheduled_main(
                        ["--scheduled"], schedule, config, now + timedelta(minutes=1),
                        base=case_base, run_attendance_mock=attend,
                    ),
                    0,
                )
                self.assertEqual(attend.call_count, 2)

    def test_concurrent_scheduled_runs_exclude_in_flight_claim_and_release_cleanly(self):
        course = dict(COURSE, register_time="14:05", register_until="14:05")
        schedule = dict(SCHEDULE, courses=[course])
        config = {"users": [{"username": "test", "student_id": "TEST001"}], "default_user": "test", "dry_run": False}
        now = datetime(2026, 10, 5, 14, 5)
        entered = threading.Event()
        proceed = threading.Event()
        should_fail = [True]
        results = {}

        def slow_attend(*args, **kwargs):
            entered.set()
            proceed.wait(timeout=5)
            if should_fail[0]:
                raise AttendanceError("transient failure")
            return "出席登録成功"

        attend = Mock(side_effect=slow_attend)
        log_mock = Mock(return_value=Mock())

        def run_worker(name: str):
            results[name] = self._run_scheduled_main(
                ["--scheduled"], schedule, config, now,
                run_attendance_mock=attend, setup_logging_mock=log_mock,
            )

        # 1. 実行中の予約は同時起動の --scheduled と読取専用の双方から排他され、失敗時に解放される。
        first = threading.Thread(target=run_worker, args=("first",))
        first.start()
        self.assertTrue(entered.wait(timeout=5))
        self.assertIsNone(due_scheduled_course(self.base, schedule, now, record=False))
        second = threading.Thread(target=run_worker, args=("second",))
        second.start()
        second.join(timeout=5)
        self.assertEqual(results["second"], 0)
        self.assertEqual(attend.call_count, 1)

        proceed.set()
        first.join(timeout=5)
        self.assertEqual(results["first"], 1)
        self.assertFalse((self.base / "logs" / "schedule-runs.json").exists())
        self.assertEqual(list((self.base / "logs").glob(".schedule-claim-*.lock")), [])

        # 2. 解放後の再試行では同時実行を抑止しつつ、成功時に記録確定・ロック削除される。
        entered.clear()
        proceed.clear()
        should_fail[0] = False
        third = threading.Thread(target=run_worker, args=("third",))
        third.start()
        self.assertTrue(entered.wait(timeout=5))
        fourth = threading.Thread(target=run_worker, args=("fourth",))
        fourth.start()
        fourth.join(timeout=5)
        self.assertEqual(results["fourth"], 0)
        self.assertEqual(attend.call_count, 2)

        proceed.set()
        third.join(timeout=5)
        self.assertEqual(results["third"], 0)
        self.assertTrue((self.base / "logs" / "schedule-runs.json").is_file())
        self.assertEqual(list((self.base / "logs").glob(".schedule-claim-*.lock")), [])
        self.assertIsNone(due_scheduled_course(self.base, schedule, now + timedelta(minutes=1), record=False))

    def test_due_scheduled_course_read_only_and_claim_cleanup_edge_cases(self):
        course = dict(COURSE, register_time="14:05", register_until="14:05")
        schedule = dict(SCHEDULE, courses=[course])
        now = datetime(2026, 10, 5, 14, 5)

        # logs 未作成時の record=False は logs ディレクトリも作成しない。
        self.assertEqual(due_scheduled_course(self.base, schedule, now, record=False), course)
        self.assertFalse((self.base / "logs").exists())

        # claim_scheduled_run が未確定のまま抜けると記録は作られず、解除後の commit() は拒否される。
        with claim_scheduled_run(self.base, schedule, now) as (claimed, commit):
            self.assertEqual(claimed, course)
            self.assertIsNone(due_scheduled_course(self.base, schedule, now, record=False))
        self.assertFalse((self.base / "logs" / "schedule-runs.json").exists())
        with self.assertRaises(ScheduleError):
            commit()

        # 古い未使用 .schedule-claim-*.lock は掃除され、新しいもの・ロック中・シンボリックリンクは適切に扱う。
        import fcntl
        log_dir = self.base / "logs"
        stale = log_dir / ".schedule-claim-stale.lock"
        fresh = log_dir / ".schedule-claim-fresh.lock"
        stale_held = log_dir / ".schedule-claim-held.lock"
        stale.write_text("", encoding="utf-8")
        fresh.write_text("", encoding="utf-8")
        stale_held.write_text("", encoding="utf-8")
        old_ts = (now - timedelta(days=15)).timestamp()
        new_ts = (now - timedelta(days=1)).timestamp()
        os.utime(stale, (old_ts, old_ts))
        os.utime(fresh, (new_ts, new_ts))
        os.utime(stale_held, (old_ts, old_ts))

        held_fd = os.open(stale_held, os.O_RDWR)
        try:
            fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with claim_scheduled_run(self.base, schedule, now) as (claimed, commit):
                self.assertEqual(claimed, course)
                self.assertFalse(stale.exists())
                self.assertTrue(fresh.exists())
                self.assertTrue(stale_held.exists())
                commit()
        finally:
            fcntl.flock(held_fd, fcntl.LOCK_UN)
            os.close(held_fd)
        self.assertTrue((log_dir / "schedule-runs.json").is_file())

        # 先行授業が実行中（in-flight）でも、猶予内で開始した隣接授業は競合せずに取得できる。
        first = dict(COURSE, start_time="14:00", register_time="14:05", register_until="14:05")
        second = dict(COURSE, start_time="14:01", course_name="次授業", register_time="14:06", register_until="14:06")
        adj_schedule = dict(SCHEDULE, courses=[first, second])
        adj_day = datetime(2026, 10, 19, 14, 5)
        with claim_scheduled_run(self.base, adj_schedule, adj_day) as (claimed_first, _):
            self.assertEqual(claimed_first, first)
            with claim_scheduled_run(self.base, adj_schedule, adj_day + timedelta(minutes=1)) as (claimed_second, commit_second):
                self.assertEqual(claimed_second, second)
                commit_second()

        # 予約ロックがシンボリックリンクなら拒否する。
        next_day = datetime(2026, 10, 12, 14, 5)
        key = _run_key(course["weekday"], course["start_time"], next_day.date())
        outside = self.root / "outside-claim.lock"
        outside.write_text("keep", encoding="utf-8")
        _claim_path(log_dir, key).symlink_to(outside)
        with self.assertRaises(ScheduleError):
            due_scheduled_course(self.base, schedule, next_day, record=False)
        with self.assertRaises(ScheduleError):
            with claim_scheduled_run(self.base, schedule, next_day):
                pass
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_pre_startup_errors_under_scheduled_write_safe_launchagent_log(self):
        with patch.object(u, "start_update_check") as start_check:
            # 1. activate_runtime 時の UpdateError
            with patch.object(u, "activate_runtime", side_effect=u.UpdateError("依存パッケージの環境が不正\nSECRET_PATH")), \
                    redirect_stderr(io.StringIO()) as err:
                code = u.run_application(self.base, ["--scheduled"], use_runtime=True)
            self.assertEqual(code, 1)
            self.assertIn("更新失敗:", err.getvalue())
            log_path = self.base / "logs" / "launchagent.log"
            self.assertEqual((self.base / "logs").stat().st_mode & 0o777, 0o700)
            self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("UpdateError:", content)
            self.assertIn("依存パッケージの環境が不正", content)
            self.assertEqual(len(content.splitlines()), 1)

            # 2. import_module 時の ModuleNotFoundError
            with patch.object(u.importlib, "import_module", side_effect=ModuleNotFoundError("No module named 'keyring'", name="keyring")), \
                    redirect_stderr(io.StringIO()) as err:
                code = u.run_application(self.base, ["--scheduled"])
            self.assertEqual(code, 1)
            self.assertIn("keyring", err.getvalue())
            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("ModuleNotFoundError:", lines[1])
            self.assertIn("keyring", lines[1])

            # 3. 破損した runtime.json（実 activate_runtime 経路）での UpdateError
            runtime_base = self.root / "runtime-bad"
            (runtime_base / ".updates").mkdir(parents=True)
            (runtime_base / ".updates" / "runtime.json").write_text('{"id": "bad"}', encoding="utf-8")
            with redirect_stderr(io.StringIO()) as err:
                code = u.run_application(runtime_base, ["--scheduled"], use_runtime=True)
            self.assertEqual(code, 1)
            self.assertIn("更新失敗:", err.getvalue())
            self.assertIn("UpdateError:", (runtime_base / "logs" / "launchagent.log").read_text(encoding="utf-8"))

            # 4. 予期しない例外でも生の秘匿情報やスタックトレースは書かない。
            with patch.object(u.importlib, "import_module", side_effect=RuntimeError("sensitive-token-value")), \
                    redirect_stderr(io.StringIO()) as err:
                code = u.run_application(self.base, ["--scheduled"])
            self.assertEqual(code, 1)
            self.assertNotIn("sensitive-token-value", err.getvalue())
            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertIn("RuntimeError", lines[2])
            self.assertNotIn("sensitive-token-value", lines[2])
            self.assertNotIn("Traceback", lines[2])
            start_check.assert_not_called()

    def test_pre_startup_log_refuses_symlink_and_non_scheduled_does_not_create_it(self):
        non_scheduled = self.root / "non-scheduled"
        non_scheduled.mkdir()
        with patch.object(u, "start_update_check") as start_check, \
                patch.object(u.importlib, "import_module", side_effect=ModuleNotFoundError("missing", name="keyring")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(u.run_application(non_scheduled, ["--check", "731"]), 1)
        start_check.assert_called_once_with(non_scheduled)
        self.assertFalse((non_scheduled / "logs" / "launchagent.log").exists())

        (self.base / "logs").mkdir(mode=0o700)
        outside = self.root / "outside.log"
        outside.write_text("keep", encoding="utf-8")
        (self.base / "logs" / "launchagent.log").symlink_to(outside)
        with patch.object(u, "start_update_check") as start_check, \
                patch.object(u.importlib, "import_module", side_effect=u.UpdateError("bad runtime")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(u.run_application(self.base, ["--scheduled"]), 1)
        start_check.assert_not_called()
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_install_agent_configures_launchagent_log_paths_and_permissions(self):
        schedule = dict(SCHEDULE, active_until=date.today() + timedelta(days=30), courses=[dict(COURSE)])
        with patch("src.scheduler.agent_path", return_value=self.plist_path), \
                patch("src.scheduler.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            installed = install_agent(self.base, schedule)
        self.assertEqual(installed, self.plist_path)
        self.assertEqual((self.base / "logs").stat().st_mode & 0o777, 0o700)
        log_path = self.base / "logs" / "launchagent.log"
        self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.plist_path.stat().st_mode & 0o777, 0o600)
        plist = plistlib.loads(self.plist_path.read_bytes())
        self.assertEqual(plist["StandardOutPath"], str(log_path))
        self.assertEqual(plist["StandardErrorPath"], str(log_path))
        run.assert_called_once_with(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(self.plist_path)], capture_output=True, check=False)

    def test_install_agent_bootstrap_failure_restores_previous_plist_and_logs_diagnostics(self):
        schedule = dict(SCHEDULE, active_until=date.today() + timedelta(days=30), courses=[dict(COURSE)])
        self.plist_path.parent.mkdir(parents=True)
        # 旧形式（StandardOutPath なし）の plist であっても所有判定でき、失敗時に復元される。
        previous_plist = {
            "Label": "local.attendance.tool",
            "ProgramArguments": [str(self.base / "att"), "--scheduled"],
            "WorkingDirectory": str(self.base),
            "StartCalendarInterval": [{"Weekday": 1, "Hour": 14, "Minute": 5}],
            "RunAtLoad": False,
            "Umask": 0o077,
        }
        previous_data = plistlib.dumps(previous_plist)
        self.plist_path.write_bytes(previous_data)
        self.plist_path.chmod(0o600)

        results = [
            subprocess.CompletedProcess([], 0),  # bootout
            subprocess.CompletedProcess([], 5, stderr=b"Bootstrap failed: 5: Input/output error\x1b[31m\nsecond-line"),  # new bootstrap fails
            subprocess.CompletedProcess([], 0),  # rollback bootstrap succeeds
        ]
        with patch("src.scheduler.agent_path", return_value=self.plist_path), \
                patch("src.scheduler.subprocess.run", side_effect=results) as run, \
                self.assertRaises(ScheduleError) as caught:
            install_agent(self.base, schedule)

        self.assertEqual(self.plist_path.read_bytes(), previous_data)
        self.assertEqual(self.plist_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ["bootout", "bootstrap", "bootstrap"])
        self.assertIn("code=5", str(caught.exception))
        self.assertIn("Bootstrap failed: 5: Input/output error", str(caught.exception))
        self.assertNotIn("second-line", str(caught.exception))
        log_text = (self.base / "logs" / "launchagent.log").read_text(encoding="utf-8")
        self.assertIn("code=5: Bootstrap failed: 5: Input/output error", log_text)
        self.assertNotIn("second-line", log_text)

    def test_install_agent_bootstrap_failure_removes_new_plist_when_none_existed(self):
        schedule = dict(SCHEDULE, active_until=date.today() + timedelta(days=30), courses=[dict(COURSE)])
        with patch("src.scheduler.agent_path", return_value=self.plist_path), \
                patch("src.scheduler.subprocess.run", return_value=subprocess.CompletedProcess([], 1, stderr="Load failed")) as run, \
                self.assertRaises(ScheduleError) as caught:
            install_agent(self.base, schedule)

        self.assertFalse(self.plist_path.exists())
        run.assert_called_once()
        self.assertIn("code=1: Load failed", str(caught.exception))
        log_text = (self.base / "logs" / "launchagent.log").read_text(encoding="utf-8")
        self.assertIn("code=1: Load failed", log_text)

    def test_install_agent_bootstrap_and_restore_both_failing_still_restores_plist_file(self):
        schedule = dict(SCHEDULE, active_until=date.today() + timedelta(days=30), courses=[dict(COURSE)])
        self.plist_path.parent.mkdir(parents=True)
        previous_data = plistlib.dumps({
            "Label": "local.attendance.tool",
            "ProgramArguments": [str(self.base / "att"), "--scheduled"],
        })
        self.plist_path.write_bytes(previous_data)

        results = [
            subprocess.CompletedProcess([], 0),  # bootout
            subprocess.CompletedProcess([], 5, stderr=b"new failed"),
            subprocess.CompletedProcess([], 3, stderr=b"restore failed"),
        ]
        with patch("src.scheduler.agent_path", return_value=self.plist_path), \
                patch("src.scheduler.subprocess.run", side_effect=results), \
                self.assertRaises(ScheduleError) as caught:
            install_agent(self.base, schedule)

        self.assertEqual(self.plist_path.read_bytes(), previous_data)
        self.assertIn("再登録失敗", str(caught.exception))
        log_text = (self.base / "logs" / "launchagent.log").read_text(encoding="utf-8")
        self.assertIn("new failed", log_text)
        self.assertIn("restore failed", log_text)

    def test_install_agent_rejects_symlink_logs_and_rotates_oversized_log(self):
        schedule = dict(SCHEDULE, active_until=date.today() + timedelta(days=30), courses=[dict(COURSE)])
        outside = self.root / "outside-dir"
        outside.mkdir()
        (self.base / "logs").symlink_to(outside)
        with patch("src.scheduler.agent_path", return_value=self.plist_path), \
                patch("src.scheduler.subprocess.run") as run, \
                self.assertRaises(ScheduleError):
            install_agent(self.base, schedule)
        run.assert_not_called()

        (self.base / "logs").unlink()
        (self.base / "logs").mkdir(mode=0o700)
        log_path = self.base / "logs" / "launchagent.log"
        log_path.write_bytes(b"x" * (256 * 1024 + 10))
        with patch("src.scheduler.agent_path", return_value=self.plist_path), \
                patch("src.scheduler.subprocess.run", return_value=subprocess.CompletedProcess([], 0)):
            install_agent(self.base, schedule)
        backup = self.base / "logs" / "launchagent.log.1"
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(log_path.stat().st_size, 0)
        self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)


class ScheduleDiagnosticsAndAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.plist = self.base / "LaunchAgents" / f"{LABEL}.plist"
        self.config = {
            "login_url": "https://example.test/attendance/login",
            "top_url": "https://example.test/attendance/top",
            "users": [{"username": "taro", "student_id": "TEST001"}],
            "default_user": "taro",
            "browser": {"headless": True, "timeout_ms": 15000},
            "dry_run": False,
        }
        (self.base / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        save_schedule(self.base / "schedule.json", schedule_data(SCHEDULE))

    def write_plist(self, payload: dict | bytes):
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, bytes):
            self.plist.write_bytes(payload)
        else:
            self.plist.write_bytes(plistlib.dumps(payload))

    def run_status(self, *, launchctl_code=0, password="secret-not-leaked", today=date(2026, 10, 5)):
        backend = Mock(get_password=Mock(return_value=password))
        proc = Mock(returncode=launchctl_code)
        output = io.StringIO()
        with patch.object(main, "__file__", str(self.base / "main.py")), \
                patch("src.scheduler.agent_path", return_value=self.plist), \
                patch("src.scheduler.subprocess.run", return_value=proc), \
                patch("src.credentials.keychain_backend", return_value=backend), \
                patch("src.scheduler._today", return_value=today), \
                redirect_stdout(output):
            code = main.main(["schedule", "status"])
        return code, output.getvalue()

    def test_schedule_status_detects_in_sync_and_drift_without_leaking_secret(self):
        self.write_plist({
            "Label": LABEL,
            "ProgramArguments": [str(self.base / "att"), "--scheduled"],
            "StartCalendarInterval": calendar_intervals([COURSE]),
        })
        code, out = self.run_status()
        self.assertEqual(code, 0)
        self.assertIn("自動実行: 有効", out)
        self.assertIn("一致", out)
        self.assertIn("Keychain パスワード: OK", out)
        self.assertNotIn("secret-not-leaked", out)

        # schedule.json を編集しただけで install し忘れた場合の設定ずれを検知する。
        updated = schedule_data(SCHEDULE)
        updated["courses"][0]["start_time"] = "15:00"
        save_schedule(self.base / "schedule.json", updated)
        code, out = self.run_status()
        self.assertEqual(code, 0)
        self.assertIn("設定ずれ: att schedule install を実行してください", out)

    def test_schedule_status_accepts_single_dict_calendar_interval(self):
        self.write_plist({
            "Label": LABEL,
            "ProgramArguments": [str(self.base / "att"), "--scheduled"],
            "StartCalendarInterval": {"Weekday": 1, "Hour": 14, "Minute": 5},
        })
        backend = Mock(get_password=Mock(return_value="pw"))
        with patch("src.scheduler.agent_path", return_value=self.plist), \
                patch("src.credentials.keychain_backend", return_value=backend):
            diag = schedule_diagnostics(self.base, SCHEDULE, installed=True, today=date(2026, 10, 5))
        self.assertTrue(diag["in_sync"])

        # bool や不正な型の StartCalendarInterval は一致とみなさない
        self.write_plist({
            "Label": LABEL,
            "ProgramArguments": [str(self.base / "att"), "--scheduled"],
            "StartCalendarInterval": [{"Weekday": True, "Hour": 14, "Minute": 5}],
        })
        with patch("src.scheduler.agent_path", return_value=self.plist), \
                patch("src.credentials.keychain_backend", return_value=backend):
            bad_diag = schedule_diagnostics(self.base, SCHEDULE, installed=True, today=date(2026, 10, 5))
        self.assertFalse(bad_diag["in_sync"])

    def test_schedule_status_reports_all_prerequisite_failures_and_returns_zero(self):
        # 期限切れ・headless=false・既定ユーザーなし・有効授業0件
        bad_config = dict(self.config, users=[], default_user="", browser={"headless": False, "timeout_ms": 15000})
        (self.base / "config.json").write_text(json.dumps(bad_config), encoding="utf-8")
        expired = schedule_data(SCHEDULE)
        expired["active_until"] = "2026-10-01"
        expired["courses"][0]["enabled"] = False
        save_schedule(self.base / "schedule.json", expired)
        code, out = self.run_status(today=date(2026, 10, 5))
        self.assertEqual(code, 0)
        self.assertIn("browser.headless: NG", out)
        self.assertIn("既定ユーザー: NG", out)
        self.assertIn("期限切れ", out)
        self.assertIn("有効な授業数: NG（0件", out)

        # ユーザーはいるが Keychain にパスワードがない場合
        (self.base / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        code, out = self.run_status(password=None, today=date(2026, 10, 5))
        self.assertEqual(code, 0)
        self.assertIn("既定ユーザー: OK", out)
        self.assertIn("Keychain パスワード: NG（未保存", out)

        # Keychain アクセス例外時も機密を漏らさず status=0 で返す
        backend = Mock(get_password=Mock(side_effect=RuntimeError("sensitive-detail")))
        output = io.StringIO()
        with patch.object(main, "__file__", str(self.base / "main.py")), \
                patch("src.scheduler.agent_path", return_value=self.plist), \
                patch("src.credentials.keychain_backend", return_value=backend), \
                redirect_stdout(output):
            self.assertEqual(main.main(["schedule", "status"]), 0)
        self.assertIn("Keychain パスワード: NG", output.getvalue())
        self.assertNotIn("sensitive-detail", output.getvalue())

        # config.json 欠損時も status=0 で返す
        (self.base / "config.json").unlink()
        code, out = self.run_status()
        self.assertEqual(code, 0)
        self.assertIn("config.json: NG", out)

    def test_schedule_status_distinguishes_moved_folder_and_foreign_plist(self):
        old_exec = "/old/moved/folder/att"
        self.write_plist({
            "Label": LABEL,
            "ProgramArguments": [old_exec, "--scheduled"],
            "StartCalendarInterval": calendar_intervals([COURSE]),
        })
        code, out = self.run_status()
        self.assertEqual(code, 0)
        self.assertIn("パス不一致", out)
        self.assertIn(LABEL, out)
        self.assertIn(old_exec, out)
        self.assertIn(f"launchctl bootout gui/{os.getuid()}/{LABEL}", out)
        self.assertIn(f"rm {self.plist}", out)
        self.assertIn("att schedule install", out)

        # 第三者エージェントはパス不一致とは区別して表示する
        self.write_plist({
            "Label": "com.example.unrelated",
            "ProgramArguments": ["/usr/local/bin/other"],
        })
        code, out = self.run_status()
        self.assertEqual(code, 0)
        self.assertIn("競合", out)
        self.assertIn("com.example.unrelated", out)
        self.assertIn("plutil -p", out)
        self.assertNotIn("パス不一致", out)

    def test_schedule_status_reports_unloaded_and_unreadable_plist_and_missing_schedule(self):
        self.write_plist({
            "Label": LABEL,
            "ProgramArguments": [str(self.base / "att"), "--scheduled"],
            "StartCalendarInterval": calendar_intervals([COURSE]),
        })
        code, out = self.run_status(launchctl_code=1)
        self.assertEqual(code, 0)
        self.assertIn("停止中", out)

        self.write_plist(b"not-a-valid-plist")
        code, out = self.run_status()
        self.assertEqual(code, 0)
        self.assertIn("読み込めません", out)
        self.assertIn("plutil -p", out)

        (self.base / "schedule.json").unlink()
        code, out = self.run_status()
        self.assertEqual(code, 0)
        self.assertIn("予定ファイル: NG", out)

    def test_install_and_remove_agent_show_path_and_repair_when_label_matches(self):
        old_exec = "/previous/location/att"
        self.write_plist({
            "Label": LABEL,
            "ProgramArguments": [old_exec, "--scheduled"],
        })
        with patch("src.scheduler.agent_path", return_value=self.plist):
            with self.assertRaises(ScheduleError) as install_exc:
                install_agent(self.base, SCHEDULE)
            with self.assertRaises(ScheduleError) as remove_exc:
                remove_agent(self.base)
        for message in (str(install_exc.exception), str(remove_exc.exception)):
            self.assertIn(LABEL, message)
            self.assertIn(old_exec, message)
            self.assertIn(f"launchctl bootout gui/{os.getuid()}/{LABEL}", message)
            self.assertIn(f"rm {self.plist}", message)
        self.assertIn("att schedule install", str(install_exc.exception))

        # 異なる Label の場合は第三者扱いとして区別する
        self.write_plist({"Label": "com.other.tool", "ProgramArguments": ["/opt/other/bin"]})
        with patch("src.scheduler.agent_path", return_value=self.plist):
            with self.assertRaises(ScheduleError) as foreign_install:
                install_agent(self.base, SCHEDULE)
            with self.assertRaises(ScheduleError) as foreign_remove:
                remove_agent(self.base)
        for exc in (foreign_install.exception, foreign_remove.exception):
            self.assertIn("com.other.tool", str(exc))
            self.assertIn("このツールのものではありません", str(exc))
            self.assertIn("plutil -p", str(exc))

        # 破損した plist でも plutil 確認を案内して上書き・削除しない
        self.write_plist(b"broken")
        with patch("src.scheduler.agent_path", return_value=self.plist):
            with self.assertRaises(ScheduleError) as corrupt_exc:
                remove_agent(self.base)
        self.assertIn("plutil -p", str(corrupt_exc.exception))


if __name__ == "__main__":
    unittest.main()
