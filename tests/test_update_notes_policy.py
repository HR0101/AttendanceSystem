"""CIが更新内容の欠落と、既存ファイルを放置したPRを拒否することを確認する。"""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_release, check_update_notes as policy
from src.release_notes import read_notes, validate_notes
from src.version import VERSION
from test_update_notifications import notes


class UpdateNotesPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "updates").mkdir()
        self.path = self.root / "updates" / f"{VERSION}.md"
        self.path.write_text(notes(VERSION))

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "test")
        return self.git("rev-parse", "HEAD")

    def test_current_version_requires_matching_file_and_real_sections(self):
        self.assertEqual(policy.check(self.root, VERSION), f"updates/{VERSION}.md")
        for text in ("", notes("9.9.9"), f"# v{VERSION}\n", notes(VERSION).replace("更新通知を追加しました。", "TODO"),
                     notes(VERSION).replace("- att update で更新してください。", "")):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises(ValueError):
                    policy.check(self.root, VERSION)
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "更新内容を記載"):
            policy.check(self.root, VERSION)

    def test_pr_must_change_notes_even_when_version_file_already_exists(self):
        self.git("init", "-q")
        base = self.commit()
        (self.root / "main.py").write_text("# new behavior\n")
        self.commit()
        with self.assertRaisesRegex(ValueError, "追記してください"):
            policy.check(self.root, VERSION, base)
        self.path.write_text(notes(VERSION) + "\n- 操作の説明を追加しました。\n")
        self.commit()
        self.assertEqual(policy.check(self.root, VERSION, base), f"updates/{VERSION}.md")

    def test_ci_uses_pr_base_and_checks_release_tag_version(self):
        event = self.root / "event.json"
        event.write_text(json.dumps({"pull_request": {"base": {"sha": "a" * 40}}}))
        self.assertEqual(policy.ci_base(self.root, {"GITHUB_EVENT_PATH": str(event)}), "a" * 40)
        self.assertIsNone(policy.ci_base(self.root, {"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": f"v{VERSION}"}))
        with self.assertRaisesRegex(ValueError, "タグと VERSION"):
            policy.ci_base(self.root, {"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "v0.0.1"})

    def test_ci_feature_push_compares_entire_branch_to_default_branch(self):
        self.git("init", "-q")
        base = self.commit()
        self.git("update-ref", "refs/remotes/origin/main", base)
        self.git("checkout", "-qb", "feature")
        event = self.root / "event.json"
        event.write_text(json.dumps({"repository": {"default_branch": "main"}, "before": base}))
        (self.root / "app.py").write_text("# change\n")
        self.commit()
        self.assertEqual(policy.ci_base(self.root, {"GITHUB_EVENT_PATH": str(event), "GITHUB_REF_NAME": "feature"}), base)
        self.assertEqual(policy.ci_base(self.root, {"GITHUB_EVENT_PATH": str(event), "GITHUB_REF_NAME": "main"}), base)

    def test_build_refuses_release_without_notes(self):
        self.path.unlink()
        with patch.object(build_release, "read_notes", side_effect=lambda root, version: read_notes(self.root, version)):
            with self.assertRaisesRegex(ValueError, "更新内容を記載"):
                build_release.build(f"v{VERSION}", self.root / "archive.zip")
        self.assertFalse((self.root / "archive.zip").exists())

    def test_ci_command_exits_nonzero_until_notes_are_present(self):
        (self.root / "scripts").mkdir()
        (self.root / "src").mkdir()
        for name in ("scripts/check_update_notes.py", "src/__init__.py", "src/release_notes.py", "src/version.py"):
            shutil.copy2(policy.ROOT / name, self.root / name)
        self.path.unlink()
        command = [sys.executable, str(self.root / "scripts/check_update_notes.py")]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"updates/{VERSION}.md", result.stderr)
        self.path.write_text(notes(VERSION))
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
