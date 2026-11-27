"""PR・ブランチ・リリースタグでバージョン引き上げと関連ファイル同期を検証する。"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import check_update_notes

SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
TAG_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
VERSION_LINE_RE = re.compile(r'^VERSION\s*=\s*["\']([^"\']+)["\']$', re.MULTILINE)
FORMULA_VERSION_RE = re.compile(r'^\s*version\s+"([^"]+)"$', re.MULTILINE)
BOT_COMMENT_MARKER = "<!-- attendance-version-bot -->"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def parse_semver(version: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(version.strip())
    if not match:
        raise ValueError(f"バージョン表記が不正です (X.Y.Z 形式が必要): {version!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def format_semver(parts: tuple[int, int, int]) -> str:
    return f"{parts[0]}.{parts[1]}.{parts[2]}"


def read_version_from_text(text: str) -> str:
    match = VERSION_LINE_RE.search(text)
    if not match:
        raise ValueError("src/version.py から VERSION を読み取れません。")
    version = match.group(1).strip()
    parse_semver(version)
    return version


def read_version_file(root: Path) -> str:
    path = root / "src/version.py"
    if not path.is_file():
        raise ValueError("src/version.py が見つかりません。")
    return read_version_from_text(path.read_text(encoding="utf-8"))


def read_formula_version(root: Path) -> str:
    path = root / "Formula/attendance.rb"
    if not path.is_file():
        raise ValueError("Formula/attendance.rb が見つかりません。")
    match = FORMULA_VERSION_RE.search(path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError("Formula/attendance.rb から version 行を読み取れません。")
    version = match.group(1).strip()
    parse_semver(version)
    return version


def list_release_tags(root: Path) -> list[tuple[tuple[int, int, int], str]]:
    output = git(root, "tag", "--list", "v*")
    tags: list[tuple[tuple[int, int, int], str]] = []
    for raw in output.splitlines():
        name = raw.strip()
        match = TAG_RE.fullmatch(name)
        if match:
            parsed = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
            tags.append((parsed, name))
    tags.sort(key=lambda item: item[0])
    return tags


def latest_release_tag(root: Path) -> tuple[tuple[int, int, int], str] | None:
    tags = list_release_tags(root)
    return tags[-1] if tags else None


def allowed_next_versions(latest: tuple[int, int, int]) -> list[str]:
    major, minor, patch = latest
    return [
        format_semver((major, minor, patch + 1)),
        format_semver((major, minor + 1, 0)),
        format_semver((major + 1, 0, 0)),
    ]


def tags_pointing_at_head(root: Path) -> set[str]:
    output = git(root, "tag", "--points-at", "HEAD")
    return {line.strip() for line in output.splitlines() if line.strip()}


def read_base_version(root: Path, base: str | None) -> str | None:
    if not base:
        return None
    try:
        content = git(root, "show", f"{base}:src/version.py")
    except subprocess.SubprocessError:
        return None
    try:
        return read_version_from_text(content)
    except ValueError:
        return None


def resolve_ci_context(root: Path, environment: dict[str, str]) -> dict:
    if environment.get("GITHUB_REF_TYPE") == "tag":
        return {
            "mode": "tag",
            "tag": environment.get("GITHUB_REF_NAME", ""),
            "base": None,
            "allow_tagged_head": False,
        }
    event_path = environment.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise ValueError("CIイベントの情報を取得できません。")
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    if "pull_request" in event:
        return {
            "mode": "pull_request",
            "tag": None,
            "base": event["pull_request"]["base"]["sha"],
            "allow_tagged_head": False,
        }
    default = event["repository"]["default_branch"]
    if environment.get("GITHUB_REF_NAME") == default:
        before = event.get("before", "")
        base = before if before and set(before) != {"0"} else None
    else:
        try:
            base = git(root, "merge-base", "HEAD", f"refs/remotes/origin/{default}")
        except subprocess.SubprocessError:
            base = None
    return {
        "mode": "push",
        "tag": None,
        "base": base,
        "allow_tagged_head": True,
    }


def _evaluate_version_bump(
    root: Path,
    *,
    mode: str,
    tag: str | None,
    current_version: str | None,
    current_tuple: tuple[int, int, int] | None,
    base_version: str | None,
    release_tags: list[tuple[tuple[int, int, int], str]],
    latest_tag_info: tuple[tuple[int, int, int], str] | None,
    allowed_next: list[str],
    allow_tagged_head: bool,
    version_error: str | None,
) -> dict:
    if version_error is not None or current_version is None or current_tuple is None:
        return {
            "key": "version_bump",
            "label": "VERSION 更新",
            "ok": False,
            "detail": version_error or "src/version.py の検証に失敗しました。",
        }

    if mode == "tag":
        if not tag or not TAG_RE.fullmatch(tag):
            return {
                "key": "version_bump",
                "label": "VERSION 更新",
                "ok": False,
                "detail": f"リリースタグ {tag!r} は vX.Y.Z 形式にしてください。",
            }
        expected_tag = f"v{current_version}"
        if tag != expected_tag:
            return {
                "key": "version_bump",
                "label": "VERSION 更新",
                "ok": False,
                "detail": f"リリースタグ {tag} と src/version.py の VERSION ({current_version}) が一致しません。",
            }
        return {
            "key": "version_bump",
            "label": "VERSION 更新",
            "ok": True,
            "detail": f"タグ {tag} と src/version.py ({current_version}) が一致しています。",
        }

    if allow_tagged_head:
        head_tags = tags_pointing_at_head(root)
        if f"v{current_version}" in head_tags:
            return {
                "key": "version_bump",
                "label": "VERSION 更新",
                "ok": True,
                "detail": f"HEAD にタグ v{current_version} が付与済みです。",
            }

    existing_tag_names = {name for _, name in release_tags}
    expected_tag = f"v{current_version}"
    if expected_tag in existing_tag_names:
        candidates = ", ".join(allowed_next) if allowed_next else "新しい X.Y.Z"
        return {
            "key": "version_bump",
            "label": "VERSION 更新",
            "ok": False,
            "detail": (
                f"src/version.py の VERSION ({current_version}) に対応するタグ {expected_tag} は既に公開済みです。"
                f" 次のバージョン（候補: {candidates}）へ上げてください。"
            ),
        }

    if latest_tag_info is not None:
        latest_tuple, latest_name = latest_tag_info
        if current_version not in allowed_next:
            candidates = " / ".join(allowed_next)
            if current_tuple <= latest_tuple:
                detail = (
                    f"VERSION ({current_version}) が最新タグ {latest_name} 以下です。"
                    f" 次のバージョン（{candidates}）へ上げてください。"
                )
            else:
                detail = (
                    f"VERSION ({current_version}) は最新タグ {latest_name} からの正しい次バージョン"
                    f"（{candidates}）ではありません。"
                )
            return {
                "key": "version_bump",
                "label": "VERSION 更新",
                "ok": False,
                "detail": detail,
            }

    if base_version is not None and current_tuple < parse_semver(base_version):
        return {
            "key": "version_bump",
            "label": "VERSION 更新",
            "ok": False,
            "detail": f"VERSION ({current_version}) がベースブランチ ({base_version}) より下げられています。",
        }

    if latest_tag_info is not None:
        detail = f"最新タグ {latest_tag_info[1]} → {current_version} への引き上げを確認しました。"
    else:
        detail = f"初期バージョン {current_version} を確認しました。"
    return {
        "key": "version_bump",
        "label": "VERSION 更新",
        "ok": True,
        "detail": detail,
    }


def evaluate_version_policy(
    root: Path,
    *,
    mode: str = "pull_request",
    tag: str | None = None,
    base: str | None = None,
    allow_tagged_head: bool = False,
    include_notes: bool = False,
) -> dict:
    current_version: str | None = None
    current_tuple: tuple[int, int, int] | None = None
    version_error: str | None = None
    try:
        current_version = read_version_file(root)
        current_tuple = parse_semver(current_version)
    except (ValueError, OSError) as exc:
        version_error = str(exc)

    release_tags = list_release_tags(root)
    latest_tag_info = release_tags[-1] if release_tags else None
    latest_tag = latest_tag_info[1] if latest_tag_info else None
    allowed_next = allowed_next_versions(latest_tag_info[0]) if latest_tag_info else []
    base_version = read_base_version(root, base)

    checks: list[dict] = []
    checks.append(
        _evaluate_version_bump(
            root,
            mode=mode,
            tag=tag,
            current_version=current_version,
            current_tuple=current_tuple,
            base_version=base_version,
            release_tags=release_tags,
            latest_tag_info=latest_tag_info,
            allowed_next=allowed_next,
            allow_tagged_head=allow_tagged_head,
            version_error=version_error,
        )
    )

    try:
        formula_version = read_formula_version(root)
    except (ValueError, OSError) as exc:
        checks.append({
            "key": "formula_sync",
            "label": "Formula/attendance.rb 同期",
            "ok": False,
            "detail": str(exc),
        })
    else:
        if current_version is None:
            checks.append({
                "key": "formula_sync",
                "label": "Formula/attendance.rb 同期",
                "ok": False,
                "detail": f"src/version.py を検証できないため Formula ({formula_version}) と比較できません。",
            })
        elif formula_version != current_version:
            checks.append({
                "key": "formula_sync",
                "label": "Formula/attendance.rb 同期",
                "ok": False,
                "detail": (
                    f"Formula/attendance.rb の version ({formula_version}) が src/version.py ({current_version}) "
                    "と一致しません。`python3 scripts/generate_homebrew_formula.py` を実行してください。"
                ),
            })
        else:
            checks.append({
                "key": "formula_sync",
                "label": "Formula/attendance.rb 同期",
                "ok": True,
                "detail": f'version "{formula_version}" が src/version.py と一致しています。',
            })

    if include_notes:
        if current_version is None:
            checks.append({
                "key": "update_notes",
                "label": "updates/X.Y.Z.md 更新",
                "ok": False,
                "detail": "src/version.py が不正なためリリースノートを検証できません。",
            })
        else:
            try:
                notes_path = check_update_notes.check(root, current_version, base)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                checks.append({
                    "key": "update_notes",
                    "label": "updates/X.Y.Z.md 更新",
                    "ok": False,
                    "detail": str(exc),
                })
            else:
                checks.append({
                    "key": "update_notes",
                    "label": "updates/X.Y.Z.md 更新",
                    "ok": True,
                    "detail": f"{notes_path} の形式と変更を確認しました。",
                })

    return {
        "ok": all(item["ok"] for item in checks),
        "mode": mode,
        "current_version": current_version,
        "latest_tag": latest_tag,
        "base_version": base_version,
        "allowed_next": allowed_next,
        "checks": checks,
    }


def render_markdown_report(result: dict) -> str:
    heading = (
        "## ✅ バージョン・リリース準備チェック通過"
        if result["ok"]
        else "## ❌ バージョン・リリース準備チェックに失敗しました"
    )
    current = result["current_version"] or "(読み取り不可)"
    latest = result["latest_tag"] or "(タグなし)"
    lines = [
        BOT_COMMENT_MARKER,
        heading,
        "",
        f"- 現在の `src/version.py`: `{current}`",
        f"- 最新リリースタグ: `{latest}`",
    ]
    if result["base_version"]:
        lines.append(f"- ベースブランチのバージョン: `{result['base_version']}`")
    lines.extend([
        "",
        "| チェック項目 | 状態 | 詳細 |",
        "| --- | --- | --- |",
    ])
    for item in result["checks"]:
        status = "✅ OK" if item["ok"] else "❌ NG"
        detail = item["detail"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {item['label']} | {status} | {detail} |")

    if not result["ok"]:
        lines.append("")
        if len(result["allowed_next"]) == 3:
            patch_v, minor_v, major_v = result["allowed_next"]
            lines.extend([
                "### 次の候補バージョン",
                f"- パッチ修正 (`patch`): `{patch_v}`",
                f"- 後方互換の機能追加 (`minor`): `{minor_v}`",
                f"- 破壊的変更 (`major`): `{major_v}`",
                "",
            ])
        target_ver = result["allowed_next"][0] if result["allowed_next"] else (result["current_version"] or "X.Y.Z")
        lines.extend([
            "### 修正方法（ローカル実行コマンド）",
            "1. `src/version.py` の `VERSION` を更新する",
            f"2. `updates/{target_ver}.md`（`# v{target_ver}` / `## 変更内容` / `## 利用者の操作`）を作成・更新する",
            "3. Homebrew Formula を再生成し、CI チェック一式を通す:",
            "```sh",
            "python3 scripts/generate_homebrew_formula.py",
            "python3 scripts/check_version_bump.py",
            "python3 scripts/check_update_notes.py",
            "python3 scripts/generate_homebrew_formula.py --check",
            ".venv/bin/python -m unittest discover -s tests",
            "```",
        ])

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="差分を確認する基準コミット")
    parser.add_argument("--tag", help="検証対象のリリースタグ (vX.Y.Z)")
    parser.add_argument("--ci", action="store_true", help="GitHub イベントから比較先とモードを決める")
    parser.add_argument(
        "--allow-tagged-head",
        action="store_true",
        help="HEAD に v{VERSION} タグが付いている場合に成功とみなす",
    )
    parser.add_argument(
        "--include-notes",
        action="store_true",
        help="updates/X.Y.Z.md の検証結果もレポートに含める",
    )
    parser.add_argument("--markdown-report", help="Markdown レポートの出力先パス")
    args = parser.parse_args(argv)

    try:
        if args.ci:
            ctx = resolve_ci_context(ROOT, os.environ)
            mode = ctx["mode"]
            tag = args.tag or ctx["tag"]
            base = args.base if args.base is not None else ctx["base"]
            allow_tagged_head = args.allow_tagged_head or ctx["allow_tagged_head"]
        else:
            mode = "tag" if args.tag else "pull_request"
            tag = args.tag
            base = args.base
            allow_tagged_head = args.allow_tagged_head

        result = evaluate_version_policy(
            ROOT,
            mode=mode,
            tag=tag,
            base=base,
            allow_tagged_head=allow_tagged_head,
            include_notes=args.include_notes,
        )
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(f"バージョン検証失敗: {exc}", file=sys.stderr)
        return 1

    if args.markdown_report:
        report_path = Path(args.markdown_report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_markdown_report(result), encoding="utf-8")

    if result["ok"]:
        summary = "; ".join(f"{item['label']}: {item['detail']}" for item in result["checks"])
        print(f"バージョン検証成功: {summary}")
        return 0

    for item in result["checks"]:
        if not item["ok"]:
            print(f"バージョン検証失敗 [{item['label']}]: {item['detail']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
