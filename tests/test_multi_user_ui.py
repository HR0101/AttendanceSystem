"""複数選択、ユーザー別の認証・照合と一括登録。大学には接続しない。"""

import curses
import io
import logging
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from src.credentials import CredentialError
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

    def test_browser_pool_used_only_for_non_demo_multi_user_batch(self):
        def run(*args, on_snapshot, **kwargs):
            on_snapshot(VALUES, False, False)
            return "未出席（確認のみ）"

        multi_ui = self.screen(count=2, demo=False)
        with patch("src.terminal_ui.get_credentials", side_effect=self.credentials), \
                patch("src.browser.browser_pool") as pool, \
                patch("src.browser.open_page", side_effect=lambda config: nullcontext(Mock())), \
                patch("src.attendance.run_attendance", side_effect=run):
            pool.return_value = nullcontext()
            multi_ui.start()
            self.wait_for_operation(multi_ui)
        pool.assert_called_once_with()

        single_ui = self.screen(count=1, demo=False)
        with patch("src.browser.browser_pool") as pool, \
                patch("src.browser.open_page", side_effect=lambda config: nullcontext(Mock())), \
                patch("src.attendance.run_attendance", side_effect=run):
            pool.return_value = nullcontext()
            single_ui.start()
            self.wait_for_operation(single_ui)
        pool.assert_not_called()

        demo_ui = self.screen(count=2, demo=True)
        with patch("src.browser.browser_pool") as pool:
            pool.return_value = nullcontext()
            demo_ui.start()
            self.wait_for_operation(demo_ui)
        pool.assert_not_called()

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

    def test_parallel_logs_include_anonymous_worker_ids_and_safe_failure_reasons(self):
        stream = io.StringIO()
        logger = logging.getLogger("test_multi_user_logs")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(handler)

        users = [{"username": f"user{i}", "student_id": f"TEST00{i}"} for i in range(1, 4)]
        config = {"users": users, "default_user": "user1", "_active_username": "user1", "dry_run": False}
        ui = AttendanceScreen(config, "TEST001", "secret1", logger, "731", base=Path("/unused"))
        ui.selected_user_ids = {user["student_id"] for user in users}

        def credentials(cfg, path, *, interactive, username):
            if username == "user2":
                raise CredentialError("Keychainにパスワードがありません。")
            return self.credentials(cfg, path, interactive=interactive, username=username)

        def run(page, cfg, student_id, password, room, route, worker_logger, course, start, *, on_snapshot, expected_values):
            worker_logger.info("段階=教室URLへアクセス")
            if student_id == "TEST003":
                raise AttendanceError("確認に失敗しました。")
            on_snapshot(VALUES, False, False)
            return "未出席（確認のみ）"

        with patch("src.terminal_ui.get_credentials", side_effect=credentials), \
                patch("src.browser.open_page", side_effect=lambda cfg: nullcontext(Mock())), \
                patch("src.attendance.run_attendance", side_effect=run):
            ui.start()
            self.wait_for_operation(ui)

        logs = stream.getvalue()
        lines = logs.splitlines()
        self.assertIn("INFO 対象=1/3 段階=教室URLへアクセス", lines)
        self.assertIn("INFO 対象=1/3 画面操作 結果=未出席（確認のみ）", lines)
        self.assertIn("ERROR 対象=2/3 画面操作停止 種別=CredentialError 理由=Keychainにパスワードがありません。", lines)
        self.assertIn("INFO 対象=3/3 段階=教室URLへアクセス", lines)
        self.assertIn("ERROR 対象=3/3 画面操作停止 種別=AttendanceError 理由=確認に失敗しました。", lines)
        for pii in ("user1", "user2", "user3", "TEST001", "TEST002", "TEST003", "secret1", "secret2", "secret3"):
            self.assertNotIn(pii, logs)


if __name__ == "__main__":
    unittest.main()
