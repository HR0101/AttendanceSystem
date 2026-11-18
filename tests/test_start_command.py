"""start.command が --ui モードで main.py を起動することを隔離環境で確認する。"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StartCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "tool with spaces"
        self.project.mkdir()
        shutil.copy2(ROOT / "start.command", self.project / "start.command")
        (self.project / "main.py").write_text("# mock entry\n", encoding="utf-8")
        self.mock_bin = self.base / "mock-bin"
        self.mock_bin.mkdir()
        self.log = self.base / "python.log"
        self.env = dict(os.environ, PATH=f"{self.mock_bin}:/usr/bin:/bin", START_LOG=str(self.log))

    def make_python(self, path: Path, label: str, exit_code: int = 0):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'#!/bin/bash\nprintf "%s|%s|%s\\n" "{label}" "$PWD" "$*" >> "$START_LOG"\nexit {exit_code}\n',
            encoding="utf-8",
        )
        path.chmod(0o755)

    def run_start(self, *, env=None):
        return subprocess.run(
            ["/bin/bash", str(self.project / "start.command")],
            env=self.env if env is None else env,
            cwd=self.base,
            input="\n",
            capture_output=True,
            text=True,
        )

    def test_prefers_venv_python_with_ui_flag(self):
        self.make_python(self.project / ".venv/bin/python", "venv")
        self.make_python(self.mock_bin / "python3", "system")
        result = self.run_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        label, cwd, args = self.log.read_text(encoding="utf-8").strip().splitlines()[-1].split("|")
        self.assertEqual(label, "venv")
        self.assertEqual(Path(cwd).resolve(), self.project.resolve())
        self.assertEqual(args, "main.py --ui")

    def test_falls_back_to_python3_with_ui_flag(self):
        self.make_python(self.mock_bin / "python3", "system")
        result = self.run_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        label, cwd, args = self.log.read_text(encoding="utf-8").strip().splitlines()[-1].split("|")
        self.assertEqual(label, "system")
        self.assertEqual(Path(cwd).resolve(), self.project.resolve())
        self.assertEqual(args, "main.py --ui")

    def test_reports_failure_and_propagates_exit_status(self):
        self.make_python(self.project / ".venv/bin/python", "venv", exit_code=2)
        result = self.run_start()
        self.assertEqual(result.returncode, 2)
        self.assertIn("logs フォルダを確認してください", result.stdout)

    def test_missing_python3_reports_error(self):
        dirname_bin = shutil.which("dirname")
        self.assertIsNotNone(dirname_bin)
        isolated_bin = self.base / "no-python-bin"
        isolated_bin.mkdir()
        (isolated_bin / "dirname").symlink_to(dirname_bin)
        env = dict(self.env, PATH=str(isolated_bin))
        result = self.run_start(env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Python 3 が見つかりません", result.stdout)


if __name__ == "__main__":
    unittest.main()
