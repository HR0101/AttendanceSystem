"""依存パッケージの準備・切替・復元。通常のvenvと個人設定は保持する。"""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from dependency_support import lock_source, seed_environment, spec, test_packages
import test_updater as fixtures
from src import dependencies as d
from src import updater as u


class DependencyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.UpdaterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base = self.fixture.base

    def test_packaged_lock_matches_hash_locked_source_file(self):
        current = d.read_spec(fixtures.ROOT)
        self.assertEqual(current["text"], (fixtures.ROOT / "requirements.lock").read_text())
        self.assertEqual(current["lock_hash"], hashlib.sha256(current["text"].encode()).hexdigest())

    def test_stale_or_executable_lock_data_is_rejected_without_execution(self):
        path = self.base / "src/dependency_lock.py"
        marker = self.base / "executed"
        for source in (b"LOCK_TEXT = ''", b"LOCK_TEXT = __import__('os').system('false')", b"broken(:"):
            with self.subTest(source=source):
                path.write_bytes(source)
                with self.assertRaisesRegex(u.UpdateError, "ロック情報"):
                    d.read_spec(self.base)
                self.assertFalse(marker.exists())

    def fake_install(self, fail_at=None):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if command[1:3] == ["-m", "venv"]:
                directory = Path(command[-1])
                (directory / "bin").mkdir(parents=True)
                (directory / "bin/python").write_text("# interpreter")
                (directory / "pyvenv.cfg").write_text("# environment")
            return 1 if len(calls) == fail_at else 0

        return calls, run

    def test_preparation_hash_checks_downloads_chromium_and_uses_stable_final_path(self):
        current = spec("package==1 --hash=sha256:" + "0" * 64 + "\n")
        calls, run = self.fake_install()
        with patch.object(d, "run_command", side_effect=run):
            state = d.prepare_environment(self.base, current)
        directory = u.runtime_directory(self.base, state)
        self.assertEqual(len(calls), 5)
        self.assertEqual(Path(calls[0][0][-1]), directory)
        self.assertIn("--require-hashes", calls[1][0])
        self.assertIn("--only-binary=:all:", calls[1][0])
        self.assertEqual(calls[2][0][-2:], ["pip", "check"])
        self.assertEqual(calls[3][0][-3:], ["playwright", "install", "chromium"])
        self.assertEqual(calls[3][1]["env"]["PLAYWRIGHT_BROWSERS_PATH"], str(directory / "browsers"))
        self.assertFalse((self.base / ".updates/runtime.json").exists())
        self.fixture.assert_private_preserved()
        with patch.object(d, "run_command") as command:
            self.assertEqual(d.prepare_environment(self.base, current), state)
            command.assert_not_called()

    def test_failure_at_any_setup_stage_keeps_working_environment_and_removes_candidate(self):
        old = seed_environment(self.base, "old lock")
        u.set_runtime_state(self.base, old)
        for stage in range(1, 6):
            with self.subTest(stage=stage):
                calls, run = self.fake_install(fail_at=stage)
                with patch.object(d, "run_command", side_effect=run), self.assertRaises(u.UpdateError):
                    d.prepare_environment(self.base, spec("different lock"))
                self.assertEqual(u.read_runtime_state(self.base), old)
                self.assertEqual([path.name for path in (self.base / ".updates/environments").iterdir()], [old["id"]])
                self.assertEqual(len(calls), stage)
                self.fixture.assert_private_preserved()

    def test_setup_timeout_does_not_expose_exception_contents(self):
        error = subprocess.TimeoutExpired(["private-token"], 300)
        with patch.object(d, "run_command", side_effect=error), self.assertRaises(u.UpdateError) as caught:
            d.prepare_environment(self.base, spec("different lock"))
        self.assertNotIn("private-token", str(caught.exception))
        self.fixture.assert_private_preserved()

    def test_timeout_and_interruption_stop_child_processes_before_cleanup(self):
        for error in (subprocess.TimeoutExpired(["install"], 1), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                process = Mock(pid=12345)
                process.wait.side_effect = [error, -signal.SIGKILL]
                with patch.object(d.subprocess, "Popen", return_value=process) as launch, patch.object(d.os, "killpg") as kill, \
                        self.assertRaises(type(error)):
                    d.run_command(["install"], env={}, stdout=None, timeout=1)
                self.assertTrue(launch.call_args.kwargs["start_new_session"])
                kill.assert_called_once_with(process.pid, signal.SIGKILL)
                self.assertEqual(process.wait.call_count, 2)

    def changed_archive(self, text):
        requirements = (self.base / "requirements.txt").read_bytes()
        return self.fixture.archive({"src/dependency_lock.py": lock_source(text, requirements)}, omit=("src/dependency_lock.py",))

    def test_changed_lock_is_prepared_before_code_update_and_runtime_rolls_back_together(self):
        old = seed_environment(self.base, "old runtime lock")
        new_text = d.read_spec(self.base)["text"] + "\n# revised lock\n"
        new = seed_environment(self.base, new_text, "b" * 32)
        u.set_runtime_state(self.base, old)
        with patch.object(d, "prepare_environment", return_value=new) as prepare, patch.object(u.subprocess, "run", return_value=Mock(returncode=0)):
            self.fixture.prepare(self.changed_archive(new_text))
        self.assertEqual(prepare.call_args.args[1]["lock_hash"], new["lock_hash"])
        self.assertEqual(u.read_runtime_state(self.base), old)
        self.assertEqual(u.fingerprint(self.base), self.fixture.original)
        self.assertEqual(u.read_json(self.base / ".updates/pending/metadata.json")["runtime"], new)
        u.apply_pending(self.base)
        self.assertEqual(u.read_runtime_state(self.base), new)
        u.update_command(self.base, ["--rollback"])
        u.finish_update(self.base)
        self.assertEqual(u.read_runtime_state(self.base), old)
        self.assertEqual(u.fingerprint(self.base), self.fixture.original)
        self.fixture.assert_private_preserved()

    def test_dependency_failure_never_installs_new_source(self):
        old = seed_environment(self.base, "working lock")
        u.set_runtime_state(self.base, old)
        with patch.object(d, "prepare_environment", side_effect=u.UpdateError("依存パッケージの準備失敗")), self.assertRaises(u.UpdateError):
            self.fixture.prepare(self.changed_archive(d.read_spec(self.base)["text"] + "\n# new lock"))
        self.assertEqual(u.fingerprint(self.base), self.fixture.original)
        self.assertEqual(u.read_runtime_state(self.base), old)
        self.assertFalse((self.base / ".updates/pending").exists())

    def test_runtime_switch_failure_restores_both_source_and_previous_runtime(self):
        old = seed_environment(self.base, "old lock")
        new = seed_environment(self.base, "new lock", "b" * 32)
        u.set_runtime_state(self.base, old)
        with patch.object(d, "prepare_environment", return_value=new), patch.object(u.subprocess, "run", return_value=Mock(returncode=0)):
            self.fixture.prepare(self.changed_archive("new lock"))
        set_state = u.set_runtime_state

        def fail_new(base, state):
            if state == new:
                raise PermissionError("cannot activate candidate")
            return set_state(base, state)

        with patch.object(u, "set_runtime_state", side_effect=fail_new), self.assertRaises(PermissionError):
            u.apply_pending(self.base)
        self.assertEqual(u.fingerprint(self.base), self.fixture.original)
        self.assertEqual(u.read_runtime_state(self.base), old)

    def test_invalid_runtime_ids_and_symlinked_directories_cannot_be_executed(self):
        for identity in ("../outside", "", "a" * 31, "/tmp/python"):
            with self.subTest(identity=identity), self.assertRaises(u.UpdateError):
                u.runtime_directory(self.base, {"id": identity, "lock_hash": "0" * 64})
        state = seed_environment(self.base, "lock")
        path = u.runtime_directory(self.base, state)
        moved = path.with_name("outside")
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(u.UpdateError, "シンボリックリンク"):
            u.runtime_directory(self.base, state)

    def test_corrupt_runtime_is_repaired_before_reexec_and_help_does_not_download(self):
        state = seed_environment(self.base, d.read_spec(self.base)["text"])
        u.write_json(self.base / ".updates/runtime.json", {"id": "../../bad"})
        with u.file_lock(self.base / ".updates/runtime.lock") as runtime, patch.dict(os.environ, dict(os.environ)), \
                patch.object(d, "prepare_environment", return_value=state), patch.object(u.os, "execv") as execute:
            u.activate_runtime(self.base, ["update"], runtime)
            self.assertEqual(Path(execute.call_args.args[0]), u.runtime_directory(self.base, state) / "bin/python")
            self.assertEqual(u.read_runtime_state(self.base), state)
        for args in (["--help"], ["--version"], ["update", "--rollback"], ["--ui", "--demo"]):
            with self.subTest(args=args), patch.object(d, "prepare_environment") as prepare, patch.object(u.os, "execv") as execute:
                u.activate_runtime(self.base, args, None)
                prepare.assert_not_called()
                execute.assert_not_called()

    def test_legacy_backup_without_runtime_record_restores_original_venv(self):
        self.fixture.prepare()
        u.apply_pending(self.base)
        shutil.copytree(self.base / ".updates/backup", self.base / ".updates/transaction")
        (self.base / ".updates/transaction/runtime.json").unlink()
        state = seed_environment(self.base, "active lock")
        u.set_runtime_state(self.base, state)
        u.recover(self.base)
        self.assertIsNone(u.read_runtime_state(self.base))
        self.assertEqual(u.fingerprint(self.base), self.fixture.original)
        self.fixture.assert_private_preserved()

    def test_bootstrap_rechecks_dependency_spec_after_waiting_for_other_processes(self):
        first, second = spec("before wait"), spec("after wait")
        new = seed_environment(self.base, second["text"])
        with u.file_lock(self.base / ".updates/runtime.lock") as runtime, patch.dict(os.environ, dict(os.environ)), \
                patch.object(d, "read_spec", side_effect=[first, second]), patch.object(d, "prepare_environment", return_value=new) as prepare, \
                patch.object(u.os, "execv"):
            u.activate_runtime(self.base, ["update"], runtime)
        self.assertEqual(prepare.call_args.args[1], second)

    def test_interrupted_update_recovers_even_if_dependency_module_is_broken(self):
        old = seed_environment(self.base, "old lock")
        new = seed_environment(self.base, "new lock", "b" * 32)
        u.set_runtime_state(self.base, old)
        with patch.object(d, "prepare_environment", return_value=new), patch.object(u.subprocess, "run", return_value=Mock(returncode=0)):
            self.fixture.prepare(self.changed_archive("new lock"))
        u.apply_pending(self.base)
        shutil.copytree(self.base / ".updates/backup", self.base / ".updates/transaction")
        (self.base / "src/dependencies.py").write_text("broken(:\n")
        result = subprocess.run([sys.executable, str(self.base / "main.py"), "--version"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(u.read_runtime_state(self.base), old)
        self.assertEqual(u.fingerprint(self.base), self.fixture.original)

    def test_orphaned_environments_are_deleted_after_apply_finish_and_rollback(self):
        oldest = seed_environment(self.base, "oldest lock", "0" * 32)
        old = seed_environment(self.base, "old lock", "a" * 32)
        new = seed_environment(self.base, "new lock", "b" * 32)
        incomplete = self.base.resolve() / ".updates/environments" / ("1" * 32)
        (incomplete / "bin").mkdir(parents=True)
        (self.base.resolve() / ".updates/environments/.DS_Store").write_bytes(b"finder")
        u.set_runtime_state(self.base, old)
        with patch.object(d, "prepare_environment", return_value=new), patch.object(u.subprocess, "run", return_value=Mock(returncode=0)):
            self.fixture.prepare(self.changed_archive("new lock"))
        applied = u.apply_pending(self.base)
        self.assertIsNotNone(applied)
        self.assertIn("更新が完了", applied)
        envs = self.base.resolve() / ".updates/environments"
        self.assertTrue((envs / old["id"]).is_dir())
        self.assertTrue((envs / new["id"]).is_dir())
        self.assertFalse((envs / oldest["id"]).exists())
        self.assertFalse(incomplete.exists())
        self.assertTrue((envs / ".DS_Store").is_file())

        orphan = seed_environment(self.base, "interrupted lock", "c" * 32)
        self.assertIsNone(u.finish_update(self.base))
        self.assertFalse((envs / orphan["id"]).exists())
        self.assertTrue((envs / old["id"]).is_dir())
        self.assertTrue((envs / new["id"]).is_dir())

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(u.run_application(self.base, ["update", "--rollback"]), 0)
        self.assertIn("戻しました", errors.getvalue())
        self.assertEqual(u.read_runtime_state(self.base), old)
        self.assertTrue((envs / old["id"]).is_dir())
        self.assertFalse((envs / new["id"]).exists())
        self.fixture.assert_private_preserved()

    def test_cleanup_preserves_all_referenced_environments_and_uses_dependency_lock(self):
        self.assertEqual(u.cleanup_environments(self.base), [])
        self.assertFalse((self.base / ".updates/dependency.lock").exists())

        active = seed_environment(self.base, "active lock", "a" * 32)
        pending_env = seed_environment(self.base, "pending lock", "b" * 32)
        backup_env = seed_environment(self.base, "backup lock", "c" * 32)
        transaction_env = seed_environment(self.base, "transaction lock", "d" * 32)
        orphan = seed_environment(self.base, "orphan lock", "e" * 32)

        u.set_runtime_state(self.base, active)
        u.write_json(self.base / ".updates/pending/metadata.json", {"runtime": pending_env})
        u.write_json(self.base / ".updates/backup/runtime.json", backup_env)
        u.write_json(self.base / ".updates/transaction/runtime.json", transaction_env)

        expected_ids = {active["id"], pending_env["id"], backup_env["id"], transaction_env["id"]}
        self.assertEqual(u.referenced_environment_ids(self.base), expected_ids)

        lock_calls = []
        real_lock = u.file_lock

        def spy_lock(path, exclusive=False, blocking=True):
            lock_calls.append((path, exclusive, blocking))
            return real_lock(path, exclusive=exclusive, blocking=blocking)

        with patch.object(u, "file_lock", side_effect=spy_lock):
            removed = u.cleanup_environments(self.base)

        self.assertEqual(removed, [orphan["id"]])
        self.assertEqual(lock_calls, [(self.base.resolve() / ".updates/dependency.lock", True, True)])
        envs = self.base.resolve() / ".updates/environments"
        for env_id in expected_ids:
            self.assertTrue((envs / env_id).is_dir(), env_id)
        self.assertFalse((envs / orphan["id"]).exists())

        # null 記録や runtime フィールド省略の pending はエラーにせず未参照として扱う。
        u.write_json(self.base / ".updates/backup/runtime.json", None)
        u.write_json(self.base / ".updates/transaction/runtime.json", None)
        u.write_json(self.base / ".updates/pending/metadata.json", {"version": "v99.0.0"})
        self.assertEqual(u.referenced_environment_ids(self.base), {active["id"]})
        self.assertEqual(sorted(u.cleanup_environments(self.base)), sorted([pending_env["id"], backup_env["id"], transaction_env["id"]]))
        self.assertTrue((envs / active["id"]).is_dir())

    def test_cleanup_refuses_symlinks_and_corrupt_records_without_deleting_targets(self):
        outside = Path(self.fixture.temp.name) / "outside-envs"
        (outside / "keep").mkdir(parents=True)
        marker = outside / "keep/marker.txt"
        marker.write_text("must survive")

        updates = self.base.resolve() / ".updates"
        updates.mkdir(exist_ok=True)
        envs = updates / "environments"
        envs.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(u.UpdateError, "シンボリックリンク"):
            u.cleanup_environments(self.base)
        self.assertEqual(marker.read_text(), "must survive")

        envs.unlink()
        envs.symlink_to(outside / "missing", target_is_directory=True)
        with self.assertRaisesRegex(u.UpdateError, "シンボリックリンク"):
            u.cleanup_environments(self.base)
        envs.unlink()

        envs.mkdir()
        child_link = envs / ("f" * 32)
        child_link.symlink_to(outside, target_is_directory=True)
        self.assertEqual(u.cleanup_environments(self.base), [])
        self.assertEqual(marker.read_text(), "must survive")
        child_link.unlink()

        kept = seed_environment(self.base, "kept lock", "a" * 32)
        (updates / "backup").mkdir()
        (updates / "backup/runtime.json").write_text("{broken")
        with self.assertRaisesRegex(u.UpdateError, "読み取れません"):
            u.cleanup_environments(self.base)
        self.assertTrue((envs / kept["id"]).is_dir())
        (updates / "backup/runtime.json").unlink()

        runtime_link = updates / "runtime.json"
        runtime_link.symlink_to(marker)
        with self.assertRaisesRegex(u.UpdateError, "シンボリックリンク"):
            u.cleanup_environments(self.base)
        self.assertTrue((envs / kept["id"]).is_dir())
        runtime_link.unlink()

        shutil.rmtree(updates)
        updates.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(u.UpdateError, "シンボリックリンク"):
            u.cleanup_environments(self.base)
        self.assertEqual(marker.read_text(), "must survive")
        self.assertFalse((outside / "dependency.lock").exists())

    def test_stale_pending_is_discarded_before_att_update_checks_current_release(self):
        from src.version import VERSION
        active = seed_environment(self.base, "active lock", "a" * 32)
        stale_env = seed_environment(self.base, "stale pending lock", "b" * 32)
        u.set_runtime_state(self.base, active)

        with patch.object(d, "prepare_environment", return_value=stale_env), patch.object(u.subprocess, "run", return_value=Mock(returncode=0)):
            self.fixture.prepare(self.changed_archive("stale pending lock"))
        metadata_path = self.base / ".updates/pending/metadata.json"
        metadata = u.read_json(metadata_path)
        metadata["version"] = f"v{VERSION}"
        u.write_json(metadata_path, metadata)

        current_manifest = fixtures.manifest_for(self.fixture.archive(), VERSION)
        with patch.object(u, "fetch", return_value=json.dumps(current_manifest).encode()) as fetch, \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            code = u.run_application(self.base, ["update"])
        self.assertEqual(code, 0)
        fetch.assert_called_once()
        self.assertIn("更新なし: 最新版です", stdout.getvalue())
        self.assertIn(VERSION, stdout.getvalue())
        self.assertNotIn("更新の準備ができています", stdout.getvalue() + stderr.getvalue())
        self.assertFalse((self.base / ".updates/pending").exists())
        envs = self.base.resolve() / ".updates/environments"
        self.assertTrue((envs / active["id"]).is_dir())
        self.assertFalse((envs / stale_env["id"]).exists())

    def test_stale_pending_older_than_current_version_allows_fresh_download(self):
        from src.version import VERSION
        self.fixture.prepare()
        metadata_path = self.base / ".updates/pending/metadata.json"
        metadata = u.read_json(metadata_path)
        metadata["version"] = "v0.0.1"
        u.write_json(metadata_path, metadata)
        u.write_json(self.base / ".updates/check.json", {"checked_at": time.time(), "interval": 86400})

        archive = self.fixture.archive()
        manifest = fixtures.manifest_for(archive, fixtures.NEXT)
        with patch.object(u, "fetch", side_effect=[json.dumps(manifest).encode(), archive]) as fetch:
            message = u.prepare_update(self.base)
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("ダウンロードしました", message)
        self.assertIn(fixtures.NEXT, u.apply_pending(self.base))
        self.assertIn(fixtures.NEXT, (self.base / "src/version.py").read_text())

    def test_stale_pending_is_discarded_when_remote_release_is_older_than_current(self):
        from src.version import VERSION
        self.fixture.prepare()
        metadata_path = self.base / ".updates/pending/metadata.json"
        metadata = u.read_json(metadata_path)
        metadata["version"] = f"v{VERSION}"
        u.write_json(metadata_path, metadata)
        older_manifest = fixtures.manifest_for(self.fixture.archive(), "0.0.1")
        with patch.object(u, "fetch", return_value=json.dumps(older_manifest).encode()), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            self.assertEqual(u.run_application(self.base, ["update"]), 0)
        self.assertIn("より新しいバージョンです", stdout.getvalue())
        self.assertFalse((self.base / ".updates/pending").exists())


class DependencyOfflineIntegrationTests(unittest.TestCase):
    def test_real_pip_update_reexec_and_rollback_using_local_hash_checked_wheels(self):
        fixture = fixtures.UpdaterTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        base = fixture.base
        wheels = Path(fixture.temp.name) / "wheels"
        wheels.mkdir()
        first = test_packages(wheels, "1.0.0")
        second = test_packages(wheels, "1.1.0")
        requirements = b"keyring>=25\nplaywright>=1\n"
        (base / "requirements.txt").write_bytes(requirements)
        (base / "src/dependency_lock.py").write_bytes(lock_source(first, requirements))
        config = (base / "config.example.json").read_bytes()
        (base / "config.json").write_bytes(config)
        original = u.fingerprint(base)
        env = {"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(wheels), "PIP_CACHE_DIR": str(Path(fixture.temp.name) / "pip-cache")}
        with patch.dict(os.environ, env):
            old = d.prepare_environment(base, spec(first))
            u.set_runtime_state(base, old)
            bad = second.replace(second.split("sha256:")[1].split()[0], "0" * 64, 1)
            corrupted = fixture.archive({"src/dependency_lock.py": lock_source(bad, requirements)}, omit=("src/dependency_lock.py",))
            with self.assertRaisesRegex(u.UpdateError, "ハッシュ検証付き導入"):
                fixture.prepare(corrupted)
            self.assertEqual(u.read_runtime_state(base), old)
            self.assertEqual(u.fingerprint(base), original)
            archive = fixture.archive({"src/dependency_lock.py": lock_source(second, requirements)}, omit=("src/dependency_lock.py",))
            fixture.prepare(archive)
            new = u.read_json(base / ".updates/pending/metadata.json")["runtime"]
            self.assertNotEqual(old["id"], new["id"])
            self.assertEqual(u.read_runtime_state(base), old)
            u.apply_pending(base)
            self.assertEqual(u.read_runtime_state(base), new)
            self.assertTrue((base.resolve() / ".updates/environments" / old["id"]).is_dir())
            self.assertTrue((base.resolve() / ".updates/environments" / new["id"]).is_dir())
            python = u.runtime_directory(base, new) / "bin/python"
            version = subprocess.check_output([str(python), "-c", "import playwright; print(playwright.__version__)"], text=True).strip()
            self.assertEqual(version, "1.1.0")
            result = subprocess.run([sys.executable, str(base / "main.py"), "user", "list"], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([sys.executable, str(base / "main.py"), "update", "--rollback"], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(u.read_runtime_state(base), old)
            self.assertTrue((base.resolve() / ".updates/environments" / old["id"]).is_dir())
            self.assertFalse((base.resolve() / ".updates/environments" / new["id"]).exists())
            self.assertEqual(u.fingerprint(base), original)
            python = u.runtime_directory(base, old) / "bin/python"
            version = subprocess.check_output([str(python), "-c", "import playwright; print(playwright.__version__)"], text=True).strip()
            self.assertEqual(version, "1.0.0")
        self.assertEqual((base / "config.json").read_bytes(), config)
        self.assertEqual((base / ".venv/marker").read_bytes(), fixture.private[".venv/marker"])


if __name__ == "__main__":
    unittest.main()
