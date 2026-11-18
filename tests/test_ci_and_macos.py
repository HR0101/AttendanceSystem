"""CI設定・ロックファイル同期・macOS実環境境界の検証テスト。"""

from datetime import date, timedelta
import hashlib
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

from scripts import generate_dependency_lock
from src import credentials, dependencies, scheduler, updater


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/tests.yml"
RELEASE_WORKFLOW_PATH = ROOT / ".github/workflows/release.yml"
GITLEAKS_CONFIG_PATH = ROOT / ".gitleaks.toml"
SHA_REF_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")


def _strip_yaml_comment(line: str) -> str:
    """クォート外の YAML コメントを除去する。"""
    quote = None
    for idx, ch in enumerate(line):
        if quote is None and ch in ("'", '"'):
            quote = ch
        elif quote == ch:
            quote = None
        elif quote is None and ch == "#" and (idx == 0 or line[idx - 1].isspace()):
            return line[:idx].rstrip()
    return line.rstrip()


def _parse_yaml_scalar(value: str):
    value = value.strip()
    if value == "true":
        return True
    if value == "false":
        return False
    if value.isascii() and value.isdecimal():
        return int(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _split_yaml_key_value(content: str) -> tuple[str, str] | None:
    if ": " in content:
        key, _, rest = content.partition(": ")
        return key.strip(), rest.strip()
    if content.endswith(":"):
        return content[:-1].strip(), ""
    return None


def _parse_yaml_block(lines: list[tuple[int, str]], index: int, base_indent: int):
    """tests.yml / release.yml の辞書・リスト・`|` ブロックを標準ライブラリだけで解析する。"""
    if index >= len(lines):
        return {}, index
    if lines[index][1].startswith("- "):
        items = []
        while index < len(lines) and lines[index][0] == base_indent and lines[index][1].startswith("- "):
            item_text = lines[index][1][2:].strip()
            index += 1
            split = _split_yaml_key_value(item_text)
            if split is None:
                items.append(_parse_yaml_scalar(item_text))
                continue
            key, rest = split
            item = {}
            if rest == "|":
                block_lines, index = _collect_block_scalar(lines, index, base_indent)
                item[key] = block_lines
            elif rest == "":
                nested, index = _parse_yaml_block(lines, index, lines[index][0]) if index < len(lines) and lines[index][0] > base_indent else ({}, index)
                item[key] = nested
            else:
                item[key] = _parse_yaml_scalar(rest)
            while index < len(lines) and lines[index][0] > base_indent:
                sub_indent = lines[index][0]
                if lines[index][1].startswith("- "):
                    raise ValueError(f"予期しない入れ子リスト: {lines[index][1]!r}")
                sub_map, index = _parse_yaml_mapping(lines, index, sub_indent)
                item.update(sub_map)
            items.append(item)
        return items, index
    return _parse_yaml_mapping(lines, index, base_indent)


def _collect_block_scalar(lines: list[tuple[int, str]], index: int, parent_indent: int) -> tuple[str, int]:
    collected = []
    while index < len(lines) and lines[index][0] > parent_indent:
        collected.append(lines[index][1])
        index += 1
    return "\n".join(collected), index


def _parse_yaml_mapping(lines: list[tuple[int, str]], index: int, base_indent: int) -> tuple[dict, int]:
    mapping = {}
    while index < len(lines) and lines[index][0] == base_indent and not lines[index][1].startswith("- "):
        split = _split_yaml_key_value(lines[index][1])
        if split is None:
            raise ValueError(f"YAML キーを解釈できません: {lines[index][1]!r}")
        key, rest = split
        index += 1
        if rest == "|":
            mapping[key], index = _collect_block_scalar(lines, index, base_indent)
        elif rest == "":
            if index < len(lines) and lines[index][0] > base_indent:
                mapping[key], index = _parse_yaml_block(lines, index, lines[index][0])
            else:
                mapping[key] = {}
        elif rest == "{}":
            mapping[key] = {}
        else:
            mapping[key] = _parse_yaml_scalar(rest)
    return mapping, index


def load_workflow(path: Path) -> dict:
    raw_lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        cleaned = _strip_yaml_comment(raw)
        if cleaned.strip():
            indent = len(cleaned) - len(cleaned.lstrip(" "))
            raw_lines.append((indent, cleaned.strip()))
    parsed, next_index = _parse_yaml_block(raw_lines, 0, raw_lines[0][0])
    if next_index != len(raw_lines):
        raise ValueError(f"未処理の YAML 行が残りました: {raw_lines[next_index]}")
    return parsed


def _lock_sync_command(workflow: dict, job_id: str) -> str:
    for step in workflow["jobs"][job_id]["steps"]:
        if step.get("name") == "Verify dependency lock sync":
            command = step["run"]
            prefix = "python -c "
            if command.startswith(prefix) and len(command) > len(prefix) + 2:
                quoted = command[len(prefix):]
                if quoted[0] == quoted[-1] and quoted[0] in ('"', "'"):
                    return quoted[1:-1]
            return command
    raise AssertionError(f"{job_id} に Verify dependency lock sync ステップがありません")


class WorkflowAndSecretsConfigTests(unittest.TestCase):
    def setUp(self):
        self.workflow = load_workflow(WORKFLOW_PATH)

    def test_primary_test_job_name_and_steps_preserved(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(self.workflow["on"]), {"push", "pull_request", "workflow_call"})

        test_job = self.workflow["jobs"]["test"]
        self.assertEqual(test_job["runs-on"], "ubuntu-latest")
        # ブランチ保護ルールセットの required status check 名 "test" を維持する。
        self.assertEqual(test_job.get("name", "test"), "test")

        runs = [step.get("run", "") for step in test_job["steps"] if "run" in step]
        names = [step.get("name", "") for step in test_job["steps"]]
        self.assertIn("Validate update notes", names)
        self.assertIn("Verify dependency lock sync", names)
        self.assertIn("Audit dependencies", names)
        self.assertIn("python scripts/check_update_notes.py --ci", runs)
        self.assertIn("python -m pip install --require-hashes -r requirements.lock", runs)
        self.assertIn("python -m playwright install --with-deps chromium", runs)
        self.assertIn("python -m unittest discover -s tests", runs)
        self.assertTrue(any("pip-audit" in run and "--require-hashes" in run for run in runs))

    def test_macos_job_runs_real_environment_verification(self):
        macos_job = self.workflow["jobs"]["macos"]
        self.assertEqual(macos_job["runs-on"], "macos-latest")

        steps = macos_job["steps"]
        checkout = steps[0]
        setup_python = steps[1]
        self.assertEqual(checkout["uses"], "actions/checkout@11d5960a326750d5838078e36cf38b85af677262")
        self.assertEqual(checkout["with"], {"persist-credentials": False, "fetch-depth": 0})
        self.assertEqual(setup_python["uses"], "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065")
        self.assertEqual(setup_python["with"], {"python-version": "3.11"})

        names = [step.get("name", "") for step in steps]
        runs = [step.get("run", "") for step in steps if "run" in step]
        self.assertIn("Validate update notes", names)
        self.assertIn("Verify dependency lock sync", names)
        self.assertIn("python scripts/check_update_notes.py --ci", runs)
        self.assertIn("python -m pip install --require-hashes -r requirements.lock", runs)
        self.assertIn("python -m playwright install chromium", runs)
        self.assertFalse(any("--with-deps" in run for run in runs))
        self.assertIn("python -m unittest discover -s tests", runs)
        self.assertEqual(_lock_sync_command(self.workflow, "macos"), _lock_sync_command(self.workflow, "test"))

    def test_secret_scan_job_uses_sha_pinned_gitleaks(self):
        scan_job = self.workflow["jobs"]["secret-scan"]
        self.assertEqual(scan_job["runs-on"], "ubuntu-latest")
        self.assertEqual(scan_job["permissions"], {"contents": "read"})

        steps = scan_job["steps"]
        self.assertEqual(steps[0]["uses"], "actions/checkout@11d5960a326750d5838078e36cf38b85af677262")
        self.assertEqual(steps[0]["with"], {"persist-credentials": False, "fetch-depth": 0})
        self.assertEqual(steps[1]["uses"], "gitleaks/gitleaks-action@e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e")
        self.assertEqual(steps[1]["env"]["GITHUB_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")
        self.assertEqual(steps[1]["env"]["GITLEAKS_CONFIG"], ".gitleaks.toml")

    def test_all_third_party_actions_are_sha_pinned(self):
        for path in (WORKFLOW_PATH, RELEASE_WORKFLOW_PATH):
            workflow = load_workflow(path)
            for job_id, job in workflow["jobs"].items():
                for step in job.get("steps", []):
                    uses = step.get("uses")
                    if uses and not uses.startswith("./"):
                        with self.subTest(workflow=path.name, job=job_id, uses=uses):
                            self.assertRegex(uses, SHA_REF_PATTERN)

    def test_gitleaks_config_allowlists_only_synthetic_http_test_fixture(self):
        config = tomllib.loads(GITLEAKS_CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertTrue(config["extend"]["useDefault"])
        allowlists = config["allowlists"]
        self.assertEqual(len(allowlists), 1)
        entry = allowlists[0]
        self.assertEqual(entry["condition"], "AND")
        self.assertEqual(entry["regexTarget"], "line")
        self.assertTrue(any(r"tests/test_updater_http\.py" in pattern for pattern in entry["paths"]))

        path_patterns = [re.compile(pattern) for pattern in entry["paths"]]
        line_patterns = [re.compile(pattern) for pattern in entry["regexes"]]
        self.assertTrue(any(p.search("tests/test_updater_http.py") for p in path_patterns))
        self.assertFalse(any(p.search("src/updater.py") for p in path_patterns))

        target_fixture = '"https://' + 'user:pass' + '@github.com/file"'
        fixture_lines = [
            line for line in (ROOT / "tests/test_updater_http.py").read_text(encoding="utf-8").splitlines()
            if target_fixture in line
        ]
        self.assertEqual(len(fixture_lines), 1)
        self.assertTrue(any(p.search(fixture_lines[0]) for p in line_patterns))
        synthetic_real_looking = '"https://' + 'alice:real-secret' + '@example.com/file"'
        self.assertFalse(any(p.search(synthetic_real_looking) for p in line_patterns))


class LockfileSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        (self.workspace / "src").mkdir()
        for rel in ("requirements.txt", "requirements.lock", "src/__init__.py", "src/updater.py",
                    "src/dependencies.py", "src/dependency_lock.py"):
            shutil.copy2(ROOT / rel, self.workspace / rel)
        self.workflow = load_workflow(WORKFLOW_PATH)
        self.ci_script = _lock_sync_command(self.workflow, "test")

    def run_ci_sync_check(self, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-c", self.ci_script], cwd=cwd, capture_output=True, text=True, timeout=15)

    def test_current_repo_lockfiles_are_in_sync(self):
        spec = dependencies.read_spec(ROOT)
        self.assertEqual(spec["text"], (ROOT / "requirements.lock").read_text(encoding="utf-8"))
        result = self.run_ci_sync_check(ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_out_of_sync_requirements_lock_fails_ci_check(self):
        lock_file = self.workspace / "requirements.lock"
        lock_file.write_text(lock_file.read_text(encoding="utf-8") + "# drifted\n", encoding="utf-8")
        result = self.run_ci_sync_check(self.workspace)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AssertionError", result.stderr)

    def test_out_of_sync_dependency_lock_py_fails_ci_check(self):
        dep_lock = self.workspace / "src/dependency_lock.py"
        dep_lock.write_text(dep_lock.read_text(encoding="utf-8").replace("keyring==25.7.0", "keyring==25.6.0", 1), encoding="utf-8")
        result = self.run_ci_sync_check(self.workspace)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AssertionError", result.stderr)

    def test_out_of_sync_requirements_txt_raises_update_error(self):
        req_file = self.workspace / "requirements.txt"
        req_file.write_text(req_file.read_text(encoding="utf-8") + "# drifted\n", encoding="utf-8")
        with self.assertRaises(updater.UpdateError):
            dependencies.read_spec(self.workspace)
        result = self.run_ci_sync_check(self.workspace)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UpdateError", result.stderr)

    def test_generate_dependency_lock_resynchronizes_all_files(self):
        req_file = self.workspace / "requirements.txt"
        req_file.write_text(req_file.read_text(encoding="utf-8") + "# updated\n", encoding="utf-8")
        lock_file = self.workspace / "requirements.lock"
        lock_file.write_text(lock_file.read_text(encoding="utf-8") + "# updated\n", encoding="utf-8")
        generate_dependency_lock.generate(self.workspace)
        spec = dependencies.read_spec(self.workspace)
        self.assertEqual(spec["text"], lock_file.read_text(encoding="utf-8"))
        self.assertEqual(spec["lock_hash"], hashlib.sha256(spec["text"].encode("utf-8")).hexdigest())
        self.assertEqual(self.run_ci_sync_check(self.workspace).returncode, 0)


class MacOSCredentialsBoundaryTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS 実環境でのみ実行")
    def test_real_macos_keychain_backend_returns_macos_keyring(self):
        from keyring.backends.macOS import Keyring as MacOSKeyring

        backend = credentials.keychain_backend()
        self.assertIsInstance(backend, MacOSKeyring)

    def test_keychain_backend_accepts_macos_keyring_on_darwin(self):
        from keyring.backends.macOS import Keyring as MacOSKeyring

        fake_backend = MacOSKeyring()
        with patch.object(credentials.sys, "platform", "darwin"), \
                patch("keyring.get_keyring", return_value=fake_backend):
            self.assertIs(credentials.keychain_backend(), fake_backend)

    def test_keychain_backend_rejects_non_macos_keyring_on_darwin(self):
        with patch.object(credentials.sys, "platform", "darwin"), \
                patch("keyring.get_keyring", return_value=object()):
            with self.assertRaisesRegex(credentials.CredentialError, "macOS Keychain を利用できません"):
                credentials.keychain_backend()

    def test_keychain_backend_rejects_non_darwin_platforms(self):
        for platform_name in ("linux", "win32"):
            with self.subTest(platform=platform_name), \
                    patch.object(credentials.sys, "platform", platform_name):
                with self.assertRaisesRegex(credentials.CredentialError, "macOS Keychain が必要です"):
                    credentials.keychain_backend()

    def test_keychain_backend_reports_missing_keyring_package(self):
        real_import = __import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "keyring" or name.startswith("keyring."):
                raise ImportError("no keyring")
            return real_import(name, globals, locals, fromlist, level)

        with patch.object(credentials.sys, "platform", "darwin"), \
                patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaisesRegex(credentials.CredentialError, "keyring が見つかりません"):
                credentials.keychain_backend()


class MacOSSchedulerLaunchAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.base = root / "app"
        self.base.mkdir()
        (self.base / "att").write_text("#!/bin/sh\n", encoding="utf-8")
        today = date.today()
        self.schedule = {
            "active_from": today - timedelta(days=1),
            "active_until": today + timedelta(days=30),
            "blackout_dates": frozenset(),
            "courses": [{
                "weekday": "mon",
                "start_time": "10:40",
                "room": "731",
                "route": "7301",
                "course_name": "テスト授業",
                "enabled": True,
                "register_time": "",
                "register_until": "",
            }],
        }
        home_patch = patch.object(Path, "home", return_value=self.home)
        env_patch = patch.dict(os.environ, {"HOME": str(self.home)})
        home_patch.start()
        env_patch.start()
        self.addCleanup(home_patch.stop)
        self.addCleanup(env_patch.stop)

    def test_install_agent_writes_valid_plist_and_passes_plutil_lint(self):
        with patch.object(scheduler.subprocess, "run", return_value=Mock(returncode=0)) as launchctl:
            plist_path = scheduler.install_agent(self.base, self.schedule)

        self.assertTrue(plist_path.is_relative_to(self.home))
        self.assertEqual(plist_path, self.home / "Library/LaunchAgents" / f"{scheduler.LABEL}.plist")
        self.assertEqual(plist_path.stat().st_mode & 0o777, 0o600)

        plist = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(plist["Label"], scheduler.LABEL)
        self.assertEqual(plist["ProgramArguments"], [str(self.base / "att"), "--scheduled"])
        self.assertEqual(plist["WorkingDirectory"], str(self.base))
        self.assertEqual(plist["StartCalendarInterval"], [{"Weekday": 1, "Hour": 10, "Minute": 45}])
        self.assertFalse(plist["RunAtLoad"])
        self.assertEqual(plist["Umask"], 0o077)

        launchctl.assert_called_once_with(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
            capture_output=True,
            check=False,
        )

        plutil = shutil.which("plutil")
        if sys.platform == "darwin":
            self.assertIsNotNone(plutil)
        if plutil:
            lint = subprocess.run([plutil, "-lint", str(plist_path)], capture_output=True, text=True, check=False)
            self.assertEqual(lint.returncode, 0, lint.stderr or lint.stdout)

    def test_install_agent_replaces_owned_plist_with_bootout_then_bootstrap(self):
        with patch.object(scheduler.subprocess, "run", return_value=Mock(returncode=0)):
            scheduler.install_agent(self.base, self.schedule)

        self.schedule["courses"][0]["start_time"] = "13:10"
        with patch.object(scheduler.subprocess, "run", return_value=Mock(returncode=0)) as launchctl:
            plist_path = scheduler.install_agent(self.base, self.schedule)

        self.assertEqual(launchctl.call_count, 2)
        self.assertEqual(launchctl.call_args_list[0].args[0][:2], ["launchctl", "bootout"])
        self.assertEqual(launchctl.call_args_list[1].args[0][:2], ["launchctl", "bootstrap"])
        plist = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(plist["StartCalendarInterval"], [{"Weekday": 1, "Hour": 13, "Minute": 15}])

    def test_install_agent_refuses_foreign_or_corrupt_plist(self):
        target = self.home / "Library/LaunchAgents" / f"{scheduler.LABEL}.plist"
        target.parent.mkdir(parents=True, exist_ok=True)
        foreign_cases = [
            plistlib.dumps({"Label": scheduler.LABEL, "ProgramArguments": ["/other/att", "--scheduled"]}),
            b"not-a-valid-plist",
        ]
        for payload in foreign_cases:
            with self.subTest(payload=payload):
                target.write_bytes(payload)
                with patch.object(scheduler.subprocess, "run") as launchctl, \
                        self.assertRaisesRegex(scheduler.ScheduleError, "上書きしません"):
                    scheduler.install_agent(self.base, self.schedule)
                launchctl.assert_not_called()
                self.assertEqual(target.read_bytes(), payload)

    def test_install_agent_raises_on_launchctl_failure_or_invalid_schedule(self):
        with patch.object(scheduler.subprocess, "run", return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(scheduler.ScheduleError, "LaunchAgentを開始できません"):
                scheduler.install_agent(self.base, self.schedule)

        expired = dict(self.schedule, active_until=date.today() - timedelta(days=1))
        with patch.object(scheduler.subprocess, "run") as launchctl, \
                self.assertRaisesRegex(scheduler.ScheduleError, "終了しています"):
            scheduler.install_agent(self.base, expired)
        launchctl.assert_not_called()

        disabled_course = dict(self.schedule["courses"][0], enabled=False)
        no_enabled = dict(self.schedule, courses=[disabled_course])
        with patch.object(scheduler.subprocess, "run") as launchctl, \
                self.assertRaisesRegex(scheduler.ScheduleError, "有効な授業を設定"):
            scheduler.install_agent(self.base, no_enabled)
        launchctl.assert_not_called()

    def test_remove_agent_and_agent_installed_respect_ownership(self):
        self.assertFalse(scheduler.agent_installed(self.base))
        self.assertFalse(scheduler.remove_agent(self.base))

        with patch.object(scheduler.subprocess, "run", return_value=Mock(returncode=0)):
            plist_path = scheduler.install_agent(self.base, self.schedule)

        with patch.object(scheduler.subprocess, "run", side_effect=[Mock(returncode=0), Mock(returncode=1)]):
            self.assertTrue(scheduler.agent_installed(self.base))
            self.assertFalse(scheduler.agent_installed(self.base))

        with patch.object(scheduler.subprocess, "run", return_value=Mock(returncode=0)) as launchctl:
            self.assertTrue(scheduler.remove_agent(self.base))
        launchctl.assert_called_once_with(["launchctl", "bootout", f"gui/{os.getuid()}/{scheduler.LABEL}"],
                                          capture_output=True, check=False)
        self.assertFalse(plist_path.exists())

        plist_path.write_bytes(plistlib.dumps({"Label": scheduler.LABEL, "ProgramArguments": ["/other/att", "--scheduled"]}))
        with patch.object(scheduler.subprocess, "run") as launchctl, \
                self.assertRaisesRegex(scheduler.ScheduleError, "削除しません"):
            scheduler.remove_agent(self.base)
        launchctl.assert_not_called()
        self.assertTrue(plist_path.exists())


class MacOSInstallerRealUnameTests(unittest.TestCase):
    def _prepare_installer_workspace(self) -> tuple[Path, Path, Path, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        project = base / "tool with spaces"
        project.mkdir()
        for name in ("install.sh", "att", "config.example.json", "requirements.txt", "requirements.lock"):
            shutil.copy2(ROOT / name, project / name)
        home = base / "home with spaces"
        home.mkdir()
        mock_bin = base / "mock-bin"
        mock_bin.mkdir()
        log = base / "calls.log"
        python3_mock = mock_bin / "python3"
        python3_mock.write_text(
            '#!/bin/bash\n'
            'printf \'%s\\n\' "$*" >> "$INSTALL_LOG"\n'
            'if [ "$1" = -m ] && [ "$2" = venv ]; then\n'
            '    mkdir -p "$3/bin"\n'
            '    cp "$0" "$3/bin/python"\n'
            'fi\n',
            encoding="utf-8",
        )
        python3_mock.chmod(0o755)
        return base, project, home, mock_bin

    @unittest.skipUnless(sys.platform == "darwin", "macOS 実環境でのみ実行")
    def test_install_sh_uses_real_system_uname_on_macos(self):
        base, project, home, mock_bin = self._prepare_installer_workspace()
        log = base / "calls.log"
        env = dict(os.environ, HOME=str(home), SHELL="/bin/zsh",
                   PATH=f"{mock_bin}:/usr/bin:/bin", INSTALL_LOG=str(log))
        env.pop("ZDOTDIR", None)

        resolved_uname = shutil.which("uname", path=env["PATH"])
        self.assertIsNotNone(resolved_uname)
        self.assertFalse(Path(resolved_uname).is_relative_to(mock_bin))

        result = subprocess.run(["/bin/bash", str(project / "install.sh")],
                                env=env, cwd=base, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((home / ".local/bin/att").resolve(), (project / "att").resolve())
        self.assertEqual((project / "config.json").stat().st_mode & 0o777, 0o600)
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', (home / ".zshrc").read_text(encoding="utf-8"))

        calls = log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(any(line.startswith("-m pip install --require-hashes -r ") for line in calls))
        self.assertIn("-m playwright install chromium", calls)
        self.assertFalse(any("--with-deps" in line for line in calls))

    def test_install_sh_rejects_non_darwin_os(self):
        base, project, home, mock_bin = self._prepare_installer_workspace()
        log = base / "calls.log"
        uname_mock = mock_bin / "uname"
        uname_mock.write_text("#!/bin/bash\necho Linux\n", encoding="utf-8")
        uname_mock.chmod(0o755)
        env = dict(os.environ, HOME=str(home), SHELL="/bin/zsh",
                   PATH=f"{mock_bin}:/usr/bin:/bin", INSTALL_LOG=str(log))

        result = subprocess.run(["/bin/bash", str(project / "install.sh")],
                                env=env, cwd=base, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("macOS が必要です", result.stderr)
        self.assertFalse(log.exists())
        self.assertFalse((home / ".local/bin/att").exists())


if __name__ == "__main__":
    unittest.main()
