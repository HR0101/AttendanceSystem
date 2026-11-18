"""インストーラーを隔離HOMEと模擬Pythonで検証する。外部接続はしない。"""

import os
from pathlib import Path
import re
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
        for name in ("install.sh", "att", "config.example.json", "requirements.txt", "requirements.lock"):
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

    def test_dependencies_are_installed_from_hashed_lock(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        pip_calls = [line for line in self.log.read_text().splitlines() if line.startswith("-m pip install")]
        self.assertEqual(len(pip_calls), 1)
        self.assertTrue(
            pip_calls[0].startswith("-m pip install --no-input --require-hashes --only-binary=:all: -r ")
        )
        self.assertEqual(Path(pip_calls[0].split(" -r ", 1)[1]).resolve(),
                         (self.project / "requirements.lock").resolve())

    def test_missing_lock_fails_closed(self):
        (self.project / "requirements.lock").unlink()
        for existing_profile in (None, "# existing settings\n"):
            with self.subTest(existing_profile=existing_profile is not None):
                self.log.unlink(missing_ok=True)
                profile = self.home / ".zshrc"
                if existing_profile is None:
                    profile.unlink(missing_ok=True)
                else:
                    profile.write_text(existing_profile)
                result = self.install()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requirements.lock", result.stderr)
                log = self.log.read_text()
                self.assertNotIn("-m pip install", log)
                self.assertNotIn("-m venv", log)
                self.assertFalse((self.project / ".venv").exists())
                self.assertFalse((self.home / ".local/bin/att").is_symlink())
                self.assertFalse((self.home / ".local/bin/att").exists())
                self.assertFalse((self.project / "config.json").exists())
                if existing_profile is None:
                    self.assertFalse(profile.exists())
                else:
                    self.assertEqual(profile.read_text(), existing_profile)

    def test_directory_lock_fails_closed(self):
        (self.project / "requirements.lock").unlink()
        (self.project / "requirements.lock").mkdir()
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requirements.lock", result.stderr)
        self.assertNotIn("-m pip install", self.log.read_text())

    def test_shell_profiles(self):
        for shell, profile in (("/bin/bash", self.home / ".bash_profile"),
                               ("/bin/zsh", self.home / "zsh config/.zshrc")):
            with self.subTest(shell=shell):
                self.env["SHELL"] = shell
                self.env["ZDOTDIR"] = str(self.home / "zsh config")
                result = self.install()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('export PATH=', profile.read_text())


class WorkflowSupplyChainTests(unittest.TestCase):
    """CIワークフローとロックファイルがハッシュ検証とwheel限定を強制することを確認する。"""

    def test_installer_has_no_requirements_txt_fallback(self):
        script = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn("requirements.txt", script)
        self.assertIn("--no-input --require-hashes --only-binary=:all:", script)

    def test_ci_workflow_requires_hashes_and_binary_wheels_for_all_pip_installs(self):
        workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^  test:\s*$")
        for line_number, line in enumerate(workflow.splitlines(), start=1):
            match = re.match(r"^\s*(?:-\s*)?[A-Za-z0-9_-]+:\s+(.+)$", line)
            if match:
                value = match.group(1).strip()
                if value not in ("|", "|-", ">", ">-") and not value.startswith(("'", '"', "#")):
                    self.assertNotIn(
                        ": ", value.split(" #", 1)[0],
                        f"tests.yml line {line_number} contains ': ' inside an unquoted plain YAML scalar",
                    )
        pip_install_lines = [
            line.strip() for line in workflow.splitlines()
            if not line.lstrip().startswith("#") and re.search(r"\bpip\s+install\b", line)
        ]
        self.assertGreaterEqual(len(pip_install_lines), 2)
        for line in pip_install_lines:
            self.assertIn("--no-input", line)
            self.assertIn("--require-hashes", line)
            self.assertIn("--only-binary=:all:", line)
        self.assertTrue(any("-r requirements.lock" in line for line in pip_install_lines))
        self.assertTrue(any("-r requirements-audit.lock" in line for line in pip_install_lines))

    def test_lock_files_pin_sha256_hashes(self):
        audit_lock = (ROOT / "requirements-audit.lock").read_text(encoding="utf-8")
        self.assertIn("pip-audit==2.10.1", audit_lock)
        self.assertIn("--hash=sha256:", audit_lock)
        for lock_name in ("requirements.lock", "requirements-audit.lock"):
            with self.subTest(lock=lock_name):
                lines = (ROOT / lock_name).read_text(encoding="utf-8").splitlines()
                current_package = None
                has_hash = False
                package_count = 0
                for line in lines:
                    if not line or line.startswith("#"):
                        continue
                    if not line[0].isspace():
                        if current_package is not None:
                            self.assertTrue(has_hash, f"{current_package} in {lock_name} is missing --hash=sha256:")
                        self.assertIn("==", line)
                        current_package = line
                        has_hash = False
                        package_count += 1
                    elif "--hash=sha256:" in line:
                        has_hash = True
                self.assertGreater(package_count, 0)
                if current_package is not None:
                    self.assertTrue(has_hash, f"{current_package} in {lock_name} is missing --hash=sha256:")


if __name__ == "__main__":
    unittest.main()
