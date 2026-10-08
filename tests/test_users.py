"""複数ユーザーの選択、既存設定の移行、Keychain の分離を確認する。"""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import main
from src.config import ConfigurationError, load_config
from src.credentials import SERVICE, get_credentials
from src.user_commands import user_command
from src.users import get_users, select_user


CONFIG = {
    "login_url": "https://example.test/attendance/login",
    "top_url": "https://example.test/attendance/top",
    "student_id": "OLD001",
    "browser": {"headless": True, "timeout_ms": 15000},
    "dry_run": False,
}


class UserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / "config.json"
        self.path.write_text(json.dumps(CONFIG))

    def command(self, *args):
        with redirect_stdout(io.StringIO()):
            return user_command(self.base, list(args))

    def test_legacy_user_survives_add_and_rename(self):
        backend = Mock(get_password=Mock(return_value="secret-new"))
        with patch("src.user_commands.keychain_backend", return_value=backend), patch("src.user_commands.getpass.getpass", return_value="secret-new"):
            self.assertEqual(self.command("add", "花子", "--student-id", "NEW001"), 0)
        config = load_config(self.path)
        self.assertEqual(select_user(config)["student_id"], "OLD001")
        self.assertEqual(len(get_users(config)), 2)
        self.assertNotIn("secret-new", self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.command("rename", "OLD001", "太郎"), 0)
        self.assertEqual(select_user(load_config(self.path))["username"], "太郎")
        self.assertEqual(self.command("use", "花子"), 0)
        self.assertEqual(select_user(load_config(self.path))["student_id"], "NEW001")

    def test_keychain_lookup_uses_selected_student_id(self):
        config = dict(CONFIG, users=[{"username": "太郎", "student_id": "OLD001"}, {"username": "花子", "student_id": "NEW001"}], default_user="太郎")
        backend = Mock()
        backend.get_password.side_effect = lambda service, account: {"OLD001": "old-secret", "NEW001": "new-secret"}[account]
        with patch("src.credentials.keychain_backend", return_value=backend):
            self.assertEqual(get_credentials(config, self.path, username="花子"), ("NEW001", "new-secret"))
            self.assertEqual(get_credentials(config, self.path, interactive=False), ("OLD001", "old-secret"))
        self.assertEqual(config["default_user"], "太郎")
        self.assertEqual(config["student_id"], "OLD001")
        backend.get_password.assert_any_call(SERVICE, "NEW001")
        backend.get_password.assert_any_call(SERVICE, "OLD001")

    def test_duplicate_or_unknown_user_does_not_change_file(self):
        before = self.path.read_text()
        with patch("src.user_commands.keychain_backend") as backend:
            self.assertEqual(self.command("add", "別名", "--student-id", "OLD001"), 1)
            backend.assert_not_called()
        self.assertEqual(self.command("use", "未登録"), 1)
        self.assertEqual(self.path.read_text(), before)
        with self.assertRaises(ConfigurationError):
            select_user(CONFIG, "未登録")

    def test_invalid_profile_config_is_rejected(self):
        for users, default in [
            ([{"username": "a", "student_id": "A"}, {"username": "a", "student_id": "B"}], "a"),
            ([{"username": "a", "student_id": "A"}], "missing"),
            ([{"username": "\x1bname", "student_id": "A"}], ""),
        ]:
            self.path.write_text(json.dumps(dict(CONFIG, users=users, default_user=default)))
            with self.assertRaises(ConfigurationError):
                load_config(self.path)

    def test_first_registration_persists_name_without_password(self):
        config = dict(CONFIG, student_id="")
        backend = Mock(get_password=Mock(side_effect=[None, "first-secret"]))
        with patch("src.credentials.keychain_backend", return_value=backend), patch("builtins.input", side_effect=["FIRST001", "最初のユーザー"]), patch("src.credentials.getpass.getpass", return_value="first-secret"):
            self.assertEqual(get_credentials(config, self.path), ("FIRST001", "first-secret"))
        stored = load_config(self.path)
        self.assertEqual(select_user(stored)["username"], "最初のユーザー")
        self.assertNotIn("first-secret", self.path.read_text())
        self.assertNotIn("_active_username", stored)

    def test_cli_passes_user_to_ui_and_credentials(self):
        from contextlib import nullcontext
        config = copy.deepcopy(CONFIG)
        def credentials(*args, **kwargs):
            args[0]["_active_username"] = kwargs["username"]
            return "NEW001", "dummy"
        with patch("main.load_config", return_value=config), patch("main.setup_logging", return_value=Mock()), patch("main.get_credentials", side_effect=credentials) as get, patch("src.browser.open_page", return_value=nullcontext(Mock())), patch("src.attendance.run_attendance", return_value="出席済み"), patch("src.terminal_ui.launch_ui", return_value=0) as ui, redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(["642", "--user", "花子"]), 0)
            self.assertEqual(get.call_args.kwargs["username"], "花子")
            ui.assert_not_called()
            self.assertEqual(main.main(["--ui", "642", "--user", "花子"]), 0)
            self.assertEqual(ui.call_args.args[-1], "花子")

    def test_schedule_status_does_not_require_ui_arguments(self):
        schedule = {"courses": [], "active_from": "2026-01-01", "active_until": "2026-12-31"}
        with patch("main.load_schedule", return_value=schedule), patch("main.agent_installed", return_value=False), redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(["schedule", "status"]), 0)


if __name__ == "__main__":
    unittest.main()
