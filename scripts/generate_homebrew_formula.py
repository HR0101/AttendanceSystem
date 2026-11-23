"""requirements.lock に整合する Homebrew Formula (Formula/attendance.rb) を生成・検証する。"""

import argparse
import hashlib
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "HR0101/AttendanceSystem"

# macOS + Homebrew python@3.12 用のハッシュ固定 wheel 一覧（requirements.lock と一致必須）。
MACOS_WHEEL_SPECS = (
    (
        "greenlet",
        "3.5.6",
        "https://files.pythonhosted.org/packages/72/18/3fc6d951466ae9a2a688edcddde3b2e388da0a8244e0caf7117bbeb0eb95/greenlet-3.5.6-cp312-cp312-macosx_11_0_universal2.whl",
        "a5876d0a60355af98d535c47f6cd6eb0f8a432396dab26845d380b92f8412422",
    ),
    (
        "jaraco-classes",
        "3.4.0",
        "https://files.pythonhosted.org/packages/7f/66/b15ce62552d84bbfcec9a4873ab79d993a1dd4edb922cbfccae192bd5b5f/jaraco.classes-3.4.0-py3-none-any.whl",
        "f662826b6bed8cace05e7ff873ce0f9283b5c924470fe664fff1c2f00f581790",
    ),
    (
        "jaraco-context",
        "6.1.2",
        "https://files.pythonhosted.org/packages/f2/58/bc8954bda5fcda97bd7c19be11b85f91973d67a706ed4a3aec33e7de22db/jaraco_context-6.1.2-py3-none-any.whl",
        "bf8150b79a2d5d91ae48629d8b427a8f7ba0e1097dd6202a9059f29a36379535",
    ),
    (
        "jaraco-functools",
        "4.6.0",
        "https://files.pythonhosted.org/packages/02/36/ecc85bc96c273dc8a11273ed4782272975e6338d4a3e9228621175edf0e3/jaraco_functools-4.6.0-py3-none-any.whl",
        "99e3dc0060c5cbe8fcd1cdb36258e2a65ca40f1566b2033b12abb1bb44dd3c30",
    ),
    (
        "keyring",
        "25.7.0",
        "https://files.pythonhosted.org/packages/81/db/e655086b7f3a705df045bf0933bdd9c2f79bb3c97bfef1384598bb79a217/keyring-25.7.0-py3-none-any.whl",
        "be4a0b195f149690c166e850609a477c532ddbfbaed96a404d4e43f8d5e2689f",
    ),
    (
        "more-itertools",
        "11.2.1",
        "https://files.pythonhosted.org/packages/5d/57/3c4b1c6f2e0cc29ba023802458ea3d2a996f87085559e4a5f2192d549532/more_itertools-11.2.1-py3-none-any.whl",
        "35a7377edd1dd6608dcb2cdf534ded55ea32d49448875ad042cd3f879fb1ded0",
    ),
    (
        "playwright",
        "1.64.1",
        "https://files.pythonhosted.org/packages/44/fa/468f313a56d33a6420705147c8d47bef558f087856edf38c5b13eef8aa6c/playwright-1.64.1-py3-none-macosx_11_0_universal2.whl",
        "6a6f99adad1ae40e6ee62efe89785db861ce7ddd57651521cb31894183cc4ae3",
    ),
    (
        "pyee",
        "14.0.0",
        "https://files.pythonhosted.org/packages/81/12/5347938b1f9a6453f0dbdfcc3e2388a1320ef9b9ec17fbefbc4ab647ea98/pyee-14.0.0-py3-none-any.whl",
        "3ac2d3229a9677f7de2c33d7f52fe25b638a46b19c413fea2edc8c6d0a644e4d",
    ),
    (
        "typing-extensions",
        "4.16.0",
        "https://files.pythonhosted.org/packages/49/d3/b8441a820a491ddfc024b0b0cf0393375b75ea13866d9c66727e54c2fc80/typing_extensions-4.16.0-py3-none-any.whl",
        "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
    ),
)


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock_entries(lock_text: str) -> list[tuple[str, str, str, set[str]]]:
    """requirements.lock から (package, version, marker, sha256_set) を抽出する。"""
    logical_lines, buf = [], []
    for raw in lock_text.splitlines():
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            buf.append(line[:-1].strip())
            continue
        buf.append(line)
        logical_lines.append(" ".join(p for p in buf if p))
        buf.clear()
    if buf:
        raise ValueError("requirements.lock の行継続が途中で終わっています。")

    parsed = []
    for entry in logical_lines:
        head, *hash_parts = entry.split("--hash=")
        spec_part, _, marker = head.partition(";")
        match = re.fullmatch(r"([A-Za-z0-9._-]+)==([A-Za-z0-9.+!_*-]+)", spec_part.strip())
        if not match:
            raise ValueError(f"requirements.lock のエントリを解釈できません: {entry}")
        hashes = set()
        for part in hash_parts:
            digest_match = re.match(r"sha256:([0-9a-f]{64})", part.strip())
            if not digest_match:
                raise ValueError(f"requirements.lock のハッシュが不正です: {part}")
            hashes.add(digest_match.group(1))
        if not hashes:
            raise ValueError(f"requirements.lock にハッシュがありません: {entry}")
        parsed.append((_normalize_name(match.group(1)), match.group(2), marker.strip(), hashes))
    return parsed


def verify_wheels_against_lock(root: Path) -> None:
    """Formula 用 wheel 一覧と requirements.lock の版・SHA-256 一致を検証する。"""
    lock_text = (root / "requirements.lock").read_text(encoding="utf-8")
    entries = parse_lock_entries(lock_text)
    by_pkg: dict[str, list[tuple[str, str, set[str]]]] = {}
    for pkg, ver, marker, hashes in entries:
        by_pkg.setdefault(pkg, []).append((ver, marker, hashes))

    for pkg, ver, url, sha256 in MACOS_WHEEL_SPECS:
        norm = _normalize_name(pkg)
        if not re.fullmatch(r"[0-9a-f]{64}", sha256) or not url.startswith("https://files.pythonhosted.org/packages/"):
            raise ValueError(f"wheel 定義が不正です: {pkg}")
        matches = [hashes for entry_ver, _, hashes in by_pkg.get(norm, []) if entry_ver == ver and sha256 in hashes]
        if not matches:
            raise ValueError(f"{pkg}=={ver} ({sha256}) が requirements.lock と一致しません。")


def _read_version(root: Path) -> str:
    text = (root / "src/version.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', text, re.MULTILINE)
    if not match:
        raise ValueError("src/version.py から VERSION を取得できません。")
    return match.group(1)


def render_formula(root: Path, *, tag: str | None = None, tarball_sha256: str | None = None) -> str:
    verify_wheels_against_lock(root)
    version = _read_version(root)
    if (tag is None) != (tarball_sha256 is None):
        raise ValueError("--tag と --sha256 は両方指定するか両方省略してください。")
    if tarball_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", tarball_sha256):
        raise ValueError("--sha256 は 64 桁の16進数にしてください。")
    if tag is not None and tag.removeprefix("v") != version:
        raise ValueError(f"タグ {tag} と src/version.py ({version}) が一致しません。")

    lines = [
        "# typed: false",
        "# frozen_string_literal: true",
        "",
        "class Attendance < Formula",
        '  desc "University attendance confirmation and registration CLI/TUI for macOS"',
        f'  homepage "https://github.com/{REPOSITORY}"',
    ]
    if tag and tarball_sha256:
        lines.extend([
            f'  url "https://github.com/{REPOSITORY}/archive/refs/tags/{tag}.tar.gz"',
            f'  sha256 "{tarball_sha256}"',
        ])
    else:
        lines.append(f'  version "{version}"')
    lines.extend([
        '  license "MIT"',
        f'  head "https://github.com/{REPOSITORY}.git", branch: "main"',
        "",
        "  depends_on :macos",
        '  depends_on "python@3.12"',
        "",
    ])

    for name, _, url, sha256 in MACOS_WHEEL_SPECS:
        lines.extend([
            f'  resource "{name}" do',
            f'    url "{url}", using: :nounzip',
            f'    sha256 "{sha256}"',
            "  end",
            "",
        ])

    lines.extend([
        "  def install",
        '    python3 = formula_opt_bin("python@3.12")/"python3.12"',
        '    system python3, "-m", "venv", libexec',
        "",
        '    wheelhouse = buildpath/".brew-wheels"',
        "    wheelhouse.mkpath",
        "    resources.each do |r|",
        "      r.stage { wheelhouse.install Pathname.pwd.children }",
        "    end",
        "",
        '    system libexec/"bin/python", "-m", "pip", "install",',
        '           "--no-input", "--disable-pip-version-check",',
        '           "--no-index", "--find-links", wheelhouse,',
        '           "--require-hashes", "--only-binary=:all:",',
        '           "-r", buildpath/"requirements.lock"',
        "",
        '    app_dir = libexec/"app"',
        "    app_dir.install %w[",
        "      main.py",
        "      att",
        "      attendance",
        "      start.command",
        "      install.command",
        "      install.sh",
        "      requirements.txt",
        "      requirements.lock",
        "      config.example.json",
        "      schedule.example.json",
        "      README.md",
        "      COMMANDS.md",
        "      LICENSE",
        "      src",
        "    ]",
        "",
        '    (bin/"att").write <<~SH',
        "      #!/bin/bash",
        '      export ATTENDANCE_HOME="${ATTENDANCE_HOME:-$HOME/AttendanceSystem}"',
        '      export ATTENDANCE_EXECUTABLE="${ATTENDANCE_EXECUTABLE:-#{opt_bin}/att}"',
        '      export ATTENDANCE_MANAGED_BY="homebrew"',
        '      export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-#{var}/attendance/browsers}"',
        '      export PYTHONNOUSERSITE="1"',
        "      unset PLAYWRIGHT_DOWNLOAD_HOST PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST "
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT",
        '      exec "#{libexec}/bin/python" "#{app_dir}/main.py" "$@"',
        "    SH",
        '    chmod 0755, bin/"att"',
        '    bin.install_symlink "att" => "attendance"',
        "  end",
        "",
        "  post_install_steps do",
        '    mkdir_p "attendance/browsers", base: :var',
        '    run "/usr/bin/env",',
        "        args:           [",
        '          "-u",',
        '          "PLAYWRIGHT_DOWNLOAD_HOST",',
        '          "-u",',
        '          "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST",',
        '          "-u",',
        '          "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD",',
        '          "-u",',
        '          "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT",',
        '          "{{libexec}}/bin/python",',
        '          "-m",',
        '          "playwright",',
        '          "install",',
        '          "chromium",',
        "        ],",
        '        env:            { "PLAYWRIGHT_BROWSERS_PATH" => "{{var}}/attendance/browsers" },',
        '        writable_paths: ["attendance/browsers"],',
        "        writable_base:  :var,",
        "        network_access: true",
        "  end",
        "",
        "  def caveats",
        "    <<~EOS",
        "      設定ファイル・予定・ログは既定で次のフォルダに保存されます（環境変数 ATTENDANCE_HOME で変更可能）:",
        "        ~/AttendanceSystem",
        "",
        "      初期設定と起動:",
        "        att user add",
        "        att --ui",
        "",
        "      アップデート:",
        "        brew upgrade attendance",
        "    EOS",
        "  end",
        "",
        "  test do",
        '    ENV["ATTENDANCE_HOME"] = (testpath/"attendance-home").to_s',
        '    ENV["ATTENDANCE_NO_UPDATE"] = "1"',
        '    assert_match version.to_s, shell_output("#{bin}/att --version")',
        '    assert_match "att", shell_output("#{bin}/att --help")',
        '    assert_match "att user add", shell_output("#{bin}/att user list")',
        '    assert_path_exists testpath/"attendance-home/config.json"',
        '    assert_equal 0600, (testpath/"attendance-home/config.json").stat.mode & 0777',
        '    assert_path_exists testpath/"attendance-home/start.command"',
        '    assert_predicate testpath/"attendance-home/start.command", :executable?',
        '    system libexec/"bin/python", "-c", "import keyring, playwright"',
        "  end",
        "end",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="配布タグ名 (例: v0.3.1)")
    parser.add_argument("--sha256", help="タグ tarball の SHA-256 (64桁hex)")
    parser.add_argument("--output", default="Formula/attendance.rb", help="書き出し先パス")
    parser.add_argument("--check", action="store_true", help="現在の Formula と一致するか検証する")
    args = parser.parse_args(argv)

    rendered = render_formula(ROOT, tag=args.tag, tarball_sha256=args.sha256)
    target = Path(args.output)
    if not target.is_absolute():
        target = ROOT / target
    if args.check:
        if not target.is_file() or target.read_text(encoding="utf-8") != rendered:
            print(
                f"Homebrew Formula が古いか一致しません: {target}. "
                "python3 scripts/generate_homebrew_formula.py を再実行してください。",
                file=sys.stderr,
            )
            return 1
        print(f"Homebrew Formula 検証成功: {target}")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    os.chmod(target, 0o644)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
