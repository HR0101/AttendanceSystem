"""学生番号と macOS Keychain の境界。"""

import getpass
import json
import os
import sys
import tempfile
from pathlib import Path


SERVICE = "UniversityAttendanceTool"


class CredentialError(Exception):
    pass


def keychain_backend():
    if sys.platform != "darwin":
        raise CredentialError("認証情報の保存には macOS Keychain が必要です。")
    try:
        import keyring
        from keyring.backends.macOS import Keyring as MacOSKeyring
        backend = keyring.get_keyring()
        if not isinstance(backend, MacOSKeyring):
            raise CredentialError("macOS Keychain を利用できません。")
    except ImportError as exc:
        raise CredentialError("keyring が見つかりません。セットアップを確認してください。") from exc
    return backend


def _persist_initial_user(path: Path, config: dict, user: dict, disk_before: bytes | None) -> None:
    from .config import load_config, private_file_lock
    from .users import get_users
    with private_file_lock(path):
        disk_now = None
        if isinstance(path, Path):
            if path.is_symlink():
                raise CredentialError("config.json にシンボリックリンクは使用できません。")
            if path.is_file():
                disk_now = path.read_bytes()
        if disk_now is not None and disk_now != disk_before:
            latest = load_config(path)
            users = get_users(latest)
            if any(u["username"] == user["username"] or u["student_id"] == user["student_id"] for u in users):
                raise CredentialError("そのユーザーネームまたは学生番号は登録済みです。")
            latest["users"] = users + [user]
            if not latest.get("default_user"):
                latest["default_user"] = users[0]["username"] if users else user["username"]
            _save_private_json(path, latest)
            config.clear()
            config.update(latest)
            return
        config["users"] = get_users(config) + [user]
        config["default_user"] = user["username"]
        _save_private_json(path, config)


def has_stored_password(student_id: str) -> bool:
    """Keychain に指定した学生番号のパスワードがあるか確認する。値は返さない。"""
    if not student_id:
        return False
    backend = keychain_backend()
    try:
        password = backend.get_password(SERVICE, student_id)
    except CredentialError:
        raise
    except Exception as exc:
        raise CredentialError("Keychain の操作に失敗しました。") from exc
    present = bool(password)
    del password
    return present


def get_credentials(config: dict, path: Path, interactive: bool = True, username: str | None = None) -> tuple[str, str]:
    from .users import get_users, select_user, valid_student_id, valid_username
    backend = keychain_backend()
    user = select_user(config, username)
    student_id = user["student_id"] if user else ""
    creating_user = not student_id
    disk_before = None

    if creating_user:
        if not interactive:
            raise CredentialError("学生番号が未設定です。手動で一度起動してください。")
        if isinstance(path, Path) and path.is_file() and not path.is_symlink():
            disk_before = path.read_bytes()
        student_id = input("学生番号: ").strip()
        if not valid_student_id(student_id):
            raise CredentialError("学生番号を確認してください。")
        name = input("ユーザーネーム: ").strip()
        if not valid_username(name):
            raise CredentialError("ユーザーネームは1～60文字で入力してください。")
        if any(u["username"] == name or u["student_id"] == student_id for u in get_users(config)):
            raise CredentialError("そのユーザーネームまたは学生番号は登録済みです。")
        user = {"username": name, "student_id": student_id}

    try:
        password = backend.get_password(SERVICE, student_id)
    except Exception as exc:
        raise CredentialError("Keychain の操作に失敗しました。") from exc

    if password is None:
        if not interactive:
            raise CredentialError("Keychainにパスワードがありません。手動で一度起動してください。")
        password = getpass.getpass("パスワード (macOS Keychain に保存): ")
        if not password:
            raise CredentialError("パスワードが入力されていません。")
        try:
            backend.set_password(SERVICE, student_id, password)
            if backend.get_password(SERVICE, student_id) != password:
                raise CredentialError("Keychain への保存を確認できません。")
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError("Keychain の操作に失敗しました。") from exc

    # Keychain への保存・照合が完了してから config.json を更新し、中断時の半端な登録を防ぐ。
    if creating_user:
        _persist_initial_user(path, config, user, disk_before)

    config["_active_username"] = user["username"]
    return student_id, password


def _save_private_json(path: Path, config: dict) -> None:
    from .config import private_file_lock
    with private_file_lock(path):
        if isinstance(path, Path) and path.is_symlink():
            raise CredentialError("config.json にシンボリックリンクは使用できません。")
        fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({key: value for key, value in config.items() if not key.startswith("_")}, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
