"""更新の通信・配布・保存・復元・起動条件を隔離環境で検証する。"""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import errno
import hashlib
import http.client
import io
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
import warnings
import zipfile

import test_updater as fixtures
from src import updater as u


class UpdaterMatrixTests(unittest.TestCase):
    @contextmanager
    def installation(self):
        fixture = fixtures.UpdaterTests()
        fixture.setUp()
        try:
            yield fixture
        finally:
            fixture.doCleanups()

    def release(self, data):
        return fixtures.manifest_for(data)

    def assert_unchanged(self, fixture):
        self.assertEqual(u.fingerprint(fixture.base), fixture.original)
        fixture.assert_private_preserved()

    def test_network_http_statuses_at_manifest_and_archive_download(self):
        for stage in ("manifest", "archive"):
            for status in (400, 401, 403, 404, 407, 408, 410, 422, 429, 500, 502, 503, 504):
                with self.subTest(stage=stage, status=status), self.installation() as fixture:
                    error = HTTPError(u.MANIFEST_URL, status, "private response", {}, None)
                    replies = [error] if stage == "manifest" else [json.dumps(self.release(fixture.archive())).encode(), error]
                    with patch.object(u, "fetch", side_effect=replies), \
                            redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                        code = u.run_application(fixture.base, ["update"])
                    self.assertEqual(code, 1)
                    self.assertIn("更新失敗:", output.getvalue() + errors.getvalue())
                    self.assert_unchanged(fixture)
                    self.assertFalse((fixture.base / ".updates/pending").exists())

    def test_network_dns_tls_timeout_reset_refusal_and_truncated_response(self):
        failures = {
            "dns": URLError(socket.gaierror(-2, "private DNS")),
            "certificate": URLError(ssl.SSLCertVerificationError(1, "private certificate")),
            "tls": URLError(ssl.SSLError("private TLS")),
            "timeout": TimeoutError("private timeout"),
            "wrapped_timeout": URLError(TimeoutError()),
            "refused": URLError(ConnectionRefusedError()),
            "reset": URLError(ConnectionResetError()),
            "truncated": http.client.IncompleteRead(b"private", 20),
        }
        for name, error in failures.items():
            with self.subTest(failure=name), self.installation() as fixture, \
                    patch.object(u, "fetch", side_effect=error), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as output:
                self.assertEqual(u.run_application(fixture.base, ["update"]), 1)
                self.assertIn("更新失敗:", output.getvalue())
                self.assertNotIn("private", output.getvalue())
                expected = {"dns": "DNS", "certificate": "証明書", "tls": "HTTPS", "timeout": "タイムアウト",
                            "wrapped_timeout": "タイムアウト", "truncated": "接続が切れ"}.get(name, "接続できません")
                self.assertIn(expected, output.getvalue())
                self.assert_unchanged(fixture)

    def test_network_fetch_accepts_exact_limit_and_rejects_oversize(self):
        for size, valid in ((0, True), (5, True), (6, False), (131073, False)):
            with self.subTest(size=size):
                response = io.BytesIO(b"a" * size)
                with patch.object(u, "urlopen", return_value=response) as request:
                    if valid:
                        self.assertEqual(u.fetch(u.MANIFEST_URL, 5, time.monotonic() + 5), b"a" * size)
                    else:
                        with self.assertRaisesRegex(u.UpdateError, "上限"):
                            u.fetch(u.MANIFEST_URL, 5, time.monotonic() + 5)
                self.assertTrue(response.closed)
                self.assertEqual(request.call_args.kwargs["timeout"], 5)
                self.assertEqual(request.call_args.args[0].get_header("User-agent"), "AttendanceTool-Updater")

    def test_network_fetch_deadline_and_midstream_failure_close_response(self):
        with patch.object(u, "urlopen", return_value=io.BytesIO(b"body")) as request:
            with self.assertRaisesRegex(u.UpdateError, "タイムアウト"):
                u.fetch(u.MANIFEST_URL, 100, time.monotonic() - 1)
            self.assertTrue(request.return_value.closed)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = [b"part", ConnectionResetError()]
        with patch.object(u, "urlopen", return_value=response), self.assertRaises(ConnectionResetError):
            u.fetch(u.MANIFEST_URL, 100, time.monotonic() + 5)
        response.__exit__.assert_called_once()

    def test_metadata_invalid_json_and_release_shapes(self):
        cases = (b"not json", b"\xff", b"null", b"[]", b"1", b'"string"', b"{}", b'{"version": 123}')
        for body in cases:
            with self.subTest(body=repr(body)), self.installation() as fixture, \
                    patch.object(u, "fetch", return_value=body), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(u.run_application(fixture.base, ["update"]), 1)
                self.assertIn("更新失敗:", errors.getvalue())
                self.assert_unchanged(fixture)

    def test_metadata_archive_and_checksums_are_validated(self):
        for value in (None, {}, "bad", [None], [], {"name": "other.zip"}):
            with self.subTest(archive=value), self.installation() as fixture:
                with self.assertRaises(u.UpdateError):
                    fixture.prepare(archive=value)
                self.assert_unchanged(fixture)
        for digest in (None, "", "md5:" + "a" * 32, "A" * 64, "a" * 63, "0" * 64):
            with self.subTest(digest=digest), self.installation() as fixture:
                release = self.release(fixture.archive())
                release["archive"]["sha256"] = digest
                with self.assertRaises(u.UpdateError):
                    fixture.prepare(**release)
                self.assert_unchanged(fixture)

    def test_metadata_release_gating_and_numeric_versions(self):
        for changes in ({"version": "0.0.0"}, {"version": fixtures.VERSION}):
            with self.subTest(changes=changes), self.installation() as fixture:
                message, fetch = fixture.prepare(**changes)
                self.assertIn("更新なし:", message)
                self.assertEqual(fetch.call_count, 1)
                self.assert_unchanged(fixture)
        for version in ("", "v1", "1.2", "1.2.3.4", "1.2.3-beta", "01.2.3", "1.02.3", "../1.2.3", "v1.2.3"):
            with self.subTest(version=version), self.installation() as fixture:
                with self.assertRaises(u.UpdateError):
                    fixture.prepare(version=version)
                self.assert_unchanged(fixture)

    def test_archive_malformed_zip_and_python_syntax(self):
        for data in (b"not a zip", b"PK\x03\x04truncated"):
            with self.subTest(data=data), self.installation() as fixture:
                with self.assertRaises(zipfile.BadZipFile):
                    fixture.prepare(data)
                self.assert_unchanged(fixture)
        with self.installation() as fixture:
            with self.assertRaises(SyntaxError):
                fixture.prepare(fixture.archive({"src/broken.py": b"def broken(:\n"}))
            self.assert_unchanged(fixture)

    def test_archive_duplicate_directory_many_files_and_expanded_size(self):
        for case in ("duplicate", "directory", "count", "expanded"):
            with self.subTest(case=case), self.installation() as fixture:
                stream = io.BytesIO(fixture.archive())
                with zipfile.ZipFile(stream, "a", zipfile.ZIP_DEFLATED) as archive, warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    if case == "duplicate":
                        archive.writestr("main.py", b"# duplicate")
                    elif case == "directory":
                        archive.writestr("src/folder/", b"")
                    elif case == "count":
                        for index in range(257):
                            archive.writestr(f"src/extra{index}.py", b"# extra")
                    else:
                        archive.writestr("src/huge.py", b"#" * u.MAX_ARCHIVE)
                with self.assertRaises(u.UpdateError):
                    fixture.prepare(stream.getvalue())
                self.assert_unchanged(fixture)

    def test_archive_every_application_module_is_required(self):
        for module in ("src/terminal_ui.py", "src/schedule_ui.py", "src/attendance.py", "src/browser.py", "src/credentials.py"):
            with self.subTest(module=module), self.installation() as fixture:
                with self.assertRaisesRegex(u.UpdateError, "構成"):
                    fixture.prepare(fixture.archive(omit=(module,)))
                self.assert_unchanged(fixture)

    def test_archive_absolute_private_and_non_python_paths_are_rejected(self):
        for path in ("src/./unsafe.py", "src//unsafe.py", "src/../main.py", "src/.aws/config", "src/data.json", "logs/test.log", ".venv/secret", "README.md/child.py"):
            with self.subTest(path=path), self.installation() as fixture:
                with self.assertRaises(u.UpdateError):
                    fixture.prepare(fixture.archive({path: b"# unsafe"}))
                self.assert_unchanged(fixture)

    def test_archive_executable_modes_are_restored_and_cache_is_cleared(self):
        with self.installation() as fixture:
            fixture.prepare()
            cache = fixture.base / "src/__pycache__"
            cache.mkdir(exist_ok=True)
            (cache / "stale.pyc").write_bytes(b"stale")
            u.apply_pending(fixture.base)
            for name in ("att", "attendance", "start.command", "install.command", "install.sh"):
                self.assertEqual((fixture.base / name).stat().st_mode & 0o777, 0o755)
            self.assertFalse(cache.exists())
            fixture.assert_private_preserved()

    def test_smoke_timeout_or_unavailable_python_never_changes_installation(self):
        for error in (subprocess.TimeoutExpired(["python"], 20), OSError(errno.ENOENT, "private interpreter")):
            with self.subTest(error=type(error).__name__), self.installation() as fixture, \
                    patch.object(u.subprocess, "run", side_effect=error):
                with self.assertRaises(type(error)):
                    fixture.prepare()
                self.assert_unchanged(fixture)
                self.assertFalse((fixture.base / ".updates/pending").exists())

    def test_storage_disk_full_and_permission_denied_during_prepare(self):
        for phase in ("check", "payload", "metadata"):
            for code in (errno.ENOSPC, errno.EACCES):
                with self.subTest(phase=phase, errno=code), self.installation() as fixture:
                    write = u.atomic_write

                    def fail(path, data, mode=0o600):
                        if ((phase == "check" and path.name == "check.json")
                                or (phase == "payload" and path.name == "main.py")
                                or (phase == "metadata" and path.name == "metadata.json")):
                            raise OSError(code, "private storage detail")
                        return write(path, data, mode)

                    with patch.object(u, "atomic_write", side_effect=fail), self.assertRaises(OSError):
                        fixture.prepare()
                    self.assert_unchanged(fixture)
                    self.assertFalse((fixture.base / ".updates/pending").exists())

    def test_storage_atomic_write_failure_keeps_original_and_removes_temp_file(self):
        with self.installation() as fixture:
            target = fixture.base / "README.md"
            before = target.read_bytes()
            with patch.object(u.os, "replace", side_effect=OSError(errno.ENOSPC, "disk full")), self.assertRaises(OSError):
                u.atomic_write(target, b"new")
            self.assertEqual(target.read_bytes(), before)
            self.assertFalse(list(fixture.base.glob(".update-*")))

    def test_storage_apply_failure_at_first_middle_and_last_file_rolls_back(self):
        for name in ("COMMANDS.md", "src/config.py", "src/version.py"):
            with self.subTest(file=name), self.installation() as fixture:
                fixture.prepare()
                write = u.atomic_write
                failed = False

                def fail_once(path, data, mode=0o600):
                    nonlocal failed
                    if path == fixture.base / name and not failed:
                        failed = True
                        raise PermissionError("private path")
                    return write(path, data, mode)

                with patch.object(u, "atomic_write", side_effect=fail_once), self.assertRaises(PermissionError):
                    u.apply_pending(fixture.base)
                self.assertTrue(failed)
                self.assert_unchanged(fixture)
                self.assertFalse((fixture.base / ".updates/transaction").exists())

    def test_storage_symlinked_source_is_never_overwritten(self):
        with self.installation() as fixture:
            target = fixture.base / "src/config.py"
            outside = Path(fixture.temp.name) / "outside.py"
            outside.write_bytes(target.read_bytes())
            target.unlink()
            target.symlink_to(outside)
            with self.assertRaisesRegex(u.UpdateError, "シンボリックリンク"):
                fixture.prepare()
            self.assertTrue(target.is_symlink())
            self.assertEqual(outside.read_bytes(), (fixtures.ROOT / "src/config.py").read_bytes())

    def test_cache_malformed_check_state_does_not_disable_updates(self):
        states = (b"{broken", b"[]", b"null", b'"text"', b'{"checked_at":"bad"}', b'{"interval":null}',
                  b'{"checked_at":1e99}', b'{"checked_at":NaN}', b'{"interval":Infinity}',
                  b'{"checked_at":' + b"9" * 500 + b"}")
        for state in states:
            with self.subTest(state=state), self.installation() as fixture:
                path = fixture.base / ".updates/check.json"
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(state)
                with patch.object(u, "fetch", return_value=json.dumps(fixtures.manifest_for(fixture.archive(), fixtures.VERSION)).encode()) as fetch:
                    message = u.prepare_update(fixture.base)
                self.assertIn("更新なし:", message)
                fetch.assert_called_once()
                self.assert_unchanged(fixture)

    def test_cache_corrupt_or_orphan_pending_can_be_downloaded_again(self):
        for state in (None, b"{broken", b"{}", b"[]", b'{"version":"bad"}'):
            with self.subTest(state=state), self.installation() as fixture:
                pending = fixture.base / ".updates/pending"
                pending.mkdir(parents=True)
                (pending / "partial.py").write_text("# interrupted extraction")
                if state is not None:
                    (pending / "metadata.json").write_bytes(state)
                message, fetch = fixture.prepare()
                self.assertEqual(fetch.call_count, 2)
                self.assertIn("ダウンロード", message)
                self.assertIn(fixtures.NEXT, u.apply_pending(fixture.base))
                fixture.assert_private_preserved()

    def test_cache_corrupt_pending_bypasses_daily_interval_for_automatic_retry(self):
        for corruption in ("invalid_json", "missing_metadata"):
            with self.subTest(corruption=corruption), self.installation() as fixture:
                fixture.prepare()
                metadata = fixture.base / ".updates/pending/metadata.json"
                if corruption == "invalid_json":
                    metadata.write_text("{broken")
                else:
                    metadata.unlink()
                data = fixture.archive()
                with patch.object(u, "fetch", side_effect=[json.dumps(self.release(data)).encode(), data]) as fetch:
                    message = u.prepare_update(fixture.base)
                self.assertEqual(fetch.call_count, 2)
                self.assertIn("ダウンロード", message)
                self.assert_unchanged(fixture)

    def test_cache_corrupt_notice_does_not_stop_normal_application(self):
        for state in (b"[]", b"null", b'"text"', b'{"message": 123}', b"{broken"):
            with self.subTest(state=state), self.installation() as fixture:
                notice = fixture.base / ".updates/notice.json"
                notice.parent.mkdir(parents=True)
                notice.write_bytes(state)
                app = Mock(main=Mock(return_value=0))
                with patch.object(u.importlib, "import_module", return_value=app), patch.object(u.subprocess, "Popen"), redirect_stderr(io.StringIO()):
                    self.assertEqual(u.run_application(fixture.base, ["--ui"]), 0)
                app.main.assert_called_once()

    def test_cache_notice_permission_failure_does_not_raise_second_exception(self):
        with self.installation() as fixture, patch.object(u, "prepare_update", side_effect=URLError("offline")), \
                patch.object(u, "write_json", side_effect=PermissionError()), redirect_stdout(io.StringIO()) as output:
            u.background_update(fixture.base)
            self.assertIn("更新失敗:", output.getvalue())
            self.assert_unchanged(fixture)

    def test_runtime_all_opt_out_modes_do_not_download_or_apply(self):
        cases = (["--scheduled"], ["--ui", "--demo"], ["--help"], ["-h"], ["--version"],
                 ["user", "list"], ["schedule", "status"], ["update", "--help"], ["update", "-h"])
        for args in cases:
            with self.subTest(args=args), self.installation() as fixture:
                with patch.object(u.importlib, "import_module", return_value=Mock(main=Mock(return_value=0))), \
                        patch.object(u.subprocess, "Popen") as launch, patch.object(u, "finish_update") as finish, \
                        redirect_stdout(io.StringIO()):
                    if args[0] == "update":
                        with self.assertRaises(SystemExit) as result:
                            u.run_application(fixture.base, args)
                        self.assertEqual(result.exception.code, 0)
                    else:
                        self.assertEqual(u.run_application(fixture.base, args), 0)
                    launch.assert_not_called()
                    finish.assert_not_called()

    def test_runtime_environment_opt_out_and_background_launch_failure_preserve_app(self):
        for mode in ("disabled", "launch_failed"):
            with self.subTest(mode=mode), self.installation() as fixture:
                app = Mock(main=Mock(return_value=7))
                with patch.object(u.importlib, "import_module", return_value=app), \
                        patch.object(u.subprocess, "Popen", side_effect=OSError("cannot launch")) as launch, \
                        patch.dict(u.os.environ, {"ATTENDANCE_NO_UPDATE": "1"} if mode == "disabled" else {}, clear=True):
                    self.assertEqual(u.run_application(fixture.base, ["--check", "731"]), 7)
                self.assertEqual(launch.call_count, 0 if mode == "disabled" else 1)
                app.main.assert_called_once()

    def test_runtime_manual_update_deferral_returns_waiting_status(self):
        with self.installation() as fixture:
            fixture.prepare()
            with u.file_lock(fixture.base / ".updates/runtime.lock"), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(u.run_application(fixture.base, ["update"]), 1)
            self.assertIn("更新待機:", errors.getvalue())
            self.assert_unchanged(fixture)

    def test_runtime_apply_error_preserves_specific_reason(self):
        with self.installation() as fixture:
            fixture.prepare()
            (fixture.base / "README.md").write_text("local modification")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(u.run_application(fixture.base, ["update"]), 1)
            self.assertIn("準備後にファイルが変更", errors.getvalue())
            self.assertEqual((fixture.base / "README.md").read_text(), "local modification")
            fixture.assert_private_preserved()

    def test_recovery_invalid_journal_stops_before_importing_application(self):
        for journal in (b"{broken", b"[]", b"{}", b'{"../outside.py":420}', b'{"main.py":"bad"}'):
            with self.subTest(journal=journal), self.installation() as fixture:
                transaction = fixture.base / ".updates/transaction"
                transaction.mkdir(parents=True)
                (transaction / "journal.json").write_bytes(journal)
                with patch.object(u.importlib, "import_module") as app, redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(u.run_application(fixture.base, ["--version"]), 1)
                app.assert_not_called()
                self.assertIn("復元", errors.getvalue())
                self.assert_unchanged(fixture)

    def test_recovery_missing_backup_fails_before_any_file_is_restored(self):
        with self.installation() as fixture:
            transaction = fixture.base / ".updates/transaction"
            (transaction / "files").mkdir(parents=True)
            (transaction / "files/README.md").write_text("wrong backup")
            u.write_json(transaction / "journal.json", {"README.md": 0o644, "main.py": 0o644})
            with self.assertRaises((u.UpdateError, OSError)):
                u.recover(fixture.base)
            self.assert_unchanged(fixture)
            self.assertTrue(transaction.exists())

    def test_recovery_rollback_without_backup_has_clear_failure(self):
        with self.installation() as fixture, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(u.run_application(fixture.base, ["update", "--rollback"]), 1)
            self.assertIn("復元できる更新前の版がありません", errors.getvalue())
            self.assert_unchanged(fixture)

    def test_runtime_real_process_locks_defer_manual_update(self):
        for lock_name in ("runtime.lock", "download.lock"):
            with self.subTest(lock=lock_name), self.installation() as fixture:
                if lock_name == "runtime.lock":
                    fixture.prepare()
                holder = subprocess.Popen([
                    sys.executable, "-c",
                    "from pathlib import Path; import select; import sys; from src.updater import file_lock; "
                    "base = Path.cwd(); "
                    "lock = file_lock(base / '.updates' / sys.argv[1], exclusive=(sys.argv[1] == 'download.lock')); "
                    "lock.__enter__(); (base / 'lock-ready').write_text('ready'); "
                    "select.select([sys.stdin], [], [], 5); lock.__exit__(None, None, None)", lock_name,
                ], cwd=fixture.base, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 5
                    while not (fixture.base / "lock-ready").exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue((fixture.base / "lock-ready").exists())
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                        self.assertEqual(u.run_application(fixture.base, ["update"]), 1)
                    self.assertIn("更新待機:", errors.getvalue())
                    self.assert_unchanged(fixture)
                finally:
                    holder.communicate(input=b"x", timeout=5)
                self.assertEqual(holder.returncode, 0)
                if lock_name == "runtime.lock":
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        self.assertEqual(u.run_application(fixture.base, ["update"]), 0)
                    self.assertIn(fixtures.NEXT, (fixture.base / "src/version.py").read_text())
                    fixture.assert_private_preserved()

    def test_storage_unwritable_runtime_lock_reports_permission_and_stops_safely(self):
        with self.installation() as fixture, patch.object(u, "file_lock", side_effect=PermissionError("private path")), \
                patch.object(u.importlib, "import_module") as app, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(u.run_application(fixture.base, ["update"]), 1)
            self.assertIn("権限", errors.getvalue())
            self.assertNotIn("private", errors.getvalue())
            app.assert_not_called()
            self.assert_unchanged(fixture)

    def test_recovery_persistent_disk_failure_blocks_start_until_recovery_succeeds(self):
        with self.installation() as fixture:
            fixture.prepare()
            write = u.atomic_write

            def fail_source(path, data, mode=0o600):
                if path == fixture.base / "src/config.py":
                    raise OSError(errno.ENOSPC, "disk full")
                return write(path, data, mode)

            with patch.object(u, "atomic_write", side_effect=fail_source), \
                    patch.object(u.importlib, "import_module") as app, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(u.run_application(fixture.base, ["update"]), 1)
                self.assertTrue((fixture.base / ".updates/transaction/journal.json").exists())
                self.assertEqual(u.run_application(fixture.base, ["--version"]), 1)
                app.assert_not_called()
            app = Mock(main=Mock(return_value=0))
            with patch.object(u.importlib, "import_module", return_value=app):
                self.assertEqual(u.run_application(fixture.base, ["--version"]), 0)
            app.main.assert_called_once()
            self.assert_unchanged(fixture)

    def test_recovery_interruption_after_replacement_restores_on_next_start(self):
        with self.installation() as fixture:
            fixture.prepare()
            replace = u.os.replace

            def fail_finalize(source, destination):
                if destination == fixture.base / ".updates/backup":
                    raise PermissionError("cannot finalize")
                return replace(source, destination)

            with patch.object(u.os, "replace", side_effect=fail_finalize), self.assertRaises(PermissionError):
                u.apply_pending(fixture.base)
            self.assertTrue((fixture.base / ".updates/transaction/journal.json").exists())
            app = Mock(main=Mock(return_value=0))
            with patch.object(u.importlib, "import_module", return_value=app):
                self.assertEqual(u.run_application(fixture.base, ["--version"]), 0)
            self.assert_unchanged(fixture)

    def test_cache_pending_metadata_invalid_maps_and_old_version_are_handled(self):
        for kind in ("before", "files", "digest", "missing", "old", "symlink", "broken_symlink"):
            with self.subTest(kind=kind), self.installation() as fixture:
                fixture.prepare()
                pending = fixture.base / ".updates/pending"
                metadata = u.read_json(pending / "metadata.json")
                if kind in ("before", "files"):
                    metadata[kind] = []
                elif kind == "digest":
                    metadata["files"]["main.py"] = "bad"
                elif kind == "missing":
                    del metadata["files"]["main.py"]
                elif kind == "old":
                    metadata["version"] = fixtures.VERSION
                else:
                    outside = Path(fixture.temp.name) / "external-pending"
                    pending.rename(outside)
                    pending.symlink_to(outside / "missing" if kind == "broken_symlink" else outside, target_is_directory=True)
                    with self.assertRaisesRegex(u.UpdateError, "通常のフォルダ"):
                        u.pending_metadata(fixture.base)
                    self.assertTrue((outside / "main.py").exists())
                    continue
                u.write_json(pending / "metadata.json", metadata)
                self.assertIsNone(u.apply_pending(fixture.base))
                self.assertFalse(pending.exists())
                self.assert_unchanged(fixture)

    def test_runtime_saved_notice_is_shown_even_if_it_cannot_be_deleted(self):
        for writable in (True, False):
            with self.subTest(writable=writable), self.installation() as fixture:
                notice = fixture.base / ".updates/notice.json"
                u.write_json(notice, {"message": "更新失敗: 前回のDNSエラー"})
                unlink = Path.unlink

                def fail_notice(path, *args, **kwargs):
                    if path == notice and not writable:
                        raise PermissionError()
                    return unlink(path, *args, **kwargs)

                app = Mock(main=Mock(return_value=0))
                with patch.object(u.importlib, "import_module", return_value=app), \
                        patch.object(u.subprocess, "Popen"), patch.object(Path, "unlink", fail_notice), \
                        redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(u.run_application(fixture.base, ["--ui"]), 0)
                self.assertIn("前回のDNSエラー", errors.getvalue())
                self.assertEqual(notice.exists(), not writable)
                app.main.assert_called_once()

    def test_runtime_smoke_timeout_is_reported_with_its_specific_reason(self):
        with self.installation() as fixture:
            data = fixture.archive()
            with patch.object(u, "fetch", side_effect=[json.dumps(self.release(data)).encode(), data]), \
                    patch.object(u.subprocess, "run", side_effect=subprocess.TimeoutExpired(["python"], 20)), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(u.run_application(fixture.base, ["update"]), 1)
            self.assertIn("起動確認がタイムアウト", errors.getvalue())
            self.assert_unchanged(fixture)

    def test_runtime_background_does_not_compete_with_downloader_or_recovery(self):
        for state in ("downloading", "recovering"):
            with self.subTest(state=state), self.installation() as fixture:
                if state == "recovering":
                    u.write_json(fixture.base / ".updates/transaction/journal.json", {"main.py": 0o644})
                    with patch.object(u, "prepare_update") as prepare:
                        u.background_update(fixture.base)
                        prepare.assert_not_called()
                else:
                    with u.file_lock(fixture.base / ".updates/download.lock", exclusive=True), \
                            patch.object(u, "fetch") as fetch:
                        u.background_update(fixture.base)
                        fetch.assert_not_called()
                    self.assertFalse((fixture.base / ".updates/notice.json").exists())
                self.assert_unchanged(fixture)

    def test_recovery_abandoned_backup_before_journal_can_be_replaced(self):
        with self.installation() as fixture:
            self.assertIsNone(u.recover(fixture.base))
            fixture.prepare()
            partial = fixture.base / ".updates/transaction/files/README.md"
            partial.parent.mkdir(parents=True)
            partial.write_text("incomplete backup")
            self.assertIn(fixtures.NEXT, u.apply_pending(fixture.base))
            fixture.assert_private_preserved()

    def test_build_wrong_tag_and_missing_required_module_are_rejected(self):
        from scripts import build_release
        with self.installation() as fixture:
            with self.assertRaisesRegex(ValueError, "タグ"):
                build_release.build("v99.99.99", Path(fixture.temp.name) / "bad.zip")
            (fixture.base / "src/terminal_ui.py").unlink()
            with patch.object(build_release, "ROOT", fixture.base), self.assertRaisesRegex(ValueError, "構成"):
                build_release.build("v" + fixtures.VERSION, Path(fixture.temp.name) / "bad.zip")


if __name__ == "__main__":
    unittest.main()
