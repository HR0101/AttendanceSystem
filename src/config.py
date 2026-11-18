"""利用者設定の読み込みと検証。"""

import contextlib
import errno
import fcntl
import json
import os
import stat
import threading
from pathlib import Path
from urllib.parse import urlparse


class ConfigurationError(Exception):
    pass


_LOCK_STATE = threading.local()


def _held_locks() -> dict[str, int]:
    held = getattr(_LOCK_STATE, "held", None)
    if held is None:
        held = {}
        _LOCK_STATE.held = held
    return held


def tighten_fd_mode(fd: int, mode: int) -> None:
    """グループ・他者のアクセス権が付いていれば指定モードへ制限する。"""
    if stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
        os.fchmod(fd, mode)


def read_private_text(path: Path, error_cls: type[Exception], label: str) -> str:
    """シンボリックリンクを拒否し、権限を 0o600 へ締めたうえで UTF-8 テキストを読む。"""
    if not isinstance(path, Path):
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise error_cls(f"{label} がありません。") from exc
    if path.is_symlink():
        raise error_cls(f"{label} にシンボリックリンクは使用できません。")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise error_cls(f"{label} は通常のファイルにしてください。")
        tighten_fd_mode(fd, 0o600)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            return stream.read()
    except FileNotFoundError as exc:
        raise error_cls(f"{label} がありません。") from exc
    except UnicodeDecodeError as exc:
        raise error_cls(f"{label} の文字コードを確認してください。") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise error_cls(f"{label} にシンボリックリンクは使用できません。") from exc
        raise error_cls(f"{label} を読み込めません。") from exc
    finally:
        if fd is not None:
            os.close(fd)


@contextlib.contextmanager
def private_file_lock(path: Path):
    """設定・予定ファイルの read-modify-write 全体を保護する排他ロック。"""
    if not isinstance(path, (str, os.PathLike)):
        yield
        return
    target = Path(path)
    lock_path = target.with_name(f"{target.name}.lock")
    if lock_path.is_symlink():
        raise OSError(f"{lock_path.name} にシンボリックリンクは使用できません。")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = os.path.realpath(lock_path)
    held = _held_locks()
    # 同一スレッド内のネストしたロック取得による flock 自己デッドロックを防ぐ。
    if held.get(key, 0) > 0:
        held[key] += 1
        try:
            yield lock_path
        finally:
            held[key] -= 1
            if held[key] == 0:
                del held[key]
        return
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{lock_path.name} は通常のファイルにしてください。")
        tighten_fd_mode(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        held[key] = 1
        try:
            yield lock_path
        finally:
            held.pop(key, None)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def load_config(path: Path) -> dict:
    try:
        config = json.loads(read_private_text(path, ConfigurationError, "config.json"))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"config.json の {exc.lineno} 行目を確認してください。") from exc
    if not isinstance(config, dict):
        raise ConfigurationError("config.json のルートはオブジェクトにしてください。")
    for key in ("login_url", "top_url"):
        value = config.get(key)
        try:
            parsed = urlparse(value) if isinstance(value, str) else None
            valid_host = parsed and parsed.hostname and not parsed.username and not parsed.password and not parsed.port
        except ValueError as exc:
            raise ConfigurationError(f"{key} は有効な HTTPS のURLにしてください。") from exc
        if not parsed or parsed.scheme != "https" or not valid_host:
            raise ConfigurationError(f"{key} は HTTPS のURLにしてください。")
        if parsed.path != ("/attendance/login" if key == "login_url" else "/attendance/top"):
            raise ConfigurationError(f"{key} のパスを確認してください。")
        if parsed.query or parsed.fragment:
            raise ConfigurationError(f"{key} にクエリやフラグメントは指定できません。")
    if urlparse(config["login_url"]).hostname != urlparse(config["top_url"]).hostname:
        raise ConfigurationError("login_url と top_url のホスト名を一致させてください。")
    student_id = config.get("student_id", "")
    if not isinstance(student_id, str):
        raise ConfigurationError("student_id は文字列にしてください。")
    from .users import validate_users
    validate_users(config)
    browser = config.get("browser", {})
    if not isinstance(browser, dict) or not isinstance(browser.get("headless"), bool):
        raise ConfigurationError("browser.headless は true または false にしてください。")
    timeout = browser.get("timeout_ms")
    if type(timeout) is not int or not 1000 <= timeout <= 120000:
        raise ConfigurationError("browser.timeout_ms は 1000～120000 の整数にしてください。")
    if not isinstance(config.get("dry_run", False), bool):
        raise ConfigurationError("dry_run は true または false にしてください。")
    return config
