"""複数選択、ユーザー別の認証・照合と一括登録。大学には接続しない。"""

import curses
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from src.credentials import CredentialError
from src.terminal_layout import Canvas, text_width
from src.terminal_ui import AttendanceScreen
from src.verification import AttendanceError


VALUES = {"授業名": "テスト授業", "時限": "2-3限", "教室名": "731講義室", "開始時刻": "10:00", "終了時刻": "12:00"}


class MultiUserUITests(unittest.TestCase):
    def screen(self, count=2, demo=False):
        users = [{"username": f"user{i}", "student_id": f"TEST00{i}"} for i in range(1, count + 1)]
        config = {"users": users, "default_user": "user1", "_active_username": "user1", "dry_run": False}
        ui = AttendanceScreen(config, "TEST001", "secret1", Mock(), "731", demo=demo, base=Path("/unused"))
        ui.selected_user_ids = {user["student_id"] for user in users}
        return ui

    def wait_for_operation(self, ui):
        deadline = time.monotonic() + 5
        while ui.busy and time.monotonic() < deadline:
            ui.poll()
            time.sleep(0.005)
        self.assertFalse(ui.busy, "処理が完了しませんでした")

    def credentials(self, config, path, *, interactive, username):
        self.assertFalse(interactive)
        number = username.removeprefix("user")
        config["_active_username"] = username
        return f"TEST00{number}", f"secret{number}"

    def test_space_selects_multiple_enter_applies_and_escape_cancels(self):
        ui = self.screen(demo=True)
        ui.selected_user_ids = {"TEST001"}
        ui.values = dict(VALUES)
        ui.open_users()
        ui.handle_modal(curses.KEY_DOWN)
        ui.handle_modal(" ")
        self.assertEqual(ui.pending_user_ids, {"TEST001", "TEST002"})
        self.assertEqual(ui.selected_user_ids, {"TEST001"})
        ui.handle_modal("\x1b")
        self.assertEqual(ui.selected_user_ids, {"TEST001"})
        self.assertEqual(ui.values, VALUES)
        self.assertTrue(ui.save_prompt.active)
        ui.handle_modal(curses.KEY_UP)
        ui.handle_modal("\n")
        ui.open_users()
        ui.handle_modal(curses.KEY_DOWN)
        ui.handle_modal(" ")
        with patch.object(ui, "start") as start:
            ui.handle_modal("\n")
            start.assert_called_once_with()
        self.assertEqual(ui.selected_user_ids, {"TEST001", "TEST002"})
        self.assertEqual(ui.values, {})
        self.assertEqual(ui.config["default_user"], "user1")
        self.assertIsNone(ui.modal)

    def test_empty_selection_is_rejected(self):
        ui = self.screen()
        ui.selected_user_ids = {"TEST001"}
        ui.open_users()
        ui.handle_modal(" ")
        with patch.object(ui, "start") as start:
            ui.handle_modal("\n")
            start.assert_not_called()
        self.assertEqual(ui.modal, "users")
        self.assertIn("1人以上", ui.form_error)
        self.assertEqual(ui.selected_user_ids, {"TEST001"})

    def test_parallel_registration_isolates_credentials_and_rechecks_each_snapshot(self):
        ui = self.screen()
        barrier = threading.Barrier(2)
        calls = []

        def run(page, config, student_id, password, room, route, logger, course, start, *, on_snapshot, expected_values):
            barrier.wait(timeout=2)
            calls.append((page, config, student_id, password, route, course, start, expected_values))
            on_snapshot(VALUES, False, False)
            return "未出席（確認のみ）" if config["dry_run"] else "出席登録成功"

        with patch("src.terminal_ui.get_credentials", side_effect=self.credentials) as credentials, \
                patch("src.browser.open_page", side_effect=lambda config: nullcontext(Mock())) as pages, \
                patch("src.attendance.run_attendance", side_effect=run):
            ui.start()
            self.wait_for_operation(ui)
            self.assertTrue(ui.can_register())
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(call[1]["dry_run"] and call[7] is None for call in calls))
            calls.clear()
            ui.start(register=True)
            self.wait_for_operation(ui)
            self.assertEqual(pages.call_count, 4)
            self.assertEqual(credentials.call_count, 2)

        self.assertEqual({(call[2], call[3]) for call in calls}, {("TEST001", "secret1"), ("TEST002", "secret2")})
        self.assertIsNot(calls[0][0], calls[1][0])
        self.assertIsNot(calls[0][1], calls[1][1])
        for call in calls:
            self.assertFalse(call[1]["dry_run"])
            self.assertEqual(call[4:7], ("7301", "テスト授業", "10:00"))
            self.assertEqual(call[7], VALUES)
        self.assertTrue(ui.attended)
        self.assertFalse(ui.can_register())
        self.assertEqual(ui.status()[0], "全員出席済み（2人）")
        self.assertEqual(ui.config["_active_username"], "user1")
        self.assertEqual(ui.config["default_user"], "user1")

    def test_only_eligible_users_are_registered_and_skipped_results_remain(self):
        ui = self.screen(count=5)
        registered = []

        def run(page, config, student_id, password, room, route, logger, course, start, *, on_snapshot, expected_values):
            if not config["dry_run"]:
                registered.append(student_id)
                on_snapshot(VALUES, False, False)
                return "出席登録成功"
            if student_id == "TEST004":
                raise AttendanceError("確認に失敗しました。")
            attended, unenrolled = student_id == "TEST001", student_id == "TEST002"
            on_snapshot({} if student_id == "TEST005" else VALUES, attended, unenrolled)
            return {"TEST001": "出席済み", "TEST002": "履修登録なし", "TEST005": "出席受付対象なし"}.get(student_id, "未出席（確認のみ）")

        with patch("src.terminal_ui.get_credentials", side_effect=self.credentials), \
                patch("src.browser.open_page", side_effect=lambda config: nullcontext(Mock())), \
                patch("src.attendance.run_attendance", side_effect=run):
            ui.start()
            self.wait_for_operation(ui)
            self.assertEqual([u["student_id"] for u in ui.registration_targets()], ["TEST003"])
            self.assertTrue(ui.can_register())
            ui.start(register=True)
            self.wait_for_operation(ui)
        self.assertEqual(registered, ["TEST003"])
        self.assertEqual(ui.user_results["TEST001"]["result"], "出席済み")
        self.assertTrue(ui.user_results["TEST002"]["unenrolled"])
        self.assertTrue(ui.user_results["TEST004"]["failed"])
        self.assertEqual(ui.user_results["TEST005"]["values"], {})
        self.assertFalse(ui.can_register())

    def test_missing_password_does_not_block_other_selected_users(self):
        ui = self.screen(count=3)
        # 起動時のユーザーを選択から外した場合も、失敗はそのユーザーだけ。
        ui.selected_user_ids = {"TEST001"}
        ui.open_users()
        ui.pending_user_ids = {"TEST002", "TEST003"}

        def credentials(config, path, *, interactive, username):
            if username == "user2":
                raise CredentialError("Keychainにパスワードがありません。")
            return self.credentials(config, path, interactive=interactive, username=username)

        def run(*args, on_snapshot, **kwargs):
            on_snapshot(VALUES, False, False)
            return "未出席（確認のみ）"

        with patch("src.terminal_ui.get_credentials", side_effect=credentials), \
                patch("src.browser.open_page", return_value=nullcontext(Mock())), \
                patch("src.user_commands.save_ui_selection", return_value={"ui_selected_users": ["TEST002", "TEST003"]}), \
                patch("src.attendance.run_attendance", side_effect=run) as attendance:
            ui.apply_user_selection()
            self.wait_for_operation(ui)
        attendance.assert_called_once()
        self.assertEqual(attendance.call_args.args[2:4], ("TEST003", "secret3"))
        self.assertTrue(ui.user_results["TEST002"]["failed"])
        self.assertEqual([u["student_id"] for u in ui.registration_targets()], ["TEST003"])

    def test_different_course_is_excluded_and_failed_refresh_discards_old_snapshots(self):
        ui = self.screen()

        def run(page, config, student_id, *args, on_snapshot, **kwargs):
            values = dict(VALUES, 授業名="別の授業") if student_id == "TEST002" else VALUES
            on_snapshot(values, False, False)
            return "未出席（確認のみ）"

        with patch("src.terminal_ui.get_credentials", side_effect=self.credentials), \
                patch("src.browser.open_page", side_effect=lambda config: nullcontext(Mock())), \
                patch("src.attendance.run_attendance", side_effect=run) as attendance:
            ui.start()
            self.wait_for_operation(ui)
            self.assertEqual([u["student_id"] for u in ui.registration_targets()], ["TEST001"])
            self.assertIn("授業情報が他のユーザーと異なります", ui.user_results["TEST002"]["result"])
            attendance.side_effect = AttendanceError("通信失敗")
            ui.start()
            self.wait_for_operation(ui)
        self.assertEqual(ui.values, {})
        self.assertFalse(ui.can_register())
        self.assertTrue(all(result["failed"] and not result["values"] for result in ui.user_results.values()))

    def test_check_mode_disables_batch_registration_and_demo_tracks_each_user(self):
        ui = self.screen(demo=True)
        ui.config["dry_run"] = True
        ui.start()
        self.wait_for_operation(ui)
        ui.start(register=True)
        self.assertFalse(ui.busy)
        self.assertFalse(ui.demo_attended)
        self.assertFalse(ui.can_register())
        ui.config["dry_run"] = False
        ui.start(register=True)
        self.wait_for_operation(ui)
        self.assertEqual(ui.demo_attended, {("TEST001", "731"), ("TEST002", "731")})
        self.assertTrue(ui.attended)

    def test_selection_confirmation_and_individual_results_fit_supported_sizes(self):
        ui = self.screen(demo=True)
        ui.start()
        self.wait_for_operation(ui)
        for width in (62, 80, 120):
            screen = Mock(getmaxyx=Mock(return_value=(24, width)))
            ui.open_users()
            ui.draw(screen)
            drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
            self.assertIn("[x] user1", drawn)
            self.assertIn("[x] user2", drawn)
            self.assertIn("Space 選択/解除", drawn)
            ui.handle_modal("v")
            screen.addstr.reset_mock()
            ui.draw(screen)
            drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
            self.assertIn("ユーザーごとの結果", drawn)
            self.assertIn("デモ: 未出席", drawn)
            ui.handle_modal("\x1b")
            self.assertEqual(ui.modal, "users")
            ui.handle_modal("\x1b")
            ui.confirm = True
            screen.addstr.reset_mock()
            ui.draw(screen)
            drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
            self.assertIn("user1、user2", drawn)
            self.assertIn("2人を登録します（対象外 0人）", drawn)
            ui.confirm = False

    def draw_confirmation_text(self, ui, width=80, height=24):
        screen = Mock(getmaxyx=Mock(return_value=(height, width)))
        ui.draw(screen)
        return screen, "\n".join(call.args[2] for call in screen.addstr.call_args_list)

    def test_confirmation_shows_all_selected_users_and_exclusion_reasons(self):
        ui = self.screen(count=6)
        ui.values = dict(VALUES)
        ui.user_results = {
            "TEST001": {"values": VALUES, "attended": False, "unenrolled": False, "failed": False, "result": "未出席（確認のみ）"},
            "TEST002": {"values": VALUES, "attended": True, "unenrolled": False, "failed": False, "result": "出席済み"},
            "TEST003": {"values": VALUES, "attended": False, "unenrolled": True, "failed": False, "result": "履修登録なし（出席操作なし）"},
            "TEST004": {"values": {}, "attended": False, "unenrolled": False, "failed": False, "result": "出席受付対象なし"},
            "TEST005": {"values": {}, "attended": False, "unenrolled": False, "failed": True, "result": "確認に失敗しました。"},
            "TEST006": {"values": {}, "attended": False, "unenrolled": False, "failed": True, "result": "授業情報が他のユーザーと異なります。Rで再確認してください。"},
        }
        ui.open_confirmation()
        _, drawn = self.draw_confirmation_text(ui, width=80)
        self.assertIn("登録対象 1人 / 対象外 5人", drawn)
        self.assertIn("1人を登録します（対象外 5人）", drawn)
        self.assertIn("表示 1-5 / 6人", drawn)
        self.assertIn("user1 / TEST001", drawn)
        self.assertIn("登録対象", drawn)
        self.assertIn("user2 / TEST002", drawn)
        self.assertIn("対象外: 出席済み", drawn)
        self.assertIn("user3 / TEST003", drawn)
        self.assertIn("対象外: 未履修（履修登録なし）", drawn)
        self.assertIn("user4 / TEST004", drawn)
        self.assertIn("対象外: 受付対象なし", drawn)
        self.assertIn("user5 / TEST005", drawn)
        self.assertIn("対象外: 確認失敗", drawn)
        ui.handle_confirmation(curses.KEY_DOWN)
        _, drawn_next = self.draw_confirmation_text(ui, width=80)
        self.assertIn("表示 2-6 / 6人", drawn_next)
        self.assertIn("user6 / TEST006", drawn_next)
        self.assertIn("対象外: 授業情報不一致", drawn_next)

        ui.config["dry_run"] = True
        ui.confirm_scroll = 0
        _, drawn_dry = self.draw_confirmation_text(ui, width=80)
        self.assertIn("登録対象 0人 / 対象外 6人", drawn_dry)
        self.assertIn("対象外: 確認のみモード", drawn_dry)

    def test_confirmation_scrolls_many_users_and_truncates_long_names_within_borders(self):
        long_prefix = "とても長い日本語ユーザーネームテスト" * 2
        users = [{"username": f"{long_prefix}{i:02d}", "student_id": f"LONG{i:03d}"} for i in range(1, 13)]
        config = {"users": users, "default_user": users[0]["username"], "_active_username": users[0]["username"], "dry_run": False}
        ui = AttendanceScreen(config, "LONG001", "secret1", Mock(), "731", base=Path("/unused"))
        ui.selected_user_ids = {user["student_id"] for user in users}
        ui.values = dict(VALUES)
        ui.user_results = {
            user["student_id"]: {
                "values": VALUES if idx % 3 else {},
                "attended": idx % 4 == 1,
                "unenrolled": idx % 4 == 2,
                "failed": False,
                "result": "出席受付対象なし" if idx % 3 == 0 else "確認結果",
            }
            for idx, user in enumerate(users)
        }

        for width in (62, 80, 120):
            with self.subTest(width=width):
                ui.open_confirmation()
                box_width, _, per_page = ui.confirmation_layout(24, width)
                box_left = (width - box_width) // 2
                box_right = box_left + box_width - 1
                seen = set()
                for step in range(len(users)):
                    screen = Mock(getmaxyx=Mock(return_value=(24, width)))
                    ui.draw_confirmation(Canvas(screen))
                    drawn = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
                    for user in users:
                        if user["student_id"] in drawn:
                            seen.add(user["student_id"])
                    # 枠外への文字はみ出し（右罫線以降への描画）がないことを検証する。
                    for call in screen.addstr.call_args_list:
                        row, col, text = call.args[:3]
                        if box_left < col < box_right and text.strip():
                            self.assertLess(col + text_width(text), box_right)
                    if step < len(users) - per_page:
                        ui.handle_confirmation(curses.KEY_DOWN, 24, width)
                self.assertEqual(seen, {user["student_id"] for user in users})

                ui.handle_confirmation(curses.KEY_PPAGE, 24, width)
                self.assertEqual(ui.confirm_scroll, len(users) - 2 * per_page)
                ui.handle_confirmation(curses.KEY_NPAGE, 24, width)
                self.assertEqual(ui.confirm_scroll, len(users) - per_page)
                ui.handle_confirmation(curses.KEY_UP, 24, width)
                self.assertEqual(ui.confirm_scroll, len(users) - per_page - 1)
                ui.handle_confirmation(curses.KEY_PPAGE, 24, width)
                ui.handle_confirmation(curses.KEY_PPAGE, 24, width)
                ui.handle_confirmation(curses.KEY_PPAGE, 24, width)
                self.assertEqual(ui.confirm_scroll, 0)

    def test_confirmation_run_loop_supports_scrolling_and_single_user_reasons(self):
        # run()経由で確認画面を開き、PgDn/↑でスクロールしEscで閉じられることを確認する。
        ui = self.screen(count=8)
        ui.values = dict(VALUES)
        ui.user_results = {
            f"TEST00{i}": {"values": VALUES, "attended": False, "unenrolled": False, "failed": False, "result": "未出席"}
            for i in range(1, 9)
        }
        ui.room = ""
        ui.selected = 2
        screen = Mock(getmaxyx=Mock(return_value=(24, 80)))
        screen.get_wch.side_effect = ["\n", curses.KEY_NPAGE, curses.KEY_UP, "\x1b", "q"]
        with patch("src.terminal_ui.curses.has_colors", return_value=False):
            self.assertEqual(ui.run(screen), 0)
        self.assertFalse(ui.confirm)
        self.assertEqual(ui.confirm_scroll, 0)

        # 単一ユーザーでも状態に応じた対象可否と理由が表示される。
        single = self.screen(count=1)
        single.values = dict(VALUES)
        self.assertEqual(single.confirmation_entries()[0]["reason"], "登録対象")
        single.attended = True
        self.assertEqual(single.confirmation_entries()[0]["reason"], "出席済み")
        single.attended = False
        single.unenrolled = True
        self.assertEqual(single.confirmation_entries()[0]["reason"], "未履修（履修登録なし）")
        single.unenrolled = False
        single.failed = True
        self.assertEqual(single.confirmation_entries()[0]["reason"], "確認失敗")
        single.failed = False
        single.values = {}
        single.checked = True
        self.assertEqual(single.confirmation_entries()[0]["reason"], "受付対象なし")

    def test_confirmation_defaults_to_back_and_blocks_all_excluded_registration(self):
        ui = self.screen(count=3)
        ui.values = dict(VALUES)
        ui.user_results = {
            "TEST001": {"values": VALUES, "attended": False, "unenrolled": False, "failed": False, "result": "未出席（確認のみ）"},
            "TEST002": {"values": VALUES, "attended": True, "unenrolled": False, "failed": False, "result": "出席済み"},
            "TEST003": {"values": VALUES, "attended": False, "unenrolled": True, "failed": False, "result": "履修登録なし"},
        }
        screen = Mock(getmaxyx=Mock(return_value=(24, 80)))
        screen.get_wch.side_effect = ["\n", "\n", "q"]
        ui.selected = 2
        with patch("src.terminal_ui.curses.has_colors", return_value=False), patch.object(ui, "start") as start:
            self.assertEqual(ui.run(screen), 0)
            # room設定済みの初期取得1回のみで、登録([ 戻る ]でEnter)は実行されない。
            start.assert_called_once_with()
        self.assertFalse(ui.confirm)
        self.assertEqual(ui.confirm_selection, 0)

        # EscやTab後の戻るでも登録は開始されない。
        ui.open_confirmation()
        self.assertEqual(ui.confirm_selection, 0)
        ui.handle_confirmation("\t")
        self.assertEqual(ui.confirm_selection, 1)
        ui.handle_confirmation(curses.KEY_BTAB)
        self.assertEqual(ui.confirm_selection, 0)
        with patch.object(ui, "start") as start:
            ui.handle_confirmation("\x1b")
            start.assert_not_called()
        self.assertFalse(ui.confirm)

        # Tab -> Enter で対象者のみ・expected_values付きで登録を開始する。
        ui.open_confirmation()
        ui.handle_confirmation(curses.KEY_RIGHT)
        self.assertEqual(ui.confirm_selection, 1)
        registered = []

        def run(page, config, student_id, password, room, route, logger, course, start, *, on_snapshot, expected_values):
            registered.append((student_id, expected_values))
            on_snapshot(VALUES, True, False)
            return "出席登録成功"

        with patch("src.terminal_ui.get_credentials", side_effect=self.credentials), \
                patch("src.browser.open_page", side_effect=lambda config: nullcontext(Mock())), \
                patch("src.attendance.run_attendance", side_effect=run):
            ui.handle_confirmation("\n")
            self.wait_for_operation(ui)
        self.assertEqual(registered, [("TEST001", VALUES)])

        # 全員対象外なら確認モーダルを開けず、確認画面を表示しても登録できない。
        ui.user_results["TEST001"] = {"values": VALUES, "attended": True, "unenrolled": False, "failed": False, "result": "出席済み"}
        ui.attended = True
        self.assertFalse(ui.can_register())
        screen.get_wch.side_effect = ["\n", "q"]
        ui.selected = 2
        with patch("src.terminal_ui.curses.has_colors", return_value=False), patch.object(ui, "start") as start:
            self.assertEqual(ui.run(screen), 0)
            start.assert_called_once_with()
        self.assertFalse(ui.confirm)

        ui.open_confirmation()
        _, drawn = self.draw_confirmation_text(ui, width=80)
        self.assertIn("登録対象 0人 / 対象外 3人", drawn)
        self.assertIn("0人を登録します（対象外 3人）", drawn)
        ui.handle_confirmation("\t")
        self.assertEqual(ui.confirm_selection, 0)
        with patch.object(ui, "start") as start:
            ui.confirm_selection = 1
            ui.handle_confirmation("\n")
            start.assert_not_called()
        self.assertFalse(ui.confirm)


if __name__ == "__main__":
    unittest.main()
