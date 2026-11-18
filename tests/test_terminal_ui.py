"""画面で表示した授業の照合と登録可否を確認する。"""

import json
import tempfile
import threading
import unittest
import curses
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import Mock, patch

from src.terminal_ui import AttendanceScreen, fit_text
from src.verification import AttendanceError
from src.credentials import CredentialError
from test_users import CONFIG


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
        self.assertTrue(ui.save_prompt.active)
        ui.handle_modal(curses.KEY_UP)
        ui.handle_modal("\n")
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
        self.assertTrue(ui.save_prompt.active)
        ui.handle_modal(curses.KEY_UP)
        ui.handle_modal("\n")
        self.assertEqual(ui.form_values, [])
        self.assertEqual(ui.modal, "users")

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

    @patch("src.terminal_ui.threading.Thread")
    def test_user_submodals_return_to_users_on_escape_and_keep_parent_across_errors(self, thread):
        thread.side_effect = lambda **kwargs: Mock(start=kwargs["target"])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = {k: v for k, v in CONFIG.items() if k != "student_id"}
            config.update(users=[{"username": "one", "student_id": "ONE001"}, {"username": "two", "student_id": "TWO001"}], default_user="one")
            (base / "config.json").write_text(json.dumps(config))
            ui = AttendanceScreen(config, "ONE001", "secret", Mock(), base=base)
            for key, modal in (("v", "user_result"), ("x", "delete"), ("d", "default")):
                ui.open_users()
                ui.user_index = 0
                ui.handle_modal(key)
                self.assertEqual(ui.modal, modal)
                ui.handle_modal("\x1b")
                self.assertEqual(ui.modal, "users")
                ui.handle_modal(key)
                ui.handle_modal("q")
                self.assertEqual(ui.modal, "users")
            # 変更対象の default モーダルでも保存確認の破棄・キャンセルで "users" へ戻る。
            ui.open_users()
            ui.user_index = 1
            ui.handle_modal("d")
            ui.handle_modal("\x1b")
            self.assertTrue(ui.save_prompt.active)
            ui.handle_modal(curses.KEY_UP)
            ui.handle_modal("\n")
            self.assertEqual(ui.modal, "users")
            for action in ("add", "rename", "password", "student_id"):
                ui.open_users()
                ui.open_form(action)
                self.assertTrue(ui.form_parent_users)
                # 入力エラー後の再編集・Escでも親が "users" のまま戻る。
                ui.form_values = ["", "", ""] if action == "add" else ["", ""] if action == "password" else [""]
                ui.handle_modal("\x13")
                self.assertTrue(ui.form_error)
                self.assertTrue(ui.form_parent_users)
                ui.handle_modal("\x1b")
                if ui.save_prompt.active:
                    ui.handle_modal(curses.KEY_UP)
                    ui.handle_modal("\n")
                self.assertEqual(ui.modal, "users")
            # 保存して戻る→検証失敗後も back_after_save を持ち越さない。
            ui.open_users()
            ui.open_form("password")
            ui.form_values = ["mismatch", "other"]
            ui.handle_modal("\x1b")
            ui.handle_modal(curses.KEY_DOWN)
            ui.handle_modal("\n")
            self.assertEqual(ui.modal, "password")
            self.assertFalse(ui.back_after_save)
            ui.form_values = ["valid-pass", "valid-pass"]
            with patch("src.user_commands.change_password", return_value=dict(config)):
                ui.handle_modal("\x13")
                ui.poll()
            self.assertIsNone(ui.modal)

    @patch("src.terminal_ui.threading.Thread")
    def test_async_account_error_restores_form_cursor_parent_and_masks_password(self, thread):
        thread.side_effect = lambda **kwargs: Mock(start=kwargs["target"])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = dict(CONFIG, users=[{"username": "one", "student_id": "ONE001"}, {"username": "two", "student_id": "TWO001"}], default_user="one")
            (base / "config.json").write_text(json.dumps(config))
            ui = AttendanceScreen(config, "ONE001", "secret", Mock(), base=base)
            ui.open_users()
            ui.pending_user_ids = {"ONE001", "TWO001"}
            ui.open_form("add")
            ui.form_values = ["three", "THREE003", "hidden-pw"]
            ui.form_editors[0].sync("three")
            ui.form_editors[0].position = 2
            ui.form_editors[2].sync("hidden-pw")
            ui.form_editors[2].position = 4
            ui.form_index = 1
            ui.handle_modal("\x14")
            self.assertTrue(ui.password_visible)
            with patch("src.user_commands.add_user", side_effect=CredentialError("Keychain エラー")):
                ui.handle_modal("\x13")
                self.assertEqual(ui.form_values, [])
                self.assertFalse(ui.password_visible)
                ui.poll()
            self.assertEqual(ui.modal, "add")
            self.assertEqual(ui.form_values, ["three", "THREE003", "hidden-pw"])
            self.assertEqual([e.position for e in ui.form_editors], [2, 0, 4])
            self.assertEqual(ui.form_index, 1)
            self.assertEqual(ui.form_initial_values, ("", "", ""))
            self.assertTrue(ui.form_parent_users)
            self.assertEqual(ui.pending_user_ids, {"ONE001", "TWO001"})
            self.assertFalse(ui.password_visible)
            self.assertEqual(ui.form_error, "Keychain エラー")
            ui.handle_modal("\x1b")
            self.assertTrue(ui.save_prompt.active)
            ui.handle_modal(curses.KEY_UP)
            ui.handle_modal("\n")
            self.assertEqual(ui.modal, "users")
            # 直接フォームを開いた場合の失敗時は親モーダルなしのまま戻る。
            ui.modal = None
            ui.open_form("add")
            ui.form_values = ["three", "THREE003", "pw"]
            with patch("src.user_commands.add_user", side_effect=PermissionError()):
                ui.handle_modal("\x13")
                ui.poll()
            self.assertEqual(ui.modal, "add")
            self.assertFalse(ui.form_parent_users)
            self.assertTrue(ui.form_error)

    def test_ctrl_s_saves_any_field_and_ctrl_t_toggles_password_visibility(self):
        ui = self.demo_screen()
        ui.open_users()
        ui.open_form("add")
        ui.form_values = ["four", "FOUR004", "secret-pw"]
        ui.form_index = 0
        screen = Mock(getmaxyx=Mock(return_value=(24, 62)))
        ui.draw(screen)
        drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
        self.assertNotIn("secret-pw", drawn)
        self.assertIn("Ctrl+T", drawn)
        self.assertIn("Ctrl+S", drawn)
        ui.handle_modal("\x14")
        self.assertTrue(ui.password_visible)
        screen.addstr.reset_mock()
        ui.draw(screen)
        drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
        self.assertIn("secret-pw", drawn)
        with patch("src.terminal_ui.threading.Thread", side_effect=lambda **kwargs: Mock(start=kwargs["target"])):
            ui.handle_modal("\x13")
            ui.poll()
        self.assertIsNone(ui.modal)
        self.assertTrue(any(u["username"] == "four" for u in ui.config["users"]))
        # help モーダルでも Ctrl+T / Ctrl+S / 中断操作を表示する。
        ui.modal = "help"
        screen.addstr.reset_mock()
        ui.draw(screen)
        help_text = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
        self.assertIn("Ctrl+T", help_text)
        self.assertIn("Ctrl+S", help_text)
        self.assertIn("中断", help_text)
        # rename / student_id / password でも Ctrl+S と Ctrl+T を検証する。
        for action, values in (("rename", ["renamed"]), ("student_id", ["FIXED009"]), ("password", ["pw1", "pw1"])):
            with self.subTest(action=action):
                ui.open_users()
                ui.open_form(action)
                if action == "rename":
                    ui.handle_modal("\x14")
                    self.assertFalse(ui.password_visible)
                else:
                    ui.handle_modal("\x14")
                    self.assertEqual(ui.password_visible, action == "password")
                ui.form_values = list(values)
                ui.form_index = 0
                with patch("src.terminal_ui.threading.Thread", side_effect=lambda **kwargs: Mock(start=kwargs["target"])):
                    ui.handle_modal("\x13")
                    ui.poll()
                self.assertIsNone(ui.modal)

    def test_busy_cancellation_with_esc_q_ctrl_c_stops_safely_before_register(self):
        started = threading.Event()
        release = threading.Event()
        attempted = []
        looked_up = []

        def run(page, config, student_id, password, room, route, logger, course, start, *, on_snapshot, expected_values, cancel_event=None):
            if student_id == "TEST001":
                on_snapshot({"授業名": "テスト授業", "開始時刻": "10:00"}, False, False)
                return "未出席（確認のみ）" if config["dry_run"] else "出席登録成功"
            started.set()
            release.wait(timeout=2)
            if cancel_event is not None and cancel_event.is_set():
                from src.attendance import AttendanceCancelled
                raise AttendanceCancelled("中断")
            attempted.append(student_id)
            return "出席登録成功"

        for cancel_key in ("\x1b", "q", "\x03", KeyboardInterrupt()):
            with self.subTest(cancel_key=repr(cancel_key)):
                started.clear()
                release.clear()
                attempted.clear()
                looked_up.clear()
                users = [{"username": f"user{i}", "student_id": f"TEST00{i}"} for i in range(1, 7)]
                config = {"users": users, "default_user": "user1", "_active_username": "user1", "dry_run": False}
                ui = AttendanceScreen(config, "TEST001", "pw1", Mock(), "731", base=Path("/unused"))
                ui.selected_user_ids = {u["student_id"] for u in users}
                ui.user_results = {u["student_id"]: {"values": {"授業名": "テスト授業", "開始時刻": "10:00"}, "attended": False, "unenrolled": False, "failed": False} for u in users}

                def credentials(cfg, path, *, interactive, username):
                    looked_up.append(username)
                    num = username.removeprefix("user")
                    return f"TEST00{num}", f"pw{num}"

                def next_key():
                    if ui.busy:
                        started.wait(timeout=2)
                        if ui.cancel_event is not None and not ui.cancel_event.is_set():
                            if isinstance(cancel_key, BaseException):
                                raise cancel_key
                            return cancel_key
                        release.set()
                        return curses.KEY_RESIZE
                    return "q"

                screen = Mock(getmaxyx=Mock(return_value=(24, 80)), get_wch=Mock(side_effect=next_key))
                with patch("src.terminal_ui.curses.has_colors", return_value=False), \
                        patch("src.terminal_ui.get_credentials", side_effect=credentials), \
                        patch("src.browser.open_page", side_effect=lambda cfg: nullcontext(Mock())), \
                        patch("src.attendance.run_attendance", side_effect=run):
                    ui.start(register=True)
                    self.assertEqual(ui.run(screen), 0)
                self.assertFalse(ui.busy)
                self.assertTrue(ui.cancelled)
                self.assertEqual(attempted, [])
                self.assertNotIn("user6", looked_up)
                self.assertIn("中断", ui.message)
                self.assertIn("中断", ui.status()[0])
                self.assertEqual(ui.user_results["TEST006"]["result"], "中断しました")

    def test_single_user_cancel_and_account_busy_does_not_exit_early(self):
        ui = self.screen()
        ui.room = "731"
        started = threading.Event()
        release = threading.Event()

        def run(*args, cancel_event=None, **kwargs):
            started.set()
            release.wait(timeout=2)
            if cancel_event is not None and cancel_event.is_set():
                from src.attendance import AttendanceCancelled
                raise AttendanceCancelled("中断")
            return "出席登録成功"

        def next_key():
            if ui.busy:
                started.wait(timeout=2)
                if ui.cancel_event is not None and not ui.cancel_event.is_set():
                    return "\x1b"
                release.set()
                return curses.KEY_RESIZE
            return "q"

        screen = Mock(getmaxyx=Mock(return_value=(24, 80)), get_wch=Mock(side_effect=next_key))
        with patch("src.terminal_ui.curses.has_colors", return_value=False), \
                patch("src.browser.open_page", side_effect=lambda cfg: nullcontext(Mock())), \
                patch("src.attendance.run_attendance", side_effect=run):
            ui.start(register=True)
            self.assertEqual(ui.run(screen), 0)
        self.assertFalse(ui.busy)
        self.assertTrue(ui.cancelled)
        self.assertFalse(ui.can_register())
        self.assertEqual(ui.status()[0], "処理を中断しました")
        # account_action 等の cancel_event なし処理中は Esc/Q でも中断終了しない。
        ui.busy = True
        ui.cancel_event = None
        ui.room = ""
        seq = iter(("\x1b", "q", "done", "q"))

        def account_key():
            val = next(seq)
            if val == "done":
                ui.busy = False
                return curses.KEY_RESIZE
            return val

        screen2 = Mock(getmaxyx=Mock(return_value=(24, 80)), get_wch=Mock(side_effect=account_key))
        with patch("src.terminal_ui.curses.has_colors", return_value=False):
            self.assertEqual(ui.run(screen2), 0)

    def test_small_terminal_and_demo_busy_cancellation_never_exit_early(self):
        ui = self.demo_screen()
        ui.room = "731"
        ui.busy = True
        ui.cancel_event = threading.Event()
        keys = iter(("\x1b", "finish", "q"))

        def next_key():
            value = next(keys)
            if value == "finish":
                self.assertTrue(ui.cancel_event.is_set())
                self.assertEqual(ui.message, "中断しています…")
                self.assertEqual(ui.status()[0], "中断しています…")
                ui.busy = False
                ui.cancel_event = None
                return curses.KEY_RESIZE
            return value

        screen = Mock(getmaxyx=Mock(return_value=(10, 30)), get_wch=Mock(side_effect=next_key))
        with patch("src.terminal_ui.curses.has_colors", return_value=False):
            self.assertEqual(ui.run(screen), 0)


if __name__ == "__main__":
    unittest.main()
