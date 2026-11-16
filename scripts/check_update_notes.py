"""更新内容の存在・形式・PRでの記載漏れをCIで検出する。"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.release_notes import read_notes
from src.version import VERSION


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def ci_base(root, environment):
    if environment.get("GITHUB_REF_TYPE") == "tag":
        if environment.get("GITHUB_REF_NAME") != f"v{VERSION}":
            raise ValueError("リリースタグと VERSION が一致しません。")
        return None
    event_path = environment.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise ValueError("CIイベントの情報を取得できません。")
    event = json.loads(Path(event_path).read_text())
    if "pull_request" in event:
        return event["pull_request"]["base"]["sha"]
    default = event["repository"]["default_branch"]
    if environment.get("GITHUB_REF_NAME") == default:
        before = event.get("before", "")
        return before if before and set(before) != {"0"} else None
    return git(root, "merge-base", "HEAD", f"refs/remotes/origin/{default}")


def check(root, version, base=None):
    read_notes(root, version)
    expected = f"updates/{version}.md"
    if base:
        changed = git(root, "diff", "--name-only", "--no-renames", base, "HEAD", "--").splitlines()
        if changed and expected not in changed:
            raise ValueError(f"この変更の内容と利用者の操作を {expected} に追記してください。")
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="差分を確認する基準コミット")
    parser.add_argument("--ci", action="store_true", help="GitHubイベントから比較先を決める")
    args = parser.parse_args()
    try:
        base = ci_base(ROOT, os.environ) if args.ci else args.base
        print("更新内容の検証成功: " + check(ROOT, VERSION, base))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(f"更新内容の検証失敗: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
