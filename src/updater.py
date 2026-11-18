"""GitHub Releasesからの更新。標準ライブラリだけでアプリ起動前に動作する。"""

import argparse
import contextlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import http.client
import importlib
import json
import os
import platform
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

REPOSITORY = "HR0101/AttendanceSystem"
ASSET_NAME = "attendance-update.zip"
MANIFEST_NAME = "attendance-update.json"
MANIFEST_URL = f"https://github.com/{REPOSITORY}/releases/latest/download/{MANIFEST_NAME}"
MAX_MANIFEST = 64 * 1024
MAX_ARCHIVE = 8 * 1024 * 1024
DOWNLOAD_HOSTS = frozenset({"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
                            "github-releases.githubusercontent.com"})
ROOT_FILES = frozenset({
    "main.py", "att", "attendance", "start.command", "install.command", "install.sh",
    "requirements.txt", "config.example.json", "schedule.example.json", "README.md",
    "COMMANDS.md", "LICENSE",
})
REQUIRED = ROOT_FILES | {f"src/{name}.py" for name in (
    "__init__", "attendance", "browser", "config", "credentials", "dependencies", "dependency_lock", "logging_utils", "room",
    "release_notes", "schedule_ui", "scheduler", "terminal_layout", "terminal_ui", "text_input", "unsaved_changes", "updater", "user_commands",
    "users", "verification", "version",
)}


class UpdateError(Exception):
    pass


def header_value(headers, name):
    return next((str(value) for key, value in (headers.items() if headers else []) if key.lower() == name.lower()), "")


def rate_limit_retry_at(exc):
    """拒否と利用制限を区別し、サーバーが指定した再試行時刻を読む。"""
    if not isinstance(exc, HTTPError) or exc.code not in (403, 429):
        return None
    remaining = header_value(exc.headers, "X-RateLimit-Remaining")
    retry_after = header_value(exc.headers, "Retry-After")
    if exc.code != 429 and remaining != "0" and not retry_after:
        return None
    now, candidates = time.time(), []
    reset = header_value(exc.headers, "X-RateLimit-Reset")
    if remaining == "0" and reset.isascii() and reset.isdecimal() and len(reset) <= 12:
        candidates.append(int(reset))
    if retry_after.isascii() and retry_after.isdecimal() and len(retry_after) <= 10:
        candidates.append(now + int(retry_after))
    elif retry_after:
        try:
            value = parsedate_to_datetime(retry_after)
            candidates.append(value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp())
        except (ValueError, TypeError, OverflowError):
            pass
    candidates = [value for value in candidates if now < value < 253402300799]
    return max(candidates) if candidates else now + 60


def update_failure_message(exc):
    """通信・配布・保存の失敗を、更新なしという結果と区別して表示する。"""
    if isinstance(exc, UpdateError):
        if str(exc).startswith("更新失敗:"):
            return str(exc)
        reason = str(exc)
    elif isinstance(exc, HTTPError):
        retry_at = rate_limit_retry_at(exc)
        if retry_at is not None:
            when = datetime.fromtimestamp(retry_at).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            reason = f"GitHubの利用制限中です（HTTP {exc.code}）。{when} 以降に再実行してください。"
        elif exc.code == 403:
            reason = "GitHubへのアクセスが拒否されました（HTTP 403）。ネットワーク設定や配布先へのアクセスを確認してください。"
        elif exc.code >= 500:
            reason = f"配布サーバーでエラーが発生しました（HTTP {exc.code}）。時間をおいて再実行してください。"
        elif exc.code == 404:
            reason = "更新ファイルが見つかりません（HTTP 404）。配布元のリリースを確認してください。"
        else:
            reason = f"配布サーバーとの通信に失敗しました（HTTP {exc.code}）。"
    elif isinstance(exc, TimeoutError) or (isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError)):
        reason = "通信がタイムアウトしました。インターネット接続を確認して再実行してください。"
    elif isinstance(exc, URLError) and isinstance(exc.reason, ssl.SSLCertVerificationError):
        reason = "HTTPS証明書を検証できません。Pythonの証明書設定とネットワークの認証・プロキシ設定を確認してください。"
    elif isinstance(exc, URLError) and isinstance(exc.reason, socket.gaierror):
        reason = "配布サーバーの名前を解決できません（DNS）。インターネット接続とDNS設定を確認してください。"
    elif isinstance(exc, URLError) and isinstance(exc.reason, ssl.SSLError):
        reason = "HTTPS接続の確立に失敗しました。ネットワークの認証・プロキシ設定を確認してください。"
    elif isinstance(exc, URLError):
        reason = "配布サーバーに接続できません。インターネット接続を確認して再実行してください。"
    elif isinstance(exc, (http.client.IncompleteRead, ConnectionError)):
        reason = "更新データの受信中に接続が切れました。インターネット接続を確認して再実行してください。"
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


def runtime_directory(base, state):
    """復元前にも読めるよう、環境の記録は標準ライブラリだけで検証する。"""
    base = base.resolve()
    if not isinstance(state, dict) or not isinstance(state.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", state["id"]):
        raise UpdateError("依存パッケージの実行環境の記録が不正です。")
    digest = state.get("lock_hash")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise UpdateError("依存パッケージの実行環境のハッシュが不正です。")
    directory = base / ".updates/environments" / state["id"]
    parent = directory
    while parent != base:
        if parent.is_symlink():
            raise UpdateError("依存パッケージの実行環境がシンボリックリンクです。")
        parent = parent.parent
    ready = read_json(directory / "ready.json", {})
    if (not isinstance(ready, dict) or ready.get("lock_hash") != digest
            or ready.get("directory") != str(directory) or ready.get("python") != list(sys.version_info[:2])
            or ready.get("platform") != sys.platform or ready.get("machine") != platform.machine()
            or not (directory / "bin/python").is_file() or not (directory / "pyvenv.cfg").is_file()
            or not (directory / "requirements.lock").is_file()
            or hashlib.sha256((directory / "requirements.lock").read_bytes()).hexdigest() != digest):
        raise UpdateError("依存パッケージの実行環境を確認できません。att update で再準備してください。")
    return directory


def read_runtime_state(base):
    path = base / ".updates/runtime.json"
    if not path.exists():
        return None
    state = read_json(path)
    runtime_directory(base, state)
    return state


def set_runtime_state(base, state):
    if state is None:
        (base / ".updates/runtime.json").unlink(missing_ok=True)
    else:
        runtime_directory(base, state)
        write_json(base / ".updates/runtime.json", state)


def runtime_environment(base, state):
    directory = runtime_directory(base, state)
    return {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(directory / "browsers"),
            "PYTHONNOUSERSITE": "1", "VIRTUAL_ENV": str(directory)}


def _ensure_no_symlinks(base, path, message):
    current = path
    while current != base:
        if current.is_symlink():
            raise UpdateError(message)
        current = current.parent


def referenced_environment_ids(base):
    """現在・待機中・バックアップ・復元記録が参照している環境IDを集める。"""
    base = Path(base).resolve()
    updates = base / ".updates"
    records = (
        (updates / "runtime.json", lambda data: data),
        (updates / "pending/metadata.json", lambda data: data.get("runtime") if isinstance(data, dict) else "invalid"),
        (updates / "backup/runtime.json", lambda data: data),
        (updates / "transaction/runtime.json", lambda data: data),
    )
    referenced = set()
    for path, select in records:
        _ensure_no_symlinks(base, path, "依存パッケージの参照記録がシンボリックリンクです。")
        if not path.exists():
            continue
        if not path.is_file():
            raise UpdateError("依存パッケージの参照記録が通常のファイルではありません。")
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise UpdateError("依存パッケージの参照記録を読み取れません。") from exc
        state = select(data)
        if state is None:
            continue
        if not isinstance(state, dict) or not isinstance(state.get("id"), str) or not state["id"]:
            raise UpdateError("依存パッケージの参照記録が不正です。")
        referenced.add(state["id"])
    return referenced


def cleanup_environments(base):
    """参照されていない専用仮想環境を排他ロック下で削除する。"""
    base = Path(base).resolve()
    updates = base / ".updates"
    environments = updates / "environments"
    _ensure_no_symlinks(base, environments, "依存パッケージの保存先がシンボリックリンクです。")
    _ensure_no_symlinks(base, updates / "dependency.lock", "依存パッケージの保存先がシンボリックリンクです。")
    if not updates.exists():
        return []
    if not updates.is_dir():
        raise UpdateError("依存パッケージの保存先が通常のフォルダではありません。")
    if not environments.exists():
        return []
    if not environments.is_dir():
        raise UpdateError("依存パッケージの保存先が通常のフォルダではありません。")
    # ロック順序を runtime.lock -> dependency.lock に統一し、準備処理と競合しないようにする。
    with file_lock(updates / "dependency.lock", exclusive=True):
        _ensure_no_symlinks(base, environments, "依存パッケージの保存先がシンボリックリンクです。")
        if not environments.exists():
            return []
        if not environments.is_dir():
            raise UpdateError("依存パッケージの保存先が通常のフォルダではありません。")
        referenced = referenced_environment_ids(base)
        removed = []
        for entry in sorted(environments.iterdir()):
            if entry.is_symlink() or not entry.is_dir():
                continue
            if entry.name not in referenced:
                shutil.rmtree(entry)
                removed.append(entry.name)
        return removed


def activate_runtime(base, argv, runtime):
    # ヘルプ・復元・デモはネットワークや依存パッケージの準備なしで利用できる。
    if any(flag in argv for flag in ("--help", "-h", "--version", "--demo", "--rollback", "--notes")):
        return
    invalid_state = False
    try:
        state = read_runtime_state(base)
    except UpdateError:
        if not (base / "src/dependencies.py").is_file():
            raise
        state = None
        invalid_state = True
    if (base / "src/dependencies.py").is_file():
        from .dependencies import read_spec, prepare_environment
        spec = read_spec(base)
        if state is None or state["lock_hash"] != spec["lock_hash"]:
            if argv != ["update"]:
                if invalid_state or state is not None:
                    raise UpdateError("依存パッケージの環境が不正、または現在の版と一致しません。att update で再準備してください。")
            else:
                print("依存パッケージの準備のため、実行中の出席処理があれば終了を待ちます…", flush=True)
                fcntl.flock(runtime, fcntl.LOCK_UN)
                fcntl.flock(runtime, fcntl.LOCK_EX)
                try:
                    spec = read_spec(base)
                    print("依存パッケージとChromiumを準備しています…", flush=True)
                    state = prepare_environment(base, spec)
                    set_runtime_state(base, state)
                finally:
                    fcntl.flock(runtime, fcntl.LOCK_SH)
    if state is not None:
        directory = runtime_directory(base, state)
        os.environ.update(runtime_environment(base, state))
        if Path(sys.prefix) != directory:
            os.execv(str(directory / "bin/python"), [str(directory / "bin/python"), str(base / "main.py"), *argv])
    elif (base / ".venv/bin/python").is_file() and Path(sys.prefix).parent == base / ".updates/environments":
        python = base / ".venv/bin/python"
        os.execv(str(python), [str(python), str(base / "main.py"), *argv])


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


def require_download_url(url):
    try:
        parsed = urlparse(url)
        valid = (parsed.scheme == "https" and parsed.hostname in DOWNLOAD_HOSTS and parsed.port in (None, 443)
                 and not parsed.username and not parsed.password)
    except ValueError:
        valid = False
    if not valid:
        raise UpdateError("更新ファイルの配布元またはリダイレクト先が不正です。")


class DownloadRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen(request, timeout):
    return build_opener(DownloadRedirectHandler()).open(request, timeout=timeout)


def fetch(url, limit, deadline, *, headers=None, on_headers=None):
    require_download_url(url)
    request = Request(url, headers={"Accept": "application/json" if url == MANIFEST_URL else "application/octet-stream",
                                    "User-Agent": "AttendanceTool-Updater", **(headers or {})})
    chunks = []
    total = 0
    with urlopen(request, timeout=5) as response:
        if on_headers is not None:
            on_headers(getattr(response, "headers", {}))
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


def validate_manifest(manifest):
    """公開JSONを検証する。ZIPの取得先は版番号から組み立てる。"""
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise UpdateError("更新情報JSONの形式または形式バージョンが不正です。")
    version = manifest.get("version")
    if version != ".".join(map(str, version_tuple(version))):
        raise UpdateError("更新情報JSONのバージョンが不正です。")
    archive = manifest.get("archive")
    if not isinstance(archive, dict) or archive.get("name") != ASSET_NAME:
        raise UpdateError("更新情報JSONの更新用ZIPの指定が不正です。")
    digest, size = archive.get("sha256"), archive.get("size")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise UpdateError("更新ファイルのチェックサムを確認できません。")
    if type(size) is not int or not 0 < size <= MAX_ARCHIVE:
        raise UpdateError("更新情報JSONのZIPサイズが不正です。")
    if "release_notes" in manifest:
        from .release_notes import validate_notes
        try:
            validate_notes(manifest["release_notes"], version)
        except (ValueError, UnicodeError) as exc:
            raise UpdateError("更新情報JSONの更新内容が不正です。") from exc
    return manifest


def fetch_manifest(state, deadline):
    """検証済みJSONとETagを保存し、304ではそのJSONを再利用する。"""
    cached = state.get("manifest")
    try:
        validate_manifest(cached)
    except (UpdateError, ValueError, TypeError):
        cached = None
    etag = state.get("etag")
    if not valid_etag(etag):
        etag = None
    conditional = {"If-None-Match": etag} if cached is not None and etag else {}
    for attempt in range(2):
        received = {}
        try:
            data = fetch(MANIFEST_URL, MAX_MANIFEST, deadline, headers=conditional,
                         on_headers=lambda headers: received.update(headers))
        except HTTPError as exc:
            if exc.code != 304:
                raise
            exc.close()
            if conditional:
                return cached
            if attempt == 0:
                continue
            raise UpdateError("更新情報JSONが304を返しましたが、再利用できるキャッシュがありません。") from exc
        manifest = validate_manifest(json.loads(data))
        state["manifest"] = manifest
        value = header_value(received, "ETag")
        state.pop("etag", None)
        if valid_etag(value):
            state["etag"] = value
        return manifest


def valid_etag(value):
    return isinstance(value, str) and 0 < len(value) <= 1024 and re.fullmatch(r'(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"', value) is not None


def record_rate_limit(updates, state, exc):
    retry_at = rate_limit_retry_at(exc)
    if retry_at is not None:
        write_json(updates / "check.json", {**state, "checked_at": time.time(), "interval": 3600, "retry_at": retry_at})


def pending_metadata(base):
    """中断・破損・期限切れの準備データは破棄し、次のダウンロードを妨げない。"""
    pending = base / ".updates/pending"
    if pending.is_symlink():
        raise UpdateError("更新の一時領域が通常のフォルダではありません。インストール先を確認してください。")
    if not pending.exists():
        return None
    if not pending.is_dir():
        raise UpdateError("更新の一時領域が通常のフォルダではありません。インストール先を確認してください。")
    metadata = read_json(pending / "metadata.json")
    from .version import VERSION
    try:
        if not isinstance(metadata, dict):
            raise ValueError
        if version_tuple(metadata.get("version")) <= version_tuple(VERSION):
            raise ValueError
        for key in ("before", "files"):
            files = metadata.get(key)
            if not isinstance(files, dict) or not files or not all(
                    isinstance(name, str) and allowed(name) and isinstance(digest, str)
                    and re.fullmatch(r"[0-9a-f]{64}", digest) for name, digest in files.items()):
                raise ValueError
        if not REQUIRED.issubset(metadata["files"]):
            raise ValueError
        if metadata.get("runtime") is not None:
            runtime_directory(base, metadata["runtime"])
    except (ValueError, UpdateError):
        shutil.rmtree(pending)
        return None
    return metadata


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


def refresh_manifest(base, force=False, *, every_start=False):
    """呼出側がdownload.lockを保持する。更新情報だけを取得し、失敗時は再試行を抑止する。"""
    updates = base / ".updates"
    state = read_json(updates / "check.json", {})
    if not isinstance(state, dict):
        state = {}
    retry_at = state.get("retry_at")
    if type(retry_at) in (int, float) and 0 < retry_at < 253402300799:
        if not force and time.time() < retry_at:
            return None
        state.pop("retry_at", None)
        state.pop("checked_at", None)
    checked_at, interval = state.get("checked_at", 0), state.get("interval", 86400)
    now = time.time()
    valid_clock = (type(checked_at) in (int, float) and 0 <= checked_at <= now
                   and type(interval) in (int, float) and 0 < interval <= 86400)
    if not force and valid_clock and now - checked_at < interval:
        return None
    state["check_failed"] = True
    write_json(updates / "check.json", {**state, "checked_at": now, "interval": 3600})
    deadline = time.monotonic() + (5 if every_start else 25)
    try:
        manifest = fetch_manifest(state, deadline)
    except HTTPError as exc:
        record_rate_limit(updates, state, exc)
        if exc.code == 404:
            raise UpdateError("更新情報JSONが見つかりません。配布元の正式リリースに attendance-update.json が添付されているか確認してください。") from exc
        raise
    state.pop("check_failed", None)
    return manifest, state, deadline


def available_message(manifest):
    from .version import VERSION
    if version_tuple(manifest["version"]) <= version_tuple(VERSION):
        return None
    return (f"アップデートがあります: v{manifest['version']}（現在 {VERSION}）\n"
            "  更新内容を確認: att update --notes\n"
            "  アップデートする: att update")


def cached_manifest(base):
    state = read_json(base / ".updates/check.json", {})
    try:
        return validate_manifest(state.get("manifest"))
    except (AttributeError, UpdateError, ValueError, TypeError):
        return None


def check_update(base):
    """通常起動ではJSONだけを確認する。ZIP・依存環境・適用待ちデータには触れない。"""
    updates = base / ".updates"
    with file_lock(updates / "download.lock", exclusive=True, blocking=False):
        result = refresh_manifest(base, every_start=True)
        if result is None:
            manifest = cached_manifest(base)
            if manifest is None:
                return None
        else:
            manifest, state, _ = result
            write_json(updates / "check.json", {**state, "checked_at": time.time(), "interval": 86400})
        return available_message(manifest)


def log_check_failure(base, exc):
    try:
        with os.fdopen(os.open(base / ".updates/updater.log", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600), "a") as log:
            print(update_failure_message(exc), file=log)
    except OSError:
        pass


def start_update_check(base):
    """通信を待たず起動する。ワーカーは通知用キャッシュだけを書き、端末には出力しない。"""
    def work():
        try:
            # 別プロセスによるコード更新中には確認せず、起動中も共有ロックを保持する。
            with file_lock(base / ".updates/runtime.lock", blocking=False):
                if not (base / ".updates/transaction/journal.json").exists():
                    check_update(base)
        except BlockingIOError:
            pass
        except Exception as exc:
            log_check_failure(base, exc)

    worker = threading.Thread(target=work, name="attendance-update-check", daemon=True)
    try:
        worker.start()
    except (RuntimeError, OSError) as exc:
        log_check_failure(base, exc)
        return None
    return worker


def cached_update_message(base):
    manifest = cached_manifest(base)
    return available_message(manifest) if manifest else None


def show_update_notes(base):
    with file_lock(base / ".updates/download.lock", exclusive=True, blocking=False):
        manifest, state, _ = refresh_manifest(base, force=True)
        write_json(base / ".updates/check.json", {**state, "checked_at": time.time(), "interval": 86400})
    message = available_message(manifest)
    if message:
        print(message)
    notes = manifest.get("release_notes")
    if notes:
        print(notes)
    else:
        print(f"v{manifest['version']} の配布元は更新内容をJSONに添付していません。")
        print(f"https://github.com/{REPOSITORY}/releases/tag/v{manifest['version']}")


def prepare_update(base, force=False):
    """att updateで通信と検証を行う。起動中のコードは変更しない。"""
    updates = base / ".updates"
    with file_lock(updates / "download.lock", exclusive=True, blocking=False):
        had_pending = (updates / "pending").exists()
        if pending_metadata(base) is not None:
            return "更新の準備ができています。終了時に適用します。"
        result = refresh_manifest(base, force=force or had_pending)
        if result is None:
            return None
        manifest, state, deadline = result
        from .version import VERSION
        version = manifest["version"]
        if not force and version == state.get("skip_version"):
            write_json(updates / "check.json", {**state, "checked_at": time.time(), "interval": 86400})
            return None
        tag = "v" + version
        if version_tuple(version) <= version_tuple(VERSION):
            write_json(updates / "check.json", {**state, "checked_at": time.time(), "interval": 86400})
            if version_tuple(version) == version_tuple(VERSION):
                return f"更新なし: 最新版です（現在 {VERSION}／公開版 {tag}）。"
            return f"更新なし: 使用中の {VERSION} は公開版 {tag} より新しいバージョンです。"
        url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{ASSET_NAME}"
        try:
            data = fetch(url, MAX_ARCHIVE, deadline)
        except HTTPError as exc:
            record_rate_limit(updates, state, exc)
            raise
        if len(data) != manifest["archive"]["size"]:
            raise UpdateError("更新ファイルのサイズが更新情報JSONと一致しません。")
        if hashlib.sha256(data).hexdigest() != manifest["archive"]["sha256"]:
            raise UpdateError("更新ファイルのチェックサムが一致しません。")
        with tempfile.TemporaryDirectory(dir=updates, prefix="download-") as temp:
            temporary = Path(temp)
            archive = temporary / "release.zip"
            archive.write_bytes(data)
            payload = temporary / "payload"
            validate_payload(archive, payload, tag)
            from .dependencies import read_spec, prepare_environment
            spec = read_spec(payload)
            previous_spec = read_spec(base) if (base / "src/dependency_lock.py").is_file() else None
            target_runtime = read_runtime_state(base)
            if target_runtime is not None or previous_spec is None or previous_spec["lock_hash"] != spec["lock_hash"]:
                print("更新版の依存パッケージとChromiumを準備しています…", flush=True)
                target_runtime = prepare_environment(base, spec)
            python = str(runtime_directory(base, target_runtime) / "bin/python") if target_runtime is not None else sys.executable
            environment = runtime_environment(base, target_runtime) if target_runtime is not None else dict(os.environ)
            result = subprocess.run([python, str(payload / "main.py"), "--help"],
                                    capture_output=True, timeout=20,
                                    env={**environment, "ATTENDANCE_NO_UPDATE": "1"})
            if result.returncode:
                raise UpdateError("更新版の起動確認に失敗しました。現在の版を継続します。")
            shutil.rmtree(payload / ".updates", ignore_errors=True)
            # 起動確認で作成されたキャッシュは配布対象にしない。
            for cache in payload.rglob("__pycache__"):
                shutil.rmtree(cache)
            write_json(payload / "metadata.json", {"version": tag, "before": fingerprint(base),
                                                    "files": fingerprint(payload), "runtime": target_runtime})
            os.replace(payload, updates / "pending")
        state.pop("skip_version", None)
        write_json(updates / "check.json", {**state, "checked_at": time.time(), "interval": 86400})
        return f"{tag} をダウンロードしました。終了時に適用します。"


def recover(base):
    """中断された更新は次回のアプリ読込前に元のファイルへ戻す。"""
    transaction = base / ".updates/transaction"
    if not (transaction / "journal.json").exists():
        return
    journal = read_json(transaction / "journal.json")
    # 復元途中でバックアップ不足が判明しないよう、変更前に全件を確認する。
    restored = []
    try:
        if not isinstance(journal, dict) or not journal:
            raise ValueError
        for name, mode in journal.items():
            if not isinstance(name, str) or (mode is not None and (type(mode) is not int or not 0 <= mode <= 0o7777)):
                raise ValueError
            target = safe_target(base, name)
            data = None if mode is None else safe_target(transaction / "files", name).read_bytes()
            restored.append((target, mode, data))
        runtime_path = transaction / "runtime.json"
        previous_runtime = read_json(runtime_path, "invalid") if runtime_path.exists() else None
        if previous_runtime is not None:
            runtime_directory(base, previous_runtime)
    except (ValueError, OSError, UpdateError) as exc:
        raise UpdateError("更新前の版の復元データが破損または不足しています。.updates/transaction を保持して確認してください。") from exc
    set_runtime_state(base, previous_runtime)
    for target, mode, data in restored:
        if mode is None:
            target.unlink(missing_ok=True)
        else:
            atomic_write(target, data, mode)
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
    metadata = pending_metadata(base)
    if not metadata:
        cleanup_environments(base)
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
    write_json(transaction / "runtime.json", read_runtime_state(base))
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
        if metadata.get("runtime") is not None:
            set_runtime_state(base, metadata["runtime"])
    except Exception:
        recover(base)
        raise
    # 成功後も直前の版を残す。次回更新時に入れ替える。
    shutil.rmtree(updates / "backup", ignore_errors=True)
    os.replace(transaction, updates / "backup")
    shutil.rmtree(pending)
    cleanup_environments(base)
    return f"{metadata['version']} への更新が完了しました。次回起動から使用します。"


def update_command(base, argv):
    parser = argparse.ArgumentParser(prog="att update", description="最新版を取得し、終了時に適用します。")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--rollback", action="store_true", help="直前の更新前の版に戻します。")
    group.add_argument("--notes", action="store_true", help="公開版の更新内容を表示します。取得・適用はしません。")
    args = parser.parse_args(argv)
    if args.notes:
        show_update_notes(base)
        return 0
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
        cleanup_environments(base)
        return "更新前の版に戻しました。次回起動から使用します。"
    return apply_pending(base)


def run_application(base, argv=None, *, use_runtime=False):
    argv = sys.argv[1:] if argv is None else argv
    updates = base / ".updates"
    automatic = (not os.environ.get("ATTENDANCE_NO_UPDATE") and "--scheduled" not in argv
                 and "--demo" not in argv and "--help" not in argv and "-h" not in argv
                 and "--version" not in argv
                 and (not argv or argv[0] not in {"update", "user", "schedule"}))
    manual_update = bool(argv and argv[0] == "update" and not any(flag in argv for flag in ("--help", "-h", "--notes")))
    try:
        with file_lock(updates / "runtime.lock") as runtime:
            if (updates / "transaction/journal.json").exists():
                fcntl.flock(runtime, fcntl.LOCK_UN)
                fcntl.flock(runtime, fcntl.LOCK_EX)
                recover(base)
                fcntl.flock(runtime, fcntl.LOCK_SH)
            if use_runtime:
                activate_runtime(base, argv, runtime)
            shown_message = None
            if automatic:
                if "--ui" not in argv:
                    shown_message = cached_update_message(base)
                    if shown_message:
                        print(shown_message, file=sys.stderr)
                start_update_check(base)
            code = 1
            try:
                if argv and argv[0] == "update":
                    code = update_command(base, argv[1:])
                else:
                    code = importlib.import_module("main").main(argv)
            finally:
                if automatic and "--ui" not in argv:
                    # 入力中の端末へワーカーから割り込まず、完了済みの新情報だけを案内する。
                    message = cached_update_message(base)
                    if message and message != shown_message:
                        print(message, file=sys.stderr)
                # 明示的な更新が成功した時だけ、他の実行がなければ適用する。
                if manual_update and code == 0:
                    fcntl.flock(runtime, fcntl.LOCK_UN)
                    try:
                        fcntl.flock(runtime, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        if (updates / "pending").exists() or (updates / "rollback.json").exists():
                            print("更新待機: 別の処理が実行中です。終了後に att update を再実行してください。", file=sys.stderr)
                            if manual_update:
                                code = 1
                    else:
                        try:
                            message = finish_update(base)
                            if message:
                                print(message, file=sys.stderr)
                        except Exception as exc:
                            print(update_failure_message(exc), file=sys.stderr)
                            if (updates / "transaction/journal.json").exists():
                                raise UpdateError("復元が未完了です。次回起動時に復元を再試行します。")
                            if manual_update:
                                code = 1
            return code
    except (UpdateError, OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print(update_failure_message(exc), file=sys.stderr)
        return 1


def background_update(base):
    """以前の起動経路との互換用。更新情報の確認だけを行う。"""
    try:
        with file_lock(base / ".updates/runtime.lock") as runtime:
            if (base / ".updates/transaction/journal.json").exists():
                return
            message = check_update(base)
            (base / ".updates/notice.json").unlink(missing_ok=True)
            if message:
                print(message, flush=True)
    except BlockingIOError:
        pass
    except Exception as exc:
        message = update_failure_message(exc)
        print(message, flush=True)
        try:
            write_json(base / ".updates/notice.json", {"message": message})
        except OSError:
            pass


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--background":
        importlib.import_module("src.updater").background_update(Path(sys.argv[2]).resolve())
