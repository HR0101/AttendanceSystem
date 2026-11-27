"""バージョン引き上げチェックと PR Bot コメント投稿の検証テスト。"""

from email.message import Message
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import check_version_bump as policy
from scripts import post_pr_version_comment as bot
from test_update_notifications import notes


class _FakeHTTPResponse:
    def __init__(self, payload, *, link: str = ""):
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = Message()
        if link:
            self.headers["Link"] = link

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class VersionBumpPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "src").mkdir()
        (self.root / "Formula").mkdir()
        (self.root / "updates").mkdir()
        (self.root / "src/__init__.py").write_text("", encoding="utf-8")
        shutil.copy2(policy.ROOT / "src/release_notes.py", self.root / "src/release_notes.py")
        self._write_state("0.3.3")
        self.git("init", "-q")
        self.base = self.commit("Initial v0.3.3")
        self.git(
            "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid",
            "tag", "-a", "v0.3.3", "-m", "Release v0.3.3",
        )

    def _write_state(self, version: str, *, formula_version: str | None = None, write_notes: bool = True):
        (self.root / "src/version.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
        fver = formula_version if formula_version is not None else version
        (self.root / "Formula/attendance.rb").write_text(
            f'class Attendance < Formula\n  version "{fver}"\nend\n',
            encoding="utf-8",
        )
        if write_notes:
            (self.root / "updates" / f"{version}.md").write_text(notes(version), encoding="utf-8")

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def commit(self, message: str = "test") -> str:
        self.git("add", ".")
        self.git(
            "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid",
            "commit", "-qm", message,
        )
        return self.git("rev-parse", "HEAD")

    def test_rejects_same_or_older_version_than_latest_tag(self):
        for bad_version in ("0.3.3", "0.3.2"):
            with self.subTest(version=bad_version):
                self._write_state(bad_version)
                result = policy.evaluate_version_policy(self.root, mode="pull_request", base=self.base)
                self.assertFalse(result["ok"])
                bump_check = next(c for c in result["checks"] if c["key"] == "version_bump")
                self.assertFalse(bump_check["ok"])
                self.assertIn("0.3.4", bump_check["detail"])

    def test_accepts_valid_patch_minor_major_bumps(self):
        for next_version in ("0.3.4", "0.4.0", "1.0.0"):
            with self.subTest(version=next_version):
                self._write_state(next_version)
                result = policy.evaluate_version_policy(
                    self.root,
                    mode="pull_request",
                    base=self.base,
                    include_notes=True,
                )
                self.assertTrue(result["ok"], result)

    def test_rejects_skipped_versions_and_invalid_formats(self):
        for bad_version in ("0.3.5", "0.4.1", "2.0.0", "0.3", "0.03.4"):
            with self.subTest(version=bad_version):
                self._write_state(bad_version, formula_version="0.3.4", write_notes=False)
                result = policy.evaluate_version_policy(self.root, mode="pull_request", base=self.base)
                self.assertFalse(result["ok"])
                bump_check = next(c for c in result["checks"] if c["key"] == "version_bump")
                self.assertFalse(bump_check["ok"])

    def test_multi_pr_accumulation_on_untagged_next_version(self):
        self._write_state("0.3.4")
        next_base = self.commit("PR 1 bumps to 0.3.4")

        # まだ v0.3.4 タグがない状態なら 0.3.4 維持および 0.4.0 引き上げは許可する
        (self.root / "updates/0.3.4.md").write_text(notes("0.3.4") + "\n- 追記\n", encoding="utf-8")
        same_next = policy.evaluate_version_policy(self.root, mode="pull_request", base=next_base, include_notes=True)
        self.assertTrue(same_next["ok"], same_next)

        self._write_state("0.4.0")
        minor_up = policy.evaluate_version_policy(self.root, mode="pull_request", base=next_base, include_notes=True)
        self.assertTrue(minor_up["ok"], minor_up)

        # ベースが既に 0.4.0 の状態から 0.3.4 への引き下げは拒否する
        higher_base = self.commit("PR 2 bumps to 0.4.0")
        self._write_state("0.3.4")
        downgraded = policy.evaluate_version_policy(self.root, mode="pull_request", base=higher_base)
        self.assertFalse(downgraded["ok"])
        bump_check = next(c for c in downgraded["checks"] if c["key"] == "version_bump")
        self.assertIn("ベースブランチ", bump_check["detail"])

    def test_rejects_formula_version_mismatch(self):
        self._write_state("0.3.4", formula_version="0.3.3")
        result = policy.evaluate_version_policy(self.root, mode="pull_request", base=self.base)
        self.assertFalse(result["ok"])
        formula_check = next(c for c in result["checks"] if c["key"] == "formula_sync")
        self.assertFalse(formula_check["ok"])
        self.assertIn("generate_homebrew_formula.py", formula_check["detail"])

    def test_ci_tag_push_mode_validates_exact_tag_match(self):
        event = self.root / "event.json"
        event.write_text("{}", encoding="utf-8")
        ctx_ok = policy.resolve_ci_context(self.root, {"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "v0.3.3"})
        self.assertEqual(ctx_ok["mode"], "tag")
        res_ok = policy.evaluate_version_policy(self.root, **ctx_ok)
        self.assertTrue(res_ok["ok"])

        ctx_bad = policy.resolve_ci_context(self.root, {"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "v0.3.4"})
        res_bad = policy.evaluate_version_policy(self.root, **ctx_bad)
        self.assertFalse(res_bad["ok"])

    def test_ci_push_mode_allows_tagged_head_and_rejects_untagged_unchanged_commit(self):
        event = self.root / "event.json"
        event.write_text(json.dumps({"repository": {"default_branch": "main"}, "before": self.base}), encoding="utf-8")
        ctx = policy.resolve_ci_context(
            self.root,
            {"GITHUB_EVENT_PATH": str(event), "GITHUB_REF_NAME": "main"},
        )
        self.assertTrue(ctx["allow_tagged_head"])
        # HEAD に v0.3.3 タグが付いている状態は成功する
        self.assertTrue(policy.evaluate_version_policy(self.root, **ctx)["ok"])

        # タグなしの新しいコミットで VERSION 据え置きなら失敗する
        (self.root / "app.py").write_text("# change\n", encoding="utf-8")
        self.commit("Unbumped change")
        res = policy.evaluate_version_policy(self.root, **ctx)
        self.assertFalse(res["ok"])

    def test_cli_execution_and_markdown_report(self):
        (self.root / "scripts").mkdir()
        shutil.copy2(policy.ROOT / "scripts/check_update_notes.py", self.root / "scripts/check_update_notes.py")
        shutil.copy2(policy.ROOT / "scripts/check_version_bump.py", self.root / "scripts/check_version_bump.py")
        script = str(self.root / "scripts/check_version_bump.py")
        report_file = self.root / "report.md"

        # タグ付き HEAD では --allow-tagged-head ありのみ成功し、通常時は失敗して案内レポートを出す
        ok_tagged = subprocess.run(
            [sys.executable, script, "--allow-tagged-head"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(ok_tagged.returncode, 0, ok_tagged.stderr)

        ng_res = subprocess.run(
            [sys.executable, script, "--include-notes", "--markdown-report", str(report_file)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(ng_res.returncode, 1)
        report_ng = report_file.read_text(encoding="utf-8")
        self.assertTrue(report_ng.startswith(policy.BOT_COMMENT_MARKER))
        self.assertIn("❌", report_ng)
        self.assertIn("`0.3.4`", report_ng)
        self.assertIn("`0.4.0`", report_ng)
        self.assertIn("`1.0.0`", report_ng)
        self.assertIn("generate_homebrew_formula.py", report_ng)

        # バージョン引き上げ + Formula 同期 + 更新ノート作成後は終了コード 0
        self._write_state("0.3.4")
        ok_res = subprocess.run(
            [sys.executable, script, "--base", self.base, "--include-notes", "--markdown-report", str(report_file)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(ok_res.returncode, 0, ok_res.stderr)
        report_ok = report_file.read_text(encoding="utf-8")
        self.assertTrue(report_ok.startswith(policy.BOT_COMMENT_MARKER))
        self.assertIn("✅", report_ok)
        self.assertIn("updates/0.3.4.md", report_ok)

        # --tag 単独指定でリリースタグ検証
        tag_ok = subprocess.run(
            [sys.executable, script, "--tag", "v0.3.4"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(tag_ok.returncode, 0, tag_ok.stderr)


class PostPRVersionCommentTests(unittest.TestCase):
    def test_creates_new_comment_when_none_exists(self):
        requests = []

        def fake_urlopen(req, timeout=20):
            requests.append((req.get_method(), req.full_url, req.data))
            if req.get_method() == "GET":
                return _FakeHTTPResponse([{"id": 10, "body": "unrelated"}])
            return _FakeHTTPResponse({"id": 99})

        with patch.object(bot, "urlopen", side_effect=fake_urlopen):
            res = bot.upsert_pr_comment(
                api_url="https://api.github.com",
                repo="HR0101/AttendanceSystem",
                pr_number="65",
                token="ghp_test_token",
                body=f"{policy.BOT_COMMENT_MARKER}\nhello",
            )
        self.assertEqual(res, "created:99")
        self.assertEqual([r[0] for r in requests], ["GET", "POST"])
        self.assertTrue(requests[1][1].endswith("/repos/HR0101/AttendanceSystem/issues/65/comments"))

    def test_updates_existing_sticky_comment_across_pages(self):
        requests = []

        def fake_urlopen(req, timeout=20):
            requests.append((req.get_method(), req.full_url))
            if req.get_method() == "GET" and "page=2" not in req.full_url:
                return _FakeHTTPResponse(
                    [{"id": 11, "body": "other"}],
                    link='<https://api.github.com/repos/HR0101/AttendanceSystem/issues/65/comments?page=2>; rel="next"',
                )
            if req.get_method() == "GET":
                return _FakeHTTPResponse([{"id": 77, "body": f"{policy.BOT_COMMENT_MARKER}\nold"}])
            return _FakeHTTPResponse({"id": 77})

        with patch.object(bot, "urlopen", side_effect=fake_urlopen):
            res = bot.upsert_pr_comment(
                api_url="https://api.github.com",
                repo="HR0101/AttendanceSystem",
                pr_number="65",
                token="ghp_test_token",
                body=f"{policy.BOT_COMMENT_MARKER}\nnew",
            )
        self.assertEqual(res, "updated:77")
        self.assertEqual(requests[-1], ("PATCH", "https://api.github.com/repos/HR0101/AttendanceSystem/issues/comments/77"))

    def test_main_skips_gracefully_on_403_fork_pr(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.md"
            report.write_text(f"{policy.BOT_COMMENT_MARKER}\nbody\n", encoding="utf-8")
            err = HTTPError(
                url="https://api.github.com/repos/HR0101/AttendanceSystem/issues/65/comments",
                code=403,
                msg="Resource not accessible by integration",
                hdrs=Message(),
                fp=io.BytesIO(b""),
            )
            env = {
                "GH_TOKEN": "token-value",
                "REPOSITORY": "HR0101/AttendanceSystem",
                "PR_NUMBER": "65",
                "REPORT_PATH": str(report),
                "GITHUB_API_URL": "https://api.github.com",
            }
            with patch.dict(os.environ, env, clear=False), \
                    patch.object(bot, "upsert_pr_comment", side_effect=err):
                self.assertEqual(bot.main(), 0)

    def test_rejects_invalid_api_url_repo_or_pr_number(self):
        with self.assertRaises(ValueError):
            bot.upsert_pr_comment(
                api_url="http://api.github.com",
                repo="HR0101/AttendanceSystem",
                pr_number="65",
                token="t",
                body="b",
            )
        with self.assertRaises(ValueError):
            bot.upsert_pr_comment(
                api_url="https://api.github.com",
                repo="invalid-repo",
                pr_number="65",
                token="t",
                body="b",
            )
        with self.assertRaises(ValueError):
            bot.upsert_pr_comment(
                api_url="https://api.github.com",
                repo="HR0101/AttendanceSystem",
                pr_number="abc",
                token="t",
                body="b",
            )


if __name__ == "__main__":
    unittest.main()
