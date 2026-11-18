"""白背景・NO_COLOR 環境・色覚配慮（文字接頭辞と属性フォールバック）の確認。"""

import copy
import curses
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.scheduler import ScheduleError, save_schedule
from src.terminal_layout import Canvas, color, detect_theme, init_colors, no_color_enabled, prefix_text
from src.terminal_ui import AttendanceScreen
from src.unsaved_changes import SavePrompt
from test_schedule_ui import DATA


class UIAccessibilityTests(unittest.TestCase):
    def make_screen(self, *, demo=True, room="731", base=None):
        config = {
            "users": [
                {"username": "one", "student_id": "TEST001"},
                {"username": "two", "student_id": "TEST002"},
            ],
            "default_user": "one",
            "_active_username": "one",
            "dry_run": False,
        }
        return AttendanceScreen(config, "TEST001", "", Mock(), room=room, demo=demo, base=base)

    def render(self, ui, height=24, width=80):
        screen = Mock(getmaxyx=Mock(return_value=(height, width)))
        ui.draw(screen)
        calls = [(call.args[0], call.args[1], call.args[2], call.args[3] if len(call.args) > 3 else 0)
                 for call in screen.addstr.call_args_list]
        text = "\n".join(item[2] for item in calls)
        return screen, calls, text

    def test_no_color_env_detection_and_headless_color_safety(self):
        self.assertFalse(no_color_enabled({}))
        self.assertFalse(no_color_enabled({"NO_COLOR": ""}))
        for value in ("1", "0", "true"):
            self.assertTrue(no_color_enabled({"NO_COLOR": value}))

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(color(1), 0)

        with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True), \
                patch("src.terminal_layout.curses.color_pair", return_value=256) as color_pair:
            self.assertEqual(color(1), 0)
            color_pair.assert_not_called()

    def test_theme_detection_supports_explicit_setting_and_colorfgbg(self):
        self.assertEqual(detect_theme({}), "dark")
        self.assertEqual(detect_theme({"ATTENDANCE_UI_THEME": "light"}), "light")
        self.assertEqual(detect_theme({"ATTENDANCE_UI_THEME": " LIGHT "}), "light")
        self.assertEqual(detect_theme({"ATTENDANCE_UI_THEME": "dark", "COLORFGBG": "0;15"}), "dark")
        self.assertEqual(detect_theme({"ATTENDANCE_UI_THEME": "auto", "COLORFGBG": "0;15"}), "light")
        self.assertEqual(detect_theme({"ATTENDANCE_UI_THEME": "unknown", "COLORFGBG": "0;7"}), "light")
        for bg_light in ("0;15", "0;7", "0;default;15", "0;255", "0;231"):
            with self.subTest(colorfgbg=bg_light):
                self.assertEqual(detect_theme({"COLORFGBG": bg_light}), "light")
        for bg_dark in ("15;0", "0;8", "15;232", "0;16", "default;default", "", "invalid"):
            with self.subTest(colorfgbg=bg_dark):
                self.assertEqual(detect_theme({"COLORFGBG": bg_dark}), "dark")

    def test_init_colors_respects_no_color_and_monochrome_terminals(self):
        for env, has_colors in (({"NO_COLOR": "1"}, True), ({}, False)):
            with self.subTest(env=env, has_colors=has_colors), \
                    patch("src.terminal_layout.curses.has_colors", return_value=has_colors), \
                    patch("src.terminal_layout.curses.start_color") as start_color, \
                    patch("src.terminal_layout.curses.use_default_colors") as use_default, \
                    patch("src.terminal_layout.curses.init_pair") as init_pair:
                self.assertFalse(init_colors(env))
                start_color.assert_not_called()
                use_default.assert_not_called()
                init_pair.assert_not_called()

    def test_light_theme_avoids_yellow_and_cyan_and_uses_high_contrast_pairs(self):
        for env in ({"ATTENDANCE_UI_THEME": "light"}, {"COLORFGBG": "0;15"}, {"COLORFGBG": "0;7"}):
            with self.subTest(env=env), \
                    patch("src.terminal_layout.curses.has_colors", return_value=True), \
                    patch("src.terminal_layout.curses.start_color") as start_color, \
                    patch("src.terminal_layout.curses.use_default_colors"), \
                    patch("src.terminal_layout.curses.init_pair") as init_pair:
                self.assertTrue(init_colors(env))
                start_color.assert_called_once()
                pairs = {call.args[0]: (call.args[1], call.args[2]) for call in init_pair.call_args_list}
                self.assertEqual(pairs, {
                    1: (curses.COLOR_BLUE, -1),
                    2: (curses.COLOR_GREEN, -1),
                    3: (curses.COLOR_RED, -1),
                    4: (curses.COLOR_MAGENTA, -1),
                })
                foregrounds = {fg for fg, _ in pairs.values()}
                self.assertNotIn(curses.COLOR_YELLOW, foregrounds)
                self.assertNotIn(curses.COLOR_CYAN, foregrounds)

    def test_init_colors_default_color_fallback_and_curses_error_safety(self):
        for theme, expected_bg in (("light", curses.COLOR_WHITE), ("dark", curses.COLOR_BLACK)):
            with self.subTest(theme=theme), \
                    patch("src.terminal_layout.curses.has_colors", return_value=True), \
                    patch("src.terminal_layout.curses.start_color"), \
                    patch("src.terminal_layout.curses.use_default_colors", side_effect=curses.error), \
                    patch("src.terminal_layout.curses.init_pair") as init_pair:
                self.assertTrue(init_colors({"ATTENDANCE_UI_THEME": theme}))
                self.assertTrue(all(call.args[2] == expected_bg for call in init_pair.call_args_list))

        with patch("src.terminal_layout.curses.has_colors", return_value=True), \
                patch("src.terminal_layout.curses.start_color", side_effect=curses.error):
            self.assertFalse(init_colors({}))

    def test_run_with_no_color_skips_color_setup_and_retains_bold_reverse_and_prefixes(self):
        ui = self.make_screen()
        ui.room = "731"
        ui.failed = True
        ui.message = "通信に失敗しました。"
        ui.update_version = "9.9.9"
        screen = Mock(getmaxyx=Mock(return_value=(24, 80)), get_wch=Mock(side_effect=["q"]))
        with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True), \
                patch.object(ui, "start"), \
                patch("src.terminal_layout.curses.has_colors", return_value=True), \
                patch("src.terminal_layout.curses.start_color") as start_color, \
                patch("src.terminal_layout.curses.init_pair") as init_pair, \
                patch("src.terminal_layout.curses.color_pair", return_value=512) as color_pair:
            self.assertEqual(ui.run(screen), 0)
            start_color.assert_not_called()
            init_pair.assert_not_called()
            color_pair.assert_not_called()

        attrs = [call.args[3] for call in screen.addstr.call_args_list if len(call.args) > 3]
        texts = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
        self.assertFalse(ui.use_colors)
        self.assertTrue(any(attr & curses.A_BOLD for attr in attrs))
        self.assertTrue(any(attr & curses.A_REVERSE for attr in attrs))
        self.assertTrue(any(attr & curses.A_UNDERLINE for attr in attrs))
        self.assertIn("[更新] アップデートがあります: v9.9.9", texts)
        self.assertIn("[エラー] 確認できませんでした", texts)
        self.assertIn("[エラー] 通信に失敗しました。", texts)

    def test_run_with_light_background_initializes_light_palette_without_yellow(self):
        ui = self.make_screen(room="")
        screen = Mock(getmaxyx=Mock(return_value=(24, 80)), get_wch=Mock(side_effect=["q"]))
        with patch.dict(os.environ, {"COLORFGBG": "0;15"}, clear=True), \
                patch("src.terminal_layout.curses.has_colors", return_value=True), \
                patch("src.terminal_layout.curses.start_color"), \
                patch("src.terminal_layout.curses.use_default_colors"), \
                patch("src.terminal_layout.curses.init_pair") as init_pair, \
                patch("src.terminal_layout.curses.color_pair", side_effect=lambda number: number * 256):
            self.assertEqual(ui.run(screen), 0)
        self.assertTrue(ui.use_colors)
        foregrounds = [call.args[1] for call in init_pair.call_args_list]
        self.assertEqual(foregrounds, [curses.COLOR_BLUE, curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_MAGENTA])
        self.assertNotIn(curses.COLOR_YELLOW, foregrounds)
        self.assertNotIn(curses.COLOR_CYAN, foregrounds)

    def test_canvas_button_keeps_distinct_non_color_attributes_under_no_color(self):
        screen = Mock(getmaxyx=Mock(return_value=(24, 80)))
        with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
            canvas = Canvas(screen)
            canvas.button(2, 2, "[ A ]", focused=False, enabled=True)
            canvas.button(4, 2, "[ B ]", focused=True, enabled=True)
            canvas.button(6, 2, "[ C ]", focused=False, enabled=False)
        attrs = [call.args[3] for call in screen.addstr.call_args_list]
        self.assertEqual(attrs[0], curses.A_BOLD)
        self.assertEqual(attrs[1], curses.A_BOLD | curses.A_REVERSE)
        self.assertEqual(attrs[2], curses.A_DIM)

    def test_status_and_error_banners_have_text_prefixes_at_supported_sizes(self):
        for width in (62, 80, 120):
            with self.subTest(width=width), patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
                ui = self.make_screen()
                ui.update_version = "1.2.3"
                ui.failed = True
                ui.message = "接続できませんでした。"
                _, calls, text = self.render(ui, width=width)
                self.assertIn("[更新] アップデートがあります: v1.2.3", text)
                self.assertIn("[エラー] 確認できませんでした", text)
                self.assertIn("[エラー] 接続できませんでした。", text)
                error_attrs = [attr for _, _, line, attr in calls if "[エラー] 接続できませんでした。" in line]
                self.assertTrue(error_attrs and (error_attrs[0] & curses.A_BOLD))
                self.assertEqual(ui.message, "接続できませんでした。")

                ui.failed = False
                ui.unenrolled = True
                _, _, text = self.render(ui, width=width)
                self.assertIn("[注意] 履修登録を確認してください", text)

                ui.unenrolled = False
                ui.values = {"授業名": "テスト講義"}
                _, _, text = self.render(ui, width=width)
                self.assertIn("[注意] 未出席", text)

                ui.attended = True
                _, _, text = self.render(ui, width=width)
                self.assertIn("[完了] 出席済み", text)

    def test_small_screen_still_renders_update_and_status_prefixes(self):
        ui = self.make_screen()
        ui.failed = True
        ui.update_version = "2.0.0"
        ui.save_prompt.open("編集", save=Mock(), discard=Mock())
        _, _, text = self.render(ui, height=20, width=50)
        self.assertIn("[エラー] 確認できませんでした", text)
        self.assertIn("[更新] 更新あり: v2.0.0", text)
        self.assertIn("[注意] 未保存の変更があります", text)

    def test_account_success_after_failure_does_not_get_false_error_prefix(self):
        ui = self.make_screen()
        ui.failed = True
        ui.events.put(("account", (dict(ui.config), "TEST001", "", "password", {"TEST001"})))
        ui.poll()
        _, _, text = self.render(ui)
        self.assertIn("保存パスワードを更新しました。", text)
        self.assertNotIn("[エラー] 保存パスワードを更新しました。", text)

    def test_modals_and_save_prompt_include_non_color_indicators_at_supported_sizes(self):
        for width in (62, 80, 120):
            with self.subTest(width=width), patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
                ui = self.make_screen()
                ui.values = {"授業名": "テスト講義", "教室名": "642", "時限": "1限", "開始時刻": "09:00", "終了時刻": "10:30"}
                ui.confirm = True
                _, _, text = self.render(ui, width=width)
                self.assertIn("[注意] 出席登録の確認", text)
                ui.confirm = False

                ui.open_users()
                ui.form_error = "選択が必要です。"
                ui.user_results["TEST001"] = {"failed": True, "result": "認証エラー"}
                _, calls, text = self.render(ui, width=width)
                self.assertIn("[エラー] 選択が必要です。", text)
                self.assertIn("[エラー] 認証エラー", text)
                self.assertTrue(any(attr & curses.A_BOLD for _, _, line, attr in calls if "[エラー] 選択が必要です。" in line))

                ui.modal = "user_result"
                _, calls, text = self.render(ui, width=width)
                self.assertIn("[エラー] 認証エラー", text)
                self.assertTrue(any(attr & curses.A_BOLD for _, _, line, attr in calls if "[エラー] 認証エラー" in line))

                ui.open_form("password")
                ui.form_error = "パスワードが一致しません。"
                _, calls, text = self.render(ui, width=width)
                self.assertIn("[エラー] パスワードが一致しません。", text)
                self.assertEqual(ui.form_error, "パスワードが一致しません。")
                self.assertTrue(any(attr & curses.A_BOLD for _, _, line, attr in calls if "[エラー] パスワードが一致しません。" in line))

                ui.open_users()
                ui.handle_modal("d")
                _, calls, text = self.render(ui, width=width)
                self.assertIn("[注意] 変更先:", text)
                self.assertTrue(any(attr & curses.A_BOLD for _, _, line, attr in calls if "[注意] 変更先:" in line))

                prompt = SavePrompt()
                prompt.open("テスト編集", save=Mock(), discard=Mock())
                screen = Mock(getmaxyx=Mock(return_value=(24, width)))
                prompt.draw(Canvas(screen))
                prompt_text = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
                self.assertIn("[注意] 未保存の変更", prompt_text)

    def test_schedule_panel_distinguishes_errors_warnings_and_info_without_color(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            save_schedule(base / "schedule.json", DATA)
            ui = self.make_screen(demo=False, base=base)
            panel = ui.schedule_panel
            panel.data = copy.deepcopy(DATA)
            ui.modal = "schedule"

            with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
                panel.message, panel.message_level = "Aで授業を追加できます。", "info"
                for width in (62, 80, 120):
                    _, calls, text = self.render(ui, width=width)
                    self.assertIn("Aで授業を追加できます。", text)
                    self.assertNotIn("[エラー]", text)
                    self.assertNotIn("[注意]", text)

                panel.form("add")
                panel.fields = ["授業", "mon", "25:00", "642", "", ""]
                panel.submit()
                for width in (62, 80, 120):
                    _, calls, text = self.render(ui, width=width)
                    self.assertIn("[エラー]", text)
                    self.assertIn("HH:MM", text)
                    err_attrs = [attr for _, _, line, attr in calls if "[エラー]" in line]
                    self.assertTrue(err_attrs and (err_attrs[0] & curses.A_BOLD))

                panel.mode = "list"
                panel.active = True
                with patch("src.schedule_ui.threading.Thread", side_effect=lambda **kw: Mock(start=kw["target"])), \
                        patch("src.schedule_ui.install_agent", side_effect=ScheduleError("失敗")), \
                        patch("src.schedule_ui.agent_installed", return_value=False):
                    panel.persist(DATA)
                    ui.poll()
                _, calls, text = self.render(ui, width=62)
                self.assertIn("[注意] 保存済み。", text)
                warn_attrs = [attr for _, _, line, attr in calls if "[注意] 保存済み。" in line]
                self.assertTrue(warn_attrs and (warn_attrs[0] & curses.A_BOLD))

                panel.handle("d")
                _, calls, text = self.render(ui, width=62)
                self.assertIn("[注意] この授業の自動出席予定を削除します。", text)

                panel.data = None
                ui.busy = False
                _, calls, text = self.render(ui, width=62)
                self.assertIn("[エラー] 予定を読み込めませんでした。Rで再試行。", text)

    def test_prefix_text_does_not_duplicate_existing_prefix(self):
        self.assertEqual(prefix_text("error", "[エラー] 既に付いています"), "[エラー] 既に付いています")
        self.assertEqual(prefix_text("error", ""), "")


if __name__ == "__main__":
    unittest.main()
