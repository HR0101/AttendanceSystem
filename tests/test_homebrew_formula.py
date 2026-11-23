"""Homebrew Formula 生成スクリプトとリリースワークフロー整合性の検証テスト。"""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts import generate_homebrew_formula as gen
from src.version import VERSION


ROOT = Path(__file__).resolve().parents[1]


class HomebrewFormulaTests(unittest.TestCase):
    def test_checked_in_formula_matches_generator_and_lockfile(self):
        gen.verify_wheels_against_lock(ROOT)
        result = gen.main(["--check"])
        self.assertEqual(result, 0)

        formula = (ROOT / "Formula/attendance.rb").read_text(encoding="utf-8")
        self.assertIn(f'version "{VERSION}"', formula)
        self.assertIn('depends_on :macos', formula)
        self.assertIn('depends_on "python@3.12"', formula)
        self.assertIn('ATTENDANCE_MANAGED_BY="homebrew"', formula)
        self.assertIn('ATTENDANCE_HOME="${ATTENDANCE_HOME:-$HOME/AttendanceSystem}"', formula)
        self.assertIn('bin.install_symlink "att" => "attendance"', formula)
        self.assertIn("post_install_steps do", formula)
        self.assertIn("using: :nounzip", formula)
        self.assertNotIn("0o", formula)

    def test_playwright_wheels_are_native_per_architecture(self):
        # universal2 wheel は同梱 node が x86_64 のみのため Apple Silicon で Rosetta 2 が必要になる
        playwright_specs = [spec for spec in gen.MACOS_WHEEL_SPECS if spec[0] == "playwright"]
        arches = {spec[4]: spec[2] for spec in playwright_specs}
        self.assertEqual(set(arches), {gen.ARCH_ARM, gen.ARCH_INTEL})
        self.assertTrue(arches[gen.ARCH_ARM].endswith("_arm64.whl"))
        self.assertTrue(arches[gen.ARCH_INTEL].endswith("_x86_64.whl"))

        formula = (ROOT / "Formula/attendance.rb").read_text(encoding="utf-8")
        self.assertNotIn("playwright-1.64.1-py3-none-macosx_11_0_universal2.whl", formula)
        self.assertIn('  resource "playwright" do\n    on_arm do\n', formula)
        self.assertIn("    on_intel do\n", formula)

    def test_verify_wheels_rejects_unknown_architecture(self):
        original = gen.MACOS_WHEEL_SPECS
        broken = tuple((*spec[:4], "riscv") if spec[0] == "playwright" else spec for spec in original)
        gen.MACOS_WHEEL_SPECS = broken
        try:
            with self.assertRaises(ValueError):
                gen.verify_wheels_against_lock(ROOT)
        finally:
            gen.MACOS_WHEEL_SPECS = original

    def test_render_formula_with_release_tag_and_sha256(self):
        digest = "a" * 64
        rendered = gen.render_formula(ROOT, tag=f"v{VERSION}", tarball_sha256=digest)
        self.assertIn(f'url "https://github.com/HR0101/AttendanceSystem/archive/refs/tags/v{VERSION}.tar.gz"', rendered)
        self.assertIn(f'sha256 "{digest}"', rendered)
        self.assertNotIn(f'  version "{VERSION}"', rendered)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "attendance.rb"
            self.assertEqual(gen.main(["--tag", f"v{VERSION}", "--sha256", digest, "--output", str(out)]), 0)
            self.assertEqual(out.read_text(encoding="utf-8"), rendered)
            if shutil.which("ruby"):
                check = subprocess.run(["ruby", "-c", str(out)], capture_output=True, text=True, check=False)
                self.assertEqual(check.returncode, 0, check.stderr)

    def test_render_formula_rejects_invalid_tag_or_sha256(self):
        with self.assertRaises(ValueError):
            gen.render_formula(ROOT, tag="v0.0.1", tarball_sha256="a" * 64)
        with self.assertRaises(ValueError):
            gen.render_formula(ROOT, tag=f"v{VERSION}", tarball_sha256="not-hex")

    def test_formula_ruby_syntax_valid_when_ruby_available(self):
        if not shutil.which("ruby"):
            self.skipTest("ruby がインストールされていない環境のため構文チェックを省略")
        result = subprocess.run(
            ["ruby", "-c", str(ROOT / "Formula/attendance.rb")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_workflow_generates_attaches_and_syncs_homebrew_formula(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/generate_homebrew_formula.py", workflow)
        self.assertIn("dist/attendance.rb", workflow)
        self.assertIn("HOMEBREW_TAP_TOKEN", workflow)
        self.assertIn("homebrew-tap", workflow)


if __name__ == "__main__":
    unittest.main()
