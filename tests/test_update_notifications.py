"""通常起動は通知のみ。更新内容の確認もパッケージを変更しない。"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import test_updater as fixtures
from src import dependencies as d
from src import updater as u


def notes(version):
    return f"# v{version}\n\n## 変更内容\n\n- 更新通知を追加しました。\n\n## 利用者の操作\n\n- att update で更新してください。\n"


class UpdateNotificationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.UpdaterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base = self.fixture.base
        self.manifest = {**fixtures.manifest_for(self.fixture.archive()), "release_notes": notes(fixtures.NEXT)}
        self.workers = []
        original_start = u.start_update_check

        def tracked_start(base):
            worker = original_start(base)
            if worker:
                self.workers.append(worker)
            return worker

        context = patch.object(u, "start_update_check", side_effect=tracked_start)
        context.start()
        self.addCleanup(context.stop)
        self.addCleanup(self.wait_workers)

    def wait_workers(self):
        for worker in self.workers:
            worker.join(3)
            self.assertFalse(worker.is_alive(), "更新確認ワーカーが終了していない")

    def test_slow_network_does_not_block_application_start_or_exit(self):
        network_started, release_network, application_started = (threading.Event() for _ in range(3))
        result = []

        def slow_fetch(*args, **kwargs):
            network_started.set()
            release_network.wait(3)
            return json.dumps(self.manifest).encode()

        def main(argv):
            application_started.set()
            return 7

        with patch.dict(os.environ, {}, clear=True), patch.object(u, "fetch", side_effect=slow_fetch), \
                patch.object(u.importlib, "import_module", return_value=Mock(main=main)), redirect_stderr(io.StringIO()):
            caller = threading.Thread(target=lambda: result.append(u.run_application(self.base, ["--ui"])), daemon=True)
            caller.start()
            try:
                self.assertTrue(network_started.wait(1))
                self.assertTrue(application_started.wait(.5), "更新通信の完了待ちでアプリが起動しない")
                caller.join(.5)
                self.assertFalse(caller.is_alive(), "アプリ終了時も更新通信を待っている")
                self.assertEqual(result, [7])
            finally:
                release_network.set()
                caller.join(3)
                self.wait_workers()

    def start(self, argv=None):
        output = io.StringIO()

        def main(argv):
            self.wait_workers()
            return 0

        with patch.dict(os.environ, {}), patch.object(u.importlib, "import_module", return_value=Mock(main=main)), \
                redirect_stderr(output):
            os.environ.pop("ATTENDANCE_NO_UPDATE", None)
            self.assertEqual(u.run_application(self.base, argv or ["--check", "731"], use_runtime=True), 0)
        return output.getvalue()

    def test_each_start_notifies_from_cache_and_daily_check_fetches_json_only(self):
        with patch.object(u, "fetch", return_value=json.dumps(self.manifest).encode()) as fetch, \
                patch.object(d, "prepare_environment") as prepare, patch.object(u, "finish_update") as apply:
            for _ in range(2):
                output = self.start()
                self.assertIn("アップデートがあります", output)
                self.assertIn("att update --notes", output)
                self.assertIn("アップデートする: att update", output)
            self.assertEqual(fetch.call_count, 1)
            self.assertTrue(all(call.args[0] == u.MANIFEST_URL for call in fetch.call_args_list))
            prepare.assert_not_called()
            apply.assert_not_called()
        self.assertFalse((self.base / ".updates/pending").exists())
        self.assertEqual(u.fingerprint(self.base), self.fixture.original)

    def test_ui_start_is_silent_and_completed_check_is_cached(self):
        with patch.object(u, "fetch", return_value=json.dumps(self.manifest).encode()):
            self.assertEqual(self.start(["--ui"]), "")
            self.assertEqual(self.start(["--ui"]), "")
        self.assertEqual(u.cached_manifest(self.base), self.manifest)

    def test_expired_cache_is_refreshed_but_fresh_cache_is_reused(self):
        u.write_json(self.base / ".updates/check.json", {"checked_at": u.time.time() - 86401,
                                                       "interval": 86400, "manifest": self.manifest})
        with patch.object(u, "fetch", return_value=json.dumps(self.manifest).encode()) as fetch:
            self.start()
            self.start()
        fetch.assert_called_once()

    def test_cached_cli_notice_is_not_duplicated_or_printed_during_input(self):
        u.write_json(self.base / ".updates/check.json", {"manifest": self.manifest})
        with patch.object(u, "fetch", return_value=json.dumps(self.manifest).encode()):
            self.assertEqual(self.start().count("アップデートがあります"), 1)

    def test_busy_check_lock_does_not_log_failure_or_block_app(self):
        with u.file_lock(self.base / ".updates/download.lock", exclusive=True), patch.object(u, "fetch") as fetch:
            self.assertEqual(self.start(["--ui"]), "")
        fetch.assert_not_called()
        self.assertFalse((self.base / ".updates/updater.log").exists())

    def test_worker_creation_failure_does_not_stop_application(self):
        with patch.object(u.threading.Thread, "start", side_effect=RuntimeError("no threads")):
            self.assertEqual(self.start(["--ui"]), "")
        self.assertTrue((self.base / ".updates/updater.log").is_file())

    def test_current_and_older_releases_are_silent(self):
        for version in (fixtures.VERSION, "0.0.1"):
            manifest = fixtures.manifest_for(self.fixture.archive(), version)
            with self.subTest(version=version), patch.object(u, "fetch", return_value=json.dumps(manifest).encode()):
                self.assertEqual(self.start(), "")

    def test_offline_start_is_silent_and_backoff_prevents_repeated_requests(self):
        with patch.object(u, "fetch", side_effect=URLError("offline")) as fetch:
            self.assertEqual(self.start(), "")
            self.assertEqual(self.start(), "")
            fetch.assert_called_once()
        self.assertIn("接続できません", (self.base / ".updates/updater.log").read_text())
        self.assertEqual(u.fingerprint(self.base), self.fixture.original)

    def test_start_keeps_already_prepared_update_without_applying(self):
        self.fixture.prepare()
        with patch.object(u, "fetch", return_value=json.dumps(self.manifest).encode()), patch.object(u, "finish_update") as apply:
            self.assertIn("att update", self.start())
            apply.assert_not_called()
        self.assertTrue((self.base / ".updates/pending").exists())
        self.assertEqual(u.fingerprint(self.base), self.fixture.original)

    def test_notes_display_does_not_prepare_or_apply_even_with_pending_update(self):
        self.fixture.prepare()
        output = io.StringIO()
        with patch.object(u, "fetch", return_value=json.dumps(self.manifest).encode()) as fetch, \
                patch.object(d, "prepare_environment") as prepare, patch.object(u, "finish_update") as apply, redirect_stdout(output):
            self.assertEqual(u.run_application(self.base, ["update", "--notes"], use_runtime=True), 0)
            fetch.assert_called_once()
            prepare.assert_not_called()
            apply.assert_not_called()
        self.assertIn(self.manifest["release_notes"], output.getvalue())
        self.assertTrue((self.base / ".updates/pending").exists())
        self.assertEqual(u.fingerprint(self.base), self.fixture.original)

    def test_legacy_manifest_without_notes_links_to_release(self):
        manifest = fixtures.manifest_for(self.fixture.archive())
        output = io.StringIO()
        with patch.object(u, "fetch", return_value=json.dumps(manifest).encode()), redirect_stdout(output):
            self.assertEqual(u.run_application(self.base, ["update", "--notes"]), 0)
        self.assertIn(f"/releases/tag/v{fixtures.NEXT}", output.getvalue())

    def test_notes_failure_returns_error_without_applying_pending_update(self):
        self.fixture.prepare()
        output = io.StringIO()
        with patch.object(u, "fetch", side_effect=HTTPError(u.MANIFEST_URL, 403, "", {}, None)), \
                patch.object(u, "finish_update") as apply, redirect_stderr(output):
            self.assertEqual(u.run_application(self.base, ["update", "--notes"]), 1)
            apply.assert_not_called()
        self.assertIn("HTTP 403", output.getvalue())
        self.assertEqual(u.fingerprint(self.base), self.fixture.original)

    def test_invalid_notes_are_rejected_before_printing_terminal_control_characters(self):
        for text in ("", notes("1.2.3"), notes(fixtures.NEXT) + "\x1b[2J", "x" * 20000, None):
            with self.subTest(text=str(text)[:30]), self.assertRaises(u.UpdateError):
                u.validate_manifest({**self.manifest, "release_notes": text})

    def test_screen_keeps_notification_and_commands_visible_after_redraw(self):
        from src.terminal_ui import AttendanceScreen
        u.write_json(self.base / ".updates/check.json", {"manifest": self.manifest})
        config = {"users": [{"username": "test", "student_id": "TEST001"}], "default_user": "test"}
        with patch.dict(os.environ, {}, clear=True):
            ui = AttendanceScreen(config, "TEST001", "", Mock(), base=self.base)
        for height, width in ((24, 62), (24, 80), (30, 120), (20, 50)):
            with self.subTest(size=(height, width)):
                screen = Mock(getmaxyx=Mock(return_value=(height, width)))
                for _ in range(2):
                    ui.draw(screen)
                    text = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
                    self.assertIn(fixtures.NEXT, text)
                    self.assertIn("att update --notes", text)
                    self.assertIn("適用: att update", text)
                    screen.reset_mock()

    def test_screen_demo_and_opt_out_do_not_show_cached_update(self):
        from src.terminal_ui import AttendanceScreen
        u.write_json(self.base / ".updates/check.json", {"manifest": self.manifest})
        for demo, environment in ((True, {}), (False, {"ATTENDANCE_NO_UPDATE": "1"})):
            with self.subTest(demo=demo), patch.dict(os.environ, environment, clear=True):
                ui = AttendanceScreen({}, "TEST001", "", Mock(), base=self.base, demo=demo)
                self.assertIsNone(ui.update_version)

    def test_screen_picks_up_finished_check_after_start_without_network_or_stderr(self):
        from src.terminal_ui import AttendanceScreen
        config = {"users": [{"username": "test", "student_id": "TEST001"}], "default_user": "test"}
        with patch.dict(os.environ, {}, clear=True):
            ui = AttendanceScreen(config, "TEST001", "", Mock(), base=self.base)
            self.assertIsNone(ui.update_version)
            with patch.object(u, "fetch", return_value=json.dumps(self.manifest).encode()):
                self.assertEqual(self.start(["--ui"]), "")
            ui.update_check_after = 0
            with patch.object(u, "fetch", side_effect=AssertionError("UI must not fetch")), redirect_stderr(io.StringIO()) as output:
                ui.poll()
                self.assertEqual(ui.update_version, fixtures.NEXT)
                screen = Mock(getmaxyx=Mock(return_value=(24, 80)))
                ui.draw(screen)
                shown = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
                self.assertIn("att update --notes", shown)
                self.assertIn("適用: att update", shown)
                self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
