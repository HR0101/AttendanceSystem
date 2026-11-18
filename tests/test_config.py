"""設定 URL、パーミッション検証、ファイルロック、ログ設定の保護を確認する。"""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from src.config import ConfigurationError, load_config, private_file_lock
from src.logging_utils import setup_logging

SAMPLE_CONFIG = {
    "login_url": "https://example.test/attendance/login",
    "top_url": "https://example.test/attendance/top",
    "student_id": "",
    "browser": {"headless": True, "timeout_ms": 15000},
    "dry_run": False,
}


class ConfigTests(unittest.TestCase):
    def config_path(self, top_url):
        config = {
            "login_url": "https://attendance.is.chibatech.ac.jp/attendance/login",
            "top_url": top_url,
            "student_id": "",
            "browser": {"headless": True, "timeout_ms": 15000},
            "dry_run": False,
        }
        return Mock(read_text=Mock(return_value=json.dumps(config)))

    def test_top_url_uses_login_host(self):
        path = self.config_path("https://attendance.is.chibatech.ac.jp/attendance/top")
        self.assertEqual(load_config(path)["student_id"], "")

    def test_rejects_different_top_host(self):
        path = self.config_path("https://attendance.is.it-chiba.ac.jp/attendance/top")
        with self.assertRaisesRegex(ConfigurationError, "ホスト名"):
            load_config(path)

    def test_load_config_tightens_loose_permissions_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = base / "config.json"
            path.write_text(json.dumps(SAMPLE_CONFIG), encoding="utf-8")
            path.chmod(0o644)
            loaded = load_config(path)
            self.assertEqual(loaded["student_id"], "")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            sym = base / "sym-config.json"
            sym.symlink_to(path)
            with self.assertRaisesRegex(ConfigurationError, "シンボリックリンク"):
                load_config(sym)

            broken = base / "broken-config.json"
            broken.symlink_to(base / "missing.json")
            with self.assertRaisesRegex(ConfigurationError, "シンボリックリンク"):
                load_config(broken)

    def test_private_file_lock_tightens_mode_rejects_symlink_and_is_reentrant_on_same_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / "nested" / "config.json"
            lock_path = target.with_name("config.json.lock")
            lock_path.parent.mkdir(mode=0o755, parents=True)
            lock_path.write_text("", encoding="utf-8")
            lock_path.chmod(0o644)

            with private_file_lock(target):
                self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
                with private_file_lock(target):
                    self.assertTrue(lock_path.exists())

            sym_target = base / "nested" / "other.json"
            sym_lock = sym_target.with_name("other.json.lock")
            sym_lock.symlink_to(lock_path)
            with self.assertRaises(OSError):
                with private_file_lock(sym_target):
                    pass

    def test_private_file_lock_serializes_across_threads_and_subprocesses(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / "config.json"
            target.write_text("0", encoding="utf-8")

            # 別スレッドからの同時 read-modify-write が直列化されることを確認する。
            def bump():
                for _ in range(10):
                    with private_file_lock(target):
                        value = int(target.read_text(encoding="utf-8"))
                        target.write_text(str(value + 1), encoding="utf-8")

            threads = [threading.Thread(target=bump) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            self.assertEqual(int(target.read_text(encoding="utf-8")), 40)

            # 別プロセスがロックを保持している間は待機し、解放後に更新を引き継ぐことを確認する。
            script = "\n".join([
                "import sys",
                "from pathlib import Path",
                "from src.config import private_file_lock",
                "p = Path(sys.argv[1])",
                "with private_file_lock(p):",
                "    print('LOCKED', flush=True)",
                "    sys.stdin.read()",
                "    p.write_text(str(int(p.read_text()) + 100))",
            ])
            proc = subprocess.Popen(
                [sys.executable, "-c", script, str(target)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=str(Path(__file__).resolve().parents[1]),
            )
            try:
                line = proc.stdout.readline().strip()
                if line != "LOCKED":
                    self.fail(f"unexpected lock output {line!r}: {proc.stderr.read()}")
                entered = threading.Event()

                def parent_writer():
                    with private_file_lock(target):
                        entered.set()
                        target.write_text(str(int(target.read_text()) + 1))

                t = threading.Thread(target=parent_writer)
                t.start()
                self.assertFalse(entered.wait(timeout=0.05))
                proc.stdin.close()
                t.join(timeout=5)
                self.assertTrue(entered.is_set())
                self.assertEqual(proc.wait(timeout=5), 0, proc.stderr.read())
                self.assertEqual(int(target.read_text(encoding="utf-8")), 141)
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream and not stream.closed:
                        stream.close()
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)

    def test_setup_logging_tightens_permissions_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            log_dir = base / "logs"
            log_dir.mkdir(mode=0o755)
            log_dir.chmod(0o755)
            today_log = log_dir / f"attendance-{datetime.now():%Y-%m-%d}.log"
            today_log.write_text("existing\n", encoding="utf-8")
            today_log.chmod(0o644)
            other_log = log_dir / "attendance-2099-01-01.log"
            other_log.write_text("future\n", encoding="utf-8")
            other_log.chmod(0o644)

            logger = setup_logging(base)
            self.addCleanup(lambda: [h.close() for h in logger.handlers] or logger.handlers.clear())
            self.assertEqual(log_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(today_log.stat().st_mode & 0o777, 0o600)
            self.assertEqual(other_log.stat().st_mode & 0o777, 0o600)
            for h in list(logger.handlers):
                h.close()
            logger.handlers.clear()

            # ログファイルがシンボリックリンクなら拒否する。
            today_log.unlink()
            decoy = base / "decoy.txt"
            decoy.write_text("x", encoding="utf-8")
            today_log.symlink_to(decoy)
            with self.assertRaises(OSError):
                setup_logging(base)
            today_log.unlink()

            # 壊れたシンボリックリンクが過去ログに紛れても拒否する。
            broken_old = log_dir / "attendance-2099-01-02.log"
            broken_old.symlink_to(base / "nonexistent")
            with self.assertRaises(OSError):
                setup_logging(base)
            broken_old.unlink()

            # logs ディレクトリ自体がシンボリックリンクなら拒否する。
            sym_base = base / "sym_base"
            sym_base.mkdir()
            (sym_base / "logs").symlink_to(log_dir)
            with self.assertRaises(OSError):
                setup_logging(sym_base)


if __name__ == "__main__":
    unittest.main()
