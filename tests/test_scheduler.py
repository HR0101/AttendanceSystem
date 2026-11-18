import io
import json
import os
import plistlib
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import main
from src.scheduler import (
    LABEL, ScheduleError, agent_status, calendar_intervals, due_course,
    format_schedule_diagnostics, install_agent, registration_time, remove_agent,
    save_schedule, schedule_data, schedule_diagnostics, validate_schedule,
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
