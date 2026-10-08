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


def get_credentials(config: dict, path: Path, interactive: bool = True, username: str | None = None) -> tuple[str, str]:
    from .users import get_users, select_user, valid_username
    backend = keychain_backend()
    user = select_user(config, username)
    student_id = user["student_id"] if user else ""

    if not student_id:
        if not interactive:
            raise CredentialError("学生番号が未設定です。手動で一度起動してください。")
        student_id = input("学生番号: ").strip()
        if not student_id or not student_id.isascii() or not student_id.isalnum():
            raise CredentialError("学生番号を確認してください。")
        name = input("ユーザーネーム: ").strip()
        if not valid_username(name):
            raise CredentialError("ユーザーネームは1～60文字で入力してください。")
        user = {"username": name, "student_id": student_id}
        config["users"] = get_users(config) + [user]
        config["default_user"] = name
        _save_private_json(path, config)
    config["_active_username"] = user["username"]
    try:
        password = backend.get_password(SERVICE, student_id)
        if password is None:
            if not interactive:
                raise CredentialError("Keychainにパスワードがありません。手動で一度起動してください。")
            password = getpass.getpass("パスワード (macOS Keychain に保存): ")
            if not password:
                raise CredentialError("パスワードが入力されていません。")
            backend.set_password(SERVICE, student_id, password)
            if backend.get_password(SERVICE, student_id) != password:
                raise CredentialError("Keychain への保存を確認できません。")
        return student_id, password
    except CredentialError:
        raise
    except Exception as exc:
        raise CredentialError("Keychain の操作に失敗しました。") from exc


def _save_private_json(path: Path, config: dict) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({key: value for key, value in config.items() if not key.startswith("_")}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
