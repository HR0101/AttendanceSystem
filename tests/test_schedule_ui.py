import copy
import curses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from src.scheduler import ScheduleError, load_schedule, save_schedule, schedule_lock
from src.terminal_ui import AttendanceScreen

"""GUIでの予定編集と自動実行への反映。大学・launchctlには接続しない。"""

import copy
import curses
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from src.scheduler import WEEKDAYS, ScheduleError, load_schedule, next_scheduled_run, save_schedule, update_schedule, validate_schedule
from src.terminal_ui import AttendanceScreen


DATA = {
    "active_from": "2026-09-18", "active_until": "2026-12-21", "blackout_dates": [],
    "courses": [{"weekday": "mon", "start_time": "14:00", "room": "731", "course_name": "授業", "enabled": True}],
}
ISOLATED_CONFIG = {
    "login_url": "https://example.test/attendance/login",
    "top_url": "https://example.test/attendance/top",
    "browser": {"headless": True, "timeout_ms": 15000},
    "dry_run": False,
    "users": [{"username": "test", "student_id": "TEST001"}],
    "default_user": "test",
}


def active_schedule(days_ahead=30, courses=None, blackout_dates=()):
    today = date.today()
    return {
        "active_from": (today - timedelta(days=7)).isoformat(),
        "active_until": (today + timedelta(days=days_ahead)).isoformat(),
        "blackout_dates": list(blackout_dates),
        "courses": courses if courses is not None else copy.deepcopy(DATA["courses"]),
    }


class SchedulePanelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.path = self.base / "schedule.json"
        save_schedule(self.path, DATA)
        self.ui = AttendanceScreen({"users": [{"username": "test", "student_id": "TEST001"}], "default_user": "test"}, "TEST001", "", Mock(), base=self.base)
        self.panel = self.ui.schedule_panel
        self.panel.data = copy.deepcopy(DATA)
        self.ui.modal = "schedule"
        thread = patch("src.schedule_ui.threading.Thread", side_effect=lambda **kw: Mock(start=kw["target"]))
        thread.start()
        self.addCleanup(thread.stop)

    def test_add_edit_disable_delete_from_form(self):
        with patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.handle("a")
            self.panel.fields = ["追加授業", "火曜日", "11:00", "642", "11:03", "11:10"]
            self.panel.field_index = 5
            self.ui.handle_modal("\n")
            self.ui.poll()
            self.assertEqual(load_schedule(self.path)["courses"][1]["weekday"], "tue")
            self.assertEqual(load_schedule(self.path)["courses"][1]["register_time"], "11:03")
            self.assertEqual(load_schedule(self.path)["courses"][1]["register_until"], "11:10")
            self.panel.handle(curses.KEY_DOWN)
            self.panel.handle("\n")
            self.panel.fields[3] = "611"
            self.panel.submit()
            self.ui.poll()
            self.assertEqual(load_schedule(self.path)["courses"][1]["room"], "611")
            self.panel.handle("t")
            self.ui.poll()
            self.assertFalse(load_schedule(self.path)["courses"][1]["enabled"])
            self.panel.handle("d")
            self.assertEqual(self.panel.mode, "delete")
            self.assertEqual(self.panel.confirm_selection, 0)
            self.assertEqual(len(load_schedule(self.path)["courses"]), 2)
            self.panel.handle("\t")
            self.assertEqual(self.panel.confirm_selection, 1)
            self.panel.handle("\n")
            self.ui.poll()
            self.assertEqual(len(load_schedule(self.path)["courses"]), 1)

    def test_delete_confirmation_defaults_to_cancel_and_shows_course_details(self):
        self.panel.data["courses"][0]["register_time"] = "14:05"
        self.panel.data["courses"][0]["register_until"] = "14:12"
        self.panel.handle("d")
        self.assertEqual(self.panel.mode, "delete")
        self.assertEqual(self.panel.confirm_selection, 0)
        for key, expected in (("\t", 1), (curses.KEY_LEFT, 0), (curses.KEY_RIGHT, 1), (curses.KEY_BTAB, 0)):
            self.panel.handle(key)
            self.assertEqual(self.panel.confirm_selection, expected)
        for width in (62, 120):
            screen = Mock(getmaxyx=Mock(return_value=(24, width)))
            self.ui.draw(screen)
            drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
            for text in ("授業", "月", "14:00", "14:05～14:12", "731", "キャンセル", "削除する"):
                self.assertIn(text, drawn)

    def test_delete_default_cancel_repeated_enter_esc_and_q_never_delete(self):
        original = self.path.read_bytes()
        self.panel.active = True
        with patch("src.schedule_ui.save_schedule") as save, patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.remove_agent") as remove:
            for sequence in (("\n", "\n"), ("\x1b",), ("q",), ("Q",)):
                with self.subTest(sequence=sequence):
                    self.panel.mode = "list"
                    self.panel.handle("d")
                    self.assertEqual(self.panel.confirm_selection, 0)
                    for key in sequence:
                        self.panel.handle(key)
                    self.assertEqual(len(self.panel.data["courses"]), 1)
                    self.assertEqual(self.path.read_bytes(), original)
                    save.assert_not_called()
                    install.assert_not_called()
                    remove.assert_not_called()

    def test_explicit_delete_updates_active_agent_and_reports_save_failure(self):
        self.panel.active = True
        self.panel.data["courses"].append({"weekday": "tue", "start_time": "10:00", "room": "642", "course_name": "火曜授業", "enabled": True})
        save_schedule(self.path, self.panel.data)
        with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.agent_installed", return_value=True):
            self.panel.handle("d")
            self.panel.handle(curses.KEY_RIGHT)
            self.panel.handle("\n")
            self.ui.poll()
        install.assert_called_once()
        self.assertEqual(len(load_schedule(self.path)["courses"]), 1)
        with patch("src.schedule_ui.remove_agent") as remove, patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.handle("d")
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
        remove.assert_called_once_with(self.base)
        self.assertEqual(load_schedule(self.path)["courses"], [])
        self.panel.data = copy.deepcopy(DATA)
        save_schedule(self.path, self.panel.data)
        before = self.path.read_bytes()
        for err in (PermissionError("書き込み不可"), PermissionError()):
            with self.subTest(err=repr(err)), patch("src.schedule_ui.save_schedule", side_effect=err), \
                    patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.remove_agent") as remove:
                self.panel.handle("d")
                self.panel.handle("\t")
                self.panel.handle("\n")
                self.ui.poll()
                self.assertEqual(self.panel.mode, "delete")
                self.assertTrue(self.panel.message)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(len(self.panel.data["courses"]), 1)
                install.assert_not_called()
                remove.assert_not_called()
                self.panel.handle("\x1b")

    def test_ctrl_s_saves_schedule_forms_from_any_field(self):
        with patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.handle("a")
            self.panel.fields = ["途中保存授業", "wed", "13:00", "642", "", ""]
            self.panel.field_index = 0
            self.panel.handle("\x13")
            self.ui.poll()
            self.assertEqual(self.panel.mode, "list")
            self.assertEqual(load_schedule(self.path)["courses"][1]["course_name"], "途中保存授業")
            self.panel.handle(curses.KEY_DOWN)
            self.panel.handle("\n")
            self.panel.fields[3] = "611"
            self.panel.field_index = 1
            self.panel.handle("\x13")
            self.ui.poll()
            self.assertEqual(load_schedule(self.path)["courses"][1]["room"], "611")
            self.panel.handle("p")
            self.panel.fields[1] = "2026-12-25"
            self.panel.field_index = 0
            self.panel.handle("\x13")
            self.ui.poll()
            self.assertEqual(load_schedule(self.path)["active_until"].isoformat(), "2026-12-25")
            self.panel.handle("a")
            self.panel.fields = ["不正な授業", "mon", "99:99", "731", "", ""]
            self.panel.field_index = 2
            self.panel.handle("\x13")
            self.assertEqual(self.panel.mode, "add")
            self.assertEqual(self.panel.field_index, 2)
            self.assertIn("HH:MM", self.panel.message)

    def test_invalid_form_preserves_file_and_input(self):
        original = self.path.read_bytes()
        self.panel.form("add")
        self.panel.fields = ["授業", "mon", "25:00", "642", "", ""]
        self.panel.submit()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.panel.mode, "add")
        self.assertIn("HH:MM", self.panel.message)
        self.assertFalse(self.ui.busy)

    def test_period_and_blackouts_save(self):
        with patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.handle("p")
            self.panel.fields = ["2026-10-01", "2026-12-01", "2026-11-03、2026-11-23"]
            self.panel.submit()
            self.ui.poll()
        self.assertEqual(json.loads(self.path.read_text())["blackout_dates"], ["2026-11-03", "2026-11-23"])

    def test_enabled_schedule_refreshes_launch_agent_on_edit(self):
        self.panel.active = True
        with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.agent_installed", return_value=True):
            self.panel.form("edit")
            self.panel.fields[2] = "15:00"
            self.panel.fields[4] = "15:02"
            self.panel.fields[5] = "15:08"
            self.panel.submit()
            self.ui.poll()
        self.assertEqual(install.call_args.args[1]["courses"][0]["start_time"], "15:00")
        self.assertEqual(install.call_args.args[1]["courses"][0]["register_time"], "15:02")
        self.assertEqual(install.call_args.args[1]["courses"][0]["register_until"], "15:08")
        self.assertTrue(self.panel.active)

    def test_disabling_last_course_removes_agent(self):
        self.panel.active = True
        with patch("src.schedule_ui.remove_agent") as remove, patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.handle("t")
            self.ui.poll()
        remove.assert_called_once_with(self.base)
        self.assertFalse(self.panel.active)

    def test_install_failure_is_visible_and_keeps_saved_data(self):
        self.panel.active = True
        with patch("src.schedule_ui.install_agent", side_effect=ScheduleError("失敗")), patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.persist(DATA)
            self.ui.poll()
        self.assertIn("反映に失敗", self.panel.message)
        self.assertFalse(self.ui.busy)
        self.assertEqual(len(self.panel.data["courses"]), 1)

    def write_config(self, **overrides):
        config = copy.deepcopy(ISOLATED_CONFIG)
        config.update(overrides)
        (self.base / "config.json").write_text(json.dumps(config), encoding="utf-8")
        return config

    def draw_calls(self, width=62, height=24):
        screen = Mock(getmaxyx=Mock(return_value=(height, width)))
        self.ui.draw(screen)
        return screen.addstr.call_args_list

    def test_start_stop_from_gui(self):
        self.write_config()
        self.panel.data = active_schedule()
        save_schedule(self.path, self.panel.data)
        with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.remove_agent") as remove:
            self.panel.handle("i")
            self.assertEqual(self.panel.mode, "confirm_start")
            self.assertEqual(self.panel.confirm_selection, 0)
            install.assert_not_called()
            self.panel.handle("\t")
            self.assertEqual(self.panel.confirm_selection, 1)
            self.panel.handle("\n")
            self.ui.poll()
            self.assertTrue(self.panel.active)
            self.assertEqual(self.panel.mode, "list")
            install.assert_called_once()
            self.panel.handle("i")
            self.ui.poll()
            self.assertFalse(self.panel.active)
            remove.assert_called_once()

    def test_start_confirmation_defaults_to_cancel_and_preserves_state(self):
        self.write_config()
        self.panel.data = active_schedule()
        save_schedule(self.path, self.panel.data)
        original = self.path.read_bytes()
        for keys in (("\n",), ("\x1b",), ("q",), ("Q",), ("\t", curses.KEY_LEFT, "\n"), (curses.KEY_RIGHT, curses.KEY_BTAB, "\n")):
            with self.subTest(keys=keys), patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.save_schedule") as save:
                self.panel.mode = "list"
                self.panel.handle("i")
                self.assertEqual(self.panel.mode, "confirm_start")
                self.assertEqual(self.panel.confirm_selection, 0)
                for key in keys:
                    self.panel.handle(key)
                self.ui.poll()
                self.assertEqual(self.panel.mode, "list")
                self.assertFalse(self.panel.active)
                self.assertEqual(self.ui.modal, "schedule")
                self.assertEqual(self.path.read_bytes(), original)
                install.assert_not_called()
                save.assert_not_called()

        self.path.unlink()
        with patch("src.schedule_ui.install_agent") as install:
            self.panel.handle("i")
            self.assertEqual(self.panel.mode, "confirm_start")
            self.panel.handle("\x1b")
            self.assertFalse(self.path.exists())
            install.assert_not_called()

    def test_start_confirmation_preview_displays_targets_at_supported_sizes(self):
        long_user = {"username": "とても長い表示名のテストユーザー確認用", "student_id": "K9990001"}
        self.write_config(users=[long_user], default_user=long_user["username"])
        courses = [
            {"weekday": "mon", "start_time": "14:00", "room": "731", "course_name": "情報システム設計論", "enabled": True},
            {"weekday": "wed", "start_time": "10:00", "room": "642", "course_name": "無効な授業", "enabled": False},
            {"weekday": "fri", "start_time": "11:00", "room": "611", "course_name": "計算機工学", "enabled": True},
        ]
        self.panel.data = active_schedule(courses=courses)
        expected_dt, expected_course = next_scheduled_run(validate_schedule(self.panel.data))
        expected_day = "月火水木金土日"[expected_dt.weekday()]
        for width in (62, 80, 120):
            with self.subTest(width=width):
                self.panel.mode = "list"
                self.panel.handle("i")
                self.assertEqual(self.panel.mode, "confirm_start")
                calls = self.draw_calls(width=width)
                drawn = "\n".join(call.args[2] for call in calls)
                self.assertIn("K9990001", drawn)
                self.assertIn("とても長い", drawn)
                self.assertIn(f"{expected_dt:%Y-%m-%d} ({expected_day}) {expected_dt:%H:%M}", drawn)
                self.assertIn(f"QR{expected_course['room']}", drawn)
                self.assertIn(expected_course["course_name"], drawn)
                self.assertIn(f"{self.panel.data['active_from']} ～ {self.panel.data['active_until']}", drawn)
                self.assertIn("有効な予定: 2件", drawn)
                cancel_attrs = [call.args[3] for call in calls if "[ キャンセル ]" in call.args[2]]
                start_attrs = [call.args[3] for call in calls if "[ 開始する ]" in call.args[2]]
                self.assertTrue(cancel_attrs and (cancel_attrs[0] & curses.A_REVERSE))
                self.assertTrue(start_attrs and not (start_attrs[0] & curses.A_REVERSE))
                self.panel.handle("\t")
                calls = self.draw_calls(width=width)
                cancel_attrs = [call.args[3] for call in calls if "[ キャンセル ]" in call.args[2]]
                start_attrs = [call.args[3] for call in calls if "[ 開始する ]" in call.args[2]]
                self.assertTrue(cancel_attrs and not (cancel_attrs[0] & curses.A_REVERSE))
                self.assertTrue(start_attrs and (start_attrs[0] & curses.A_REVERSE))
                self.panel.handle("\x1b")

    def test_start_blocked_with_clear_reason_when_unrunnable(self):
        today = date.today()
        weekday_key = list(WEEKDAYS)[today.weekday()]
        all_blackouts = [
            (today + timedelta(days=offset)).isoformat()
            for offset in range(0, 15)
        ]
        cases = (
            ("no_enabled_courses", {}, active_schedule(courses=[{"weekday": "mon", "start_time": "14:00", "room": "731", "course_name": "授業", "enabled": False}]), "有効な授業"),
            ("expired_period", {}, {"active_from": (today - timedelta(days=30)).isoformat(), "active_until": (today - timedelta(days=1)).isoformat(), "blackout_dates": [], "courses": copy.deepcopy(DATA["courses"])}, "終了"),
            ("all_blackout_dates", {}, {"active_from": today.isoformat(), "active_until": (today + timedelta(days=14)).isoformat(), "blackout_dates": all_blackouts, "courses": [{"weekday": weekday_key, "start_time": "14:00", "room": "731", "course_name": "授業", "enabled": True}]}, "実行できる予定がありません"),
            ("headless_false", {"browser": {"headless": False, "timeout_ms": 15000}}, active_schedule(), "headless"),
            ("no_default_user", {"users": [], "default_user": "", "student_id": ""}, active_schedule(), "既定ユーザー"),
        )
        for name, config_overrides, data, expected_text in cases:
            with self.subTest(case=name), patch("src.schedule_ui.install_agent") as install:
                self.write_config(**config_overrides)
                self.panel.data = data
                save_schedule(self.path, data)
                original = self.path.read_bytes()
                self.panel.mode = "list"
                self.panel.handle("i")
                self.assertEqual(self.panel.mode, "list")
                self.assertFalse(self.panel.active)
                self.assertIn(expected_text, self.panel.message)
                self.assertEqual(self.path.read_bytes(), original)
                install.assert_not_called()

    def test_start_reconfirms_when_default_user_changes_during_confirmation(self):
        users = [{"username": "A", "student_id": "A1"}, {"username": "B", "student_id": "B2"}]
        self.write_config(users=users, default_user="A")
        self.panel.data = active_schedule()
        save_schedule(self.path, self.panel.data)
        original = self.path.read_bytes()
        with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.save_schedule") as save:
            self.panel.handle("i")
            self.assertEqual(self.panel.mode, "confirm_start")
            self.assertEqual(self.panel.start_preview["user"]["username"], "A")
            self.assertEqual(self.panel.confirm_selection, 0)

            self.write_config(users=users, default_user="B")
            self.panel.handle("\t")
            self.assertEqual(self.panel.confirm_selection, 1)
            self.panel.handle("\n")
            self.ui.poll()

            install.assert_not_called()
            save.assert_not_called()
            self.assertEqual(self.path.read_bytes(), original)
            self.assertFalse(self.panel.active)
            self.assertEqual(self.panel.mode, "confirm_start")
            self.assertEqual(self.panel.start_preview["user"], {"username": "B", "student_id": "B2"})
            self.assertEqual(self.panel.confirm_selection, 0)
            self.assertEqual(self.panel.message, "確認中に既定ユーザーまたは実行条件が変更されました。新しい内容を確認してください。")

            calls = self.draw_calls()
            drawn = "\n".join(call.args[2] for call in calls)
            self.assertIn("B (B2)", drawn)
            self.assertIn("確認中に既定ユーザー", drawn)
            cancel_attrs = [call.args[3] for call in calls if "[ キャンセル ]" in call.args[2]]
            self.assertTrue(cancel_attrs and (cancel_attrs[0] & curses.A_REVERSE))

            self.panel.handle("\t")
            self.assertEqual(self.panel.confirm_selection, 1)
            self.panel.handle("\n")
            self.ui.poll()

            save.assert_called_once()
            install.assert_called_once()
            self.assertTrue(self.panel.active)
            self.assertEqual(self.panel.mode, "list")
            self.assertIsNone(self.panel.start_preview)

            # 再確認画面の初期選択はキャンセルなので、そのまま Enter で変更せず戻る。
            self.panel.active = False
            self.panel.handle("i")
            self.assertEqual(self.panel.start_preview["user"]["username"], "B")
            self.write_config(users=users, default_user="A")
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
            self.assertEqual(self.panel.mode, "confirm_start")
            self.assertEqual(self.panel.start_preview["user"]["username"], "A")
            self.assertEqual(self.panel.confirm_selection, 0)
            self.panel.handle("\n")
            self.assertEqual(self.panel.mode, "list")
            self.assertFalse(self.panel.active)
            self.assertIsNone(self.panel.start_preview)
            save.assert_called_once()
            install.assert_called_once()

    def test_start_reconfirms_when_target_course_or_period_changes_during_confirmation(self):
        base_course = {"weekday": "mon", "start_time": "14:00", "room": "731", "course_name": "授業", "enabled": True}
        extra_course = {"weekday": "fri", "start_time": "11:00", "room": "611", "course_name": "追加授業", "enabled": True}
        future_blackout = (date.today() + timedelta(days=20)).isoformat()

        def apply_student_id():
            self.write_config(users=[{"username": "test", "student_id": "TEST999"}], default_user="test")

        def apply_course():
            self.panel.data["courses"][0] = {
                "weekday": "mon",
                "start_time": "14:00",
                "room": "642",
                "course_name": "変更後の授業",
                "enabled": True,
                "register_time": "14:02",
                "register_until": "14:10",
            }

        def apply_period():
            self.panel.data["active_until"] = (date.today() + timedelta(days=45)).isoformat()
            self.panel.data["blackout_dates"] = [future_blackout]

        def apply_enabled_count():
            self.panel.data["courses"].append(copy.deepcopy(extra_course))

        for name, mutate in (
            ("student_id", apply_student_id),
            ("next_course", apply_course),
            ("period_and_blackouts", apply_period),
            ("enabled_count", apply_enabled_count),
        ):
            with self.subTest(case=name), patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.save_schedule") as save:
                self.write_config()
                self.panel.active = False
                self.panel.mode = "list"
                self.panel.data = active_schedule(courses=[copy.deepcopy(base_course)])
                self.panel.handle("i")
                self.assertEqual(self.panel.mode, "confirm_start")

                mutate()
                self.panel.handle("\t")
                self.panel.handle("\n")
                self.ui.poll()

                install.assert_not_called()
                save.assert_not_called()
                self.assertFalse(self.panel.active)
                self.assertEqual(self.panel.mode, "confirm_start")
                self.assertEqual(self.panel.confirm_selection, 0)
                self.assertEqual(self.panel.message, "確認中に既定ユーザーまたは実行条件が変更されました。新しい内容を確認してください。")

                self.panel.handle("\t")
                self.panel.handle("\n")
                self.ui.poll()
                save.assert_called_once()
                install.assert_called_once()
                self.assertTrue(self.panel.active)
                self.assertEqual(self.panel.mode, "list")

        # 確認中に予定時刻を過ぎて次回日時が繰り上がった場合も再確認する。
        first_run = datetime(2026, 11, 23, 14, 5)
        second_run = datetime(2026, 11, 30, 14, 5)
        self.write_config()
        self.panel.active = False
        self.panel.mode = "list"
        self.panel.data = active_schedule(courses=[copy.deepcopy(base_course)])
        course_obj = validate_schedule(self.panel.data)["courses"][0]
        with patch("src.schedule_ui.next_scheduled_run", side_effect=[(first_run, course_obj), (second_run, course_obj), (second_run, course_obj)]), \
             patch("src.schedule_ui.install_agent") as install, \
             patch("src.schedule_ui.save_schedule") as save:
            self.panel.handle("i")
            self.assertEqual(self.panel.start_preview["next_at"], first_run)
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
            install.assert_not_called()
            save.assert_not_called()
            self.assertFalse(self.panel.active)
            self.assertEqual(self.panel.mode, "confirm_start")
            self.assertEqual(self.panel.start_preview["next_at"], second_run)
            self.assertEqual(self.panel.confirm_selection, 0)
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
            save.assert_called_once()
            install.assert_called_once()
            self.assertTrue(self.panel.active)

    def test_start_preview_changed_compares_all_confirmed_targets(self):
        base_preview = {
            "user": {"username": "A", "student_id": "A1"},
            "next_at": datetime(2026, 11, 23, 14, 5),
            "next_course": {
                "weekday": "mon",
                "start_time": "14:00",
                "room": "731",
                "course_name": "授業",
                "register_time": "14:05",
                "register_until": "14:15",
            },
            "active_from": "2026-09-18",
            "active_until": "2026-12-21",
            "enabled_count": 1,
            "blackout_count": 0,
        }
        same_preview = copy.deepcopy(base_preview)
        same_preview["user"]["extra"] = "ignored"
        same_preview["next_course"]["route"] = "ignored"
        self.assertFalse(self.panel._start_preview_changed(base_preview, same_preview))
        self.assertFalse(self.panel._start_preview_changed(None, None))
        self.assertTrue(self.panel._start_preview_changed(None, base_preview))
        self.assertTrue(self.panel._start_preview_changed(base_preview, None))

        mutations = (
            ("user.username", lambda p: p["user"].__setitem__("username", "B")),
            ("user.student_id", lambda p: p["user"].__setitem__("student_id", "B2")),
            ("next_at", lambda p: p.__setitem__("next_at", datetime(2026, 11, 30, 14, 5))),
            ("next_course.weekday", lambda p: p["next_course"].__setitem__("weekday", "tue")),
            ("next_course.start_time", lambda p: p["next_course"].__setitem__("start_time", "15:00")),
            ("next_course.room", lambda p: p["next_course"].__setitem__("room", "642")),
            ("next_course.course_name", lambda p: p["next_course"].__setitem__("course_name", "別授業")),
            ("next_course.register_time", lambda p: p["next_course"].__setitem__("register_time", "14:06")),
            ("next_course.register_until", lambda p: p["next_course"].__setitem__("register_until", "14:20")),
            ("active_from", lambda p: p.__setitem__("active_from", "2026-10-01")),
            ("active_until", lambda p: p.__setitem__("active_until", "2026-12-25")),
            ("enabled_count", lambda p: p.__setitem__("enabled_count", 2)),
            ("blackout_count", lambda p: p.__setitem__("blackout_count", 1)),
        )
        for field, mutate in mutations:
            with self.subTest(field=field):
                changed = copy.deepcopy(base_preview)
                mutate(changed)
                self.assertTrue(self.panel._start_preview_changed(base_preview, changed))

    def test_start_aborts_if_conditions_become_invalid_during_confirmation(self):
        self.write_config()
        self.panel.data = active_schedule()
        save_schedule(self.path, self.panel.data)
        original = self.path.read_bytes()
        with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.save_schedule") as save:
            self.panel.handle("i")
            self.assertEqual(self.panel.mode, "confirm_start")
            self.write_config(browser={"headless": False, "timeout_ms": 15000})
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
            install.assert_not_called()
            save.assert_not_called()
            self.assertEqual(self.path.read_bytes(), original)
            self.assertFalse(self.panel.active)
            self.assertEqual(self.panel.mode, "list")
            self.assertIsNone(self.panel.start_preview)
            self.assertIn("headless", self.panel.message)

    def test_start_failure_keeps_inactive_and_shows_error(self):
        self.write_config()
        self.panel.data = active_schedule()
        save_schedule(self.path, self.panel.data)
        with patch("src.schedule_ui.install_agent", side_effect=ScheduleError("LaunchAgentを開始できません。macOSのログイン状態を確認してください。")):
            self.panel.handle("i")
            self.assertEqual(self.panel.mode, "confirm_start")
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
        self.assertFalse(self.panel.active)
        self.assertEqual(self.panel.mode, "list")
        self.assertFalse(self.ui.busy)
        self.assertIn("LaunchAgentを開始できません", self.panel.message)

    def test_corrupt_file_is_not_overwritten(self):
        self.path.write_text("{broken", encoding="utf-8")
        self.panel.open()
        self.ui.poll()
        self.panel.handle("a")
        self.assertIsNone(self.panel.data)
        self.assertIn("行目", self.panel.message)
        self.assertEqual(self.path.read_text(), "{broken")

    def test_demo_does_not_write_or_install(self):
        original = self.path.read_bytes()
        self.ui.demo = True
        with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.save_schedule") as save:
            self.panel.handle("t")
            self.ui.poll()
            self.panel.handle("i")
            self.ui.poll()
        self.assertFalse(self.panel.data["courses"][0]["enabled"])
        self.assertEqual(self.path.read_bytes(), original)
        install.assert_not_called()
        save.assert_not_called()

    def test_confirm_start_in_demo_mode_never_writes_or_installs(self):
        self.panel.data = active_schedule()
        save_schedule(self.path, self.panel.data)
        original = self.path.read_bytes()
        self.ui.demo = True
        with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.save_schedule") as save:
            self.panel.handle("i")
            self.assertEqual(self.panel.mode, "confirm_start")
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
        self.assertEqual(self.panel.mode, "list")
        self.assertTrue(self.panel.active)
        self.assertEqual(self.path.read_bytes(), original)
        install.assert_not_called()
        save.assert_not_called()

    def test_cancel_form_and_draw_at_minimum_size(self):
        self.write_config()
        self.panel.data = active_schedule()
        save_schedule(self.path, self.panel.data)
        original = self.path.read_bytes()
        for mode in ("list", "add", "edit", "period", "delete", "confirm_start"):
            self.ui.modal = "schedule"
            if mode in ("add", "edit", "period"):
                self.panel.form(mode)
            elif mode == "confirm_start":
                self.panel.mode = "list"
                self.panel.handle("i")
                self.assertEqual(self.panel.mode, "confirm_start")
            else:
                self.panel.mode = mode
            screen = Mock(getmaxyx=Mock(return_value=(24, 62)))
            self.ui.draw(screen)
            drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
            self.assertIn("自動出席の予定", drawn)
            self.assertIn("Esc", drawn)
            self.panel.handle("\x1b")
        self.assertEqual(self.path.read_bytes(), original)

    def test_panel_save_preserves_concurrent_disk_additions_and_detects_missing_target(self):
        # 画面を開いた後に別処理で授業が追加されても、追加・期間更新で上書き消失しない。
        def add_external(cur):
            cur["courses"].append({
                "weekday": "wed", "start_time": "09:00", "room": "642",
                "course_name": "並行追加授業", "enabled": True,
            })
            return cur

        update_schedule(self.path, add_external)

        with patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.handle("a")
            self.panel.fields = ["画面追加授業", "fri", "13:00", "731", "", ""]
            self.panel.field_index = 5
            self.ui.handle_modal("\n")
            self.ui.poll()
            courses = load_schedule(self.path)["courses"]
            self.assertEqual([c["course_name"] for c in courses], ["授業", "並行追加授業", "画面追加授業"])

            # 既に別処理で削除済みの授業を編集しようとした場合はエラー表示して他授業を守る。
            def drop_external(cur):
                cur["courses"] = [c for c in cur["courses"] if c["course_name"] != "並行追加授業"]
                return cur

            update_schedule(self.path, drop_external)
            self.panel.index = 1  # self.panel.data 上では "並行追加授業"
            self.panel.handle("t")
            self.ui.poll()
            self.assertIn("再読込", self.panel.message)
            self.assertEqual([c["course_name"] for c in load_schedule(self.path)["courses"]], ["授業", "画面追加授業"])

    def test_conflicting_add_and_edit_keep_form_and_file_intact(self):
        original = self.path.read_bytes()
        # DATA の既存授業は 月 14:00 (既定 14:05～14:30)
        self.panel.form("add")
        self.panel.fields = ["競合授業", "月曜日", "14:15", "642", "14:20", ""]
        self.panel.submit()
        self.assertEqual(self.panel.mode, "add")
        self.assertEqual(self.panel.fields[0], "競合授業")
        self.assertIn("授業", self.panel.message)
        self.assertIn("競合授業", self.panel.message)
        self.assertIn("14:05", self.panel.message)
        self.assertIn("14:20", self.panel.message)
        self.assertFalse(self.ui.busy)
        self.assertEqual(self.path.read_bytes(), original)

        screen = Mock(getmaxyx=Mock(return_value=(24, 62)))
        self.ui.draw(screen)
        drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
        self.assertIn("受付時間が重なります", drawn)

        # 2件目を追加してから 1件目と衝突する時刻へ編集すると拒否される。
        two_courses = {
            **DATA,
            "courses": [
                DATA["courses"][0],
                {"weekday": "mon", "start_time": "16:00", "room": "642", "course_name": "午後授業", "enabled": True},
            ],
        }
        save_schedule(self.path, two_courses)
        self.panel.data = copy.deepcopy(two_courses)
        self.panel.mode = "list"
        self.panel.index = 1
        before_edit = self.path.read_bytes()
        self.panel.form("edit")
        self.panel.fields[4] = "14:30"
        self.panel.submit()
        self.assertEqual(self.panel.mode, "edit")
        self.assertEqual(self.panel.fields[4], "14:30")
        self.assertIn("午後授業", self.panel.message)
        self.assertEqual(self.path.read_bytes(), before_edit)

    def test_toggling_disabled_conflicting_course_or_disabling_range_is_rejected(self):
        disabled_conflict = {
            **DATA,
            "courses": [
                DATA["courses"][0],
                {"weekday": "mon", "start_time": "14:15", "room": "642", "course_name": "無効競合", "enabled": False, "register_time": "14:20", "register_until": ""},
            ],
        }
        save_schedule(self.path, disabled_conflict)
        self.panel.data = copy.deepcopy(disabled_conflict)
        self.panel.index = 1
        before = self.path.read_bytes()
        self.panel.handle("t")
        self.assertFalse(self.panel.data["courses"][1]["enabled"])
        self.assertIn("無効競合", self.panel.message)
        self.assertEqual(self.path.read_bytes(), before)

        # exact モードを支える範囲授業を無効化すると固定授業同士が衝突するため拒否される。
        exact_schedule = {
            **DATA,
            "courses": [
                DATA["courses"][0],
                {"weekday": "mon", "start_time": "14:15", "room": "642", "course_name": "近接固定", "enabled": True, "register_time": "14:20", "register_until": ""},
                {"weekday": "tue", "start_time": "10:00", "room": "642", "course_name": "火曜範囲", "enabled": True, "register_time": "10:05", "register_until": "10:15"},
            ],
        }
        save_schedule(self.path, exact_schedule)
        self.panel.data = copy.deepcopy(exact_schedule)
        self.panel.index = 2
        before_exact = self.path.read_bytes()
        self.panel.handle("t")
        self.assertTrue(self.panel.data["courses"][2]["enabled"])
        self.assertIn("近接固定", self.panel.message)
        self.assertEqual(self.path.read_bytes(), before_exact)

    def test_schedule_screen_L_opens_log_viewer_and_returns_to_schedule(self):
        (self.base / "logs").mkdir()
        (self.base / "logs" / "attendance-2026-11-19.log").write_text("2026-11-19 10:05:00 INFO 予定画面からログ確認\n", encoding="utf-8")
        screen = Mock(getmaxyx=Mock(return_value=(24, 62)))
        self.ui.draw(screen)
        drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
        self.assertIn("L ログ", drawn)

        # 一覧で L を押すとログビューアが開き、Esc で予定画面に戻る
        self.ui.handle_modal("l")
        self.assertEqual(self.ui.modal, "logs")
        screen.addstr.reset_mock()
        self.ui.draw(screen)
        drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
        self.assertIn("予定画面からログ確認", drawn)
        self.ui.handle_modal("\x1b")
        self.assertEqual(self.ui.modal, "schedule")

        # フォーム入力中は l が文字として入力され、ログビューアは開かない
        self.panel.form("add")
        self.panel.fields[0] = ""
        self.panel.editors[0].sync("")
        self.ui.handle_modal("l")
        self.assertEqual(self.ui.modal, "schedule")
        self.assertEqual(self.panel.fields[0], "l")


    def _draw_text(self, size=(24, 62)):
        screen = Mock(getmaxyx=Mock(return_value=size))
        self.ui.draw(screen)
        return "\n".join(call.args[2] for call in screen.addstr.call_args_list)

    def test_undo_after_delete_restores_course_and_syncs_agent(self):
        two_courses = {
            **DATA,
            "courses": [
                DATA["courses"][0],
                {"weekday": "wed", "start_time": "10:30", "room": "642", "course_name": "削除対象", "enabled": True, "register_time": "10:35", "register_until": ""},
            ],
        }
        save_schedule(self.path, two_courses)
        self.panel.data = copy.deepcopy(two_courses)
        self.panel.index = 1
        self.panel.active = True
        with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.agent_installed", return_value=True):
            self.panel.handle("d")
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
            self.assertEqual(len(load_schedule(self.path)["courses"]), 1)
            self.assertIsNotNone(self.panel.undo_state)
            self.assertIn("Z", self.panel.message)
            self.assertIn("Z: 直前の変更を元に戻す", self._draw_text())

            self.panel.handle("Z")
            self.ui.poll()

        restored = load_schedule(self.path)
        self.assertEqual(len(restored["courses"]), 2)
        self.assertEqual(restored["courses"][1]["course_name"], "削除対象")
        self.assertEqual(restored["courses"][1]["room"], "642")
        self.assertEqual(self.panel.index, 1)
        self.assertIsNone(self.panel.undo_state)
        self.assertIn("元に戻し", self.panel.message)
        self.assertEqual(install.call_count, 2)
        self.assertEqual(install.call_args.args[1]["courses"][1]["course_name"], "削除対象")

    def test_undo_deleting_last_course_with_ctrl_z(self):
        with patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.handle("d")
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
            self.assertEqual(load_schedule(self.path)["courses"], [])
            self.panel.handle("\x1a")
            self.ui.poll()
        self.assertEqual(len(load_schedule(self.path)["courses"]), 1)
        self.assertEqual(load_schedule(self.path)["courses"][0]["course_name"], "授業")
        self.assertIsNone(self.panel.undo_state)

    def test_undo_after_edit_restores_fields_and_z_types_in_form(self):
        self.panel.active = True
        with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.agent_installed", return_value=True):
            self.panel.handle("\n")
            self.assertEqual(self.panel.mode, "edit")
            self.panel.handle("z")
            self.assertEqual(self.panel.fields[0], "授業z")
            self.panel.fields = ["編集後授業", "fri", "16:00", "611", "16:02", "16:08"]
            self.panel.field_index = 5
            self.panel.handle("\n")
            self.ui.poll()
            edited = load_schedule(self.path)["courses"][0]
            self.assertEqual(edited["course_name"], "編集後授業")
            self.assertEqual(edited["weekday"], "fri")

            self.panel.handle("z")
            self.ui.poll()

        restored = load_schedule(self.path)["courses"][0]
        self.assertEqual(restored["course_name"], "授業")
        self.assertEqual(restored["weekday"], "mon")
        self.assertEqual(restored["start_time"], "14:00")
        self.assertEqual(restored["room"], "731")
        self.assertEqual(restored["register_time"], "")
        self.assertEqual(install.call_count, 2)
        self.assertEqual(install.call_args.args[1]["courses"][0]["course_name"], "授業")
        self.assertEqual(install.call_args.args[1]["courses"][0]["room"], "731")

    def test_undo_unavailable_initially_after_undo_and_after_reload(self):
        before = self.path.read_bytes()
        self.assertIn("(Z 元に戻す: 不可)", self._draw_text())
        with patch("src.schedule_ui.save_schedule") as save, patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.remove_agent") as remove:
            for key in ("z", "Z", "\x1a"):
                self.panel.handle(key)
                self.assertFalse(self.ui.busy)
                self.assertIn("元に戻せる直前の予定変更はありません", self.panel.message)
        self.assertEqual(self.path.read_bytes(), before)
        save.assert_not_called()
        install.assert_not_called()
        remove.assert_not_called()

        with patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.form("edit")
            self.panel.fields[0] = "変更後"
            self.panel.submit()
            self.ui.poll()
            self.panel.handle("z")
            self.ui.poll()
            self.assertIsNone(self.panel.undo_state)
            self.panel.handle("z")
            self.assertIn("元に戻せる直前の予定変更はありません", self.panel.message)

            self.panel.form("edit")
            self.panel.fields[0] = "再変更"
            self.panel.submit()
            self.ui.poll()
            self.assertIsNotNone(self.panel.undo_state)
            self.panel.handle("r")
            self.ui.poll()
            self.assertIsNone(self.panel.undo_state)
            self.panel.handle("z")
            self.assertIn("元に戻せる直前の予定変更はありません", self.panel.message)

    def test_undo_write_failure_preserves_state_and_allows_retry(self):
        with patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.form("edit")
            self.panel.fields[0] = "編集済み"
            self.panel.submit()
            self.ui.poll()
            edited_bytes = self.path.read_bytes()
            with patch("src.schedule_ui.save_schedule", side_effect=PermissionError("書込不可")), patch("src.schedule_ui.install_agent") as install:
                self.panel.handle("z")
                self.ui.poll()
            self.assertEqual(self.panel.data["courses"][0]["course_name"], "編集済み")
            self.assertEqual(self.path.read_bytes(), edited_bytes)
            self.assertIsNotNone(self.panel.undo_state)
            self.assertIn("現在の予定を維持", self.panel.message)
            self.assertIn("Zで再試行", self.panel.message)
            self.assertNotIn("元に戻しました", self.panel.message)
            install.assert_not_called()

            self.panel.handle("z")
            self.ui.poll()
        self.assertEqual(load_schedule(self.path)["courses"][0]["course_name"], "授業")
        self.assertIsNone(self.panel.undo_state)

    def test_undo_agent_sync_failure_reports_partial_state_without_claiming_success(self):
        self.panel.active = True
        with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.agent_installed", return_value=True):
            self.panel.form("edit")
            self.panel.fields[0] = "編集済み"
            self.panel.submit()
            self.ui.poll()
            self.assertEqual(install.call_count, 1)

        with patch("src.schedule_ui.install_agent", side_effect=ScheduleError("launchctl失敗")), patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.handle("z")
            self.ui.poll()

        self.assertEqual(load_schedule(self.path)["courses"][0]["course_name"], "授業")
        self.assertEqual(self.panel.data["courses"][0]["course_name"], "授業")
        self.assertFalse(self.panel.active)
        self.assertIsNone(self.panel.undo_state)
        self.assertIn("反映に失敗", self.panel.message)
        self.assertIn("Iで停止→再開", self.panel.message)
        self.assertNotIn("元に戻し", self.panel.message)

    def test_undo_invalid_before_data_refuses_and_preserves_file(self):
        with patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.form("edit")
            self.panel.fields[0] = "編集済み"
            self.panel.submit()
            self.ui.poll()
        edited_bytes = self.path.read_bytes()
        self.panel.undo_state["data"]["courses"][0]["start_time"] = "99:99"
        self.panel.handle("z")
        self.assertEqual(self.path.read_bytes(), edited_bytes)
        self.assertEqual(self.panel.data["courses"][0]["course_name"], "編集済み")
        self.assertIsNone(self.panel.undo_state)
        self.assertIn("Rで再読込", self.panel.message)

    def test_undo_refuses_external_modification_deletion_and_symlink(self):
        for case in ("modified", "corrupt", "deleted", "symlink"):
            with self.subTest(case=case):
                save_schedule(self.path, DATA)
                self.panel.data = copy.deepcopy(DATA)
                self.panel.undo_state = None
                self.panel.active = True
                with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.agent_installed", return_value=True):
                    self.panel.form("edit")
                    self.panel.fields[0] = "編集後"
                    self.panel.submit()
                    self.ui.poll()
                    self.assertEqual(install.call_count, 1)

                    if case == "modified":
                        external = copy.deepcopy(DATA)
                        external["courses"][0]["course_name"] = "CLI変更"
                        save_schedule(self.path, external)
                        expected_raw = self.path.read_bytes()
                    elif case == "corrupt":
                        self.path.write_text("{broken", encoding="utf-8")
                        expected_raw = b"{broken"
                    elif case == "deleted":
                        self.path.unlink()
                    else:
                        target = self.base / "other.json"
                        target.write_text("{}", encoding="utf-8")
                        self.path.unlink()
                        self.path.symlink_to(target)

                    self.panel.handle("z")
                    self.ui.poll()

                self.assertEqual(install.call_count, 1)
                self.assertEqual(self.panel.data["courses"][0]["course_name"], "編集後")
                self.assertIsNone(self.panel.undo_state)
                self.assertIn("Rで再読込", self.panel.message)
                if case == "deleted":
                    self.assertFalse(self.path.exists())
                elif case == "symlink":
                    self.assertTrue(self.path.is_symlink())
                else:
                    self.assertEqual(self.path.read_bytes(), expected_raw)

    def test_undo_uses_exclusive_schedule_lock(self):
        with patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.form("edit")
            self.panel.fields[0] = "編集後"
            self.panel.submit()
            self.ui.poll()

        observed_locked = []
        original_save = save_schedule

        def verify_lock_held(path, raw):
            with self.assertRaises(BlockingIOError):
                with schedule_lock(path, exclusive=True, blocking=False):
                    pass
            observed_locked.append(True)
            return original_save(path, raw)

        with patch("src.schedule_ui.save_schedule", side_effect=verify_lock_held), patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.handle("z")
            self.ui.poll()
        self.assertEqual(observed_locked, [True])
        self.assertEqual(load_schedule(self.path)["courses"][0]["course_name"], "授業")

    def test_undo_reopening_panel_preserves_if_unchanged_and_clears_if_changed(self):
        with patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.form("edit")
            self.panel.fields[0] = "編集後"
            self.panel.submit()
            self.ui.poll()
            self.panel.handle("\x1b")
            self.assertIsNone(self.ui.modal)
            self.panel.open()
            self.ui.poll()
            self.assertIsNotNone(self.panel.undo_state)

            external = copy.deepcopy(DATA)
            external["courses"][0]["room"] = "642"
            save_schedule(self.path, external)
            self.panel.open()
            self.ui.poll()
            self.assertIsNone(self.panel.undo_state)
            self.assertEqual(self.panel.data["courses"][0]["room"], "642")

    def test_demo_undo_does_not_touch_disk_lock_or_agent(self):
        original = self.path.read_bytes()
        (self.base / "schedule.json.lock").unlink(missing_ok=True)
        self.ui.demo = True
        with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.remove_agent") as remove, patch("src.schedule_ui.save_schedule") as save, patch("src.schedule_ui.agent_installed") as installed, patch("src.schedule_ui.schedule_lock") as lock:
            self.panel.form("edit")
            self.panel.fields[0] = "デモ編集"
            self.panel.submit()
            self.ui.poll()
            self.assertEqual(self.panel.data["courses"][0]["course_name"], "デモ編集")
            self.assertIsNone(self.panel.undo_state["disk_sha256"])

            self.panel.handle("z")
            self.ui.poll()
            self.assertEqual(self.panel.data["courses"][0]["course_name"], "授業")
            self.assertIsNone(self.panel.undo_state)
            self.assertIn("デモ", self.panel.message)

            self.panel.handle("d")

            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
            self.assertEqual(self.panel.data["courses"], [])

            self.panel.handle("Z")
            self.ui.poll()
            self.assertEqual(len(self.panel.data["courses"]), 1)
            self.assertEqual(self.panel.data["courses"][0]["course_name"], "授業")

            self.panel.handle("z")
            self.assertIn("元に戻せる直前の予定変更はありません", self.panel.message)

        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.base / "schedule.json.lock").exists())
        self.assertFalse((self.base / ".schedule.json.lock").exists())
        lock.assert_not_called()
        install.assert_not_called()
        remove.assert_not_called()
        save.assert_not_called()
        installed.assert_not_called()

    def test_undo_without_active_agent_does_not_call_install_or_remove(self):
        self.panel.active = False
        with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.remove_agent") as remove, patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.handle("t")
            self.ui.poll()
            self.assertFalse(load_schedule(self.path)["courses"][0]["enabled"])
            self.panel.handle("z")
            self.ui.poll()
        self.assertTrue(load_schedule(self.path)["courses"][0]["enabled"])
        install.assert_not_called()
        remove.assert_not_called()

    def test_undo_active_to_no_enabled_courses_removes_agent(self):
        disabled = copy.deepcopy(DATA)
        disabled["courses"][0]["enabled"] = False
        save_schedule(self.path, disabled)
        self.panel.data = copy.deepcopy(disabled)
        self.panel.active = True
        with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.remove_agent") as remove, patch("src.schedule_ui.agent_installed", side_effect=[True, False]):
            self.panel.handle("t")
            self.ui.poll()
            install.assert_called_once()
            self.panel.handle("z")
            self.ui.poll()
        remove.assert_called_once_with(self.base)
        self.assertFalse(self.panel.active)
        self.assertFalse(load_schedule(self.path)["courses"][0]["enabled"])

    def test_undo_period_restores_dates(self):
        with patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.handle("p")
            self.panel.fields = ["2026-10-01", "2026-12-01", "2026-11-03"]
            self.panel.submit()
            self.ui.poll()
            self.assertEqual(json.loads(self.path.read_text())["blackout_dates"], ["2026-11-03"])
            self.panel.handle("z")
            self.ui.poll()
        restored = json.loads(self.path.read_text())
        self.assertEqual(restored["active_from"], DATA["active_from"])
        self.assertEqual(restored["active_until"], DATA["active_until"])
        self.assertEqual(restored["blackout_dates"], [])

    def test_save_from_unsaved_prompt_and_long_error_renders_retry_at_minimum_size(self):
        with patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.handle("\n")
            self.panel.fields[0] = "保存確認経由"
            self.panel.handle("\x1b")
            self.assertTrue(self.ui.save_prompt.active)
            self.panel.handle(curses.KEY_DOWN)
            self.panel.handle("\n")
            self.ui.poll()
            self.assertEqual(load_schedule(self.path)["courses"][0]["course_name"], "保存確認経由")

            self.panel.handle("d")

            self.panel.handle("\t")
            self.panel.handle("\x1b")
            self.assertEqual(self.panel.mode, "list")
            self.assertIsNotNone(self.panel.undo_state)

            with patch("src.schedule_ui.save_schedule", side_effect=PermissionError("/very/long/path/that/could/overflow/schedule.json")):
                self.panel.handle("z")
                self.ui.poll()
            drawn = self._draw_text((24, 62))
            self.assertIn("Zで再試行", drawn)
            self.assertIn("Rで再読込", drawn)

            self.panel.handle("z")
            self.ui.poll()
        self.assertEqual(load_schedule(self.path)["courses"][0]["course_name"], "授業")

    def test_undo_deleting_last_enabled_course_while_active_resumes_agent(self):
        self.panel.active = True
        installed = [True]

        def install(*_args):
            installed[0] = True

        def remove(*_args):
            installed[0] = False

        with patch("src.schedule_ui.agent_installed", side_effect=lambda _: installed[0]), patch("src.schedule_ui.install_agent", side_effect=install) as install_mock, patch("src.schedule_ui.remove_agent", side_effect=remove) as remove_mock:
            self.panel.handle("d")
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
            remove_mock.assert_called_once_with(self.base)
            install_mock.assert_not_called()
            self.assertFalse(self.panel.active)
            self.assertTrue(self.panel.undo_state["restore_active"])

            self.panel.handle("z")
            self.ui.poll()

        self.assertEqual(len(load_schedule(self.path)["courses"]), 1)
        self.assertEqual(len(self.panel.data["courses"]), 1)
        install_mock.assert_called_once()
        self.assertTrue(self.panel.active)
        self.assertIsNone(self.panel.undo_state)
        self.assertIn("再開しました", self.panel.message)

    def test_undo_disabling_last_course_while_active_resumes_agent(self):
        self.panel.active = True
        installed = [True]

        def install(*_args):
            installed[0] = True

        def remove(*_args):
            installed[0] = False

        with patch("src.schedule_ui.agent_installed", side_effect=lambda _: installed[0]), patch("src.schedule_ui.install_agent", side_effect=install) as install_mock, patch("src.schedule_ui.remove_agent", side_effect=remove) as remove_mock:
            self.panel.handle("t")
            self.ui.poll()
            remove_mock.assert_called_once_with(self.base)
            self.assertFalse(self.panel.active)
            self.assertFalse(load_schedule(self.path)["courses"][0]["enabled"])
            self.assertTrue(self.panel.undo_state["restore_active"])

            self.panel.handle("z")
            self.ui.poll()

        self.assertTrue(load_schedule(self.path)["courses"][0]["enabled"])
        self.assertTrue(self.panel.data["courses"][0]["enabled"])
        install_mock.assert_called_once()
        self.assertTrue(self.panel.active)
        self.assertIn("再開しました", self.panel.message)

    def test_undo_after_explicit_stop_with_i_does_not_restart_agent(self):
        two_courses = {
            **DATA,
            "courses": [
                DATA["courses"][0],
                {"weekday": "wed", "start_time": "10:30", "room": "642", "course_name": "2件目", "enabled": True, "register_time": "", "register_until": ""},
            ],
        }
        save_schedule(self.path, two_courses)
        self.panel.data = copy.deepcopy(two_courses)
        self.panel.active = True
        installed = [True]

        def install(*_args):
            installed[0] = True

        def remove(*_args):
            installed[0] = False

        with patch("src.schedule_ui.agent_installed", side_effect=lambda _: installed[0]), patch("src.schedule_ui.install_agent", side_effect=install) as install_mock, patch("src.schedule_ui.remove_agent", side_effect=remove) as remove_mock:
            self.panel.handle("d")
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
            self.assertEqual(install_mock.call_count, 1)
            self.assertTrue(self.panel.active)

            self.panel.handle("i")
            self.ui.poll()
            remove_mock.assert_called_once_with(self.base)
            self.assertFalse(self.panel.active)
            self.assertFalse(self.panel.undo_state["restore_active"])

            self.panel.handle("z")
            self.ui.poll()

        self.assertEqual(len(load_schedule(self.path)["courses"]), 2)
        self.assertEqual(install_mock.call_count, 1)
        self.assertFalse(self.panel.active)
        self.assertNotIn("再開", self.panel.message)

        # 直前に restore_active=True が設定されていた状態でも、明示停止(I)で必ず解除される。
        self.panel.active = True
        self.panel.undo_state = {
            "data": copy.deepcopy(two_courses),
            "disk_sha256": self.panel.undo_state["disk_sha256"] if self.panel.undo_state else None,
            "label": "授業の削除",
            "index": 0,
            "restore_active": True,
        }
        save_schedule(self.path, DATA)
        self.panel.data = copy.deepcopy(DATA)
        from src.scheduler import schedule_digest
        self.panel.undo_state["disk_sha256"] = schedule_digest(self.path)
        installed[0] = True
        with patch("src.schedule_ui.agent_installed", side_effect=lambda _: installed[0]), patch("src.schedule_ui.install_agent", side_effect=install) as install_mock, patch("src.schedule_ui.remove_agent", side_effect=remove):
            self.panel.handle("i")
            self.ui.poll()
            self.assertFalse(self.panel.active)
            self.assertFalse(self.panel.undo_state["restore_active"])
            self.panel.handle("z")
            self.ui.poll()
        self.assertEqual(len(load_schedule(self.path)["courses"]), 2)
        install_mock.assert_not_called()
        self.assertFalse(self.panel.active)

    def test_undo_reopening_panel_preserves_restore_active_unless_externally_changed(self):
        self.panel.active = True
        installed = [True]

        def install(*_args):
            installed[0] = True

        def remove(*_args):
            installed[0] = False

        with patch("src.schedule_ui.agent_installed", side_effect=lambda _: installed[0]), patch("src.schedule_ui.install_agent", side_effect=install) as install_mock, patch("src.schedule_ui.remove_agent", side_effect=remove):
            self.panel.handle("d")
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
            self.assertFalse(self.panel.active)
            self.assertTrue(self.panel.undo_state["restore_active"])

            self.panel.handle("\x1b")
            self.assertIsNone(self.ui.modal)
            self.panel.open()
            self.ui.poll()
            self.assertIsNotNone(self.panel.undo_state)
            self.assertTrue(self.panel.undo_state["restore_active"])

            self.panel.handle("z")
            self.ui.poll()
        self.assertEqual(len(load_schedule(self.path)["courses"]), 1)
        install_mock.assert_called_once()
        self.assertTrue(self.panel.active)

        # 再閉開の間に外部変更があれば、restore_active ごと undo_state を破棄する。
        with patch("src.schedule_ui.agent_installed", side_effect=lambda _: installed[0]), patch("src.schedule_ui.install_agent", side_effect=install) as install_mock, patch("src.schedule_ui.remove_agent", side_effect=remove):
            self.panel.handle("d")
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
            self.panel.handle("\x1b")
            external = copy.deepcopy(DATA)
            external["courses"][0]["room"] = "642"
            save_schedule(self.path, external)
            self.panel.open()
            self.ui.poll()
            self.assertIsNone(self.panel.undo_state)
            self.panel.handle("z")
            self.assertIn("元に戻せる直前の予定変更はありません", self.panel.message)
        install_mock.assert_not_called()
        self.assertFalse(self.panel.active)

    def test_undo_resume_agent_failure_reports_partial_state(self):
        self.panel.active = True
        installed = [True]

        def remove(*_args):
            installed[0] = False

        with patch("src.schedule_ui.agent_installed", side_effect=lambda _: installed[0]), patch("src.schedule_ui.remove_agent", side_effect=remove):
            self.panel.handle("d")
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
            self.assertFalse(self.panel.active)

        with patch("src.schedule_ui.install_agent", side_effect=ScheduleError("launchctl失敗")), patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.handle("z")
            self.ui.poll()

        self.assertEqual(len(load_schedule(self.path)["courses"]), 1)
        self.assertEqual(len(self.panel.data["courses"]), 1)
        self.assertFalse(self.panel.active)
        self.assertIsNone(self.panel.undo_state)
        self.assertIn("反映に失敗", self.panel.message)
        self.assertNotIn("元に戻し", self.panel.message)

    def test_demo_deleting_last_course_while_active_and_undo_or_explicit_stop(self):
        self.ui.demo = True
        self.panel.active = True
        with patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.remove_agent") as remove:
            self.panel.handle("d")
            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
            self.assertFalse(self.panel.active)
            self.assertTrue(self.panel.undo_state["restore_active"])

            self.panel.handle("z")
            self.ui.poll()
            self.assertEqual(len(self.panel.data["courses"]), 1)
            self.assertTrue(self.panel.active)
            self.assertIn("再開しました", self.panel.message)

            self.panel.handle("d")

            self.panel.handle("\t")
            self.panel.handle("\n")
            self.ui.poll()
            self.panel.handle("i")
            self.ui.poll()
            self.assertFalse(self.panel.active)
            self.assertIn("有効な授業", self.panel.message)
            self.assertTrue(self.panel.undo_state["restore_active"])
            self.panel.handle("z")
            self.ui.poll()
            self.assertEqual(len(self.panel.data["courses"]), 1)
            self.assertTrue(self.panel.active)
        install.assert_not_called()
        remove.assert_not_called()


    def test_undo_after_concurrent_add_preserves_external_course(self):
        external = {"weekday": "wed", "start_time": "09:00", "room": "642", "course_name": "外部追加", "enabled": True}
        raw = json.loads(self.path.read_text())
        raw["courses"].append(external)
        save_schedule(self.path, raw)
        with patch("src.schedule_ui.agent_installed", return_value=False):
            self.panel.form("edit")
            self.panel.fields[0] = "変更後"
            self.panel.submit()
            self.ui.poll()
            self.panel.undo()
            self.ui.poll()
        courses = load_schedule(self.path)["courses"]
        self.assertEqual(len(courses), 2)
        self.assertEqual(courses[0]["course_name"], DATA["courses"][0]["course_name"])
        self.assertEqual(courses[1]["course_name"], "外部追加")


if __name__ == "__main__":
    unittest.main()
