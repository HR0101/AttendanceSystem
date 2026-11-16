"""通常起動は通知のみ。更新内容の確認もパッケージを変更しない。"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
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

    def start(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}), patch.object(u.importlib, "import_module", return_value=Mock(main=Mock(return_value=0))), \
                redirect_stderr(output):
            os.environ.pop("ATTENDANCE_NO_UPDATE", None)
            self.assertEqual(u.run_application(self.base, ["--ui"], use_runtime=True), 0)
        return output.getvalue()

    def test_each_start_notifies_with_commands_and_fetches_json_only(self):
        with patch.object(u, "fetch", return_value=json.dumps(self.manifest).encode()) as fetch, \
                patch.object(d, "prepare_environment") as prepare, patch.object(u, "finish_update") as apply:
            for _ in range(2):
                output = self.start()
                self.assertIn("アップデートがあります", output)
                self.assertIn("att update --notes", output)
                self.assertIn("アップデートする: att update", output)
            self.assertEqual(fetch.call_count, 2)
            self.assertTrue(all(call.args[0] == u.MANIFEST_URL for call in fetch.call_args_list))
            prepare.assert_not_called()
            apply.assert_not_called()
        self.assertFalse((self.base / ".updates/pending").exists())
        self.assertEqual(u.fingerprint(self.base), self.fixture.original)

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


if __name__ == "__main__":
    unittest.main()
