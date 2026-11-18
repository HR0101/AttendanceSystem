"""既定変更後も、開いている画面と認証情報が旧ユーザーのままになる不具合。"""

import curses
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.config import load_config
from src.credentials import SERVICE
from src.terminal_ui import AttendanceScreen
from test_users import CONFIG


class LiveDefaultUserTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.path = self.base / "config.json"
        self.config = dict(CONFIG, users=[{"username": "one", "student_id": "OLD001"},
                                         {"username": "two", "student_id": "NEW002"},
                                         {"username": "three", "student_id": "THIRD003"}],
                           default_user="one", ui_selected_users=["OLD001"], private_marker="keep")
        self.path.write_text(json.dumps(self.config))
        self.before = self.path.read_bytes()
        self.backend = Mock()
        self.backend.get_password.side_effect = lambda service, account: {"OLD001": "old-secret", "NEW002": "new-secret", "THIRD003": "third-secret"}.get(account)
        keychain = patch("src.credentials.keychain_backend", return_value=self.backend)
        keychain.start()
        self.addCleanup(keychain.stop)
        account_keychain = patch("src.user_commands.keychain_backend", return_value=self.backend)
        account_keychain.start()
        self.addCleanup(account_keychain.stop)
        thread = patch("src.terminal_ui.threading.Thread", side_effect=lambda **kwargs: Mock(start=kwargs["target"]))
        thread.start()
        self.addCleanup(thread.stop)

    def screen(self, *, demo=False, room=""):
        ui = AttendanceScreen(load_config(self.path), "OLD001", "old-secret", Mock(), room=room, demo=demo, base=self.base)
        ui.values = {"授業名": "旧ユーザーの授業", "開始時刻": "10:00"}
        ui.user_results = {"OLD001": {"values": dict(ui.values), "attended": True}}
        ui.attended = ui.checked = True
        return ui

    def choose_second_as_default(self, ui):
        ui.open_users()
        ui.handle_modal(curses.KEY_DOWN)
        ui.handle_modal("d")
        self.assertEqual(ui.modal, "default")
        self.assertEqual(ui.confirm_selection, 0)
        ui.handle_modal("\t")
        self.assertEqual(ui.confirm_selection, 1)
        ui.handle_modal("\n")
        ui.poll()

    def test_u_d_enter_switches_current_screen_credentials_and_saved_selection(self):
        ui = self.screen()
        self.choose_second_as_default(ui)
        self.assertEqual(load_config(self.path)["default_user"], "two")
        self.assertEqual(ui.student_id, "NEW002")
        self.assertEqual(ui.password, "new-secret")
        self.assertEqual(ui.config["_active_username"], "two")
        self.assertEqual(ui.selected_user_ids, {"NEW002"})
        self.assertEqual(ui.pending_user_ids, {"NEW002"})
        self.assertEqual(load_config(self.path)["ui_selected_users"], ["NEW002"])
        self.assertEqual(ui.values, {})
        self.assertEqual(ui.user_results, {})
        self.assertFalse(ui.attended)
        self.assertFalse(ui.confirm)
        self.assertFalse(ui.can_register())
        self.backend.get_password.assert_called_once_with(SERVICE, "NEW002")
        self.backend.set_password.assert_not_called()
        self.backend.delete_password.assert_not_called()
        self.assertNotIn("new-secret", self.path.read_text())
        self.assertEqual(load_config(self.path)["private_marker"], "keep")

    def test_existing_qr_is_rechecked_with_new_credentials_without_registration(self):
        ui = self.screen(room="731")
        values = {"授業名": "新ユーザーの授業", "教室名": "731", "時限": "2限", "開始時刻": "10:00", "終了時刻": "11:00"}

        def attendance(*args, on_snapshot, **kwargs):
            self.assertEqual(args[2:4], ("NEW002", "new-secret"))
            self.assertTrue(args[1]["dry_run"])
            self.assertIsNone(kwargs["expected_values"])
            on_snapshot(values, False, False)
            return "未出席（確認のみ）"

        with patch("src.browser.open_page", return_value=nullcontext(Mock())), \
                patch("src.attendance.run_attendance", side_effect=attendance) as run:
            self.choose_second_as_default(ui)
        run.assert_called_once()
        self.assertEqual(ui.values, values)
        self.assertEqual(set(ui.user_results), {"NEW002"})
        self.assertTrue(ui.can_register())
        self.assertFalse(ui.attended)
        self.assertFalse(ui.busy)

    def test_group_and_saved_selection_are_replaced_by_the_confirmed_default(self):
        config = dict(self.config, ui_selected_users=["OLD001", "NEW002", "THIRD003"])
        self.path.write_text(json.dumps(config))
        ui = self.screen()
        self.assertEqual(len(ui.selected_user_ids), 3)
        self.choose_second_as_default(ui)
        self.assertEqual(ui.selected_user_ids, {"NEW002"})
        self.assertEqual(ui.pending_user_ids, {"NEW002"})
        self.assertEqual(load_config(self.path)["ui_selected_users"], ["NEW002"])
        self.assertEqual(ui.user_results, {})

    def test_same_default_still_activates_it_when_saved_ui_selection_differs(self):
        self.path.write_text(json.dumps(dict(self.config, default_user="two")))
        ui = self.screen()
        self.assertEqual(ui.student_id, "OLD001")
        self.choose_second_as_default(ui)
        self.assertEqual(ui.student_id, "NEW002")
        self.assertEqual(ui.password, "new-secret")
        self.assertEqual(ui.selected_user_ids, {"NEW002"})

    def test_reopening_screen_restores_the_new_target_instead_of_the_previous_saved_user(self):
        ui = self.screen()
        self.choose_second_as_default(ui)
        config = load_config(self.path)
        reopened = AttendanceScreen(config, "OLD001", "old-secret", Mock(), base=self.base)
        self.assertEqual(reopened.selected_user_ids, {"NEW002"})
        self.assertEqual(reopened.student_id, "NEW002")
        self.assertEqual(reopened.password, "")

    def assert_previous_state(self, ui):
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual((ui.student_id, ui.password), ("OLD001", "old-secret"))
        self.assertEqual(ui.selected_user_ids, {"OLD001"})
        self.assertEqual(ui.values["授業名"], "旧ユーザーの授業")
        self.assertTrue(ui.attended)
        self.assertFalse(ui.busy)

    def test_missing_password_does_not_change_config_or_live_state_and_guides_to_p(self):
        self.backend.get_password.side_effect = None
        self.backend.get_password.return_value = None
        ui = self.screen()
        self.choose_second_as_default(ui)
        self.assert_previous_state(ui)
        self.assertIn("ユーザー管理のP", ui.form_error)

    def test_keychain_failure_does_not_commit_and_does_not_leak_details(self):
        self.backend.get_password.side_effect = RuntimeError("private-keychain-details")
        ui = self.screen()
        self.choose_second_as_default(ui)
        self.assert_previous_state(ui)
        self.assertIn("Keychain", ui.form_error)
        self.assertNotIn("private-keychain-details", ui.form_error)

    def test_save_failure_preserves_default_credentials_and_snapshot(self):
        ui = self.screen()
        with patch("src.user_commands._save_private_json", side_effect=PermissionError("保存不可")):
            self.choose_second_as_default(ui)
        self.assert_previous_state(ui)
        self.assertIn("保存不可", ui.form_error)

    def test_escape_from_confirmation_does_not_change_any_state(self):
        ui = self.screen()
        ui.open_users()
        ui.handle_modal(curses.KEY_DOWN)
        ui.handle_modal("d")
        ui.handle_modal(chr(27))
        self.assert_previous_state(ui)
        self.backend.get_password.assert_not_called()

    def test_demo_switches_immediately_but_never_saves_or_reads_keychain(self):
        ui = self.screen(demo=True)
        with patch("src.user_commands._save_private_json", side_effect=AssertionError("config touched")), \
                patch("src.terminal_ui.agent_installed", side_effect=AssertionError("LaunchAgent touched")):
            self.choose_second_as_default(ui)
        self.assertEqual((ui.student_id, ui.password), ("NEW002", ""))
        self.assertEqual(ui.config["default_user"], "two")
        self.assertEqual(ui.selected_user_ids, {"NEW002"})
        self.assertEqual(ui.values, {})
        self.assertEqual(self.path.read_bytes(), self.before)
        self.backend.get_password.assert_not_called()

    def test_confirmation_explains_live_switch_and_main_screen_shows_new_user(self):
        for width in (62, 80, 120):
            with self.subTest(width=width):
                self.path.write_bytes(self.before)
                ui = self.screen()
                ui.open_users()
                ui.handle_modal(curses.KEY_DOWN)
                ui.handle_modal("d")
                self.assertEqual(ui.confirm_selection, 0)
                screen = Mock(getmaxyx=Mock(return_value=(24, width)))
                ui.draw(screen)
                shown = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
                flat = shown.replace("\n", "")
                self.assertIn("one (OLD001) → two (NEW002)", flat)
                self.assertIn("現在の出席対象（1人選択中）", flat)
                self.assertIn("1人のみに切り替わります", flat)
                self.assertIn("自動実行（停止中）も今後は two (NEW002)", flat)
                self.assertIn("[ キャンセル ]", shown)
                self.assertIn("[ 切り替える ]", shown)
                ui.handle_modal("\t")
                self.assertEqual(ui.confirm_selection, 1)
                ui.handle_modal("\n")
                ui.poll()
                screen.reset_mock()
                ui.draw(screen)
                shown = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
                self.assertIn("NEW002", shown)
                self.assertNotIn("OLD001", shown)
                self.assertNotIn("旧ユーザーの授業", shown)
                self.assertNotIn("new-secret", shown)
                self.assertNotIn("old-secret", shown)

    def test_initial_cancel_enter_returns_to_users_without_changing_anything(self):
        ui = self.screen()
        ui.open_users()
        ui.handle_modal(curses.KEY_DOWN)
        ui.handle_modal("d")
        self.assertEqual(ui.modal, "default")
        self.assertEqual(ui.confirm_selection, 0)
        ui.handle_modal("\n")
        self.assertEqual(ui.modal, "users")
        self.assert_previous_state(ui)
        self.backend.get_password.assert_not_called()

    def test_toggle_keys_and_impact_states_cover_active_unknown_and_unset_default(self):
        config = dict(self.config, default_user="", ui_selected_users=["OLD001", "NEW002", "THIRD003"])
        self.path.write_text(json.dumps(config))
        ui = self.screen()
        self.assertEqual(len(ui.selected_user_ids), 3)
        ui.open_users()
        ui.handle_modal(curses.KEY_DOWN)
        with patch("src.terminal_ui.agent_installed", return_value=True):
            ui.handle_modal("d")
        for key in ("\t", curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_BTAB):
            before = ui.confirm_selection
            ui.handle_modal(key)
            self.assertEqual(ui.confirm_selection, 1 - before)
        screen = Mock(getmaxyx=Mock(return_value=(24, 80)))
        ui.draw(screen)
        flat = "".join(call.args[2] for call in screen.addstr.call_args_list)
        self.assertIn("既定ユーザー: 未設定 → two (NEW002)", flat)
        self.assertIn("現在の出席対象（3人選択中）", flat)
        self.assertIn("自動実行（有効中）も今後は two (NEW002) で実行されます。", flat)

        with patch("src.terminal_ui.agent_installed", side_effect=OSError("launchctl missing")):
            lines = ui.default_switch_impact({"username": "two", "student_id": "NEW002"})
        self.assertIn("自動実行（状態未確認）", lines[2])

        self.path.write_bytes(self.before)
        same_ui = self.screen()
        same_lines = same_ui.default_switch_impact({"username": "one", "student_id": "OLD001"}, agent_active=False)
        self.assertIn("one (OLD001)（変更なし）", same_lines[0])
        self.assertIn("one (OLD001) のままです", same_lines[1])
        self.assertIn("自動実行（停止中）は引き続き one (OLD001)", same_lines[2])

    def test_save_prompt_shows_same_impact_and_handles_discard_and_failures(self):
        config = dict(self.config, ui_selected_users=["OLD001", "NEW002", "THIRD003"])
        self.path.write_text(json.dumps(config))
        before = self.path.read_bytes()
        ui = self.screen()
        ui.open_users()
        ui.handle_modal(curses.KEY_DOWN)
        with patch("src.terminal_ui.agent_installed", return_value=True) as probe:
            ui.handle_modal("d")
            ui.handle_modal("q")
        probe.assert_called_once_with(self.base)
        self.assertTrue(ui.save_prompt.active)
        for width in (62, 80, 120):
            with self.subTest(width=width):
                screen = Mock(getmaxyx=Mock(return_value=(24, width)))
                ui.draw(screen)
                shown = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
                flat = shown.replace("\n", "")
                self.assertIn("one (OLD001) → two (NEW002)", flat)
                self.assertIn("現在の出席対象（3人選択中）", flat)
                self.assertIn("自動実行（有効中）も今後は two (NEW002)", flat)
                self.assertIn("[ 保存して戻る ]", shown)
                self.assertIn("[ 保存せず戻る ]", shown)
                self.assertIn("[ 編集を続ける ]", shown)

        # 保存せず戻るを選んでも状態は変わらない
        ui.handle_modal(curses.KEY_UP)
        ui.handle_modal("\n")
        self.assertFalse(ui.save_prompt.active)
        self.assertEqual(ui.modal, "users")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(ui.selected_user_ids, {"OLD001", "NEW002", "THIRD003"})

        # 保存確認経由での保存失敗・Keychain失敗時も従来の状態を保つ
        for failure in ("save", "keychain"):
            with self.subTest(failure=failure):
                self.path.write_bytes(self.before)
                ui = self.screen()
                ui.open_users()
                ui.handle_modal(curses.KEY_DOWN)
                ui.handle_modal("d")
                ui.handle_modal("\x1b")
                ui.handle_modal(curses.KEY_DOWN)
                if failure == "save":
                    with patch("src.user_commands._save_private_json", side_effect=PermissionError("保存不可")):
                        ui.handle_modal("\n")
                        ui.poll()
                    self.assertIn("保存不可", ui.form_error)
                else:
                    with patch.object(self.backend, "get_password", side_effect=RuntimeError("secret-detail")):
                        ui.handle_modal("\n")
                        ui.poll()
                    self.assertIn("Keychain", ui.form_error)
                    self.assertNotIn("secret-detail", ui.form_error)
                self.assert_previous_state(ui)
                self.assertEqual(ui.modal, "users")


if __name__ == "__main__":
    unittest.main()
