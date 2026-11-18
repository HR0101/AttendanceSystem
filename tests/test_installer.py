"""インストーラーを隔離HOMEと模擬Pythonで検証する。外部接続はしない。"""

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


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
                        PATH=f"{mock_bin}:/usr/bin:/bin", INSTALL_LOG=str(self.log),
                        REAL_PYTHON=sys.executable)
        self.env.pop("ZDOTDIR", None)
        self.env.pop("FAIL_PIP", None)
        scripts = {
            "uname": '#!/bin/bash\necho Darwin\n',
            "python3": '''#!/bin/bash
printf '%s\\n' "$*" >> "$INSTALL_LOG"
if [ "$1" = -c ]; then
    exec "$REAL_PYTHON" "$@"
fi
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

    def install(self, project=None, cwd=None, env=None):
        project = project or self.project
        return subprocess.run(["/bin/bash", str(project / "install.sh")],
                              env=env or self.env, cwd=cwd or self.base, capture_output=True, text=True)

    def assert_failed_before_setup(self, result, project=None, profile=None, existing_profile=None):
        project = project or self.project
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requirements.lock", result.stderr)
        log = self.log.read_text() if self.log.exists() else ""
        self.assertNotIn("-m pip install", log)
        self.assertNotIn("-m venv", log)
        self.assertNotIn("-m playwright", log)
        self.assertFalse((project / ".venv").exists())
        self.assertFalse((self.home / ".local/bin/att").is_symlink())
        self.assertFalse((self.home / ".local/bin/att").exists())
        self.assertFalse((project / "config.json").exists())
        if profile is not None:
            if existing_profile is None:
                self.assertFalse(profile.exists())
            else:
                self.assertEqual(profile.read_text(), existing_profile)

    def extract_release_archive(self, destination):
        from scripts.build_release import build
        from src.version import VERSION

        archive = build(f"v{VERSION}", self.base / "attendance-update.zip")
        with zipfile.ZipFile(archive) as package:
            self.assertNotIn("requirements.lock", package.namelist())
            package.extractall(destination)
        return destination

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
        self.assertIn(f'rm "{command}"', result.stderr)
        self.assertIn("bash install.sh", result.stderr)
        self.assertNotIn("-m pip", self.log.read_text())
        self.assertFalse((self.home / ".zshrc").exists())

    def test_broken_and_moved_symlinks_include_recovery_commands(self):
        command = self.home / ".local/bin/att"
        command.parent.mkdir(parents=True)
        other = self.base / "other/att"
        for case, exists in (("broken", False), ("moved", True)):
            with self.subTest(case=case):
                if command.is_symlink() or command.exists():
                    command.unlink()
                if exists:
                    other.parent.mkdir(parents=True, exist_ok=True)
                    other.write_text("#!/bin/sh\n")
                command.symlink_to(other)
                result = self.install()
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(command.is_symlink())
                self.assertEqual(os.readlink(command), str(other))
                self.assertIn(f'rm "{command}"', result.stderr)
                self.assertIn("bash install.sh", result.stderr)
                self.assertNotIn("-m pip", self.log.read_text())
                if other.exists():
                    other.unlink()

    def test_broken_or_old_venv_includes_recovery_command(self):
        venv = self.project / ".venv"
        cases = ("missing_python", "broken_symlink", "old_python", "non_dir")
        for case in cases:
            with self.subTest(case=case):
                if venv.is_dir():
                    shutil.rmtree(venv)
                elif venv.exists() or venv.is_symlink():
                    venv.unlink()
                if case == "non_dir":
                    venv.write_text("not a dir")
                else:
                    (venv / "bin").mkdir(parents=True)
                    python = venv / "bin/python"
                    if case == "broken_symlink":
                        python.symlink_to(self.base / "missing-python")
                    elif case == "old_python":
                        python.write_text("#!/bin/bash\nexit 1\n")
                        python.chmod(0o755)
                result = self.install()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f'rm -rf "{venv}"', result.stderr)
                self.assertIn("bash install.sh", result.stderr)
                self.assertFalse((self.home / ".local/bin/att").exists())
                self.assertFalse((self.home / ".local/bin/att").is_symlink())

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
                self.assert_failed_before_setup(result, self.project, profile, existing_profile)

    def test_directory_lock_fails_closed(self):
        (self.project / "requirements.lock").unlink()
        (self.project / "requirements.lock").mkdir()
        result = self.install()
        self.assert_failed_before_setup(result)

    def test_extracted_update_archive_recovers_lock_and_installs(self):
        extracted = self.extract_release_archive(self.base / "extracted update with spaces")
        self.assertFalse((extracted / "requirements.lock").exists())
        for run_index in range(2):
            with self.subTest(run=run_index):
                self.log.unlink(missing_ok=True)
                result = self.install(project=extracted, cwd=extracted)
                self.assertEqual(result.returncode, 0, result.stderr)
                lock = extracted / "requirements.lock"
                self.assertTrue(lock.is_file())
                self.assertFalse(lock.is_symlink())
                self.assertEqual(lock.stat().st_mode & 0o777, 0o644)
                self.assertEqual(lock.read_bytes(), (ROOT / "requirements.lock").read_bytes())
                pip_calls = [line for line in self.log.read_text().splitlines() if line.startswith("-m pip install")]
                self.assertEqual(len(pip_calls), 1)
                self.assertTrue(
                    pip_calls[0].startswith("-m pip install --no-input --require-hashes --only-binary=:all: -r ")
                )
                self.assertEqual(Path(pip_calls[0].split(" -r ", 1)[1]).resolve(), lock.resolve())
                self.assertEqual((self.home / ".local/bin/att").resolve(), (extracted / "att").resolve())
                self.assertEqual((extracted / "config.json").stat().st_mode & 0o777, 0o600)

    def test_tampered_embedded_lock_sources_fail_closed(self):
        req_bytes = (ROOT / "requirements.txt").read_bytes()
        req_sha = hashlib.sha256(req_bytes).hexdigest()
        marker = self.base / "executed-marker"
        cases = (
            ("tampered-requirements", lambda d: (d / "requirements.txt").write_text("playwright>=1.50,<2\nevil\n")),
            ("syntax-error-lock-module", lambda d: (d / "src/dependency_lock.py").write_text("broken(:\n")),
            ("executable-lock-module", lambda d: (d / "src/dependency_lock.py").write_text(
                f"REQUIREMENTS_SHA256 = {req_sha!r}\n"
                f"LOCK_TEXT = (__import__('pathlib').Path({str(marker)!r}).write_text('pwn'), 'keyring==25.7.0 --hash=sha256:{'0' * 64}')[1]\n"
            )),
            ("missing-hash-pin", lambda d: (d / "src/dependency_lock.py").write_text(
                f"REQUIREMENTS_SHA256 = {req_sha!r}\nLOCK_TEXT = 'keyring==25.7.0\\n'\n"
            )),
            ("missing-version-pin", lambda d: (d / "src/dependency_lock.py").write_text(
                f"REQUIREMENTS_SHA256 = {req_sha!r}\nLOCK_TEXT = 'keyring>=25 --hash=sha256:{'0' * 64}\\n'\n"
            )),
            ("injected-pip-flag", lambda d: (d / "src/dependency_lock.py").write_text(
                f"REQUIREMENTS_SHA256 = {req_sha!r}\n"
                f"LOCK_TEXT = 'keyring==25.7.0 --extra-index-url https://evil.invalid --hash=sha256:{'0' * 64}\\n'\n"
            )),
            ("injected-pip-flag-after-marker", lambda d: (d / "src/dependency_lock.py").write_text(
                f"REQUIREMENTS_SHA256 = {req_sha!r}\n"
                f"LOCK_TEXT = \"keyring==25.7.0 ; sys_platform == 'darwin' --extra-index-url https://evil.invalid --hash=sha256:{'0' * 64}\\n\"\n"
            )),
            ("pip-requirement-option-line", lambda d: (d / "src/dependency_lock.py").write_text(
                f"REQUIREMENTS_SHA256 = {req_sha!r}\nLOCK_TEXT = '-r requirements.txt\\n'\n"
            )),
        )
        profile = self.home / ".zshrc"
        existing_profile = "# existing settings\n"
        for name, mutate in cases:
            with self.subTest(case=name):
                self.log.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
                profile.write_text(existing_profile)
                project = self.extract_release_archive(self.base / f"tampered-{name}")
                mutate(project)
                result = self.install(project=project, cwd=project)
                self.assertFalse((project / "requirements.lock").exists())
                self.assertFalse((project / "requirements.lock").is_symlink())
                self.assertFalse(marker.exists())
                self.assert_failed_before_setup(result, project, profile, existing_profile)

    def test_symlinked_lock_and_sources_fail_closed(self):
        valid_target = self.base / "valid-target.lock"
        valid_target.write_bytes((ROOT / "requirements.lock").read_bytes())

        for mode in ("symlink-lock-existing", "symlink-lock-broken"):
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True)
                project = self.extract_release_archive(self.base / mode)
                target = valid_target if mode == "symlink-lock-existing" else self.base / "missing-target.lock"
                (project / "requirements.lock").symlink_to(target)
                result = self.install(project=project, cwd=project)
                self.assert_failed_before_setup(result, project)
                if mode == "symlink-lock-broken":
                    self.assertFalse(target.exists())

        for rel in ("requirements.txt", "src/dependency_lock.py", "src/dependencies.py",
                    "src/updater.py", "src/__init__.py", "src"):
            with self.subTest(symlinked_source=rel):
                self.log.unlink(missing_ok=True)
                project = self.extract_release_archive(self.base / ("sym-" + rel.replace("/", "_")))
                target_path = project / rel
                backup = self.base / ("real-" + rel.replace("/", "_"))
                target_path.rename(backup)
                target_path.symlink_to(backup, target_is_directory=backup.is_dir())
                result = self.install(project=project, cwd=project)
                self.assertFalse((project / "requirements.lock").exists())
                self.assert_failed_before_setup(result, project)

    def test_recovery_ignores_cwd_decoy_and_pythonpath(self):
        extracted = self.extract_release_archive(self.base / "extracted-isolated")
        decoy_cwd = self.base / "decoy-cwd"
        (decoy_cwd / "src").mkdir(parents=True)
        (decoy_cwd / "src/__init__.py").write_text("")
        decoy_marker = self.base / "decoy-imported"
        (decoy_cwd / "src/dependencies.py").write_text(
            f"from pathlib import Path\nPath({str(decoy_marker)!r}).write_text('pwn')\n"
        )
        env = {**self.env, "PYTHONPATH": str(decoy_cwd)}
        result = self.install(project=extracted, cwd=decoy_cwd, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(decoy_marker.exists())
        self.assertEqual((extracted / "requirements.lock").read_bytes(), (ROOT / "requirements.lock").read_bytes())

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
        logical_lines, current = [], []
        for raw in script.splitlines():
            stripped = raw.strip()
            if not current and (not stripped or stripped.startswith("#")):
                continue
            if stripped.endswith("\\"):
                current.append(stripped[:-1].strip())
                continue
            current.append(stripped)
            logical_lines.append(" ".join(current))
            current.clear()
        if current:
            logical_lines.append(" ".join(current))
        pip_install_lines = [line for line in logical_lines if re.search(r"\bpip\s+install\b", line)]
        self.assertEqual(len(pip_install_lines), 1)
        pip_line = pip_install_lines[0]
        self.assertIn("--no-input", pip_line)
        self.assertIn("--require-hashes", pip_line)
        self.assertIn("--only-binary=:all:", pip_line)
        self.assertIn('"$tool_dir/requirements.lock"', pip_line)
        self.assertNotIn("requirements.txt", pip_line)
        self.assertIsNone(re.search(r"\bpip\b[^\n]*requirements\.txt", script))

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
