"""GUIでの予定編集と自動実行への反映。大学・launchctlには接続しない。"""

import copy
import curses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.scheduler import ScheduleError, load_schedule, save_schedule, schedule_lock
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
            self.panel.handle("\n")
            self.ui.poll()
            self.panel.handle("i")
            self.ui.poll()
            self.assertTrue(self.panel.active)
            self.assertFalse(self.panel.undo_state["restore_active"])
            self.panel.handle("i")
            self.ui.poll()
            self.assertFalse(self.panel.active)
            self.panel.handle("z")
            self.ui.poll()
            self.assertEqual(len(self.panel.data["courses"]), 1)
            self.assertFalse(self.panel.active)
        install.assert_not_called()
        remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
