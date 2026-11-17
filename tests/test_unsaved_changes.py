"""Esc/Qでの保存確認、保存失敗時の編集保持、破棄の明示選択。"""

import copy
import curses
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.config import load_config
from src.credentials import CredentialError
from src.scheduler import load_schedule, save_schedule
from src.terminal_ui import AttendanceScreen
from src.unsaved_changes import SavePrompt
from test_schedule_ui import DATA
from test_user_editing import MemoryKeychain
from test_users import CONFIG


class UnsavedChangesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.path = self.base / "config.json"
        self.config = dict(CONFIG, users=[{"username": "one", "student_id": "OLD001"},
                                         {"username": "two", "student_id": "NEW002"}],
                           default_user="one", ui_selected_users=["OLD001"])
        self.path.write_text(json.dumps(self.config))
        self.before = self.path.read_bytes()
        self.backend = MemoryKeychain()
        for name, value in (("src.user_commands.keychain_backend", self.backend),
                            ("src.credentials.keychain_backend", self.backend)):
            context = patch(name, return_value=value)
            context.start()
            self.addCleanup(context.stop)
        thread = patch("src.terminal_ui.threading.Thread", side_effect=lambda **kwargs: Mock(start=kwargs["target"]))
        thread.start()
        self.addCleanup(thread.stop)
        agent = patch("src.schedule_ui.agent_installed", return_value=False)
        agent.start()
        self.addCleanup(agent.stop)

    def screen(self):
        return AttendanceScreen(load_config(self.path), "OLD001", "old-secret", Mock(), base=self.base)

    def choose(self, ui, choice):
        self.assertTrue(ui.save_prompt.active)
        self.assertEqual(ui.save_prompt.selected, 2)
        if choice == "save":
            ui.handle_modal(curses.KEY_DOWN)
        elif choice == "discard":
            ui.handle_modal(curses.KEY_UP)
        ui.handle_modal("\n")
        ui.poll()

    def form(self, ui, mode):
        ui.open_users()
        ui.open_form(mode)
        ui.form_values = {"add": ["four", "FOUR004", "Q-secret"],
                          "rename": ["renamed"], "password": ["Q-secret", "Q-secret"],
                          "student_id": ["FIXED001"]}[mode]

    def schedule(self, ui, mode):
        save_schedule(self.base / "schedule.json", DATA)
        panel = ui.schedule_panel
        panel.data = copy.deepcopy(DATA)
        ui.modal = "schedule"
        panel.form(mode)
        if mode == "add":
            panel.fields = ["追加授業", "tue", "13:00", "642", "", ""]
        elif mode == "edit":
            panel.fields[0] = "変更授業"
        else:
            panel.fields[1] = "2026-12-22"
        return panel

    def test_user_selection_escape_and_q_prompt_without_writing(self):
        for key in ("\x1b", "q", "Q"):
            with self.subTest(key=repr(key)):
                ui = self.screen()
                ui.open_users()
                ui.pending_user_ids.add("NEW002")
                ui.handle_modal(key)
                self.assertTrue(ui.save_prompt.active)
                self.assertEqual(ui.save_prompt.selected, 2)
                self.assertEqual(self.path.read_bytes(), self.before)
                self.assertEqual(ui.selected_user_ids, {"OLD001"})
                self.choose(ui, "continue")
                self.assertEqual(ui.modal, "users")
                self.assertEqual(ui.pending_user_ids, {"OLD001", "NEW002"})

    def test_user_selection_save_and_discard_are_explicit(self):
        ui = self.screen()
        ui.open_users()
        ui.pending_user_ids.add("NEW002")
        ui.handle_modal("Q")
        self.choose(ui, "save")
        self.assertEqual(load_config(self.path)["ui_selected_users"], ["OLD001", "NEW002"])
        self.assertEqual(ui.selected_user_ids, {"OLD001", "NEW002"})
        saved = self.path.read_bytes()
        ui.open_users()
        ui.pending_user_ids.remove("NEW002")
        ui.handle_modal("\x1b")
        self.choose(ui, "discard")
        self.assertIsNone(ui.modal)
        self.assertEqual(ui.pending_user_ids, ui.selected_user_ids)
        self.assertEqual(self.path.read_bytes(), saved)

    def test_unchanged_selection_returns_without_prompt(self):
        ui = self.screen()
        ui.open_users()
        ui.handle_modal("q")
        self.assertFalse(ui.save_prompt.active)
        self.assertIsNone(ui.modal)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_untouched_account_forms_and_same_default_do_not_prompt(self):
        for mode in ("add", "rename", "password", "student_id", "default"):
            with self.subTest(mode=mode):
                ui = self.screen()
                ui.open_users()
                if mode == "default":
                    ui.handle_modal("d")
                else:
                    ui.open_form(mode)
                ui.handle_modal("\x1b")
                self.assertFalse(ui.save_prompt.active)
                self.assertEqual(ui.modal, "users")
                self.assertEqual(self.path.read_bytes(), self.before)

    def test_invalid_empty_selection_returns_to_editor(self):
        ui = self.screen()
        ui.open_users()
        ui.pending_user_ids.clear()
        ui.handle_modal("\x1b")
        self.choose(ui, "save")
        self.assertEqual(ui.modal, "users")
        self.assertIn("1人以上", ui.form_error)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(ui.pending_user_ids, set())

    def test_selection_save_failure_preserves_pending_checks(self):
        ui = self.screen()
        ui.open_users()
        ui.pending_user_ids.add("NEW002")
        ui.handle_modal("q")
        with patch("src.user_commands._save_private_json", side_effect=PermissionError("保存不可")):
            self.choose(ui, "save")
        self.assertEqual(ui.pending_user_ids, {"OLD001", "NEW002"})
        self.assertEqual(ui.selected_user_ids, {"OLD001"})
        self.assertEqual(ui.modal, "users")
        self.assertIn("保存できません", ui.form_error)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_default_user_change_can_be_saved_from_back_confirmation(self):
        ui = self.screen()
        ui.open_users()
        ui.handle_modal(curses.KEY_DOWN)
        ui.handle_modal("d")
        ui.handle_modal("q")
        self.assertEqual(self.path.read_bytes(), self.before)
        self.choose(ui, "save")
        self.assertEqual((ui.student_id, ui.password), ("NEW002", "second-secret"))
        self.assertEqual(load_config(self.path)["default_user"], "two")

    def test_default_discard_and_delete_cancel_do_not_save(self):
        ui = self.screen()
        ui.open_users()
        ui.handle_modal(curses.KEY_DOWN)
        ui.handle_modal("d")
        ui.handle_modal("\x1b")
        self.choose(ui, "discard")
        self.assertEqual(ui.modal, "users")
        ui.handle_modal("x")
        ui.handle_modal("q")
        self.assertFalse(ui.save_prompt.active)
        self.assertEqual(ui.modal, "users")
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_each_account_form_can_save_from_the_first_field_on_back(self):
        for mode in ("add", "rename", "password", "student_id"):
            with self.subTest(mode=mode):
                self.path.write_bytes(self.before)
                self.backend.values = {"OLD001": "old-secret", "NEW002": "second-secret"}
                ui = self.screen()
                self.form(ui, mode)
                ui.form_index = 0
                ui.handle_modal("\x1b")
                self.assertEqual(self.path.read_bytes(), self.before)
                self.choose(ui, "save")
                self.assertEqual(ui.modal, "users")
                self.assertEqual(ui.form_values, [])
                self.assertIsNone(ui.submitted_form)
                config = load_config(self.path)
                if mode == "add":
                    self.assertEqual(len(config["users"]), 3)
                elif mode == "rename":
                    self.assertEqual(config["users"][0]["username"], "renamed")
                elif mode == "password":
                    self.assertEqual(self.backend.values["OLD001"], "Q-secret")
                else:
                    self.assertEqual(config["users"][0]["student_id"], "FIXED001")
                self.assertNotIn("Q-secret", self.path.read_text())

    def test_discard_erases_account_draft_without_persistence(self):
        for mode in ("add", "rename", "password", "student_id"):
            with self.subTest(mode=mode):
                ui = self.screen()
                self.form(ui, mode)
                ui.handle_modal("\x1b")
                self.choose(ui, "discard")
                self.assertEqual(ui.modal, "users")
                self.assertEqual(ui.form_values, [])
                self.assertEqual(ui.form_editors, [])
                self.assertFalse(ui.password_visible)
                self.assertEqual(self.path.read_bytes(), self.before)

    def test_validation_failure_keeps_all_input(self):
        ui = self.screen()
        self.form(ui, "add")
        ui.form_values[2] = ""
        before = list(ui.form_values)
        ui.handle_modal("\x1b")
        self.choose(ui, "save")
        self.assertEqual(ui.modal, "add")
        self.assertEqual(ui.form_values, before)
        self.assertTrue(ui.form_error)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_password_is_masked_in_prompt_and_cursor_is_preserved_on_continue(self):
        ui = self.screen()
        self.form(ui, "password")
        ui.form_editors[0].sync(ui.form_values[0])
        ui.form_editors[0].position = 2
        ui.password_visible = True
        ui.handle_modal("\x1b")
        for width in (62, 80, 120):
            screen = Mock(getmaxyx=Mock(return_value=(24, width)))
            ui.draw(screen)
            shown = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
            self.assertIn("保存して戻る", shown)
            self.assertIn("保存せず戻る", shown)
            self.assertIn("編集を続ける", shown)
            self.assertNotIn("Q-secret", shown)
        self.choose(ui, "continue")
        self.assertEqual(ui.modal, "password")
        self.assertEqual(ui.form_values, ["Q-secret"] * 2)
        self.assertEqual(ui.form_editors[0].position, 2)
        self.assertFalse(ui.password_visible)

    def test_backend_failure_restores_password_form_and_allows_retry(self):
        ui = self.screen()
        self.form(ui, "password")
        ui.form_editors[0].sync(ui.form_values[0])
        ui.form_editors[0].position = 3
        ui.handle_modal("\x1b")
        with patch("src.user_commands.change_password", side_effect=CredentialError("保存不可")):
            self.choose(ui, "save")
        self.assertEqual(ui.modal, "password")
        self.assertEqual(ui.form_values, ["Q-secret"] * 2)
        self.assertEqual(ui.form_editors[0].position, 3)
        self.assertFalse(ui.password_visible)
        self.assertIn("保存不可", ui.form_error)
        self.assertEqual(ui.password, "old-secret")
        ui.handle_modal("\x1b")
        self.choose(ui, "save")
        self.assertEqual(ui.modal, "users")
        self.assertEqual(ui.form_values, [])
        self.assertEqual(ui.password, "Q-secret")

    def test_config_save_failure_restores_name_field_and_parent_selection(self):
        ui = self.screen()
        ui.open_users()
        ui.pending_user_ids.add("NEW002")
        ui.open_form("rename")
        ui.form_values = ["changed"]
        ui.form_editors[0].sync("changed")
        ui.form_editors[0].position = 2
        ui.handle_modal("\x1b")
        with patch("src.user_commands._save_private_json", side_effect=PermissionError("保存不可")):
            self.choose(ui, "save")
        self.assertEqual(ui.modal, "rename")
        self.assertEqual(ui.form_values, ["changed"])
        self.assertEqual(ui.form_editors[0].position, 2)
        self.assertEqual(ui.pending_user_ids, {"OLD001", "NEW002"})
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_demo_confirmed_saves_never_write_config_or_keychain(self):
        ui = AttendanceScreen(load_config(self.path), "OLD001", "", Mock(), demo=True, base=self.base)
        with patch("src.user_commands._save_private_json", side_effect=AssertionError("disk touched")), \
                patch("src.user_commands.keychain_backend", side_effect=AssertionError("Keychain touched")):
            ui.open_users()
            ui.pending_user_ids.add("NEW002")
            ui.handle_modal("q")
            self.choose(ui, "save")
            self.form(ui, "add")
            ui.handle_modal("\x1b")
            self.choose(ui, "save")
        self.assertEqual(len(ui.config["users"]), 3)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_q_is_a_valid_character_in_input_fields(self):
        ui = self.screen()
        ui.open_users()
        ui.open_form("password")
        ui.handle_modal("Q")
        ui.handle_modal("q")
        self.assertEqual(ui.form_values[0], "Qq")
        self.assertFalse(ui.save_prompt.active)

    def test_dirty_parent_selection_survives_child_save_and_id_change(self):
        ui = self.screen()
        ui.open_users()
        ui.pending_user_ids.add("NEW002")
        ui.open_form("student_id")
        ui.form_values = ["FIXED001"]
        ui.handle_modal("\x1b")
        self.choose(ui, "save")
        self.assertEqual(ui.modal, "users")
        self.assertEqual(ui.pending_user_ids, {"FIXED001", "NEW002"})
        self.assertEqual(ui.selected_user_ids, {"FIXED001"})
        ui.handle_modal("q")
        self.choose(ui, "save")
        self.assertEqual(load_config(self.path)["ui_selected_users"], ["FIXED001", "NEW002"])

    def test_schedule_forms_save_from_back_confirmation(self):
        for mode in ("add", "edit", "period"):
            with self.subTest(mode=mode):
                ui = self.screen()
                panel = self.schedule(ui, mode)
                before = (self.base / "schedule.json").read_bytes()
                ui.handle_modal("\x1b")
                self.assertEqual((self.base / "schedule.json").read_bytes(), before)
                self.choose(ui, "save")
                self.assertEqual(panel.mode, "list")
                self.assertEqual(panel.fields, [])
                saved = load_schedule(self.base / "schedule.json")
                self.assertEqual(saved["active_until"].isoformat(), "2026-12-22" if mode == "period" else DATA["active_until"])
                self.assertEqual(len(saved["courses"]), 2 if mode == "add" else 1)

    def test_schedule_discard_and_unchanged_forms(self):
        ui = self.screen()
        panel = self.schedule(ui, "edit")
        before = (self.base / "schedule.json").read_bytes()
        ui.handle_modal("\x1b")
        self.choose(ui, "discard")
        self.assertEqual(panel.mode, "list")
        self.assertEqual((self.base / "schedule.json").read_bytes(), before)
        panel.form("edit")
        ui.handle_modal("\x1b")
        self.assertFalse(ui.save_prompt.active)
        self.assertEqual(panel.mode, "list")
        ui.handle_modal("q")
        self.assertIsNone(ui.modal)

    def test_schedule_save_failure_keeps_editable_fields(self):
        ui = self.screen()
        panel = self.schedule(ui, "edit")
        fields = list(panel.fields)
        before = (self.base / "schedule.json").read_bytes()
        ui.handle_modal("\x1b")
        with patch("src.schedule_ui.save_schedule", side_effect=PermissionError("保存不可")):
            self.choose(ui, "save")
        self.assertEqual(panel.mode, "edit")
        self.assertEqual(panel.fields, fields)
        self.assertIn("保存不可", panel.message)
        self.assertEqual((self.base / "schedule.json").read_bytes(), before)

    def test_prompt_escape_and_q_always_continue_without_actions(self):
        for key in ("\x1b", "q", "Q", "\n"):
            prompt = SavePrompt()
            save, discard = Mock(), Mock()
            prompt.open("test", save=save, discard=discard)
            prompt.handle(key)
            self.assertFalse(prompt.active)
            save.assert_not_called()
            discard.assert_not_called()

    def test_small_terminal_cannot_bypass_unsaved_prompt(self):
        ui = self.screen()
        ui.open_users()
        ui.pending_user_ids.add("NEW002")
        size = [24, 80]
        sequence = iter(("small-q", "small-enter", "resize", curses.KEY_UP, "\n", "q"))

        def key():
            value = next(sequence)
            if value == "small-q":
                size[:] = [10, 30]
                return "q"
            if value == "small-enter":
                return "\n"
            if value == "resize":
                self.assertTrue(ui.save_prompt.active)
                self.assertEqual(self.path.read_bytes(), self.before)
                size[:] = [24, 80]
                return curses.KEY_RESIZE
            return value

        screen = Mock(getmaxyx=Mock(side_effect=lambda: tuple(size)), get_wch=Mock(side_effect=key))
        with patch("src.terminal_ui.curses.has_colors", return_value=False):
            self.assertEqual(ui.run(screen), 0)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(ui.selected_user_ids, {"OLD001"})


if __name__ == "__main__":
    unittest.main()
