"""項目別バリデーション案内、保存時の最初の不正項目へのフォーカス移動、パスワード非補正・非露出を検証する。"""

import copy
import curses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.config import load_config
from src.scheduler import load_schedule, save_schedule
from src.terminal_ui import AttendanceScreen
from test_schedule_ui import DATA
from test_user_editing import MemoryKeychain
from test_users import CONFIG


class FieldValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.path = self.base / "config.json"
        self.config = dict(
            CONFIG,
            users=[
                {"username": "one", "student_id": "OLD001"},
                {"username": "two", "student_id": "NEW002"},
            ],
            default_user="one",
            ui_selected_users=["OLD001"],
        )
        self.path.write_text(json.dumps(self.config))
        self.before = self.path.read_bytes()
        self.backend = MemoryKeychain()
        for name, value in (
            ("src.user_commands.keychain_backend", self.backend),
            ("src.credentials.keychain_backend", self.backend),
        ):
            patcher = patch(name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        ui_thread = patch("src.terminal_ui.threading.Thread", side_effect=lambda **kwargs: Mock(start=kwargs["target"]))
        ui_thread.start()
        self.addCleanup(ui_thread.stop)
        sched_thread = patch("src.schedule_ui.threading.Thread", side_effect=lambda **kwargs: Mock(start=kwargs["target"]))
        sched_thread.start()
        self.addCleanup(sched_thread.stop)
        agent = patch("src.schedule_ui.agent_installed", return_value=False)
        agent.start()
        self.addCleanup(agent.stop)

    def screen(self, demo=False):
        return AttendanceScreen(load_config(self.path), "OLD001", "old-secret", Mock(), demo=demo, base=self.base)

    def drawn(self, ui, size=(24, 62)):
        screen = Mock(getmaxyx=Mock(return_value=size))
        ui.draw(screen)
        return "\n".join(call.args[2] for call in screen.addstr.call_args_list)

    def choose_save_prompt_save(self, ui):
        self.assertTrue(ui.save_prompt.active)
        ui.handle_modal(curses.KEY_DOWN)
        ui.handle_modal("\n")
        ui.poll()

    def test_add_form_navigation_guides_without_altering_input(self):
        ui = self.screen()
        ui.open_users()
        ui.open_form("add")

        # 空欄のまま移動したときは入力途中としてエラーを出さない。
        ui.handle_modal("\t")
        self.assertEqual(ui.form_index, 1)
        self.assertEqual(ui.form_error, "")

        # 学籍番号の全角文字・空白・記号・重複を項目移動時に具体的に案内し、入力内容とカーソルを保持する。
        cases = [
            ("Ａ１２", "全角"),
            ("　", "全角"),
            ("Ａ １", "空白"),
            ("A B", "空白"),
            (" AB01", "空白"),
            ("AB01 ", "空白"),
            ("AB-01", "半角英数字"),
            ("ｱｲｳ", "半角英数字"),
            ("NEW002", "登録済み"),
        ]
        for raw, expected_keyword in cases:
            for key in ("\t", curses.KEY_DOWN, curses.KEY_UP, curses.KEY_BTAB, "\n"):
                with self.subTest(raw=raw, key=repr(key)):
                    ui.form_index = 1
                    ui.form_values[1] = raw
                    ui.form_editors[1].sync(raw)
                    ui.form_editors[1].position = 1 if len(raw) >= 1 else 0
                    ui.handle_modal(key)
                    self.assertEqual(ui.form_values[1], raw)
                    self.assertEqual(ui.form_editors[1].position, 1)
                    self.assertIn(expected_keyword, ui.form_error)

        # 正しい値にして移動すればエラーが消える。
        ui.form_index = 1
        ui.form_values[1] = "THIRD003"
        ui.handle_modal("\t")
        self.assertEqual(ui.form_index, 2)
        self.assertEqual(ui.form_error, "")

        # ユーザーネームの不正入力（空白のみ・前後空白・制御文字・文字数超過・重複）も移動時に案内する。
        for bad_name, keyword in (
            ("   ", "空白"),
            (" alice", "前後"),
            ("alice ", "前後"),
            ("a\x07b", "制御文字"),
            ("a" * 61, "60文字"),
            ("one", "登録済み"),
        ):
            with self.subTest(bad_name=repr(bad_name)):
                ui.form_index = 0
                ui.form_values[0] = bad_name
                ui.form_editors[0].sync(bad_name)
                ui.form_editors[0].position = 1
                ui.handle_modal("\t")
                self.assertEqual(ui.form_values[0], bad_name)
                self.assertEqual(ui.form_editors[0].position, 1)
                self.assertIn(keyword, ui.form_error)

        # 入力途中の空欄へ戻して移動すればエラーを消す。
        ui.form_index = 0
        ui.form_values[0] = ""
        ui.handle_modal("\t")
        self.assertEqual(ui.form_error, "")

    def test_add_form_save_jumps_to_first_invalid_field_and_preserves_inputs(self):
        ui = self.screen()
        ui.open_users()
        ui.open_form("add")
        ui.form_values = ["one", "Ａ １", ""]
        ui.form_editors[0].sync("one")
        ui.form_editors[0].position = 1
        ui.form_editors[1].sync("Ａ １")
        ui.form_editors[1].position = 2
        ui.form_index = 2

        # 最後の項目で Enter -> 最初の不正項目 (0: ユーザーネーム重複) へ移動。
        ui.handle_modal("\n")
        self.assertEqual(ui.modal, "add")
        self.assertEqual(ui.form_index, 0)
        self.assertEqual(ui.form_values, ["one", "Ａ １", ""])
        self.assertEqual(ui.form_editors[0].position, 1)
        self.assertEqual(ui.form_editors[1].position, 2)
        self.assertIn("登録済み", ui.form_error)
        self.assertEqual(self.path.read_bytes(), self.before)

        # 0番目を直してから Esc -> 保存して戻る -> 次の不正項目 (1: 学生番号) へ移動。
        ui.form_values[0] = "three"
        ui.handle_modal("\x1b")
        self.choose_save_prompt_save(ui)
        self.assertEqual(ui.modal, "add")
        self.assertEqual(ui.form_index, 1)
        self.assertEqual(ui.form_values, ["three", "Ａ １", ""])
        self.assertEqual(ui.form_editors[1].position, 2)
        self.assertIn("全角", ui.form_error)
        self.assertIn("空白", ui.form_error)

        # 1番目を直してから保存 -> 次の不正項目 (2: パスワード空欄) へ移動。
        ui.form_values[1] = "THIRD003"
        ui.form_index = 2
        ui.handle_modal("\n")
        self.assertEqual(ui.modal, "add")
        self.assertEqual(ui.form_index, 2)
        self.assertEqual(ui.form_values, ["three", "THIRD003", ""])
        self.assertIn("パスワード", ui.form_error)

        # 2番目を入れて最後の項目で Enter -> 登録でき、メイン画面へ戻る。
        ui.form_values[2] = "  third secret  "
        ui.handle_modal("\n")
        ui.poll()
        self.assertIsNone(ui.modal)
        self.assertEqual(self.backend.values["THIRD003"], "  third secret  ")
        self.assertEqual(load_config(self.path)["users"][-1], {"username": "three", "student_id": "THIRD003"})

    def test_keystroke_typing_navigation_and_cursor_preservation_end_to_end(self):
        ui = self.screen()
        ui.open_users()
        ui.open_form("add")
        for ch in "alice":
            ui.handle_modal(ch)
        ui.handle_modal("\t")
        for ch in "ＡＢ１":
            ui.handle_modal(ch)
        ui.handle_modal(curses.KEY_LEFT)
        self.assertEqual(ui.form_editors[1].position, 2)
        ui.handle_modal("\t")
        self.assertEqual(ui.form_index, 2)
        self.assertIn("全角", ui.form_error)
        self.assertEqual(ui.form_values[1], "ＡＢ１")
        self.assertEqual(ui.form_editors[1].position, 2)

        for ch in "secret":
            ui.handle_modal(ch)
        ui.handle_modal("\n")
        self.assertEqual(ui.form_index, 1)
        self.assertEqual(ui.form_editors[1].position, 2)
        self.assertEqual(ui.form_values, ["alice", "ＡＢ１", "secret"])

        # カーソル位置を維持したまま入力を直して Esc -> 保存して戻る で保存完了する。
        ui.handle_modal("\x15")
        for ch in "ALICE1":
            ui.handle_modal(ch)
        ui.handle_modal("\x1b")
        self.choose_save_prompt_save(ui)
        self.assertEqual(ui.modal, "users")
        self.assertEqual(self.backend.values["ALICE1"], "secret")

    def test_rename_and_student_id_forms_preserve_input_and_guide_errors(self):
        ui = self.screen()
        ui.open_users()
        ui.open_form("rename")
        ui.form_values = [" two "]
        ui.form_editors[0].sync(" two ")
        ui.form_editors[0].position = 2
        ui.handle_modal("\n")
        self.assertEqual(ui.modal, "rename")
        self.assertEqual(ui.form_index, 0)
        self.assertEqual(ui.form_values, [" two "])
        self.assertEqual(ui.form_editors[0].position, 2)
        self.assertIn("前後", ui.form_error)

        ui.form_values = ["two"]
        ui.handle_modal("\n")
        self.assertIn("登録済み", ui.form_error)
        ui.form_values = ["renamed"]
        ui.handle_modal("\n")
        ui.poll()
        self.assertEqual(load_config(self.path)["users"][0]["username"], "renamed")

        ui.open_form("student_id")
        for raw, keyword in (
            ("", "半角英数字"),
            ("１２３", "全角"),
            ("OLD 01", "空白"),
            ("OLD-01", "半角英数字"),
            ("OLD001", "異なる"),
            ("NEW002", "登録済み"),
        ):
            with self.subTest(raw=raw):
                ui.form_values = [raw]
                ui.form_editors[0].sync(raw)
                ui.form_editors[0].position = min(1, len(raw))
                ui.handle_modal("\n")
                self.assertEqual(ui.modal, "student_id")
                self.assertEqual(ui.form_index, 0)
                self.assertEqual(ui.form_values, [raw])
                self.assertEqual(ui.form_editors[0].position, min(1, len(raw)))
                self.assertIn(keyword, ui.form_error)

        ui.form_values = ["FIXED001"]
        ui.handle_modal("\n")
        ui.poll()
        self.assertEqual(load_config(self.path)["users"][0]["student_id"], "FIXED001")

    def test_password_is_never_normalized_or_exposed_in_errors(self):
        ui = self.screen()
        ui.open_users()
        ui.open_form("password")

        # 1欄目入力後に2欄目がまだ空の状態で移動しても不一致エラーを出さない。
        ui.form_values = ["  SecretPass  ", ""]
        ui.form_index = 0
        ui.handle_modal("\t")
        self.assertEqual(ui.form_index, 1)
        self.assertEqual(ui.form_error, "")

        # 前後空白・全角の違いは自動補正されず、エラーに入力値を露出しない。
        for fields in (["  SecretPass  ", "SecretPass"], ["ＰＡＳＳ", "PASS"]):
            with self.subTest(fields=fields):
                ui.form_values = list(fields)
                ui.form_editors[0].sync(fields[0])
                ui.form_editors[1].sync(fields[1])
                ui.form_editors[1].position = 2
                ui.form_index = 1
                ui.handle_modal("\n")
                self.assertEqual(ui.modal, "password")
                self.assertEqual(ui.form_index, 1)
                self.assertEqual(ui.form_values, fields)
                self.assertEqual(ui.form_editors[1].position, 2)
                self.assertIn("一致しません", ui.form_error)
                drawn = self.drawn(ui)
                for secret in fields:
                    self.assertNotIn(secret, ui.form_error)
                    self.assertNotIn(secret, ui.message)
                    self.assertNotIn(secret, drawn)

        # 1欄目が空の場合は保存時に0番目へ移動する。
        for fields in (["", ""], ["", "OnlyConfirm"]):
            ui.form_values = list(fields)
            ui.form_index = 1
            ui.handle_modal("\n")
            self.assertEqual(ui.form_index, 0)
            self.assertIn("新しいパスワード", ui.form_error)
            self.assertNotIn("OnlyConfirm", ui.form_error)

        # 2欄目が空の場合は1番目へ移動する。
        ui.form_values = ["  SecretPass  ", ""]
        ui.form_index = 0
        ui.submit_account_form()
        self.assertEqual(ui.form_index, 1)
        self.assertIn("確認用", ui.form_error)
        self.assertNotIn("SecretPass", ui.form_error)

        # 空白のみ・前後空白付きの一致入力はそのまま Keychain に保存される。
        for raw_password in ("   ", "  Exact Space Password  "):
            ui.open_form("password")
            ui.form_values = [raw_password, raw_password]
            ui.form_index = 1
            ui.handle_modal("\n")
            ui.poll()
            self.assertEqual(self.backend.values["OLD001"], raw_password)

        # add フォームで他項目が不正でも入力中のパスワードは隠されたまま保持される。
        ui.open_users()
        ui.open_form("add")
        ui.form_values = ["third", "１２３", "SuperSecret!"]
        ui.form_index = 2
        ui.handle_modal("\n")
        self.assertEqual(ui.form_index, 1)
        self.assertEqual(ui.form_values[2], "SuperSecret!")
        drawn = self.drawn(ui)
        self.assertNotIn("SuperSecret!", ui.form_error)
        self.assertNotIn("SuperSecret!", ui.message)
        self.assertNotIn("SuperSecret!", drawn)

    def test_schedule_course_navigation_and_save_focus_jump(self):
        schedule_path = self.base / "schedule.json"
        save_schedule(schedule_path, DATA)
        ui = self.screen()
        panel = ui.schedule_panel
        panel.data = copy.deepcopy(DATA)
        ui.modal = "schedule"
        panel.form("add")

        # 項目移動時のエラー案内と入力・カーソル保持、空欄時のクリア。
        panel.fields = ["", "げつ", "25:00", "73", "9:05", "25:00"]
        for i, val in enumerate(panel.fields):
            panel.editors[i].sync(val)
            panel.editors[i].position = min(1, len(val))

        # 0(空) -> 1: 空欄なのでエラーなし
        panel.field_index = 0
        panel.handle("\t")
        self.assertEqual(panel.field_index, 1)
        self.assertEqual(panel.message, "")

        # 1("げつ") -> 2: 曜日形式の案内、値・カーソル保持
        panel.handle("\t")
        self.assertEqual(panel.field_index, 2)
        self.assertIn("mon～sun", panel.message)
        self.assertEqual(panel.fields[1], "げつ")
        self.assertEqual(panel.editors[1].position, 1)

        # 2("25:00") -> 3: 時刻形式の案内
        panel.handle("\t")
        self.assertEqual(panel.field_index, 3)
        self.assertIn("HH:MM", panel.message)
        self.assertEqual(panel.fields[2], "25:00")
        self.assertEqual(panel.editors[2].position, 1)

        # 3("73") -> 4: QR番号の案内
        panel.handle(curses.KEY_DOWN)
        self.assertEqual(panel.field_index, 4)
        self.assertIn("半角数字3桁", panel.message)
        self.assertEqual(panel.fields[3], "73")

        # 4("9:05") -> 5: 登録時刻の案内
        panel.handle("\n")
        self.assertEqual(panel.field_index, 5)
        self.assertIn("HH:MM", panel.message)
        self.assertEqual(panel.fields[4], "9:05")

        # 5("25:00") -> 4 (KEY_UP): 範囲終了の案内
        panel.handle(curses.KEY_UP)
        self.assertEqual(panel.field_index, 4)
        self.assertIn("HH:MM", panel.message)

        # 4を空欄にしてから KEY_BTAB -> 3: 空欄許容項目なのでメッセージが消える
        panel.fields[4] = ""
        panel.handle(curses.KEY_BTAB)
        self.assertEqual(panel.field_index, 3)
        self.assertEqual(panel.message, "")

        # 保存時は最初の不正項目から順番にフォーカスが移動し、全入力を保持する。
        panel.fields = ["", "げつ", "25:00", "73", "9:05", "14:00"]
        for i, val in enumerate(panel.fields):
            panel.editors[i].sync(val)
            panel.editors[i].position = min(1, len(val))
        panel.field_index = 5
        before = schedule_path.read_bytes()

        expected_steps = [
            (0, "授業名", "追加授業"),
            (1, "mon～sun", "月"),
            (2, "HH:MM", "14:00"),  # DATA の mon 14:00 と重複する時刻を入れてみる
            (2, "同じ曜日・開始時刻", "15:00"),
            (3, "半角数字3桁", "642"),
            (4, "HH:MM", "15:10"),
            (5, "登録時刻以降", "15:20"),
        ]
        for expected_index, expected_keyword, fix_value in expected_steps:
            panel.submit()
            self.assertEqual(panel.mode, "add")
            self.assertEqual(panel.field_index, expected_index)
            self.assertIn(expected_keyword, panel.message)
            self.assertEqual(schedule_path.read_bytes(), before)
            panel.fields[expected_index] = fix_value
            panel.field_index = 5

        # 範囲終了だけ入っていて登録時刻が空の場合は 4番目へ移動。
        panel.fields[4] = ""
        panel.submit()
        self.assertEqual(panel.field_index, 4)
        self.assertIn("登録時刻", panel.message)
        panel.fields[4] = "15:10"
        panel.field_index = 5

        panel.handle("\n")
        ui.poll()
        self.assertEqual(panel.mode, "list")
        saved = load_schedule(schedule_path)
        self.assertEqual(len(saved["courses"]), 2)
        self.assertEqual(saved["courses"][1]["course_name"], "追加授業")
        self.assertEqual(saved["courses"][1]["start_time"], "15:00")

    def test_schedule_edit_self_and_disabled_courses_do_not_falsely_conflict(self):
        data = {
            "active_from": "2026-09-18",
            "active_until": "2026-12-21",
            "blackout_dates": [],
            "courses": [
                {"weekday": "mon", "start_time": "14:00", "room": "731", "course_name": "無効授業", "enabled": False},
                {"weekday": "mon", "start_time": "14:00", "room": "642", "course_name": "有効授業", "enabled": True},
            ],
        }
        schedule_path = self.base / "schedule.json"
        save_schedule(schedule_path, data)
        ui = self.screen()
        panel = ui.schedule_panel
        panel.data = copy.deepcopy(data)
        ui.modal = "schedule"

        # 無効な既存授業と同じ曜日・時刻でも新規追加できる。
        panel.form("add")
        panel.fields = ["新規", "tue", "14:00", "642", "", ""]
        self.assertIsNone(panel.first_invalid_field())

        # 有効な授業自身の編集では自分自身と競合しない。
        panel.index = 1
        panel.form("edit")
        panel.fields[3] = "611"
        self.assertIsNone(panel.first_invalid_field())
        panel.submit()
        ui.poll()
        self.assertEqual(load_schedule(schedule_path)["courses"][1]["room"], "611")

    def test_schedule_period_navigation_and_save_focus_jump(self):
        schedule_path = self.base / "schedule.json"
        save_schedule(schedule_path, DATA)
        ui = self.screen()
        panel = ui.schedule_panel
        panel.data = copy.deepcopy(DATA)
        ui.modal = "schedule"
        panel.form("period")
        before = schedule_path.read_bytes()

        # 不正な日付形式をそのまま保持したまま案内を表示する。
        panel.fields = ["2026/09/18", "2026-09-01", "2026-11-03 2026-11-23"]
        for i, val in enumerate(panel.fields):
            panel.editors[i].sync(val)
            panel.editors[i].position = 2

        panel.field_index = 0
        panel.handle("\t")
        self.assertEqual(panel.field_index, 1)
        self.assertIn("YYYY-MM-DD", panel.message)
        self.assertEqual(panel.fields[0], "2026/09/18")
        self.assertEqual(panel.editors[0].position, 2)

        # Esc -> 保存して戻る でも最初の不正項目 (0) へ戻る。
        panel.field_index = 2
        ui.handle_modal("\x1b")
        self.choose_save_prompt_save(ui)
        self.assertEqual(panel.mode, "period")
        self.assertEqual(panel.field_index, 0)
        self.assertEqual(panel.fields[0], "2026/09/18")
        self.assertEqual(panel.editors[0].position, 2)
        self.assertIn("YYYY-MM-DD", panel.message)
        self.assertEqual(schedule_path.read_bytes(), before)

        # 0番目を直し、1番目が終了日 < 開始日なら 1番目へ移動。
        panel.fields[0] = "2026-10-01"
        panel.field_index = 2
        panel.submit()
        self.assertEqual(panel.field_index, 1)
        self.assertIn("開始日以降", panel.message)

        # 1番目を直し、2番目が不正なら 2番目へ移動。
        panel.fields[1] = "2026-12-20"
        panel.submit()
        self.assertEqual(panel.field_index, 2)
        self.assertIn("YYYY-MM-DD", panel.message)

        # 2番目を直して保存。
        panel.fields[2] = "2026-11-03、2026-11-23"
        panel.handle("\n")
        ui.poll()
        self.assertEqual(panel.mode, "list")
        saved = load_schedule(schedule_path)
        self.assertEqual(saved["active_from"].isoformat(), "2026-10-01")
        self.assertEqual(saved["active_until"].isoformat(), "2026-12-20")
        self.assertEqual(sorted(d.isoformat() for d in saved["blackout_dates"]), ["2026-11-03", "2026-11-23"])

    def test_validation_messages_render_at_supported_terminal_widths(self):
        ui = self.screen(demo=True)
        for width in (62, 80, 120):
            with self.subTest(width=width):
                ui.open_users()
                ui.open_form("add")
                ui.form_values = ["valid", "Ａ １", "hidden-secret"]
                ui.form_index = 2
                ui.handle_modal("\n")
                drawn = self.drawn(ui, (24, width))
                self.assertIn("全角", drawn)
                self.assertNotIn("hidden-secret", drawn)

                panel = ui.schedule_panel
                panel.data = copy.deepcopy(DATA)
                ui.modal = "schedule"
                panel.form("period")
                panel.fields = ["2026-10-01", "2026-09-01", ""]
                panel.submit()
                drawn = self.drawn(ui, (24, width))
                self.assertIn("開始日以降", drawn)


if __name__ == "__main__":
    unittest.main()
