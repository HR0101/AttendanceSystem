import copy
import io
import json
import logging
import os
import plistlib
import subprocess
import tempfile
import threading
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import main as app_main
from src import updater as u
from src.credentials import CredentialError
from src.scheduler import (
    ScheduleError,
    _claim_path,
    _run_key,
    calendar_intervals,
    claim_scheduled_run,
    due_course,
    due_scheduled_course,
    install_agent,
    registration_time,
    schedule_data,
    validate_schedule,
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

    def test_due_course_grace_minutes_supports_exact_boundary_without_breaking_default(self):
        course = dict(COURSE, register_time="14:05", register_until="14:05")
        schedule = dict(SCHEDULE, courses=[course])
        self.assertIsNone(due_course(schedule, datetime(2026, 10, 5, 14, 4), grace_minutes=2))
        self.assertEqual(due_course(schedule, datetime(2026, 10, 5, 14, 5), grace_minutes=2), course)
        self.assertEqual(due_course(schedule, datetime(2026, 10, 5, 14, 6), grace_minutes=2), course)
        self.assertEqual(due_course(schedule, datetime(2026, 10, 5, 14, 7), grace_minutes=2), course)
        self.assertIsNone(due_course(schedule, datetime(2026, 10, 5, 14, 8), grace_minutes=2))
        self.assertIsNone(due_course(schedule, datetime(2026, 10, 5, 14, 6)))


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


if __name__ == "__main__":
    unittest.main()
