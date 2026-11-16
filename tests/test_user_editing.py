"""削除・学籍番号変更のUI、保存認証情報と設定の失敗時復元。実Keychainは使わない。"""

import curses
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.config import ConfigurationError, load_config
from src.credentials import CredentialError, SERVICE
from src.terminal_ui import AttendanceScreen
from src.user_commands import change_student_id, remove_user
from src.users import get_users, select_user
from test_users import CONFIG


class MemoryKeychain:
    def __init__(self):
        self.values = {"OLD001": "old-secret", "NEW002": "second-secret"}

    def get_password(self, service, account):
        assert service == SERVICE
        return self.values.get(account)

    def set_password(self, service, account, password):
        assert service == SERVICE
        self.values[account] = password

    def delete_password(self, service, account):
        assert service == SERVICE
        del self.values[account]


class UserEditingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.path = self.base / "config.json"
        self.config = dict(CONFIG, users=[{"username": "one", "student_id": "OLD001"},
                                         {"username": "two", "student_id": "NEW002"}], default_user="one")
        self.path.write_text(json.dumps(self.config))
        self.before = self.path.read_bytes()
        self.backend = MemoryKeychain()
        backend = patch("src.user_commands.keychain_backend", return_value=self.backend)
        self.lookup = backend.start()
        self.addCleanup(backend.stop)

    def screen(self, demo=False):
        config = load_config(self.path)
        config["_active_username"] = "one"
        ui = AttendanceScreen(config, "OLD001", "old-secret", Mock(), "731", demo=demo, base=self.base)
        ui.values = {"授業名": "古い授業"}
        ui.checked = True
        ui.user_results = {"OLD001": {"values": dict(ui.values)}}
        thread = patch("src.terminal_ui.threading.Thread", side_effect=lambda **kwargs: Mock(start=kwargs["target"]))
        thread.start()
        self.addCleanup(thread.stop)
        return ui

    def assert_unchanged(self):
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(self.backend.values, {"OLD001": "old-secret", "NEW002": "second-secret"})

    def test_remove_default_clears_legacy_id_and_credentials_without_resurrection(self):
        config = remove_user(self.base, "one")
        self.assertEqual(get_users(load_config(self.path)), [{"username": "two", "student_id": "NEW002"}])
        self.assertNotIn("student_id", config)
        self.assertEqual(config["default_user"], "two")
        self.assertEqual(self.backend.values, {"NEW002": "second-secret"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("secret", self.path.read_text())

    def test_remove_other_and_last_user_preserves_valid_config(self):
        config = remove_user(self.base, "two")
        self.assertEqual(config["default_user"], "one")
        config = remove_user(self.base, "one")
        self.assertEqual(get_users(load_config(self.path)), [])
        self.assertIsNone(select_user(config))
        self.assertEqual(config["default_user"], "")
        self.assertEqual(self.backend.values, {})
        self.assertEqual(config["browser"], CONFIG["browser"])

    def test_change_id_moves_password_keeps_name_default_and_removes_legacy_id(self):
        config = change_student_id(self.base, "one", "FIXED003")
        self.assertEqual(select_user(config), {"username": "one", "student_id": "FIXED003"})
        self.assertEqual(len(get_users(load_config(self.path))), 2)
        self.assertEqual(self.backend.values, {"FIXED003": "old-secret", "NEW002": "second-secret"})
        self.assertNotIn("student_id", config)
        self.assertNotIn("old-secret", self.path.read_text())

    def test_legacy_only_profile_can_change_id_without_adding_another_user(self):
        self.path.write_text(json.dumps(CONFIG))
        config = change_student_id(self.base, "OLD001", "FIXED003")
        self.assertEqual(get_users(config), [{"username": "OLD001", "student_id": "FIXED003"}])

    def test_no_saved_password_is_allowed_and_same_id_is_a_noop(self):
        change_student_id(self.base, "one", "OLD001")
        self.lookup.assert_not_called()
        self.assert_unchanged()
        del self.backend.values["OLD001"]
        change_student_id(self.base, "one", "FIXED003")
        self.assertIsNone(self.backend.values.get("FIXED003"))
        remove_user(self.base, "one")
        self.assertEqual(get_users(load_config(self.path)), [{"username": "two", "student_id": "NEW002"}])

    def test_invalid_duplicate_unknown_and_existing_keychain_target_are_rejected(self):
        for number in ("", "Ａ１２", "A B", "../path", "NEW002"):
            with self.subTest(number=number), self.assertRaises(ConfigurationError):
                change_student_id(self.base, "one", number)
            self.assert_unchanged()
        self.lookup.assert_not_called()
        for operation in (lambda: remove_user(self.base, "missing"), lambda: change_student_id(self.base, "missing", "VALID")):
            with self.assertRaises(ConfigurationError):
                operation()
            self.assert_unchanged()
        self.backend.values["ORPHAN"] = "do-not-overwrite"
        with self.assertRaisesRegex(CredentialError, "上書きせず"):
            change_student_id(self.base, "one", "ORPHAN")
        self.assertEqual(self.backend.values["ORPHAN"], "do-not-overwrite")
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_config_save_failure_restores_credentials_for_both_operations(self):
        for operation in (lambda: remove_user(self.base, "one"), lambda: change_student_id(self.base, "one", "FIXED003")):
            with self.subTest(operation=operation), patch("src.user_commands._save_private_json", side_effect=PermissionError()), \
                    self.assertRaises(PermissionError):
                operation()
            self.assert_unchanged()

    def test_failed_old_password_deletion_restores_copy_and_leaves_config(self):
        original = self.backend.delete_password
        failed = False

        def delete(service, account):
            nonlocal failed
            if account == "OLD001" and not failed:
                failed = True
                raise RuntimeError("private-keychain-details")
            original(service, account)

        with patch.object(self.backend, "delete_password", side_effect=delete), self.assertRaises(CredentialError) as error:
            change_student_id(self.base, "one", "FIXED003")
        self.assertNotIn("private-keychain-details", str(error.exception))
        self.assert_unchanged()

    def test_unverified_password_copy_is_rejected_and_original_is_preserved(self):
        original = self.backend.set_password
        with patch.object(self.backend, "set_password", side_effect=lambda service, account, password: None if account == "FIXED003" else original(service, account, password)), \
                self.assertRaisesRegex(CredentialError, "確認できません"):
            change_student_id(self.base, "one", "FIXED003")
        self.assert_unchanged()

    def test_rollback_failure_is_reported_without_exposing_passwords(self):
        with patch("src.user_commands._save_private_json", side_effect=PermissionError()), \
                patch.object(self.backend, "set_password", side_effect=RuntimeError("old-secret")), \
                self.assertRaisesRegex(CredentialError, "復元にも失敗") as error:
            remove_user(self.base, "one")
        self.assertNotIn("old-secret", str(error.exception))
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_delete_confirmation_defaults_to_cancel_and_escape_preserves_profile(self):
        ui = self.screen()
        ui.open_users()
        for key in ("\n", "\x1b"):
            ui.handle_modal("x")
            self.assertEqual(ui.modal, "delete")
            self.assertEqual(ui.confirm_selection, 0)
            ui.handle_modal(key)
            self.assertEqual(ui.modal, "users")
            self.assert_unchanged()

    def test_confirmed_delete_updates_active_selection_and_clears_old_class(self):
        ui = self.screen()
        ui.selected_user_ids = {"OLD001", "NEW002"}
        ui.open_users()
        ui.handle_modal("x")
        ui.handle_modal("\t")
        ui.handle_modal("\n")
        ui.poll()
        self.assertEqual(ui.student_id, "NEW002")
        self.assertEqual(ui.password, "")
        self.assertEqual(ui.selected_user_ids, {"NEW002"})
        self.assertEqual(ui.pending_user_ids, {"NEW002"})
        self.assertEqual(ui.config["default_user"], "two")
        self.assertEqual(ui.values, {})
        self.assertEqual(ui.user_results, {})
        self.assertFalse(ui.can_register())
        self.assertFalse(ui.busy)

    def test_deleting_unselected_user_keeps_active_credentials(self):
        ui = self.screen()
        ui.account_action("delete", "two")
        ui.poll()
        self.assertEqual((ui.student_id, ui.password), ("OLD001", "old-secret"))
        self.assertEqual(ui.selected_user_ids, {"OLD001"})

    def test_new_active_user_loads_its_own_password_before_attendance(self):
        from contextlib import nullcontext
        ui = self.screen()
        ui.account_action("delete", "one")
        ui.poll()
        with patch("src.credentials.keychain_backend", return_value=self.backend), \
                patch("src.browser.open_page", side_effect=lambda config: nullcontext(Mock())), \
                patch("src.attendance.run_attendance", return_value="未出席（確認のみ）") as attendance:
            ui.start()
            ui.poll()
        self.assertEqual(attendance.call_args.args[2:4], ("NEW002", "second-secret"))

    def test_id_form_validates_and_preserves_other_selected_users_on_success(self):
        ui = self.screen()
        ui.selected_user_ids = {"OLD001", "NEW002"}
        ui.open_users()
        ui.handle_modal("i")
        self.assertEqual(ui.modal, "student_id")
        self.assertEqual(ui.form_values, ["OLD001"])
        for value in ("", "１２３", "NEW002", "OLD001"):
            ui.form_values = [value]
            ui.handle_modal("\n")
            self.assertEqual(ui.modal, "student_id")
            self.assertTrue(ui.form_error)
            self.assert_unchanged()
        ui.form_values = ["FIXED003"]
        ui.handle_modal("\n")
        ui.poll()
        self.assertEqual(ui.student_id, "FIXED003")
        self.assertEqual(ui.password, "old-secret")
        self.assertEqual(ui.selected_user_ids, {"FIXED003", "NEW002"})
        self.assertEqual(ui.values, {})
        self.assertFalse(ui.can_register())

    def test_last_user_can_be_deleted_and_new_user_added_without_restart(self):
        remove_user(self.base, "two")
        ui = self.screen()
        ui.account_action("delete", "one")
        ui.poll()
        self.assertEqual((ui.student_id, ui.password), ("", ""))
        self.assertEqual(ui.selected_user_ids, set())
        self.assertEqual(ui.modal, "users")
        self.assertFalse(ui.can_register())
        for width in (62, 80, 120):
            ui.draw(Mock(getmaxyx=Mock(return_value=(24, width))))
        ui.handle_modal("a")
        ui.form_values, ui.form_index = ["three", "THIRD003", "third-secret"], 2
        ui.handle_modal("\n")
        ui.poll()
        self.assertEqual((ui.student_id, ui.password), ("THIRD003", "third-secret"))
        self.assertEqual(ui.selected_user_ids, {"THIRD003"})
        self.assertEqual(select_user(load_config(self.path))["username"], "three")

    def test_demo_changes_stay_in_memory_and_never_touch_keychain(self):
        ui = self.screen(demo=True)
        with patch("src.user_commands.keychain_backend", side_effect=AssertionError("Keychain touched")), \
                patch("src.user_commands._save_private_json", side_effect=AssertionError("config touched")):
            ui.account_action("student_id", "one", student_id="FIXED003")
            ui.poll()
            self.assertEqual(ui.student_id, "FIXED003")
            ui.account_action("delete", "one")
            ui.poll()
            self.assertEqual(ui.student_id, "NEW002")
        self.assert_unchanged()

    def test_failed_save_keeps_ui_selection_and_snapshot_and_displays_error(self):
        ui = self.screen()
        with patch("src.user_commands._save_private_json", side_effect=PermissionError("保存できません")):
            ui.account_action("student_id", "one", student_id="FIXED003")
            ui.poll()
        self.assertEqual((ui.student_id, ui.password), ("OLD001", "old-secret"))
        self.assertEqual(ui.selected_user_ids, {"OLD001"})
        self.assertEqual(ui.values, {"授業名": "古い授業"})
        self.assertIn("保存できません", ui.form_error)
        self.assertFalse(ui.busy)
        self.assert_unchanged()

    def test_new_controls_and_dialogs_are_visible_at_supported_sizes(self):
        ui = self.screen()
        for width in (62, 80, 120):
            for modal in ("users", "student_id", "delete"):
                with self.subTest(width=width, modal=modal):
                    ui.open_users()
                    if modal == "student_id":
                        ui.handle_modal("i")
                    elif modal == "delete":
                        ui.handle_modal("x")
                    screen = Mock(getmaxyx=Mock(return_value=(24, width)))
                    ui.draw(screen)
                    drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
                    self.assertIn({"users": "I 学籍番号変更  X 削除", "student_id": "新しい学籍番号", "delete": "キャンセル"}[modal], drawn)
                    self.assertNotIn("old-secret", drawn)
                    self.assertNotIn("second-secret", drawn)


if __name__ == "__main__":
    unittest.main()
