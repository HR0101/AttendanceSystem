"""入力位置・パスワード表示と、GUI選択の保存・復元を実ファイルで確認する。"""

import curses
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.config import ConfigurationError, load_config
from src.credentials import CredentialError
from src.terminal_layout import text_width
from src.terminal_ui import AttendanceScreen, launch_ui
from src.text_input import TextCursor
from src.user_commands import save_ui_selection
from src.users import change_student_id_config, get_users, remove_user_config, select_user, ui_selection
from test_users import CONFIG


class TextCursorTests(unittest.TestCase):
    def test_insert_backspace_and_delete_at_any_position(self):
        editor, value = TextCursor("abcd"), "abcd"
        for key in (curses.KEY_HOME, curses.KEY_RIGHT, curses.KEY_RIGHT, "X"):
            value = editor.edit(value, key, limit=20)
        self.assertEqual(value, "abXcd")
        value = editor.edit(value, curses.KEY_BACKSPACE, limit=20)
        self.assertEqual(value, "abcd")
        value = editor.edit(value, curses.KEY_DC, limit=20)
        self.assertEqual(value, "abd")
        value = editor.edit(value, "あ", limit=20)
        self.assertEqual(value, "abあd")
        self.assertEqual(editor.position, 3)

    def test_home_end_control_keys_and_bounds(self):
        editor, value = TextCursor("日本語"), "日本語"
        for key in ("\x01", curses.KEY_BACKSPACE, curses.KEY_LEFT):
            value = editor.edit(value, key, limit=20)
        self.assertEqual((value, editor.position), ("日本語", 0))
        for key in ("\x05", curses.KEY_DC, curses.KEY_RIGHT):
            value = editor.edit(value, key, limit=20)
        self.assertEqual((value, editor.position), ("日本語", 3))
        self.assertEqual(editor.edit(value, "\x15", limit=20), "")
        self.assertEqual(editor.position, 0)

    def test_limits_and_filters_apply_to_insertions_too(self):
        editor = TextCursor("731")
        value = editor.edit("731", curses.KEY_HOME, limit=3)
        value = editor.edit(value, "4", limit=3)
        self.assertEqual(value, "731")
        value = editor.edit(value, curses.KEY_DC, limit=3)
        value = editor.edit(value, "a", limit=3, accepts=str.isdecimal)
        self.assertEqual(value, "31")
        self.assertEqual(editor.edit(value, "６", limit=3, accepts=lambda text: text.isascii() and text.isdecimal()), "31")
        self.assertEqual(editor.edit(value, "\x00", limit=3), "31")

    def test_long_wide_text_scrolls_around_cursor_and_mask_hides_every_character(self):
        value = "日本語" * 30 + "SECRET"
        editor = TextCursor(value)
        for position in (len(value), 0, 20):
            editor.position = position
            for masked in (False, True):
                with self.subTest(position=position, masked=masked):
                    canvas = Mock()
                    editor.draw(canvas, 2, 3, value, 12, active=True, masked=masked)
                    calls = canvas.put.call_args_list
                    self.assertLessEqual(text_width(calls[0].args[2]), 12)
                    self.assertGreaterEqual(calls[-1].args[1], 3)
                    self.assertLessEqual(calls[-1].args[1] + text_width(calls[-1].args[2]), 15)
                    if masked:
                        self.assertNotIn("SECRET", "\n".join(call.args[2] for call in calls))
        self.assertEqual(editor.text, value)


class UIInputAndSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.path = self.base / "config.json"
        self.config = dict(CONFIG, users=[{"username": "one", "student_id": "OLD001"},
                                         {"username": "two", "student_id": "NEW002"},
                                         {"username": "three", "student_id": "THIRD003"}], default_user="one")
        self.path.write_text(json.dumps(self.config))
        thread = patch("src.terminal_ui.threading.Thread", side_effect=lambda **kwargs: Mock(start=kwargs["target"]))
        thread.start()
        self.addCleanup(thread.stop)

    def screen(self, demo=False, room=""):
        return AttendanceScreen(load_config(self.path), "OLD001", "old-secret", Mock(), room, demo, self.base)

    def drawn(self, ui, width=62):
        screen = Mock(getmaxyx=Mock(return_value=(24, width)))
        ui.draw(screen)
        return "\n".join(call.args[2] for call in screen.addstr.call_args_list)

    def test_password_visibility_toggles_for_both_fields_without_changing_values(self):
        ui = self.screen()
        ui.open_users()
        ui.open_form("password")
        ui.form_values = ["VisibleSecret", "VisibleSecret"]
        self.assertNotIn("VisibleSecret", self.drawn(ui))
        ui.handle_modal(curses.KEY_F2)
        self.assertTrue(ui.password_visible)
        self.assertIn("VisibleSecret", self.drawn(ui))
        ui.handle_modal(curses.KEY_F2)
        self.assertNotIn("VisibleSecret", self.drawn(ui))
        self.assertEqual(ui.form_values, ["VisibleSecret"] * 2)
        self.assertNotIn("VisibleSecret", self.path.read_text())

    def test_add_form_visibility_and_reopening_reset_and_discard_editor_secrets(self):
        ui = self.screen()
        ui.open_form("add")
        ui.form_values = ["new", "NEW004", "VisibleSecret"]
        ui.form_index = 2
        ui.handle_modal(curses.KEY_F2)
        self.assertIn("VisibleSecret", self.drawn(ui))
        ui.handle_modal("\x1b")
        self.assertTrue(ui.save_prompt.active)
        ui.handle_modal(curses.KEY_UP)
        ui.handle_modal("\n")
        self.assertEqual(ui.form_values, [])
        self.assertEqual(ui.form_editors, [])
        self.assertFalse(ui.password_visible)
        ui.open_form("add")
        self.assertFalse(ui.password_visible)
        self.assertEqual(ui.form_values, ["", "", ""])

    def test_submitting_visible_password_clears_form_and_visibility(self):
        ui = self.screen(demo=True)
        ui.open_users()
        ui.open_form("password")
        ui.form_values, ui.form_index = ["VisibleSecret"] * 2, 1
        ui.handle_modal(curses.KEY_F2)
        ui.handle_modal("\n")
        ui.poll()
        self.assertEqual(ui.form_values, [])
        self.assertEqual(ui.form_editors, [])
        self.assertFalse(ui.password_visible)
        self.assertIsNone(ui.modal)
        self.assertNotIn("VisibleSecret", self.path.read_text())

    def test_user_fields_allow_middle_editing_and_keep_each_cursor(self):
        ui = self.screen()
        ui.open_form("add")
        ui.form_values = ["abcd", "1234", ""]
        for key in (curses.KEY_HOME, curses.KEY_RIGHT, "X", "\t", curses.KEY_HOME, "A", curses.KEY_UP, "Y"):
            ui.handle_modal(key)
        self.assertEqual(ui.form_values[:2], ["aXYbcd", "A1234"])
        ui.handle_modal(curses.KEY_DC)
        ui.handle_modal(curses.KEY_BACKSPACE)
        self.assertEqual(ui.form_values[0], "aXcd")

    def test_long_japanese_name_can_be_edited_at_start_and_middle(self):
        ui = self.screen()
        ui.open_users()
        ui.open_form("rename")
        ui.form_values = ["日本語" * 18]
        ui.handle_modal(curses.KEY_HOME)
        ui.handle_modal("新")
        ui.handle_modal(curses.KEY_RIGHT)
        ui.handle_modal(curses.KEY_DC)
        self.assertEqual(ui.form_values[0], "新日語" + "日本語" * 17)
        for width in (62, 80, 120):
            self.assertIn("新日語", self.drawn(ui, width))

    def test_qr_editing_moves_cursor_without_triggering_a_new_lookup(self):
        ui = self.screen(demo=True, room="731")
        screen = Mock(getmaxyx=Mock(return_value=(24, 80)))
        screen.get_wch.side_effect = [curses.KEY_HOME, curses.KEY_RIGHT, curses.KEY_DC, "6", "q"]
        with patch("src.terminal_ui.curses.has_colors", return_value=False), patch.object(ui, "start") as start:
            self.assertEqual(ui.run(screen), 0)
        self.assertEqual(ui.room, "761")
        start.assert_called_once_with()

    def test_qr_input_accepts_fullwidth_and_four_digit_route_numbers(self):
        for keys, expected_room, expected_route in (
            (["６", "４", "２", "\n", "q"], "642", "642"),
            (["7", "3", "0", "1", "5", "\n", "q"], "7301", "7301"),
            (["７", "３", "０", "１", "\n", "q"], "7301", "7301"),
        ):
            with self.subTest(keys=keys):
                ui = self.screen(demo=True, room="")
                screen = Mock(getmaxyx=Mock(return_value=(24, 80)))
                screen.get_wch.side_effect = keys
                with patch("src.terminal_ui.curses.has_colors", return_value=False):
                    self.assertEqual(ui.run(screen), 0)
                self.assertEqual(ui.room, expected_room)
                self.assertEqual(ui.values.get("教室名"), f"{expected_route if expected_room == '642' else '731'}講義室")

    def test_qr_input_rejects_non_decimal_symbols_and_draws_wide_initial_room(self):
        ui = self.screen(demo=True, room="６４２")
        self.assertIn("６４２", self.drawn(ui, 80))
        ui.room = ""
        screen = Mock(getmaxyx=Mock(return_value=(24, 80)))
        screen.get_wch.side_effect = ["a", "①", "６", "q"]
        with patch("src.terminal_ui.curses.has_colors", return_value=False):
            self.assertEqual(ui.run(screen), 0)
        self.assertEqual(ui.room, "6")

    def test_schedule_name_and_time_can_be_edited_in_middle(self):
        ui = self.screen(demo=True)
        panel = ui.schedule_panel
        panel.data = panel.empty_data()
        ui.modal = "schedule"
        panel.form("add")
        panel.fields = ["授業ab", "mon", "14:00", "731", "", ""]
        for key in (curses.KEY_HOME, curses.KEY_RIGHT, "の", curses.KEY_DC):
            panel.handle(key)
        self.assertEqual(panel.fields[0], "授のab")
        panel.field_index = 2
        for key in (curses.KEY_HOME, curses.KEY_RIGHT, curses.KEY_DC, "3", "\t", curses.KEY_UP, curses.KEY_BACKSPACE, "4"):
            panel.handle(key)
        self.assertEqual(panel.fields[2], "14:00")
        self.assertEqual(panel.editors[2].position, 2)

    def test_period_dates_support_cursor_editing_and_draw_at_all_sizes(self):
        ui = self.screen(demo=True)
        panel = ui.schedule_panel
        panel.data = panel.empty_data()
        panel.data["active_from"] = "2026-01-01"
        ui.modal = "schedule"
        panel.form("period")
        for key in (curses.KEY_END, curses.KEY_LEFT, curses.KEY_DC, "2"):
            panel.handle(key)
        self.assertEqual(panel.fields[0], "2026-01-02")
        for width in (62, 80, 120):
            self.assertIn("2026-01-02", self.drawn(ui, width))

    def test_multi_selection_is_saved_and_restored_without_changing_default(self):
        ui = self.screen()
        ui.open_users()
        ui.pending_user_ids = {"NEW002", "THIRD003"}
        ui.apply_user_selection()
        config = load_config(self.path)
        self.assertEqual(config["ui_selected_users"], ["NEW002", "THIRD003"])
        self.assertEqual(config["default_user"], "one")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        reopened = self.screen()
        self.assertEqual(reopened.selected_user_ids, {"NEW002", "THIRD003"})
        self.assertEqual((reopened.student_id, reopened.password), ("NEW002", ""))
        self.assertEqual(select_user(config)["student_id"], "OLD001")
        self.assertNotIn("old-secret", self.path.read_text())

    def test_same_selection_is_saved_and_escape_does_not_save_pending_changes(self):
        ui = self.screen()
        ui.values = {"授業名": "保持する授業"}
        ui.open_users()
        ui.apply_user_selection()
        self.assertEqual(load_config(self.path)["ui_selected_users"], ["OLD001"])
        self.assertEqual(ui.values, {"授業名": "保持する授業"})
        before = self.path.read_bytes()
        ui.open_users()
        ui.handle_modal(curses.KEY_DOWN)
        ui.handle_modal(" ")
        ui.handle_modal("\x1b")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(ui.selected_user_ids, {"OLD001"})

    def test_single_user_switch_is_saved_after_credentials_are_loaded(self):
        ui = self.screen()
        ui.open_users()
        ui.pending_user_ids = {"NEW002"}
        with patch("src.terminal_ui.get_credentials", return_value=("NEW002", "second-secret")):
            ui.apply_user_selection()
            ui.poll()
        self.assertEqual((ui.student_id, ui.password), ("NEW002", "second-secret"))
        self.assertEqual(load_config(self.path)["ui_selected_users"], ["NEW002"])
        self.assertEqual(load_config(self.path)["default_user"], "one")

    def test_save_failure_leaves_selection_snapshot_and_disk_unchanged(self):
        ui = self.screen()
        ui.values = {"授業名": "保持する授業"}
        ui.open_users()
        ui.pending_user_ids = {"OLD001", "NEW002"}
        before = self.path.read_bytes()
        with patch("src.user_commands._save_private_json", side_effect=PermissionError("保存不可")):
            ui.apply_user_selection()
        self.assertEqual(ui.selected_user_ids, {"OLD001"})
        self.assertEqual(ui.values, {"授業名": "保持する授業"})
        self.assertEqual(ui.modal, "users")
        self.assertIn("保存できません", ui.form_error)
        self.assertEqual(self.path.read_bytes(), before)

    def test_selection_save_preserves_other_recently_changed_settings(self):
        ui = self.screen()
        fresh = load_config(self.path)
        fresh["browser"]["timeout_ms"] = 30000
        self.path.write_text(json.dumps(fresh))
        ui.open_users()
        ui.pending_user_ids = {"OLD001", "NEW002"}
        ui.apply_user_selection()
        self.assertEqual(load_config(self.path)["browser"]["timeout_ms"], 30000)

    def test_demo_selection_is_not_written_to_disk(self):
        ui = self.screen(demo=True)
        before = self.path.read_bytes()
        ui.open_users()
        ui.pending_user_ids = {"OLD001", "NEW002"}
        with patch("src.user_commands.save_ui_selection", side_effect=AssertionError("disk touched")):
            ui.apply_user_selection()
        self.assertEqual(ui.selected_user_ids, {"OLD001", "NEW002"})
        self.assertEqual(ui.config["ui_selected_users"], ["OLD001", "NEW002"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_saved_selection_tracks_id_change_and_deletion(self):
        config = dict(self.config, ui_selected_users=["OLD001", "NEW002"])
        config = change_student_id_config(config, "one", "FIXED001")
        self.assertEqual(config["ui_selected_users"], ["FIXED001", "NEW002"])
        config = remove_user_config(config, "one")
        self.assertEqual(config["ui_selected_users"], ["NEW002"])
        config = remove_user_config(config, "two")
        self.assertEqual(config["ui_selected_users"], ["THIRD003"])
        config = remove_user_config(config, "three")
        self.assertEqual(config["ui_selected_users"], [])

    def test_stale_ids_fall_back_and_invalid_selection_shapes_are_rejected(self):
        self.assertEqual(ui_selection(dict(self.config, ui_selected_users=["STALE"])), {"OLD001"})
        for value in (None, "OLD001", [None], [["OLD001"]], ["OLD001", "OLD001"]):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(dict(self.config, ui_selected_users=value)))
                with self.assertRaises(ConfigurationError):
                    load_config(self.path)

    def test_unregistered_or_empty_selection_cannot_be_saved(self):
        before = self.path.read_bytes()
        for selection in (set(), {"UNKNOWN"}, {"OLD001", "UNKNOWN"}):
            with self.subTest(selection=selection), self.assertRaises(CredentialError):
                save_ui_selection(self.base, selection)
        self.assertEqual(self.path.read_bytes(), before)

    def test_id_change_deduplicates_and_removes_stale_saved_ids(self):
        config = dict(self.config, ui_selected_users=["OLD001", "FIXED001", "STALE"])
        config = change_student_id_config(config, "one", "FIXED001")
        self.assertEqual(config["ui_selected_users"], ["FIXED001"])
        self.path.write_text(json.dumps(config))
        self.assertEqual(load_config(self.path)["ui_selected_users"], ["FIXED001"])

    def test_launch_restores_selection_and_explicit_user_overrides_for_one_session(self):
        self.path.write_text(json.dumps(dict(self.config, ui_selected_users=["NEW002", "THIRD003"])))
        observed = []

        def credentials(config, path, *, username):
            user = select_user(config, username)
            config["_active_username"] = user["username"]
            return user["student_id"], user["username"] + "-secret"

        def run(ui):
            observed.append((ui.student_id, ui.password, set(ui.selected_user_ids)))
            return 0

        with patch("src.terminal_ui.sys.stdin.isatty", return_value=True), patch("src.terminal_ui.sys.stdout.isatty", return_value=True), \
                patch("src.terminal_ui.setup_logging", return_value=Mock()), patch("src.terminal_ui.get_credentials", side_effect=credentials), \
                patch("src.terminal_ui.curses.wrapper", side_effect=lambda method: run(method.__self__)):
            self.assertEqual(launch_ui(self.base), 0)
            self.assertEqual(launch_ui(self.base, username="one"), 0)
        self.assertEqual(observed, [("NEW002", "two-secret", {"NEW002", "THIRD003"}), ("OLD001", "one-secret", {"OLD001"})])
        self.assertEqual(load_config(self.path)["ui_selected_users"], ["NEW002", "THIRD003"])


if __name__ == "__main__":
    unittest.main()
