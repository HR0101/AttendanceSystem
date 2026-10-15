"""インストーラーを隔離HOMEと模擬Pythonで検証する。外部接続はしない。"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "tool with spaces"
        self.project.mkdir()
        for name in ("install.sh", "att", "config.example.json", "requirements.txt"):
            shutil.copy2(ROOT / name, self.project / name)
        self.home = self.base / "home with spaces"
        self.home.mkdir()
        mock_bin = self.base / "mock-bin"
        mock_bin.mkdir()
        self.log = self.base / "calls.log"
        self.env = dict(os.environ, HOME=str(self.home), SHELL="/bin/zsh",
                        PATH=f"{mock_bin}:/usr/bin:/bin", INSTALL_LOG=str(self.log))
        self.env.pop("ZDOTDIR", None)
        self.env.pop("FAIL_PIP", None)
        scripts = {
            "uname": '#!/bin/bash\necho Darwin\n',
            "python3": '''#!/bin/bash
printf '%s\\n' "$*" >> "$INSTALL_LOG"
if [ "$1" = -m ] && [ "$2" = venv ]; then
    mkdir -p "$3/bin"
    cp "$0" "$3/bin/python"
fi
if [ "${FAIL_PIP:-}" = 1 ] && [ "$1" = -m ] && [ "$2" = pip ]; then
    exit 1
fi
''',
        }
        for name, content in scripts.items():
            path = mock_bin / name
            path.write_text(content)
            path.chmod(0o755)

    def install(self):
        return subprocess.run(["/bin/bash", str(self.project / "install.sh")],
                              env=self.env, cwd=self.base, capture_output=True, text=True)

    def test_install_repeat_and_command_from_another_directory(self):
        config = self.project / "config.json"
        config.write_text('{"existing": true}')
        schedule = self.project / "schedule.json"
        schedule.write_text('{"courses": []}')
        profile = self.home / ".zshrc"
        profile.write_text("# existing settings\n")
        for _ in range(2):
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_text(), '{"existing": true}')
        self.assertEqual(schedule.read_text(), '{"courses": []}')
        self.assertEqual(profile.read_text().count('export PATH='), 1)
        self.assertTrue(profile.read_text().startswith("# existing settings\n"))
        self.assertEqual((self.home / ".local/bin/att").resolve(), (self.project / "att").resolve())
        result = subprocess.run(
            ["/bin/bash", "-c", 'source "$HOME/.zshrc"; att --help'],
            env=self.env, cwd=self.base, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        invocation = self.log.read_text().splitlines()[-1]
        self.assertTrue(invocation.endswith("/main.py --help"))
        self.assertEqual(Path(invocation.removesuffix(" --help")).resolve(),
                         (self.project / "main.py").resolve())

    def test_new_config_is_private(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.project / "config.json"
        self.assertEqual(config.read_bytes(), (self.project / "config.example.json").read_bytes())
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_conflicting_command_is_preserved(self):
        command = self.home / ".local/bin/att"
        command.parent.mkdir(parents=True)
        command.write_text("another command")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(command.read_text(), "another command")
        self.assertNotIn("-m pip", self.log.read_text())
        self.assertFalse((self.home / ".zshrc").exists())

    def test_dependency_failure_does_not_register_command(self):
        self.env["FAIL_PIP"] = "1"
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".local/bin/att").is_symlink())
        self.assertFalse((self.home / ".zshrc").exists())
        self.assertFalse((self.project / "config.json").exists())

    def test_shell_profiles(self):
        for shell, profile in (("/bin/bash", self.home / ".bash_profile"),
                               ("/bin/zsh", self.home / "zsh config/.zshrc")):
            with self.subTest(shell=shell):
                self.env["SHELL"] = shell
                self.env["ZDOTDIR"] = str(self.home / "zsh config")
                result = self.install()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('export PATH=', profile.read_text())


if __name__ == "__main__":
    unittest.main()
