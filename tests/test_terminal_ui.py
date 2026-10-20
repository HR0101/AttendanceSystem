"""画面で表示した授業の照合と登録可否を確認する。"""

import unittest
import curses
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import Mock, patch

from src.terminal_ui import AttendanceScreen, fit_text
from src.verification import AttendanceError
from src.credentials import CredentialError


class TerminalUITests(unittest.TestCase):
    def screen(self):
        ui = AttendanceScreen({"dry_run": False}, "TEST001", "dummy", Mock(), "731")
        ui.values = {"授業名": "テスト授業", "開始時刻": "10:00"}
        return ui

    def test_registration_requires_eligible_snapshot(self):
        ui = self.screen()
        self.assertTrue(ui.can_register())
        for field in ("attended", "unenrolled"):
            setattr(ui, field, True)
            self.assertFalse(ui.can_register())
            setattr(ui, field, False)
        ui.config["dry_run"] = True
        self.assertFalse(ui.can_register())
        ui.config["dry_run"] = False
        ui.values = {}
        self.assertFalse(ui.can_register())

    @patch("src.terminal_ui.threading.Thread")
    @patch("src.browser.open_page")
    @patch("src.attendance.run_attendance")
    def test_registration_rechecks_displayed_course(self, run, open_page, thread):
        ui = self.screen()
        open_page.return_value = nullcontext(Mock())
        run.return_value = "出席登録成功"
        thread.side_effect = lambda **kwargs: Mock(start=kwargs["target"])
        ui.start(register=True)
        args = run.call_args.args
        self.assertFalse(args[1]["dry_run"])
        self.assertEqual(args[5], "7301")
        self.assertEqual(args[7:9], ("テスト授業", "10:00"))
        self.assertEqual(run.call_args.kwargs["expected_values"], {"授業名": "テスト授業", "開始時刻": "10:00"})
        ui.poll()
        self.assertTrue(ui.attended)
        self.assertFalse(ui.busy)

    @patch("src.terminal_ui.threading.Thread")
    @patch("src.browser.open_page")
    @patch("src.attendance.run_attendance")
    def test_failed_refresh_clears_registration(self, run, open_page, thread):
        ui = self.screen()
        open_page.return_value = nullcontext(Mock())
        run.side_effect = AttendanceError("授業が変更されています。")
        thread.side_effect = lambda **kwargs: Mock(start=kwargs["target"])
        ui.start()
        self.assertTrue(run.call_args.args[1]["dry_run"])
        ui.poll()
        self.assertFalse(ui.can_register())
        self.assertFalse(ui.busy)
        self.assertEqual(ui.message, "授業が変更されています。")

    def test_japanese_text_fits_terminal_columns(self):
        self.assertEqual(fit_text("出席済 TEST", 7), "出席済 ")
        self.assertEqual(fit_text("A\x1bB", 2), "AB")

    def demo_screen(self):
        config = {"users": [{"username": "one", "student_id": "ONE001"}, {"username": "two", "student_id": "TWO001"}], "default_user": "one", "_active_username": "one", "dry_run": False}
        ui = AttendanceScreen(config, "ONE001", "", Mock(), demo=True)
        ui.values = {"授業名": "前のユーザーの授業"}
        ui.attended = True
        return ui

    @patch("src.terminal_ui.threading.Thread")
    def test_switching_user_clears_previous_attendance_without_changing_default(self, thread):
        thread.side_effect = lambda **kwargs: Mock(start=kwargs["target"])
        ui = self.demo_screen()
        ui.account_action("switch", "two")
        self.assertEqual(ui.values, {})
        self.assertFalse(ui.attended)
        ui.poll()
        self.assertEqual(ui.student_id, "TWO001")
        self.assertEqual(ui.config["_active_username"], "two")
        self.assertEqual(ui.config["default_user"], "one")
        self.assertFalse(ui.can_register())

    @patch("src.terminal_ui.threading.Thread")
    def test_renaming_active_user_preserves_attendance(self, thread):
        thread.side_effect = lambda **kwargs: Mock(start=kwargs["target"])
        ui = self.demo_screen()
        ui.account_action("rename", "one", new_name="new")
        ui.poll()
        self.assertEqual(ui.config["_active_username"], "new")
        self.assertEqual(ui.config["default_user"], "new")
        self.assertEqual(ui.student_id, "ONE001")
        self.assertTrue(ui.attended)

    def test_password_is_masked_and_cancel_discards_form(self):
        ui = self.demo_screen()
        ui.open_form("add")
        ui.form_values = ["new", "NEW001", "private-password"]
        screen = Mock(getmaxyx=Mock(return_value=(24, 80)))
        ui.draw(screen)
        drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
        self.assertNotIn("private-password", drawn)
        self.assertIn("****************", drawn)
        ui.handle_modal("\x1b")
        self.assertEqual(ui.form_values, [])
        self.assertIsNone(ui.modal)

    def test_registration_disabled_during_network_operation(self):
        ui = self.screen()
        ui.busy = True
        self.assertFalse(ui.can_register())

    def test_no_active_class_has_its_own_status(self):
        ui = self.screen()
        ui.values = {}
        ui.events.put(("snapshot", ({}, False, False)))
        ui.events.put(("result", "出席受付対象なし"))
        ui.poll()
        self.assertEqual(ui.status()[0], "現在の受付対象なし")
        self.assertFalse(ui.can_register())

    def test_main_screen_can_open_schedule_without_attendance_lookup(self):
        ui = self.demo_screen()
        ui.room = ""
        screen = Mock(getmaxyx=Mock(return_value=(24, 62)))
        # Tabでも上キーでも、新しいボタンに移動して予定を開ける。
        for keys in (("\t", "\t", "\t"), (curses.KEY_UP,)):
            ui.selected = 0
            ui.modal = None
            screen.get_wch.side_effect = [*keys, "\n", "\x1b", "q"]

            def open_schedule():
                ui.modal = "schedule"

            with patch.object(ui.schedule_panel, "open", side_effect=open_schedule) as open_panel, patch.object(ui, "start") as start, patch("src.terminal_ui.curses.has_colors", return_value=False):
                self.assertEqual(ui.run(screen), 0)
                open_panel.assert_called_once()
                start.assert_not_called()

    def test_schedule_button_is_visible_at_supported_widths(self):
        ui = self.demo_screen()
        for width in (62, 80, 120):
            screen = Mock(getmaxyx=Mock(return_value=(24, width)))
            ui.draw(screen)
            labels = [call.args[2] for call in screen.addstr.call_args_list]
            self.assertIn(" [ スケジュール登録 S ] ", labels)

    def test_password_form_masks_both_fields_and_cancel_discards_input(self):
        ui = self.demo_screen()
        ui.open_users()
        ui.user_index = 1
        ui.handle_modal("p")
        self.assertEqual(ui.modal, "password")
        self.assertEqual(ui.form_values, ["", ""])
        ui.form_values = ["new-private-password", "confirmation-secret"]
        for width in (62, 80, 120):
            screen = Mock(getmaxyx=Mock(return_value=(24, width)))
            ui.draw(screen)
            drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
            self.assertNotIn("new-private-password", drawn)
            self.assertNotIn("confirmation-secret", drawn)
            self.assertIn("two / TWO001", drawn)
            self.assertIn("********************", drawn)
        ui.handle_modal("\x1b")
        self.assertEqual(ui.form_values, [])
        self.assertIsNone(ui.modal)

    def test_password_form_requires_matching_nonempty_confirmation(self):
        ui = self.demo_screen()
        ui.open_form("password")
        ui.form_index = 1
        with patch.object(ui, "account_action") as save:
            for fields in (["", ""], ["one", "two"]):
                ui.form_values = fields
                ui.handle_modal("\n")
                self.assertTrue(ui.form_error)
                save.assert_not_called()
            ui.form_values = ["new-password"] * 2
            ui.handle_modal("\n")
            save.assert_called_once_with("password", "one", password="new-password")

    @patch("src.terminal_ui.threading.Thread")
    def test_password_change_refreshes_active_password_only(self, thread):
        thread.side_effect = lambda **kwargs: Mock(start=kwargs["target"])
        for name in ("one", "two"):
            with self.subTest(name=name):
                ui = self.demo_screen()
                ui.demo = False
                ui.base = Path("/unused-test-path")
                ui.password = "old-active-password"
                with patch("src.user_commands.change_password", return_value=ui.config.copy()) as save:
                    ui.account_action("password", name, password="new-password")
                    self.assertEqual(ui.form_values, [])
                    ui.poll()
                    save.assert_called_once_with(ui.base, name, "new-password")
                self.assertEqual(ui.password, "new-password" if name == "one" else "old-active-password")
                self.assertEqual(ui.student_id, "ONE001")
                self.assertEqual(ui.config["default_user"], "one")
                self.assertTrue(ui.attended)
                self.assertFalse(ui.busy)
                self.assertIn("更新しました", ui.message)

    @patch("src.terminal_ui.threading.Thread")
    def test_failed_password_change_preserves_active_credentials(self, thread):
        thread.side_effect = lambda **kwargs: Mock(start=kwargs["target"])
        ui = self.demo_screen()
        ui.demo = False
        ui.base = Path("/unused-test-path")
        ui.password = "old-password"
        with patch("src.user_commands.change_password", side_effect=CredentialError("保存できません。")):
            ui.account_action("password", "one", password="new-password")
            ui.poll()
        self.assertEqual(ui.password, "old-password")
        self.assertEqual(ui.form_values, [])
        self.assertFalse(ui.busy)
        self.assertEqual(ui.form_error, "保存できません。")

    @patch("src.terminal_ui.threading.Thread")
    def test_demo_password_change_does_not_touch_keychain(self, thread):
        thread.side_effect = lambda **kwargs: Mock(start=kwargs["target"])
        ui = self.demo_screen()
        with patch("src.user_commands.change_password") as save:
            ui.account_action("password", "one", password="demo-password")
            ui.poll()
            save.assert_not_called()
        self.assertEqual(ui.password, "")
        self.assertEqual(ui.form_values, [])


if __name__ == "__main__":
    unittest.main()
