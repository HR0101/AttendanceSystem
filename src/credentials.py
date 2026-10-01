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


def get_credentials(config: dict, path: Path) -> tuple[str, str]:
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

    student_id = config.get("student_id", "").strip()
    if not student_id:
        student_id = input("学生番号: ").strip()
        if not student_id or any(char.isspace() for char in student_id):
            raise CredentialError("学生番号を確認してください。")
        config["student_id"] = student_id
        _save_private_json(path, config)
    try:
        password = keyring.get_password(SERVICE, student_id)
        if password is None:
            password = getpass.getpass("パスワード (macOS Keychain に保存): ")
            if not password:
                raise CredentialError("パスワードが入力されていません。")
            keyring.set_password(SERVICE, student_id, password)
            if keyring.get_password(SERVICE, student_id) != password:
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
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
