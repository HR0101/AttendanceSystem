"""GitHub Releasesからの更新。標準ライブラリだけでアプリ起動前に動作する。"""

import argparse
import contextlib
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

REPOSITORY = "HR0101/AttendanceSystem"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
ASSET_NAME = "attendance-update.zip"
MAX_ARCHIVE = 8 * 1024 * 1024
ROOT_FILES = frozenset({
    "main.py", "att", "attendance", "start.command", "install.command", "install.sh",
    "requirements.txt", "config.example.json", "schedule.example.json", "README.md",
    "COMMANDS.md", "LICENSE",
})
REQUIRED = ROOT_FILES | {"src/__init__.py", "src/updater.py", "src/version.py"}


class UpdateError(Exception):
    pass


def update_failure_message(exc):
    """通信・配布・保存の失敗を、更新なしという結果と区別して表示する。"""
    if isinstance(exc, UpdateError):
        if str(exc).startswith("更新失敗:"):
            return str(exc)
        reason = str(exc)
    elif isinstance(exc, HTTPError):
        if exc.code in (403, 429):
            reason = f"GitHubへのアクセスが拒否または制限されました（HTTP {exc.code}）。時間をおいて再実行してください。"
        elif exc.code >= 500:
            reason = f"配布サーバーでエラーが発生しました（HTTP {exc.code}）。時間をおいて再実行してください。"
        elif exc.code == 404:
            reason = "更新ファイルが見つかりません（HTTP 404）。配布元のリリースを確認してください。"
        else:
            reason = f"配布サーバーとの通信に失敗しました（HTTP {exc.code}）。"
    elif isinstance(exc, TimeoutError) or (isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError)):
        reason = "通信がタイムアウトしました。インターネット接続を確認して再実行してください。"
    elif isinstance(exc, URLError):
        reason = "配布サーバーに接続できません。インターネット接続を確認して再実行してください。"
    elif isinstance(exc, (json.JSONDecodeError, zipfile.BadZipFile, SyntaxError)):
        reason = "配布された更新データに問題があります。配布元の修正後に再実行してください。"
    elif isinstance(exc, subprocess.TimeoutExpired):
        reason = "更新版の起動確認がタイムアウトしました。att update で再試行してください。"
    elif isinstance(exc, BlockingIOError):
        return "更新待機: 別の更新処理が実行中です。完了後に att update を再実行してください。"
    elif isinstance(exc, PermissionError):
        reason = "更新ファイルを保存する権限がありません。インストール先のアクセス権を確認してください。"
    elif isinstance(exc, OSError):
        reason = "更新ファイルの読み書きに失敗しました。空き容量とアクセス権を確認してください。"
    else:
        reason = f"更新処理で予期しないエラーが発生しました（{type(exc).__name__}）。att update で再試行してください。"
    return f"更新失敗: {reason}"


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise UpdateError("リリースのバージョンが不正です。")
    return tuple(map(int, value.removeprefix("v").split(".")))


def allowed(name):
    path = PurePosixPath(name)
    return (str(path) == name and ".." not in path.parts and not path.is_absolute()
            and (name in ROOT_FILES or (len(path.parts) >= 2 and path.parts[0] == "src"
                 and path.suffix == ".py" and all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in path.parts))))


def managed_files(base):
    return sorted(name for name in ROOT_FILES if (base / name).is_file()) + sorted(
        p.relative_to(base).as_posix() for p in (base / "src").rglob("*.py"))


def safe_target(base, name):
    if not allowed(name):
        raise UpdateError("更新対象外のファイルが含まれています。")
    target = base / name
    parent = target
    while parent != base:
        if parent.is_symlink():
            raise UpdateError("更新対象がシンボリックリンクです。")
        parent = parent.parent
    return target


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".update-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False).encode())


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def fingerprint(base):
    result = {}
    for name in managed_files(base):
        target = safe_target(base, name)
        result[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    return result


@contextlib.contextmanager
def file_lock(path, exclusive=False, blocking=True):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a+") as stream:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(stream, mode | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield stream
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def fetch(url, limit, deadline):
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "AttendanceTool-Updater"})
    chunks = []
    total = 0
    with urlopen(request, timeout=5) as response:
        while True:
            if time.monotonic() > deadline:
                raise UpdateError("更新の通信がタイムアウトしました。")
            chunk = response.read(min(65536, limit + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise UpdateError("更新ファイルが上限サイズを超えています。")


def validate_payload(archive, destination, tag):
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or not REQUIRED.issubset(names):
            raise UpdateError("更新ファイルの構成が不正です。")
        if sum(entry.file_size for entry in entries) > MAX_ARCHIVE or len(entries) > 256:
            raise UpdateError("展開サイズが上限を超えています。")
        for entry in entries:
            mode = entry.external_attr >> 16
            if not allowed(entry.filename) or stat.S_ISLNK(mode) or entry.is_dir():
                raise UpdateError("更新対象外のファイルが含まれています。")
            data = package.read(entry)
            if entry.filename.endswith(".py"):
                compile(data, entry.filename, "exec")
            atomic_write(destination / entry.filename, data, 0o755 if entry.filename in {
                "att", "attendance", "start.command", "install.command", "install.sh"} else 0o644)
    source = (destination / "src/version.py").read_text()
    match = re.search(r'^VERSION = "([0-9.]+)"$', source, re.MULTILINE)
    if not match or version_tuple(match[1]) != version_tuple(tag):
        raise UpdateError("リリースタグとアプリのバージョンが一致しません。")


def prepare_update(base, force=False):
    """通信と検証だけを行う。起動中のコードは変更しない。"""
    updates = base / ".updates"
    with file_lock(updates / "download.lock", exclusive=True, blocking=False):
        if (updates / "pending/metadata.json").exists():
            return "更新の準備ができています。終了時に適用します。"
        state = read_json(updates / "check.json", {})
        if not force and time.time() - state.get("checked_at", 0) < state.get("interval", 86400):
            return None
        write_json(updates / "check.json", {**state, "checked_at": time.time(), "interval": 3600})
        deadline = time.monotonic() + 25
        try:
            release = json.loads(fetch(API_URL, 1024 * 1024, deadline))
        except HTTPError as exc:
            if exc.code == 404:
                write_json(updates / "check.json", {**state, "checked_at": time.time(), "interval": 86400})
                return "更新なし: 公開済みの正式リリースが見つかりません。現在のバージョンを継続します。"
            raise
        if not isinstance(release, dict) or not isinstance(release.get("tag_name"), str):
            raise UpdateError("リリース情報の形式が不正です。")
        from .version import VERSION
        if not force and release.get("tag_name", "").removeprefix("v") == state.get("skip_version"):
            write_json(updates / "check.json", {**state, "checked_at": time.time(), "interval": 86400})
            return None
        if release.get("draft") or release.get("prerelease"):
            write_json(updates / "check.json", {**state, "checked_at": time.time(), "interval": 86400})
            return "更新なし: 自動更新の対象となる正式リリースがありません。"
        if version_tuple(release["tag_name"]) <= version_tuple(VERSION):
            write_json(updates / "check.json", {**state, "checked_at": time.time(), "interval": 86400})
            if version_tuple(release["tag_name"]) == version_tuple(VERSION):
                return f"更新なし: 最新版です（現在 {VERSION}／公開版 {release['tag_name']}）。"
            return f"更新なし: 使用中の {VERSION} は公開版 {release['tag_name']} より新しいバージョンです。"
        tag = release["tag_name"]
        raw_assets = release.get("assets", [])
        if not isinstance(raw_assets, list) or not all(isinstance(a, dict) for a in raw_assets):
            raise UpdateError("リリース情報の形式が不正です。")
        assets = [a for a in raw_assets if a.get("name") == ASSET_NAME]
        if len(assets) != 1:
            raise UpdateError(f"新版 {tag} は公開されていますが、更新用ZIPがありません。配布元の準備完了後に再実行してください。")
        asset = assets[0]
        url = asset.get("browser_download_url", "")
        if url != f"https://github.com/{REPOSITORY}/releases/download/{tag}/{ASSET_NAME}":
            raise UpdateError("更新ファイルの配布元が不正です。")
        digest = asset.get("digest", "")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise UpdateError("更新ファイルのチェックサムを確認できません。")
        data = fetch(url, MAX_ARCHIVE, deadline)
        if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise UpdateError("更新ファイルのチェックサムが一致しません。")
        with tempfile.TemporaryDirectory(dir=updates, prefix="download-") as temp:
            temporary = Path(temp)
            archive = temporary / "release.zip"
            archive.write_bytes(data)
            payload = temporary / "payload"
            validate_payload(archive, payload, tag)
            # 環境を壊さないため、依存関係の変更を伴うリリースは手動セットアップが必要。
            if (payload / "requirements.txt").read_bytes() != (base / "requirements.txt").read_bytes():
                raise UpdateError("依存ライブラリが変更されています。最新版を手動で取得し bash install.sh を実行してください。")
            result = subprocess.run([sys.executable, str(payload / "main.py"), "--help"],
                                    capture_output=True, timeout=20,
                                    env={**os.environ, "ATTENDANCE_NO_UPDATE": "1"})
            if result.returncode:
                raise UpdateError("更新版の起動確認に失敗しました。現在の版を継続します。")
            shutil.rmtree(payload / ".updates", ignore_errors=True)
            # 起動確認で作成されたキャッシュは配布対象にしない。
            for cache in payload.rglob("__pycache__"):
                shutil.rmtree(cache)
            write_json(payload / "metadata.json", {"version": tag, "before": fingerprint(base),
                                                    "files": fingerprint(payload)})
            os.replace(payload, updates / "pending")
        write_json(updates / "check.json", {"checked_at": time.time(), "interval": 86400})
        return f"{tag} をダウンロードしました。終了時に適用します。"


def recover(base):
    """中断された更新は次回のアプリ読込前に元のファイルへ戻す。"""
    transaction = base / ".updates/transaction"
    journal = read_json(transaction / "journal.json")
    if not journal:
        return
    for name, mode in journal.items():
        target = safe_target(base, name)
        if mode is None:
            target.unlink(missing_ok=True)
        else:
            atomic_write(target, (transaction / "files" / name).read_bytes(), mode)
    clear_caches(base)
    shutil.rmtree(transaction)
    shutil.rmtree(base / ".updates/pending", ignore_errors=True)


def clear_caches(base):
    for cache in (base / "src").rglob("__pycache__"):
        shutil.rmtree(cache)
    shutil.rmtree(base / "__pycache__", ignore_errors=True)


def apply_pending(base):
    """呼出側が実行ロックを排他的に保持している時だけ呼ぶ。"""
    updates = base / ".updates"
    pending = updates / "pending"
    metadata = read_json(pending / "metadata.json")
    if not metadata:
        return None
    from .version import VERSION
    if version_tuple(metadata["version"]) <= version_tuple(VERSION):
        shutil.rmtree(pending)
        return None
    if fingerprint(base) != metadata["before"] or fingerprint(pending) != metadata["files"]:
        shutil.rmtree(pending)
        raise UpdateError("準備後にファイルが変更されたため更新を見送りました。att update で再取得してください。")
    files = sorted(set(metadata["before"]) | set(metadata["files"]))
    transaction = updates / "transaction"
    if transaction.exists():
        shutil.rmtree(transaction)
    journal = {}
    for name in files:
        target = safe_target(base, name)
        if target.exists():
            journal[name] = stat.S_IMODE(target.stat().st_mode)
            atomic_write(transaction / "files" / name, target.read_bytes())
        else:
            journal[name] = None
    write_json(transaction / "journal.json", journal)
    try:
        for name in files:
            target = safe_target(base, name)
            if name in metadata["files"]:
                source = pending / name
                atomic_write(target, source.read_bytes(), stat.S_IMODE(source.stat().st_mode))
            else:
                target.unlink(missing_ok=True)
        # 同じ秒・サイズの更新でも古いバイトコードを使わせない。
        clear_caches(base)
    except Exception:
        recover(base)
        raise
    # 成功後も直前の版を残す。次回更新時に入れ替える。
    shutil.rmtree(updates / "backup", ignore_errors=True)
    os.replace(transaction, updates / "backup")
    shutil.rmtree(pending)
    return f"{metadata['version']} への更新が完了しました。次回起動から使用します。"


def update_command(base, argv):
    parser = argparse.ArgumentParser(prog="att update", description="最新版を取得し、終了時に適用します。")
    parser.add_argument("--rollback", action="store_true", help="直前の更新前の版に戻します。")
    args = parser.parse_args(argv)
    if args.rollback:
        if not (base / ".updates/backup/journal.json").is_file():
            raise UpdateError("復元できる更新前の版がありません。")
        write_json(base / ".updates/rollback.json", {"requested": True})
        return 0
    print("更新を確認しています…")
    try:
        message = prepare_update(base, force=True)
        print(message or "更新確認を見送りました。時間をおいて再実行してください。")
    except Exception as exc:
        raise UpdateError(update_failure_message(exc)) from exc
    return 0


def finish_update(base):
    updates = base / ".updates"
    if (updates / "rollback.json").exists():
        from .version import VERSION
        with tempfile.TemporaryDirectory(dir=updates, prefix="restore-") as temp:
            restored = Path(temp) / "transaction"
            shutil.copytree(updates / "backup", restored)
            os.replace(restored, updates / "transaction")
        recover(base)
        (updates / "rollback.json").unlink()
        # 復元直後に同じ更新を再適用しない。
        write_json(updates / "check.json", {"checked_at": time.time(), "interval": 86400, "skip_version": VERSION})
        return "更新前の版に戻しました。次回起動から使用します。"
    return apply_pending(base)


def run_application(base, argv=None):
    argv = sys.argv[1:] if argv is None else argv
    updates = base / ".updates"
    automatic = (not os.environ.get("ATTENDANCE_NO_UPDATE") and "--scheduled" not in argv
                 and "--demo" not in argv and "--help" not in argv and "-h" not in argv
                 and "--version" not in argv
                 and (not argv or argv[0] not in {"update", "user", "schedule"}))
    manual_update = bool(argv and argv[0] == "update" and "--help" not in argv and "-h" not in argv)
    try:
        with file_lock(updates / "runtime.lock") as runtime:
            if (updates / "transaction/journal.json").exists():
                fcntl.flock(runtime, fcntl.LOCK_UN)
                fcntl.flock(runtime, fcntl.LOCK_EX)
                recover(base)
                fcntl.flock(runtime, fcntl.LOCK_SH)
            if automatic:
                notice = read_json(updates / "notice.json", {})
                if notice.get("message"):
                    print(f"更新のお知らせ: {notice['message']}", file=sys.stderr)
                    (updates / "notice.json").unlink(missing_ok=True)
                # 別プロセスなので短いCLI操作が終了してもダウンロードを完了できる。
                try:
                    with (updates / "updater.log").open("a") as log:
                        subprocess.Popen([sys.executable, "-m", "src.updater", "--background", str(base)],
                                         cwd=base, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         start_new_session=True)
                except OSError:
                    pass
            code = 1
            try:
                if argv and argv[0] == "update":
                    code = update_command(base, argv[1:])
                else:
                    code = importlib.import_module("main").main(argv)
            finally:
                # 起動中・自動実行中の別プロセスがあれば適用を次回へ延期。
                if automatic or manual_update:
                    fcntl.flock(runtime, fcntl.LOCK_UN)
                    try:
                        fcntl.flock(runtime, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        if (updates / "pending").exists():
                            print("別の処理が実行中のため更新は次回終了時に適用します。", file=sys.stderr)
                    else:
                        try:
                            message = finish_update(base)
                            if message:
                                print(message, file=sys.stderr)
                        except Exception:
                            print("更新を適用できませんでした。att update で再試行してください。", file=sys.stderr)
                            if (updates / "transaction/journal.json").exists():
                                raise UpdateError("復元が未完了です。次回起動時に復元を再試行します。")
                            if manual_update:
                                code = 1
            return code
    except (UpdateError, OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print(update_failure_message(exc), file=sys.stderr)
        return 1


def background_update(base):
    """本体終了後も取得を続け、他の処理がなければ適用する。"""
    try:
        with file_lock(base / ".updates/runtime.lock") as runtime:
            if (base / ".updates/transaction/journal.json").exists():
                return
            prepare_update(base)
            (base / ".updates/notice.json").unlink(missing_ok=True)
            fcntl.flock(runtime, fcntl.LOCK_UN)
            try:
                fcntl.flock(runtime, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            message = finish_update(base)
            if message:
                print(message, flush=True)
    except BlockingIOError:
        pass
    except Exception as exc:
        message = update_failure_message(exc)
        print(message, flush=True)
        write_json(base / ".updates/notice.json", {"message": message})


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--background":
        background_update(Path(sys.argv[2]).resolve())
