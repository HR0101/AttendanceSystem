"""PR にバージョンチェック結果の Bot コメントを投稿・更新する。"""

import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_version_bump import BOT_COMMENT_MARKER

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PR_NUMBER_RE = re.compile(r"^[1-9][0-9]*$")
NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _api_request(url: str, token: str, *, method: str = "GET", payload: dict | None = None):
    if not url.startswith("https://"):
        raise ValueError(f"HTTPS の GitHub API URL のみ許可されています: {url}")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "AttendanceSystem-Version-Bot",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")
        next_url = None
        link_header = resp.headers.get("Link") or ""
        match = NEXT_LINK_RE.search(link_header)
        if match:
            next_url = match.group(1)
        return json.loads(body) if body else None, next_url


def find_existing_bot_comment_id(api_url: str, repo: str, pr_number: str, token: str) -> int | None:
    url: str | None = f"{api_url}/repos/{repo}/issues/{pr_number}/comments?per_page=100"
    while url:
        comments, url = _api_request(url, token)
        if not isinstance(comments, list):
            break
        for comment in comments:
            if isinstance(comment, dict) and BOT_COMMENT_MARKER in (comment.get("body") or ""):
                return int(comment["id"])
    return None


def upsert_pr_comment(
    *,
    api_url: str,
    repo: str,
    pr_number: str,
    token: str,
    body: str,
) -> str:
    api_url = api_url.rstrip("/")
    if not api_url.startswith("https://"):
        raise ValueError("GITHUB_API_URL は https:// で始める必要があります。")
    if not REPO_RE.fullmatch(repo):
        raise ValueError(f"REPOSITORY の形式が不正です: {repo!r}")
    if not PR_NUMBER_RE.fullmatch(pr_number):
        raise ValueError(f"PR_NUMBER の形式が不正です: {pr_number!r}")
    if not token:
        raise ValueError("GH_TOKEN が設定されていません。")

    existing_id = find_existing_bot_comment_id(api_url, repo, pr_number, token)
    if existing_id is not None:
        _api_request(
            f"{api_url}/repos/{repo}/issues/comments/{existing_id}",
            token,
            method="PATCH",
            payload={"body": body},
        )
        return f"updated:{existing_id}"

    created, _ = _api_request(
        f"{api_url}/repos/{repo}/issues/{pr_number}/comments",
        token,
        method="POST",
        payload={"body": body},
    )
    comment_id = created.get("id") if isinstance(created, dict) else "unknown"
    return f"created:{comment_id}"


def main() -> int:
    token = os.environ.get("GH_TOKEN", "").strip()
    repo = os.environ.get("REPOSITORY", "").strip()
    pr_number = os.environ.get("PR_NUMBER", "").strip()
    report_path = os.environ.get("REPORT_PATH", "").strip()
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").strip()

    if not token:
        print("GH_TOKEN が未設定のため PR コメント投稿をスキップします。")
        return 0
    if not report_path or not Path(report_path).is_file():
        print("レポートファイルが見つからないため PR コメント投稿をスキップします。", file=sys.stderr)
        return 1

    body = Path(report_path).read_text(encoding="utf-8")
    try:
        result = upsert_pr_comment(
            api_url=api_url,
            repo=repo,
            pr_number=pr_number,
            token=token,
            body=body,
        )
    except HTTPError as exc:
        if exc.code == 403:
            print("フォーク PR 等の権限制限のためコメント投稿をスキップしました。詳細は Actions の Job Summary を確認してください。")
            return 0
        print(f"PR コメントの投稿に失敗しました (HTTP {exc.code}): {exc.reason}", file=sys.stderr)
        return 1
    except (ValueError, URLError, OSError) as exc:
        print(f"PR コメントの投稿に失敗しました: {exc}", file=sys.stderr)
        return 1

    print(f"PR コメントを反映しました ({result})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
