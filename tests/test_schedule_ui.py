"""GUIでの予定編集と自動実行への反映。大学・launchctlには接続しない。"""

import copy
import curses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.scheduler import ScheduleError, load_schedule, save_schedule
from src.terminal_ui import AttendanceScreen


DATA = {
    "active_from": "2026-09-18", "active_until": "2026-12-21", "blackout_dates": [],
    "courses": [{"weekday": "mon", "start_time": "14:00", "room": "731", "course_name": "授業", "enabled": True}],
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
            self.assertEqual(len(load_schedule(self.path)["courses"]), 2)
            self.panel.handle("\n")
            self.ui.poll()
            self.assertEqual(len(load_schedule(self.path)["courses"]), 1)

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

    def test_start_stop_from_gui(self):
        config = dict(self.ui.config, browser={"headless": True})
        with patch("src.schedule_ui.load_config", return_value=config), patch("src.schedule_ui.install_agent") as install, patch("src.schedule_ui.remove_agent") as remove:
            self.panel.handle("i")
            self.ui.poll()
            self.assertTrue(self.panel.active)
            install.assert_called_once()
            self.panel.handle("i")
            self.ui.poll()
            self.assertFalse(self.panel.active)
            remove.assert_called_once()

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

    def test_cancel_form_and_draw_at_minimum_size(self):
        original = self.path.read_bytes()
        for mode in ("list", "add", "edit", "period", "delete"):
            self.ui.modal = "schedule"
            if mode in ("add", "edit", "period"):
                self.panel.form(mode)
            else:
                self.panel.mode = mode
            screen = Mock(getmaxyx=Mock(return_value=(24, 62)))
            self.ui.draw(screen)
            drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
            self.assertIn("自動出席の予定", drawn)
            self.assertIn("Esc", drawn)
            self.panel.handle("\x1b")
        self.assertEqual(self.path.read_bytes(), original)

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


if __name__ == "__main__":
    unittest.main()
