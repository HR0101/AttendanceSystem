"""複数ユーザーの選択、既存設定の移行、Keychain の分離を確認する。"""

import copy
import io
import json
import logging

import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import main
from src.config import ConfigurationError, load_config, private_file_lock
from src.credentials import CredentialError, SERVICE, get_credentials
from src.user_commands import add_user, rename_user, user_command
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

    def test_password_change_targets_selected_user_and_preserves_config(self):
        config = dict(CONFIG, users=[{"username": "太郎", "student_id": "OLD001"},
                                     {"username": "花子", "student_id": "NEW001"}], default_user="太郎")
        self.path.write_text(json.dumps(config))
        before = self.path.read_bytes()
        passwords = {"OLD001": "unchanged", "NEW001": "old-password"}
        backend = Mock()
        backend.set_password.side_effect = lambda service, account, value: passwords.update({account: value})
        backend.get_password.side_effect = lambda service, account: passwords[account]
        output = io.StringIO()
        with patch("src.user_commands.keychain_backend", return_value=backend), \
                patch("src.user_commands.getpass.getpass", side_effect=[" new password! "] * 2), redirect_stdout(output):
            self.assertEqual(user_command(self.base, ["password", "花子"]), 0)
        backend.set_password.assert_called_once_with(SERVICE, "NEW001", " new password! ")
        self.assertEqual(passwords["OLD001"], "unchanged")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn(" new password! ", output.getvalue())
        with patch("src.credentials.keychain_backend", return_value=backend):
            self.assertEqual(get_credentials(load_config(self.path), self.path, interactive=False, username="花子"),
                             ("NEW001", " new password! "))

    def test_password_change_without_name_uses_default(self):
        backend = Mock(get_password=Mock(return_value="new-password"))
        with patch("src.user_commands.keychain_backend", return_value=backend), \
                patch("src.user_commands.getpass.getpass", side_effect=["new-password"] * 2):
            self.assertEqual(self.command("password"), 0)
        backend.set_password.assert_called_once_with(SERVICE, "OLD001", "new-password")

    def test_password_mismatch_empty_and_cancel_do_not_write_keychain(self):
        before = self.path.read_bytes()
        for inputs in (["secret", "different"], [""], ["secret", KeyboardInterrupt()], [EOFError()]):
            with self.subTest(inputs=len(inputs)), patch("src.user_commands.keychain_backend") as backend, \
                    patch("src.user_commands.getpass.getpass", side_effect=inputs):
                self.assertEqual(self.command("password"), 1)
                backend.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_password_target_does_not_prompt(self):
        with patch("src.user_commands.getpass.getpass") as prompt, patch("src.user_commands.keychain_backend") as backend:
            self.assertEqual(self.command("password", "未登録"), 1)
            prompt.assert_not_called()
            backend.assert_not_called()

    def test_password_keychain_failure_is_reported_without_secret(self):
        before = self.path.read_bytes()
        for backend in (Mock(set_password=Mock(side_effect=RuntimeError("private-password"))),
                        Mock(get_password=Mock(return_value="old-password"))):
            output = io.StringIO()
            with patch("src.user_commands.keychain_backend", return_value=backend), \
                    patch("src.user_commands.getpass.getpass", side_effect=["private-password"] * 2), redirect_stdout(output):
                self.assertEqual(user_command(self.base, ["password"]), 1)
            self.assertNotIn("private-password", output.getvalue())
            self.assertEqual(self.path.read_bytes(), before)

    def _memory_backend(self, initial):
        values = dict(initial)

        def get_password(service, account):
            self.assertEqual(service, SERVICE)
            return values.get(account)

        def set_password(service, account, password):
            self.assertEqual(service, SERVICE)
            values[account] = password

        def delete_password(service, account):
            self.assertEqual(service, SERVICE)
            del values[account]

        backend = Mock()
        backend.get_password.side_effect = get_password
        backend.set_password.side_effect = set_password
        backend.delete_password.side_effect = delete_password
        return backend, values

    def _write_two_users(self, **extra):
        config = dict(
            CONFIG,
            users=[
                {"username": "太郎", "student_id": "OLD001"},
                {"username": "花子", "student_id": "NEW002"},
            ],
            default_user="太郎",
            **extra,
        )
        self.path.write_text(json.dumps(config))
        return config

    def _run_user_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = user_command(self.base, list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_remove_with_confirmation_updates_default_and_deletes_keychain(self):
        self._write_two_users(ui_selected_users=["OLD001", "NEW002"])
        backend, values = self._memory_backend({"OLD001": "old-secret-pw", "NEW002": "new-secret-pw"})
        for answer in ("y", "YES"):
            with self.subTest(answer=answer):
                self._write_two_users(ui_selected_users=["OLD001", "NEW002"])
                values.clear()
                values.update({"OLD001": "old-secret-pw", "NEW002": "new-secret-pw"})
                with patch("src.user_commands.keychain_backend", return_value=backend), \
                        patch("builtins.input", return_value=answer) as prompt:
                    code, out, err = self._run_user_cli("remove", "太郎")
                self.assertEqual(code, 0)
                prompt.assert_called_once()
                self.assertIn("太郎", out)
                self.assertIn("OLD001", out)
                self.assertIn("花子", out)
                self.assertNotIn("old-secret-pw", out + err)
                self.assertNotIn("new-secret-pw", out + err)
                stored = load_config(self.path)
                self.assertEqual(get_users(stored), [{"username": "花子", "student_id": "NEW002"}])
                self.assertEqual(stored["default_user"], "花子")
                self.assertEqual(stored["ui_selected_users"], ["NEW002"])
                self.assertNotIn("student_id", stored)
                self.assertNotIn("old-secret-pw", self.path.read_text())
                self.assertEqual(values, {"NEW002": "new-secret-pw"})

    def test_remove_last_user_and_non_default_user_messages(self):
        self._write_two_users()
        backend, values = self._memory_backend({"OLD001": "old-secret-pw", "NEW002": "new-secret-pw"})
        with patch("src.user_commands.keychain_backend", return_value=backend), \
                patch("builtins.input") as prompt:
            code, out, err = self._run_user_cli("remove", "花子", "--yes")
        self.assertEqual(code, 0)
        prompt.assert_not_called()
        self.assertIn("花子", out)
        self.assertNotIn("既定ユーザーを", out)
        self.assertNotIn("new-secret-pw", out + err)
        self.assertEqual(load_config(self.path)["default_user"], "太郎")
        self.assertEqual(values, {"OLD001": "old-secret-pw"})

        with patch("src.user_commands.keychain_backend", return_value=backend), \
                patch("builtins.input") as prompt:
            code, out, err = self._run_user_cli("remove", "太郎", "-y")
        self.assertEqual(code, 0)
        prompt.assert_not_called()
        self.assertIn("登録ユーザーはなくなりました", out)
        self.assertNotIn("old-secret-pw", out + err)
        stored = load_config(self.path)
        self.assertEqual(get_users(stored), [])
        self.assertEqual(stored["default_user"], "")
        self.assertEqual(values, {})

    def test_remove_declined_or_aborted_does_not_touch_config_or_keychain(self):
        self._write_two_users()
        before = self.path.read_bytes()
        for response in ("n", "", "no", "maybe", EOFError(), KeyboardInterrupt()):
            with self.subTest(response=repr(response)), \
                    patch("src.user_commands.keychain_backend") as backend, \
                    patch("builtins.input", side_effect=[response]):
                code, out, err = self._run_user_cli("remove", "太郎")
            self.assertEqual(code, 1)
            backend.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(err, "")
            self.assertTrue(out.strip())

    def test_remove_unknown_user_rejects_before_prompting(self):
        self._write_two_users()
        before = self.path.read_bytes()
        for name in ("未登録", ""):
            with self.subTest(name=name), \
                    patch("src.user_commands.keychain_backend") as backend, \
                    patch("builtins.input") as prompt:
                code, out, _ = self._run_user_cli("remove", name)
            self.assertEqual(code, 1)
            prompt.assert_not_called()
            backend.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertIn("未登録", out)

    def test_student_id_migrates_keychain_and_preserves_other_users(self):
        self._write_two_users(ui_selected_users=["OLD001", "NEW002"])
        backend, values = self._memory_backend({"OLD001": "old-secret-pw", "NEW002": "new-secret-pw"})
        with patch("src.user_commands.keychain_backend", return_value=backend):
            code, out, err = self._run_user_cli("student-id", "太郎", "FIXED003")
        self.assertEqual(code, 0)
        self.assertIn("太郎", out)
        self.assertIn("OLD001", out)
        self.assertIn("FIXED003", out)
        self.assertNotIn("old-secret-pw", out + err)
        self.assertNotIn("new-secret-pw", out + err)
        self.assertEqual(values, {"FIXED003": "old-secret-pw", "NEW002": "new-secret-pw"})
        stored = load_config(self.path)
        self.assertEqual(get_users(stored), [
            {"username": "太郎", "student_id": "FIXED003"},
            {"username": "花子", "student_id": "NEW002"},
        ])
        self.assertEqual(stored["default_user"], "太郎")
        self.assertEqual(stored["ui_selected_users"], ["FIXED003", "NEW002"])
        self.assertNotIn("student_id", stored)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("old-secret-pw", self.path.read_text())

    def test_student_id_same_id_is_noop(self):
        self._write_two_users()
        before = self.path.read_bytes()
        with patch("src.user_commands.keychain_backend") as backend:
            code, out, err = self._run_user_cli("student-id", "太郎", "OLD001")
        self.assertEqual(code, 0)
        backend.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("OLD001", out)
        self.assertEqual(err, "")

    def test_student_id_rejects_invalid_duplicate_unknown_and_orphan_keychain(self):
        self._write_two_users()
        before = self.path.read_bytes()
        for invalid in ("", "Ａ１２", "A B", "../path", "NEW002"):
            with self.subTest(invalid=invalid), patch("src.user_commands.keychain_backend") as backend:
                code, out, _ = self._run_user_cli("student-id", "太郎", invalid)
            self.assertEqual(code, 1)
            backend.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertTrue(out.strip())

        for unknown in ("未登録", ""):
            with self.subTest(unknown=unknown), patch("src.user_commands.keychain_backend") as backend:
                code, out, _ = self._run_user_cli("student-id", unknown, "FIXED003")
            self.assertEqual(code, 1)
            backend.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertIn("未登録", out)

        backend, values = self._memory_backend({
            "OLD001": "old-secret-pw",
            "NEW002": "new-secret-pw",
            "ORPHAN01": "orphan-secret-pw",
        })
        with patch("src.user_commands.keychain_backend", return_value=backend):
            code, out, err = self._run_user_cli("student-id", "太郎", "ORPHAN01")
        self.assertEqual(code, 1)
        self.assertIn("上書きせず", out)
        self.assertNotIn("old-secret-pw", out + err)
        self.assertNotIn("orphan-secret-pw", out + err)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(values["ORPHAN01"], "orphan-secret-pw")
        self.assertEqual(values["OLD001"], "old-secret-pw")

    def test_remove_and_student_id_keychain_error_never_leaks_secret(self):
        self._write_two_users()
        before = self.path.read_bytes()
        backend = Mock()
        backend.get_password.side_effect = RuntimeError("super-secret-password")
        for args in (["remove", "太郎", "--yes"], ["student-id", "太郎", "FIXED003"]):
            with self.subTest(args=args), patch("src.user_commands.keychain_backend", return_value=backend):
                code, out, err = self._run_user_cli(*args)
            self.assertEqual(code, 1)
            self.assertNotIn("super-secret-password", out + err)
            self.assertEqual(self.path.read_bytes(), before)

    def test_user_help_lists_all_subcommands(self):
        for argv, expected in (
            (["--help"], ("list", "add", "use", "rename", "password", "student-id", "remove")),
            (["remove", "--help"], ("--yes", "-y")),
            (["student-id", "--help"], ("username", "new_student_id")),
        ):
            with self.subTest(argv=argv):
                out = io.StringIO()
                with redirect_stdout(out), self.assertRaises(SystemExit) as exit_info:
                    user_command(self.base, argv)
                self.assertEqual(exit_info.exception.code, 0)
                for token in expected:
                    self.assertIn(token, out.getvalue())

    def _memory_logger(self):
        stream = io.StringIO()
        logger = logging.getLogger("test_cli_interrupts")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(handler)
        return logger, stream

    def test_cli_handles_eof_and_keyboard_interrupt_cleanly(self):
        for exc_type, expected_code in ((EOFError, 1), (KeyboardInterrupt, 130)):
            for stage, argv in (("ask_room", []), ("get_credentials", ["642"]), ("run_attendance", ["642"])):
                with self.subTest(exc=exc_type.__name__, stage=stage):
                    logger, log_stream = self._memory_logger()
                    stderr = io.StringIO()
                    stdout = io.StringIO()
                    config = copy.deepcopy(CONFIG)
                    room_patch = patch("main.ask_room", side_effect=exc_type()) if stage == "ask_room" else patch("main.ask_room", return_value=("642", "642"))
                    cred_patch = (
                        patch("main.get_credentials", side_effect=exc_type())
                        if stage == "get_credentials"
                        else patch("main.get_credentials", side_effect=lambda cfg, *a, **kw: (cfg.__setitem__("_active_username", "太郎") or ("OLD001", "dummy")))
                    )
                    run_patch = (
                        patch("src.attendance.run_attendance", side_effect=exc_type())
                        if stage == "run_attendance"
                        else patch("src.attendance.run_attendance", return_value="出席済み")
                    )
                    with patch("main.load_config", return_value=config), \
                            patch("main.setup_logging", return_value=logger), \
                            room_patch, cred_patch, \
                            patch("src.browser.open_page", return_value=nullcontext(Mock())), \
                            run_patch, \
                            redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main.main(argv)
                    self.assertEqual(code, expected_code)
                    self.assertIn("中断", stderr.getvalue())
                    self.assertNotIn("Traceback", stderr.getvalue())
                    logs = log_stream.getvalue()
                    self.assertIn(f"INFO 中断 種別={exc_type.__name__}", logs)
                    self.assertNotIn("ERROR", logs)
                    self.assertNotIn("失敗", logs)
                    self.assertNotIn("異常終了", logs)

    def test_schedule_command_handles_eof_and_keyboard_interrupt(self):
        for exc_type, expected_code in ((EOFError, 1), (KeyboardInterrupt, 130)):
            with self.subTest(exc=exc_type.__name__):
                stderr = io.StringIO()
                with patch("main.load_schedule", side_effect=exc_type()), redirect_stderr(stderr):
                    code = main.main(["schedule", "status"])
                self.assertEqual(code, expected_code)
                self.assertIn("中断", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_rejects_invalid_legacy_student_id(self):
        for bad in ("BAD ID", "１２３", "../x", " OLD001", "OLD001 ", 123):
            with self.subTest(bad=bad):
                self.path.write_text(json.dumps(dict(CONFIG, student_id=bad)))
                with self.assertRaises(ConfigurationError):
                    load_config(self.path)

    def test_first_registration_interrupt_empty_or_keychain_error_leaves_config_untouched(self):
        empty_cfg = dict(CONFIG, student_id="")
        self.path.write_text(json.dumps(empty_cfg))
        before = self.path.read_bytes()

        # パスワード入力中に Ctrl+C / EOF で中断しても config.json を書き換えない。
        for interrupt in (KeyboardInterrupt(), EOFError()):
            with self.subTest(interrupt=type(interrupt).__name__):
                backend = Mock(get_password=Mock(return_value=None))
                with patch("src.credentials.keychain_backend", return_value=backend), \
                        patch("builtins.input", side_effect=["FIRST001", "最初のユーザー"]), \
                        patch("src.credentials.getpass.getpass", side_effect=interrupt):
                    with self.assertRaises(type(interrupt)):
                        get_credentials(dict(empty_cfg), self.path)
                self.assertEqual(self.path.read_bytes(), before)
                backend.set_password.assert_not_called()

        # 空パスワード、set_password 例外、保存確認不一致でも config.json を書き換えない。
        cases = [
            (Mock(get_password=Mock(return_value=None)), "", CredentialError),
            (Mock(get_password=Mock(return_value=None), set_password=Mock(side_effect=RuntimeError("boom"))), "secret", CredentialError),
            (Mock(get_password=Mock(side_effect=[None, "wrong-value"])), "secret", CredentialError),
        ]
        for idx, (backend, pw, expected_exc) in enumerate(cases):
            with self.subTest(case=idx):
                with patch("src.credentials.keychain_backend", return_value=backend), \
                        patch("builtins.input", side_effect=["FIRST001", "最初のユーザー"]), \
                        patch("src.credentials.getpass.getpass", return_value=pw):
                    with self.assertRaises(expected_exc):
                        get_credentials(dict(empty_cfg), self.path)
                self.assertEqual(self.path.read_bytes(), before)

    def test_first_registration_merges_concurrent_disk_addition(self):
        empty_cfg = dict(CONFIG, student_id="")
        self.path.write_text(json.dumps(empty_cfg))
        backend = Mock(get_password=Mock(side_effect=[None, "first-secret"]))

        def simulate_concurrent_add(_prompt):
            other_backend = Mock(get_password=Mock(return_value="other-secret"))
            with patch("src.user_commands.keychain_backend", return_value=other_backend):
                add_user(self.base, "並行ユーザー", "OTHER002", "other-secret")
            return "first-secret"

        with patch("src.credentials.keychain_backend", return_value=backend), \
                patch("builtins.input", side_effect=["FIRST001", "最初のユーザー"]), \
                patch("src.credentials.getpass.getpass", side_effect=simulate_concurrent_add):
            self.assertEqual(get_credentials(dict(empty_cfg), self.path), ("FIRST001", "first-secret"))

        users = get_users(load_config(self.path))
        self.assertEqual([u["student_id"] for u in users], ["OTHER002", "FIRST001"])

    def test_concurrent_config_updates_are_serialized_without_lost_updates(self):
        cfg = dict(CONFIG, student_id="", users=[{"username": "one", "student_id": "OLD001"}], default_user="one")
        self.path.write_text(json.dumps(cfg))

        # 別プロセスが config.json のロックを保持し変更する間、add_user が待機し両方の変更が残ることを確認する。
        script = "\n".join([
            "import sys",
            "from pathlib import Path",
            "from unittest.mock import Mock, patch",
            "from src.config import private_file_lock",
            "from src.user_commands import add_user",
            "base = Path(sys.argv[1])",
            "backend = Mock(get_password=Mock(return_value='p1'))",
            "with private_file_lock(base / 'config.json'):",
            "    print('LOCKED', flush=True)",
            "    sys.stdin.read()",
            "    with patch('src.user_commands.keychain_backend', return_value=backend):",
            "        add_user(base, 'two', 'NEW002', 'p1')",
        ])
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(self.base)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        try:
            line = proc.stdout.readline().strip()
            if line != "LOCKED":
                self.fail(f"unexpected lock output {line!r}: {proc.stderr.read()}")
            done = threading.Event()
            errors = []

            def run_add():
                try:
                    backend = Mock(get_password=Mock(return_value="p2"))
                    with patch("src.user_commands.keychain_backend", return_value=backend):
                        add_user(self.base, "three", "NEW003", "p2")
                        rename_user(self.base, "one", "uno")
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    done.set()

            thread = threading.Thread(target=run_add)
            thread.start()
            self.assertFalse(done.wait(timeout=0.05))
            proc.stdin.close()
            thread.join(timeout=5)
            self.assertEqual(proc.wait(timeout=5), 0, proc.stderr.read())
            self.assertEqual(errors, [])

            stored = load_config(self.path)
            self.assertEqual(
                [(u["username"], u["student_id"]) for u in get_users(stored)],
                [("uno", "OLD001"), ("two", "NEW002"), ("three", "NEW003")],
            )
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream and not stream.closed:
                    stream.close()
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
