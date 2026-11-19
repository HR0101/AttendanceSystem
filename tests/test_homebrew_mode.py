"""Homebrew 導入モード（データ分離・LaunchAgent・更新案内）の検証テスト。"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

import main as app_main
from src import scheduler, updater
from src.terminal_ui import AttendanceScreen
from src.version import VERSION


class HomebrewModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.code_dir = self.root / "cellar-app"
        self.code_dir.mkdir(mode=0o755)
        for name in ("config.example.json", "schedule.example.json"):
            shutil.copy2(Path(app_main.__file__).resolve().parent / name, self.code_dir / name)
        self.data_dir = self.root / "attendance-home"

    def test_resolve_data_dir_and_rejects_symlinks_or_files(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(updater.resolve_data_dir(self.code_dir), self.code_dir)

        with patch.dict(os.environ, {"ATTENDANCE_HOME": str(self.data_dir)}, clear=True):
            self.assertEqual(updater.resolve_data_dir(self.code_dir), self.data_dir.resolve().parent / self.data_dir.name)

        target = self.root / "real-target"
        target.mkdir()
        link = self.root / "home-link"
        link.symlink_to(target)
        with patch.dict(os.environ, {"ATTENDANCE_HOME": str(link)}, clear=True):
            with self.assertRaisesRegex(updater.UpdateError, "シンボリックリンク"):
                updater.resolve_data_dir(self.code_dir)

        regular_file = self.root / "not-a-dir"
        regular_file.write_text("x", encoding="utf-8")
        with patch.dict(os.environ, {"ATTENDANCE_HOME": str(regular_file)}, clear=True):
            with self.assertRaisesRegex(updater.UpdateError, "フォルダではありません"):
                updater.resolve_data_dir(self.code_dir)

    def test_ensure_user_workspace_creates_private_config_and_finder_launcher_only_once(self):
        env = {
            "ATTENDANCE_HOME": str(self.data_dir),
            "ATTENDANCE_MANAGED_BY": "homebrew",
            "ATTENDANCE_EXECUTABLE": "/opt/homebrew/opt/attendance/bin/att",
        }
        with patch.dict(os.environ, env, clear=True):
            updater.ensure_user_workspace(self.data_dir, self.code_dir)

        config_path = self.data_dir / "config.json"
        launcher_path = self.data_dir / "start.command"
        self.assertTrue(config_path.is_file())
        self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
        self.assertEqual(config_path.read_bytes(), (self.code_dir / "config.example.json").read_bytes())

        self.assertTrue(launcher_path.is_file())
        self.assertEqual(stat.S_IMODE(launcher_path.stat().st_mode), 0o755)
        launcher_text = launcher_path.read_text(encoding="utf-8")
        self.assertIn("/opt/homebrew/opt/attendance/bin/att --ui", launcher_text)
        self.assertIn("ATTENDANCE_HOME", launcher_text)

        # 既存設定やランチャーは上書きしない。
        config_path.write_text('{"custom": true}\n', encoding="utf-8")
        launcher_path.write_text("#!/bin/bash\necho custom\n", encoding="utf-8")
        with patch.dict(os.environ, env, clear=True):
            updater.ensure_user_workspace(self.data_dir, self.code_dir)
        self.assertEqual(config_path.read_text(encoding="utf-8"), '{"custom": true}\n')
        self.assertEqual(launcher_path.read_text(encoding="utf-8"), "#!/bin/bash\necho custom\n")

    def test_homebrew_no_workspace_flags_do_not_create_attendance_home(self):
        env = {
            "ATTENDANCE_HOME": str(self.data_dir),
            "ATTENDANCE_MANAGED_BY": "homebrew",
            "ATTENDANCE_NO_UPDATE": "1",
        }
        for flag in ("--help", "--version"):
            with self.subTest(flag=flag), patch.dict(os.environ, env, clear=True):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as exited:
                        updater.run_application(self.code_dir, [flag], use_runtime=True)
                self.assertEqual(exited.exception.code, 0)
                self.assertFalse(self.data_dir.exists())

    def test_homebrew_update_command_guides_brew_upgrade_and_blocks_rollback(self):
        env = {
            "ATTENDANCE_HOME": str(self.data_dir),
            "ATTENDANCE_MANAGED_BY": "homebrew",
            "ATTENDANCE_NO_UPDATE": "1",
        }
        with patch.dict(os.environ, env, clear=True), \
                patch.object(updater, "prepare_update", side_effect=AssertionError("Homebrew版で自己更新してはいけない")):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = updater.run_application(self.code_dir, ["update"], use_runtime=True)
            self.assertEqual(code, 0)
            self.assertIn("brew upgrade attendance", out.getvalue())

            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rollback_code = updater.run_application(self.code_dir, ["update", "--rollback"], use_runtime=True)
            self.assertEqual(rollback_code, 1)
            self.assertIn("Homebrew で導入されているため", err.getvalue())

    def test_homebrew_readonly_code_dir_with_cli_check_writes_only_to_attendance_home(self):
        main_copy = self.code_dir / "main.py"
        shutil.copy2(app_main.__file__, main_copy)
        os.chmod(self.code_dir, 0o555)
        self.addCleanup(lambda: os.chmod(self.code_dir, 0o755) if self.code_dir.exists() else None)

        env = {
            "ATTENDANCE_HOME": str(self.data_dir),
            "ATTENDANCE_MANAGED_BY": "homebrew",
            "ATTENDANCE_NO_UPDATE": "1",
        }
        with patch.dict(os.environ, env, clear=True), \
                patch.object(app_main, "__file__", str(main_copy)), \
                patch.object(app_main, "get_credentials", return_value=("S123456", "pw")) as creds:
            config_target = self.data_dir / "config.json"
            updater.ensure_user_workspace(self.data_dir, self.code_dir)
            config = json.loads(config_target.read_text(encoding="utf-8"))
            config["browser"]["headless"] = True
            config["_active_username"] = "test"
            config_target.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

            fake_page = Mock()
            with patch("src.browser.open_page") as open_page, \
                    patch("src.attendance.run_attendance", return_value="出席登録済み（テスト）"), \
                    redirect_stdout(io.StringIO()) as out:
                open_page.return_value.__enter__.return_value = fake_page
                code = updater.run_application(self.code_dir, ["--check", "642"], use_runtime=True)

        self.assertEqual(code, 0)
        self.assertIn("出席登録済み（テスト）", out.getvalue())
        self.assertEqual(creds.call_args.args[1], config_target.resolve())
        self.assertTrue((self.data_dir / "logs").is_dir())
        self.assertFalse((self.code_dir / "config.json").exists())
        self.assertFalse((self.code_dir / "logs").exists())
        self.assertFalse((self.code_dir / ".updates").exists())

    def test_available_message_and_ui_banner_show_brew_upgrade_in_homebrew_mode(self):
        next_parts = [int(p) for p in VERSION.split(".")]
        next_version = f"{next_parts[0]}.{next_parts[1]}.{next_parts[2] + 1}"
        manifest = {
            "schema_version": 1,
            "version": next_version,
            "archive": {"name": updater.ASSET_NAME, "sha256": "0" * 64, "size": 100},
        }
        updater.ensure_user_workspace(self.data_dir, self.code_dir)
        updater.write_json(self.data_dir / ".updates/check.json", {"manifest": manifest})

        with patch.dict(os.environ, {"ATTENDANCE_MANAGED_BY": "homebrew"}, clear=True):
            msg = updater.available_message(manifest)
            self.assertIn("brew upgrade attendance", msg)

            ui = AttendanceScreen({}, "TEST001", "", Mock(), base=self.data_dir)
            self.assertEqual(ui.update_version, next_version)
            for cols in (120, 80):
                screen = Mock(getmaxyx=Mock(return_value=(24, cols)))
                ui.draw(screen)
                rendered = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
                self.assertIn("brew upgrade attendance", rendered)

    def test_scheduler_install_and_status_use_homebrew_executable_and_attendance_home(self):
        plist_path = self.root / "LaunchAgents" / "com.hr0101.attendance.plist"
        updater.ensure_user_workspace(self.data_dir, self.code_dir)
        schedule = {
            "active_from": scheduler.date.today(),
            "active_until": scheduler.date.today() + scheduler.timedelta(days=30),
            "courses": [{"weekday": "mon", "start_time": "10:00", "room": "642", "course_name": "情報工学", "enabled": True}],
        }
        env = {
            "ATTENDANCE_HOME": str(self.data_dir),
            "ATTENDANCE_MANAGED_BY": "homebrew",
            "ATTENDANCE_EXECUTABLE": "/opt/homebrew/opt/attendance/bin/att",
        }
        with patch.dict(os.environ, env, clear=True), \
                patch.object(scheduler, "agent_path", return_value=plist_path), \
                patch.object(scheduler, "_launchctl_loaded", return_value=True), \
                patch.object(scheduler.subprocess, "run", return_value=Mock(returncode=0, stderr=b"", stdout=b"")):
            installed_path = scheduler.install_agent(self.data_dir, schedule)
            self.assertEqual(installed_path, plist_path)
            plist = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(plist["ProgramArguments"], ["/opt/homebrew/opt/attendance/bin/att", "--scheduled"])
            self.assertEqual(plist["WorkingDirectory"], str(self.data_dir))
            self.assertEqual(plist["EnvironmentVariables"], {"ATTENDANCE_HOME": str(self.data_dir)})
            self.assertTrue(scheduler.agent_installed(self.data_dir))
            status = scheduler.agent_status(self.data_dir)
            self.assertTrue(status["owned"])
            self.assertEqual(status["state"], "installed")
            self.assertTrue(scheduler.remove_agent(self.data_dir))


if __name__ == "__main__":
    unittest.main()
