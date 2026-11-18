"""画面で表示した授業の照合と登録可否を確認する。"""

import curses
import io
import logging

import tempfile
import unittest
from pathlib import Path
from contextlib import nullcontext, redirect_stderr
from unittest.mock import Mock, patch

from src.config import ConfigurationError
from src.room import RoomError
from src.terminal_ui import AttendanceScreen, fit_text, launch_ui
from src.verification import AttendanceError
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.config import load_config
from src.credentials import CredentialError
from src.terminal_ui import PASSWORD_TTL_SECONDS, AttendanceScreen, fit_text, launch_ui
from src.verification import AttendanceError
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
    @patch("src.browser.open_page")
    @patch("src.attendance.run_attendance")
    def test_single_user_error_logging_records_safe_reason_only(self, run, open_page, thread):
        thread.side_effect = lambda **kwargs: Mock(start=kwargs["target"])
        open_page.return_value = nullcontext(Mock())
        for exc, expected_reason in (
            (AttendanceError("確認に失敗しました。"), "確認に失敗しました。"),
            (CredentialError("Keychainにパスワードがありません。"), "Keychainにパスワードがありません。"),
            (RoomError("教室番号を確認してください。"), "教室番号を確認してください。"),
            (ConfigurationError("設定を確認してください。"), "設定を確認してください。"),
            (RuntimeError("https://internal.invalid/secret-token"), None),
        ):
            with self.subTest(exc=type(exc).__name__):
                stream = io.StringIO()
                logger = logging.getLogger("test_single_ui_log")
                logger.setLevel(logging.INFO)
                logger.handlers.clear()
                logger.addHandler(logging.StreamHandler(stream))
                ui = AttendanceScreen({"dry_run": False, "_active_username": "taro"}, "TEST001", "secret-pass", logger, "731")
                run.side_effect = exc
                ui.start()
                ui.poll()
                text = stream.getvalue()
                self.assertIn(f"画面操作停止 種別={type(exc).__name__}", text)
                self.assertNotIn("対象=", text)
                self.assertNotIn("TEST001", text)
                self.assertNotIn("secret-pass", text)
                self.assertNotIn("taro", text)
                if expected_reason is not None:
                    self.assertIn(f"理由={expected_reason}", text)
                else:
                    self.assertNotIn("secret-token", text)
                    self.assertNotIn("理由=", text)

    def test_launch_ui_handles_eof_and_keyboard_interrupt(self):
        for exc_type, expected_code in ((EOFError, 1), (KeyboardInterrupt, 130)):
            with self.subTest(exc=exc_type.__name__):
                stderr = io.StringIO()
                with patch("src.terminal_ui.sys.stdin.isatty", return_value=True), \
                        patch("src.terminal_ui.sys.stdout.isatty", return_value=True), \
                        patch("src.terminal_ui.setup_logging", return_value=Mock()), \
                        patch("src.terminal_ui.load_config", return_value={"users": [], "browser": {"headless": True, "timeout_ms": 15000}}), \
                        patch("src.terminal_ui.get_credentials", side_effect=exc_type()), \
                        redirect_stderr(stderr):
                    code = launch_ui(Path("/unused"))
                self.assertEqual(code, expected_code)
                self.assertIn("中断", stderr.getvalue())

    def _base_with_users(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        config = dict(
            CONFIG,
            users=[{"username": "one", "student_id": "TEST001"}, {"username": "two", "student_id": "TEST002"}],
            default_user="one",
            ui_selected_users=["TEST001"],
        )
        (base / "config.json").write_text(json.dumps(config), encoding="utf-8")
        return base, load_config(base / "config.json")

    @patch("src.terminal_ui.threading.Thread")
    def test_password_ttl_expiration_clears_memory_and_refetches_on_demand(self, thread):
        thread.side_effect = lambda **kwargs: Mock(start=kwargs["target"])
        base, config = self._base_with_users()
        clock = [100.0]

        def run_attendance_stub(page, cfg, student_id, password, room, route, logger, *args, on_snapshot, **kwargs):
            on_snapshot({"授業名": "テスト授業", "開始時刻": "10:00"}, False, False)
            return "未出席（確認のみ）" if cfg["dry_run"] else "出席登録成功"

        with patch("src.terminal_ui.time.monotonic", side_effect=lambda: clock[0]), \
                patch("src.terminal_ui.get_credentials", return_value=("TEST001", "refetched-secret")) as credentials, \
                patch("src.browser.open_page", side_effect=lambda cfg: nullcontext(Mock())), \
                patch("src.attendance.run_attendance", side_effect=run_attendance_stub) as attendance:
            ui = AttendanceScreen(config, "TEST001", "initial-secret", Mock(), "731", base=base)
            self.assertEqual(ui.password_ttl, PASSWORD_TTL_SECONDS)
            self.assertEqual(ui._password, "initial-secret")
            clock[0] = 399.0
            ui.poll()
            self.assertEqual(ui._password, "initial-secret")
            ui.start()
            ui.poll()
            credentials.assert_not_called()
            self.assertEqual(attendance.call_args.args[3], "initial-secret")

            clock[0] = 400.0
            ui.poll()
            self.assertEqual(ui._password, "")
            self.assertIsNone(ui._password_deadline)
            self.assertEqual(ui.password, "")

            ui.start()
            ui.poll()
            credentials.assert_called_once()
            self.assertFalse(credentials.call_args.kwargs["interactive"])
            self.assertEqual(credentials.call_args.kwargs["username"], "one")
            self.assertEqual(attendance.call_args.args[3], "refetched-secret")
            self.assertEqual(ui._password, "")

            ui.start(register=True)
            ui.poll()
            self.assertEqual(credentials.call_count, 2)
            self.assertEqual(attendance.call_args.args[3], "refetched-secret")
            self.assertEqual(ui._password, "")

            ui.password = "temp-secret"
            clock[0] = 700.0
            ui.open_users()
            self.assertEqual(ui._password, "")

            ui.password = "temp-secret"
            clock[0] = 1000.0
            with patch.object(ui.schedule_panel, "work"):
                ui.schedule_panel.open()
            self.assertEqual(ui._password, "")

            ui_no_base = AttendanceScreen({"dry_run": False}, "TEST001", "secret", Mock(), "731", password_ttl=10)
            clock[0] = 1010.0
            self.assertEqual(ui_no_base.password, "")
            self.assertEqual(ui_no_base._password, "")
            ui_no_base.start()
            ui_no_base.poll()
            self.assertTrue(ui_no_base.failed)
            self.assertIn("設定ファイル", ui_no_base.message)

    @patch("src.terminal_ui.threading.Thread")
    def test_ttl_zero_and_other_user_edits_do_not_extend_active_password_ttl(self, thread):
        thread.side_effect = lambda **kwargs: Mock(start=kwargs["target"])
        base, config = self._base_with_users()
        clock = [100.0]
        with patch("src.terminal_ui.time.monotonic", side_effect=lambda: clock[0]):
            immediate = AttendanceScreen(config, "TEST001", "secret", Mock(), password_ttl=0)
            self.assertEqual(immediate._password, "")
            self.assertEqual(immediate.password, "")

            ui = AttendanceScreen(config, "TEST001", "active-secret", Mock(), base=base, password_ttl=300)
            self.assertEqual(ui._password_deadline, 400.0)
            clock[0] = 250.0
            with patch("src.user_commands.rename_user", return_value=config), \
                    patch("src.user_commands.change_password", return_value=config):
                ui.account_action("rename", "two", new_name="two-renamed")
                ui.poll()
                self.assertEqual(ui._password_deadline, 400.0)
                ui.account_action("password", "two", password="other-secret")
                ui.poll()
                self.assertEqual(ui._password_deadline, 400.0)
            clock[0] = 400.0
            self.assertEqual(ui.password, "")
            self.assertEqual(ui._password, "")

    def test_password_cleared_on_switch_away_close_and_launch_ui_exit(self):
        base, config = self._base_with_users()
        config["users"].append({"username": "three", "student_id": "TEST003"})
        (base / "config.json").write_text(json.dumps(config), encoding="utf-8")

        ui = AttendanceScreen(config, "TEST001", "active-secret", Mock(), base=base)
        ui.clear_password()
        self.assertEqual(ui._password, "")
        self.assertIsNone(ui._password_deadline)

        ui.password = "active-secret"
        ui.open_users()
        ui.pending_user_ids = {"TEST002", "TEST003"}
        ui.apply_user_selection()
        self.assertEqual(ui._password, "")

        ui.password = "active-secret"
        screen = Mock(getmaxyx=Mock(return_value=(24, 80)), get_wch=Mock(side_effect=["q"]))
        with patch("src.terminal_ui.curses.has_colors", return_value=False):
            self.assertEqual(ui.run(screen), 0)
        self.assertEqual(ui._password, "")

        launch_base, _ = self._base_with_users()
        observed = {}

        def fake_wrapper(method):
            caller_locals = sys._getframe(1).f_locals
            observed["password_in_launch_locals"] = "password" in caller_locals
            instance = method.__self__
            observed["during_ui"] = instance.password
            observed["instance"] = instance
            return 0

        with patch("src.terminal_ui.sys.stdin.isatty", return_value=True), \
                patch("src.terminal_ui.sys.stdout.isatty", return_value=True), \
                patch("src.terminal_ui.setup_logging", return_value=Mock()), \
                patch("src.terminal_ui.get_credentials", return_value=("TEST001", "launch-secret")), \
                patch("src.terminal_ui.curses.wrapper", side_effect=fake_wrapper):
            self.assertEqual(launch_ui(launch_base), 0)
        self.assertFalse(observed["password_in_launch_locals"])
        self.assertEqual(observed["during_ui"], "launch-secret")
        self.assertEqual(observed["instance"]._password, "")

    @patch("src.terminal_ui.threading.Thread")
    def test_password_text_cursor_is_wiped_on_discard_and_submit(self, thread):
        thread.side_effect = lambda **kwargs: Mock(start=kwargs["target"])
        base, config = self._base_with_users()

        ui = AttendanceScreen(config, "TEST001", "old-secret", Mock(), base=base)
        ui.open_form("add")
        ui.form_index = 2
        for ch in "new-secret":
            ui.handle_modal(ch)
        add_editors = list(ui.form_editors)
        self.assertEqual(add_editors[2].text, "new-secret")
        self.assertGreater(add_editors[2].position, 0)
        ui.handle_modal("\x1b")
        ui.handle_modal(curses.KEY_UP)
        ui.handle_modal("\n")
        self.assertEqual(ui.form_values, [])
        self.assertEqual(ui.form_editors, [])
        for editor in add_editors:
            self.assertEqual(editor.text, "")
            self.assertEqual(editor.position, 0)

        ui.open_users()
        ui.open_form("password")
        for idx, secret in enumerate(("typed-pw", "typed-pw")):
            ui.form_index = idx
            for ch in secret:
                ui.handle_modal(ch)
        pw_editors = list(ui.form_editors)
        self.assertEqual([e.text for e in pw_editors], ["typed-pw", "typed-pw"])
        ui.handle_modal("\x1b")
        ui.handle_modal(curses.KEY_UP)
        ui.handle_modal("\n")
        for editor in pw_editors:
            self.assertEqual(editor.text, "")
            self.assertEqual(editor.position, 0)

        ui.open_form("add")
        for idx, value in enumerate(("newuser", "NEW999", "submit-secret")):
            ui.form_index = idx
            for ch in value:
                ui.handle_modal(ch)
        submit_add_editors = list(ui.form_editors)
        with patch("src.user_commands.add_user", return_value=config):
            ui.handle_modal("\n")
            ui.poll()
        self.assertIsNone(ui.submitted_form)
        for editor in submit_add_editors:
            self.assertEqual(editor.text, "")
            self.assertEqual(editor.position, 0)

        ui.open_users()
        ui.open_form("password")
        for idx, secret in enumerate(("updated-pw", "updated-pw")):
            ui.form_index = idx
            for ch in secret:
                ui.handle_modal(ch)
        submit_pw_editors = list(ui.form_editors)
        with patch("src.user_commands.change_password", side_effect=[CredentialError("保存失敗"), config]):
            ui.handle_modal("\n")
            ui.poll()
            self.assertEqual([e.text for e in submit_pw_editors], ["updated-pw", "updated-pw"])
            ui.form_index = 1
            ui.handle_modal("\n")
            ui.poll()
        self.assertIsNone(ui.submitted_form)
        self.assertEqual(ui.form_values, [])
        self.assertEqual(ui.form_editors, [])
        for editor in submit_pw_editors:
            self.assertEqual(editor.text, "")
            self.assertEqual(editor.position, 0)

    def _drawn_text(self, ui, height=24, width=80):
        screen = Mock(getmaxyx=Mock(return_value=(height, width)))
        ui.draw(screen)
        return "\n".join(call.args[2] for call in screen.addstr.call_args_list)

    def test_log_viewer_opens_via_L_switches_tabs_scrolls_and_closes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "logs").mkdir()
            (base / ".updates").mkdir()
            (base / "logs" / "attendance-2026-11-18.log").write_text("older-day-line\n", encoding="utf-8")
            lines = [f"2026-11-19 10:00:{i:02d} INFO line-{i:02d}" for i in range(1, 36)]
            lines[-1] = "2026-11-19 10:00:35 ERROR 失敗 line-35"
            (base / "logs" / "attendance-2026-11-19.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
            (base / "logs" / "launchagent.log").write_text("launchd-entry-ok\n", encoding="utf-8")
            (base / ".updates" / "updater.log").write_text("updater-warning-entry\n", encoding="utf-8")

            ui = self.demo_screen()
            ui.demo = False
            ui.base = base
            screen = Mock(getmaxyx=Mock(return_value=(24, 80)))
            screen.get_wch.side_effect = ["l", "\x1b", "L", "q", "Q"]
            with patch("src.terminal_ui.curses.has_colors", return_value=False):
                self.assertEqual(ui.run(screen), 0)
            # "l" opens logs, "\x1b" closes logs, "L" opens logs, "q" closes logs, "Q" exits UI
            self.assertIsNone(ui.modal)

            ui.open_logs()
            shown = self._drawn_text(ui)
            self.assertIn("attendance-2026-11-19.log", shown)
            self.assertNotIn("older-day-line", shown)
            self.assertIn("line-35", shown)
            self.assertNotIn("line-01", shown)

            # Scroll up by page and line to reach line-01, then back down to tail
            ui.handle_modal(curses.KEY_PPAGE)
            ui.handle_modal(curses.KEY_PPAGE)
            ui.handle_modal(curses.KEY_UP)
            scrolled = self._drawn_text(ui)
            self.assertIn("line-01", scrolled)
            self.assertNotIn("line-35", scrolled)

            ui.handle_modal(curses.KEY_NPAGE)
            ui.handle_modal(curses.KEY_DOWN)
            ui.handle_modal(curses.KEY_HOME)
            self.assertIn("line-01", self._drawn_text(ui))
            ui.handle_modal(curses.KEY_END)
            self.assertIn("line-35", self._drawn_text(ui))

            # Switch tabs via Tab/Right/Left/BTab/1/2/3
            ui.handle_modal("\t")
            self.assertIn("launchd-entry-ok", self._drawn_text(ui))
            ui.handle_modal(curses.KEY_RIGHT)
            self.assertIn("updater-warning-entry", self._drawn_text(ui))
            ui.handle_modal(curses.KEY_BTAB)
            self.assertIn("launchd-entry-ok", self._drawn_text(ui))
            ui.handle_modal(curses.KEY_LEFT)
            self.assertIn("line-35", self._drawn_text(ui))

            # Reload picks up appended lines
            with (base / "logs" / "attendance-2026-11-19.log").open("a", encoding="utf-8") as stream:
                stream.write("2026-11-19 10:00:36 INFO line-36\n")
            ui.handle_modal("r")
            self.assertIn("line-36", self._drawn_text(ui))

            # Esc and Q both close log viewer
            ui.handle_modal("\x1b")
            self.assertIsNone(ui.modal)
            ui.open_logs()
            ui.handle_modal("Q")
            self.assertIsNone(ui.modal)

    def test_log_viewer_handles_missing_empty_symlink_and_large_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ui = self.demo_screen()
            ui.demo = False
            ui.base = base

            # Missing logs/ and .updates/
            ui.open_logs()
            self.assertIn("まだありません", self._drawn_text(ui))
            ui.handle_modal("2")
            self.assertIn("まだありません", self._drawn_text(ui))
            ui.handle_modal("3")
            self.assertIn("まだありません", self._drawn_text(ui))

            # Empty and invalid-UTF8 / large tail
            (base / "logs").mkdir()
            (base / ".updates").mkdir()
            (base / "logs" / "attendance-2026-11-19.log").write_text("\n", encoding="utf-8")
            ui.handle_modal("1")
            self.assertIn("記録はまだありません", self._drawn_text(ui))

            # Symlinked updater.log is ignored safely
            secret = base / "secret.txt"
            secret.write_text("DO_NOT_READ_SYMLINK\n", encoding="utf-8")
            (base / ".updates" / "updater.log").symlink_to(secret)
            ui.handle_modal("3")
            shown = self._drawn_text(ui)
            self.assertNotIn("DO_NOT_READ_SYMLINK", shown)
            self.assertIn("まだありません", shown)

            # Large log file reads tail cleanly (drops truncated first line)
            big = base / "logs" / "attendance-2026-11-20.log"
            early_block = ("EARLY_HEAD_LINE_" + "X" * 90 + "\n") * 50
            middle_block = ("MIDDLE_LINE_" + "X" * 90 + "\n") * 700
            recent_block = ("RECENT_LINE\n") * 20
            big.write_bytes(early_block.encode("utf-8") + middle_block.encode("utf-8") + recent_block.encode("utf-8") + b"\xff\xfeTAIL_OK\n")
            ui.handle_modal("1")
            shown = self._drawn_text(ui)
            self.assertIn("TAIL_OK", shown)
            self.assertIn("RECENT_LINE", shown)
            self.assertNotIn("EARLY_HEAD_LINE_", "\n".join(ui.log_lines))

            # Unreadable log file shows friendly error message
            with patch("src.terminal_ui._read_log_tail", side_effect=PermissionError("denied")):
                ui.handle_modal("r")
                self.assertIn("読み込めませんでした", self._drawn_text(ui))

    def test_log_viewer_and_shortcuts_draw_cleanly_at_supported_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "logs").mkdir()
            (base / "logs" / "attendance-2026-11-19.log").write_text("2026-11-19 10:05:00 INFO 出席確認テスト\n", encoding="utf-8")
            ui = self.demo_screen()
            ui.demo = False
            ui.base = base

            for width in (62, 80, 120):
                ui.modal = None
                main_drawn = self._drawn_text(ui, width=width)
                self.assertIn("L: ログ", main_drawn)
                self.assertIn("L", main_drawn)

                ui.modal = "help"
                help_drawn = self._drawn_text(ui, width=width)
                self.assertIn("L: ログ", help_drawn)

                ui.open_logs()
                log_drawn = self._drawn_text(ui, width=width)
                self.assertIn("ログ確認", log_drawn)
                self.assertIn("1:実行ログ", log_drawn)
                self.assertIn("Esc/Q:戻る", log_drawn)
                self.assertIn("出席確認テスト", log_drawn)

    def test_demo_log_viewer_never_reads_disk_logs_and_none_base_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "logs").mkdir()
            (base / ".updates").mkdir()
            (base / "logs" / "attendance-2026-11-19.log").write_text("SECRET-DISK-LOG\n", encoding="utf-8")
            (base / "logs" / "launchagent.log").write_text("SECRET-AGENT-LOG\n", encoding="utf-8")
            (base / ".updates" / "updater.log").write_text("SECRET-UPDATER-LOG\n", encoding="utf-8")

            ui = self.demo_screen()
            ui.base = base
            ui.open_logs()
            for tab in ("1", "2", "3"):
                ui.handle_modal(tab)
                shown = self._drawn_text(ui)
                self.assertIn("デモ", shown)
                self.assertNotIn("SECRET-DISK-LOG", shown)
                self.assertNotIn("SECRET-AGENT-LOG", shown)
                self.assertNotIn("SECRET-UPDATER-LOG", shown)

            ui.demo = False
            ui.base = None
            ui.open_logs()
            self.assertIn("設定フォルダを確認できないため", self._drawn_text(ui))


if __name__ == "__main__":
    unittest.main()
