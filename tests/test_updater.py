"""更新は隔離ディレクトリと模擬通信で検証し、大学やGitHubへ接続しない。"""

import hashlib
import io
import json
from contextlib import redirect_stdout, redirect_stderr
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
import zipfile

from src import updater as u
from src.version import VERSION

ROOT = Path(__file__).resolve().parents[1]
NEXT = "99.0.0"


def manifest_for(data, version=NEXT):
    return {"schema_version": 1, "version": version,
            "archive": {"name": u.ASSET_NAME, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "tool with spaces"
        self.base.mkdir()
        for name in u.managed_files(ROOT):
            target = self.base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        self.original = u.fingerprint(self.base)
        self.private = {
            "config.json": b'{"users": ["private"]}',
            "schedule.json": b'{"courses": ["private"]}',
            "logs/test.log": b"private log",
            "browser_data/cookies": b"private session",
            ".venv/marker": b"existing environment",
            ".git/config": b"existing repository",
        }
        for name, content in self.private.items():
            target = self.base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def archive(self, extras=None, omit=(), version=NEXT, requirements=None):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in u.managed_files(self.base):
                if name in omit:
                    continue
                data = (self.base / name).read_bytes()
                if name == "src/version.py":
                    data = f'VERSION = "{version}"\n'.encode()
                if name == "requirements.txt" and requirements is not None:
                    data = requirements
                archive.writestr(name, data)
            for name, content in (extras or {}).items():
                archive.writestr(name, content)
        return stream.getvalue()

    def prepare(self, data=None, smoke=True, **manifest_changes):
        data = self.archive() if data is None else data
        manifest = manifest_for(data)
        manifest.update(manifest_changes)
        with patch.object(u, "fetch", side_effect=[json.dumps(manifest).encode(), data]) as fetch:
            if smoke:
                result = u.prepare_update(self.base, force=True)
            else:
                with patch.object(u.subprocess, "run", return_value=Mock(returncode=1)):
                    result = u.prepare_update(self.base, force=True)
        return result, fetch

    def assert_private_preserved(self):
        for name, content in self.private.items():
            self.assertEqual((self.base / name).read_bytes(), content, name)

    def test_download_apply_and_rollback_preserve_personal_files(self):
        message, _ = self.prepare()
        self.assertIn(NEXT, message)
        self.assertEqual(u.fingerprint(self.base), self.original)
        self.assertIn(NEXT, u.apply_pending(self.base))
        self.assertIn(NEXT, (self.base / "src/version.py").read_text())
        self.assert_private_preserved()
        self.assertTrue((self.base / ".updates/backup/journal.json").exists())
        u.update_command(self.base, ["--rollback"])
        self.assertIn("戻しました", u.finish_update(self.base))
        self.assertEqual(u.fingerprint(self.base), self.original)
        self.assert_private_preserved()

    def test_updated_version_starts_and_rollback_runs_from_new_version(self):
        self.prepare()
        u.apply_pending(self.base)
        command = [sys.executable, str(self.base / "main.py")]
        result = subprocess.run(command + ["--version"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(NEXT, result.stdout)
        result = subprocess.run(command + ["update", "--rollback"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(u.fingerprint(self.base), self.original)
        self.assertEqual(u.read_json(self.base / ".updates/check.json")["skip_version"], NEXT)
        self.assert_private_preserved()

    def test_pending_is_reused_without_network(self):
        self.prepare()
        with patch.object(u, "fetch") as fetch:
            self.assertIn("準備", u.prepare_update(self.base, force=True))
            fetch.assert_not_called()

    def test_added_and_removed_modules_are_reversible(self):
        retired = self.base / "src/retired_helper.py"
        retired.write_text("# unused old module\n")
        before = u.fingerprint(self.base)
        self.prepare(self.archive({"src/new_helper.py": b"# new module\n"}, omit=("src/retired_helper.py",)))
        u.apply_pending(self.base)
        self.assertFalse(retired.exists())
        self.assertTrue((self.base / "src/new_helper.py").exists())
        u.update_command(self.base, ["--rollback"])
        u.finish_update(self.base)
        self.assertEqual(u.fingerprint(self.base), before)
        self.assert_private_preserved()

    def test_bad_checksum_is_rejected(self):
        data = self.archive()
        with self.assertRaisesRegex(u.UpdateError, "チェックサムが一致"):
            self.prepare(data, archive={"name": u.ASSET_NAME, "size": len(data), "sha256": "0" * 64})
        self.assertEqual(u.fingerprint(self.base), self.original)

    def test_untrusted_archive_name_is_rejected(self):
        with self.assertRaises(u.UpdateError):
            self.prepare(archive={"name": "https://example.test/file", "size": 1, "sha256": "0" * 64})

    def test_archive_rejects_path_traversal_and_private_files(self):
        for name in ("../escape.py", "/tmp/escape.py", "config.json", "schedule.json", ".git/config", "src/../../escape.py"):
            with self.subTest(name=name), self.assertRaises(u.UpdateError):
                self.prepare(self.archive({name: b"overwrite"}))
        self.assert_private_preserved()

    def test_archive_rejects_symlinks(self):
        stream = io.BytesIO(self.archive())
        with zipfile.ZipFile(stream, "a") as archive:
            entry = zipfile.ZipInfo("src/link.py")
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            archive.writestr(entry, "../../config.json")
        with self.assertRaises(u.UpdateError):
            self.prepare(stream.getvalue())

    def test_missing_file_and_version_mismatch_are_rejected(self):
        for data in (self.archive(omit=("main.py",)), self.archive(version="98.0.0")):
            with self.assertRaises(u.UpdateError):
                self.prepare(data)

    def test_changed_requirements_without_regenerated_lock_are_rejected(self):
        with self.assertRaisesRegex(u.UpdateError, "ロック情報"):
            self.prepare(self.archive(requirements=b"new-dependency\n"))
        self.assertEqual(u.fingerprint(self.base), self.original)

    def test_smoke_failure_does_not_change_installation(self):
        with self.assertRaisesRegex(u.UpdateError, "起動確認"):
            self.prepare(smoke=False)
        self.assertEqual(u.fingerprint(self.base), self.original)

    def test_modified_installation_after_download_is_preserved(self):
        self.prepare()
        source = self.base / "src/verification.py"
        source.write_text("# local edit\n")
        with self.assertRaises(u.UpdateError):
            u.apply_pending(self.base)
        self.assertEqual(source.read_text(), "# local edit\n")
        self.assert_private_preserved()

    def test_modified_payload_is_rejected(self):
        self.prepare()
        (self.base / ".updates/pending/main.py").write_text("# tampered\n")
        with self.assertRaises(u.UpdateError):
            u.apply_pending(self.base)
        self.assertEqual(u.fingerprint(self.base), self.original)

    def test_partial_apply_rolls_back(self):
        self.prepare()
        write = u.atomic_write
        failed = False
        def fail_once(path, data, mode=0o600):
            nonlocal failed
            if path == self.base / "src/version.py" and not failed:
                failed = True
                raise OSError("simulated disk error")
            return write(path, data, mode)
        with patch.object(u, "atomic_write", side_effect=fail_once):
            with self.assertRaises(OSError):
                u.apply_pending(self.base)
        self.assertTrue(failed)
        self.assertEqual(u.fingerprint(self.base), self.original)
        self.assert_private_preserved()

    def test_interrupted_update_recovers_before_importing_application(self):
        self.prepare()
        u.apply_pending(self.base)
        shutil.copytree(self.base / ".updates/backup", self.base / ".updates/transaction")
        # 新版がimportに失敗しても、入口は更新前のコードへ戻せる。
        (self.base / "src/config.py").write_text('raise RuntimeError("broken version")\n')
        result = subprocess.run([sys.executable, str(self.base / "main.py"), "--version"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(VERSION, result.stdout)
        self.assertEqual(u.fingerprint(self.base), self.original)

    def test_old_version_is_ignored_and_prerelease_is_rejected(self):
        message, fetch = self.prepare(version="0.0.1")
        self.assertTrue(message.startswith("更新なし:"))
        self.assertEqual(fetch.call_count, 1)
        with self.assertRaises(u.UpdateError):
            self.prepare(version="99.0.0-rc1")

    def test_no_release_and_offline_do_not_break_background(self):
        with patch.object(u, "fetch", side_effect=HTTPError(u.MANIFEST_URL, 404, "", {}, None)):
            with self.assertRaisesRegex(u.UpdateError, "更新情報JSONが見つかりません"):
                u.prepare_update(self.base, force=True)
        with patch.object(u, "check_update", side_effect=OSError("offline")):
            u.background_update(self.base)
        self.assertEqual(u.fingerprint(self.base), self.original)

    def test_daily_check_throttles_network(self):
        u.write_json(self.base / ".updates/check.json", {"checked_at": u.time.time(), "interval": 86400})
        with patch.object(u, "fetch") as fetch:
            self.assertIsNone(u.prepare_update(self.base))
            fetch.assert_not_called()

    def test_current_release_is_explicitly_reported_as_no_update(self):
        message, _ = self.prepare(version=VERSION)
        self.assertIn("更新なし: 最新版です", message)
        self.assertIn(VERSION, message)

    def test_failures_show_reason_and_return_nonzero(self):
        cases = [
            (URLError("private-details"), "接続できません"),
            (URLError(TimeoutError()), "タイムアウト"),
            (HTTPError(u.MANIFEST_URL, 403, "", {}, None), "アクセスが拒否されました"),
            (HTTPError(u.MANIFEST_URL, 429, "", {}, None), "利用制限中"),
            (HTTPError(u.MANIFEST_URL, 503, "", {}, None), "サーバーでエラー"),
            (zipfile.BadZipFile("private-details"), "更新データに問題"),
            (PermissionError("private-details"), "保存する権限"),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__, expected=expected), \
                    patch.object(u, "prepare_update", side_effect=error), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as output:
                self.assertEqual(u.run_application(self.base, ["update"]), 1)
            message = output.getvalue()
            self.assertIn("更新失敗:", message)
            self.assertIn(expected, message)
            self.assertNotIn("更新なし", message)
            self.assertNotIn("private-details", message)

    def test_missing_manifest_or_archive_is_failure(self):
        with patch.object(u, "fetch", side_effect=HTTPError(u.MANIFEST_URL, 404, "", {}, None)), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(u.run_application(self.base, ["update"]), 1)
        self.assertIn("更新情報JSONが見つかりません", errors.getvalue())
        with patch.object(u, "fetch", side_effect=[json.dumps(manifest_for(self.archive())).encode(), HTTPError(u.MANIFEST_URL, 404, "", {}, None)]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(u.run_application(self.base, ["update"]), 1)
        self.assertIn("更新失敗:", errors.getvalue())
        self.assertIn("更新ファイルが見つかりません", errors.getvalue())

    def test_background_notice_includes_same_failure_reason(self):
        with patch.object(u, "check_update", side_effect=URLError("offline")), redirect_stdout(io.StringIO()):
            u.background_update(self.base)
        message = u.read_json(self.base / ".updates/notice.json")["message"]
        self.assertIn("更新失敗:", message)
        self.assertIn("接続できません", message)

    def test_running_downloader_is_reported_as_waiting(self):
        with u.file_lock(self.base / ".updates/download.lock", exclusive=True), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(u.run_application(self.base, ["update"]), 1)
        self.assertIn("更新待機:", errors.getvalue())
        self.assertNotIn("更新なし", errors.getvalue())
        self.assertNotIn("読み書き", errors.getvalue())

    def test_rolled_back_release_is_not_automatically_reinstalled(self):
        u.write_json(self.base / ".updates/check.json", {"skip_version": NEXT})
        manifest = manifest_for(self.archive())
        with patch.object(u, "fetch", return_value=json.dumps(manifest).encode()) as fetch:
            self.assertIsNone(u.prepare_update(self.base))
            self.assertEqual(fetch.call_count, 1)
        self.assertFalse((self.base / ".updates/pending").exists())

    def test_release_archive_contains_no_private_settings(self):
        from scripts.build_release import build
        archive = build("v" + VERSION, Path(self.temp.name) / "release.zip")
        with zipfile.ZipFile(archive) as package:
            names = package.namelist()
        self.assertTrue(u.REQUIRED.issubset(names))
        self.assertTrue(all(u.allowed(name) for name in names))
        self.assertTrue(set(self.private).isdisjoint(names))
        # 旧版の更新処理は未知のファイルを拒否するため、ロックファイルは更新用ZIPに含めない。
        self.assertNotIn("requirements.lock", names)
        u.validate_payload(archive, Path(self.temp.name) / "verified", "v" + VERSION)

    def test_scheduled_demo_and_help_do_not_start_or_apply_updates(self):
        for argv in (["--scheduled"], ["--ui", "--demo"], ["--help"], ["--version"], ["user", "list"]):
            with self.subTest(argv=argv), patch.object(u.importlib, "import_module", return_value=Mock(main=Mock(return_value=0))), \
                    patch.object(u, "check_update") as check, patch.object(u, "finish_update") as apply:
                self.assertEqual(u.run_application(self.base, argv), 0)
                check.assert_not_called()
                apply.assert_not_called()

    def test_other_running_process_defers_update(self):
        self.prepare()
        with u.file_lock(self.base / ".updates/runtime.lock"):
            with patch.object(u.importlib, "import_module", return_value=Mock(main=Mock(return_value=0))), \
                    patch.object(u.subprocess, "Popen"), patch.object(u, "finish_update") as apply:
                self.assertEqual(u.run_application(self.base, ["update"]), 1)
                apply.assert_not_called()
        self.assertEqual(u.fingerprint(self.base), self.original)
        self.assertIn(NEXT, u.apply_pending(self.base))

    def test_background_never_applies_prepared_update(self):
        self.prepare()
        with patch.object(u, "check_update", return_value=None):
            u.background_update(self.base)
        self.assertEqual(u.fingerprint(self.base), self.original)
        self.assertTrue((self.base / ".updates/pending").exists())
        self.assert_private_preserved()

    def test_background_keeps_pending_while_application_is_running(self):
        self.prepare()
        with u.file_lock(self.base / ".updates/runtime.lock"):
            with patch.object(u, "check_update", return_value=None):
                u.background_update(self.base)
        self.assertEqual(u.fingerprint(self.base), self.original)
        self.assertTrue((self.base / ".updates/pending").exists())

    def test_background_http_rejection_preserves_files_and_retries_after_one_hour(self):
        for status in (403, 429):
            with self.subTest(status=status), \
                    patch.object(u, "fetch", side_effect=HTTPError(u.MANIFEST_URL, status, "", {}, None)) as fetch, \
                    redirect_stdout(io.StringIO()):
                u.write_json(self.base / ".updates/check.json", {})
                u.background_update(self.base)
                notice = u.read_json(self.base / ".updates/notice.json")
                self.assertIn(f"HTTP {status}", notice["message"])
                self.assertIn("更新失敗:", notice["message"])
                state = u.read_json(self.base / ".updates/check.json")
                self.assertEqual(state["interval"], 3600)
                self.assertEqual(u.fingerprint(self.base), self.original)
                self.assert_private_preserved()
                self.assertFalse((self.base / ".updates/pending").exists())
                self.assertIsNone(u.prepare_update(self.base))
                fetch.assert_called_once()
                # 手動確認は待機時間内でも再試行できる。
                with self.assertRaises(HTTPError):
                    u.prepare_update(self.base, force=True)
                self.assertEqual(fetch.call_count, 2)

    def test_notification_then_manual_update_and_rollback_for_git_and_zip_installs(self):
        fixture = Path(self.temp.name) / "transport"
        fixture.mkdir()
        # ネットワーク境界だけを模擬する。起動・通知・ダウンロード処理・検証・
        # プロセス間のロック・適用・復元には実際の実装を使う。
        (fixture / "sitecustomize.py").write_text('''
import io
import os
from pathlib import Path
import time
import urllib.request

fixture = Path(os.environ["ATTENDANCE_TEST_TRANSPORT"])
def fixture_open(self, request, *args, **kwargs):
    with (fixture / "requests.log").open("a") as log:
        log.write(request.full_url + "\\n")
    if request.full_url.endswith("/releases/latest/download/attendance-update.json"):
        return io.BytesIO((fixture / "release.json").read_bytes())
    if request.full_url.endswith("/attendance-update.zip"):
        time.sleep(0.3)
        return io.BytesIO((fixture / "release.zip").read_bytes())
    raise AssertionError("Unexpected network request")
urllib.request.OpenerDirector.open = fixture_open
''')
        environment = dict(os.environ, PYTHONPATH=str(fixture), ATTENDANCE_TEST_TRANSPORT=str(fixture))
        environment.pop("ATTENDANCE_NO_UPDATE", None)
        # 出席処理だけを省略し、実際のCLIと同じ更新ライフサイクルを動かす。
        command = [sys.executable, "-c", (
            "from pathlib import Path; import sys; import main; "
            "from src.updater import run_application; "
            "main.main = lambda argv: 0; "
            "raise SystemExit(run_application(Path.cwd(), sys.argv[1:]))"
        )]
        for installation in ("git", "zip"):
            with self.subTest(installation=installation):
                if installation == "zip":
                    shutil.rmtree(self.base / ".git")
                    self.private.pop(".git/config")
                data = self.archive()
                # 即終了するCLIは通信を待たない。保存済みの通知と手動更新の連携を検証する。
                u.write_json(self.base / ".updates/check.json", {"manifest": manifest_for(data),
                                                               "checked_at": time.time(), "interval": 86400})
                (fixture / "release.zip").write_bytes(data)
                (fixture / "release.json").write_text(json.dumps(manifest_for(data)))
                backup = self.base / ".updates/backup/journal.json"
                before_backup = backup.read_bytes() if backup.exists() else None
                result = subprocess.run(command + ["--check", "731"], cwd=self.base, env=environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("アップデートがあります", result.stderr)
                self.assertIn("att update --notes", result.stderr)
                self.assertEqual(u.fingerprint(self.base), self.original)
                self.assertFalse((self.base / ".updates/pending").exists())
                self.assertEqual(backup.read_bytes() if backup.exists() else None, before_backup)
                self.assertFalse((fixture / "requests.log").exists())
                result = subprocess.run(command + ["update"], cwd=self.base, env=environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("/attendance-update.zip", (fixture / "requests.log").read_text())
                self.assertTrue((self.base / ".updates/backup/journal.json").exists())
                result = subprocess.run([sys.executable, str(self.base / "main.py"), "--version"],
                                        cwd=self.base, env=environment, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(NEXT, result.stdout)
                self.assert_private_preserved()
                result = subprocess.run([sys.executable, str(self.base / "main.py"), "update", "--rollback"],
                                        cwd=self.base, env=environment, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(u.fingerprint(self.base), self.original)
                self.assert_private_preserved()
                (fixture / "requests.log").unlink()

    def test_version_order_is_numeric(self):
        self.assertGreater(u.version_tuple("v0.10.0"), u.version_tuple("0.9.9"))
        for version in ("v1", "1.2.3-beta", "../1.2.3", "01.2.3", None):
            with self.assertRaises(u.UpdateError):
                u.version_tuple(version)


if __name__ == "__main__":
    unittest.main()
