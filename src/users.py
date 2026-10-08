"""表示名付きの利用者一覧と既定ユーザー。パスワードは保持しない。"""

import unicodedata

from .config import ConfigurationError


def validate_users(config):
    users = config.get("users", [])
    if not isinstance(users, list):
        raise ConfigurationError("users は配列にしてください。")
    names, ids = set(), set()
    for user in users:
        if not isinstance(user, dict):
            raise ConfigurationError("users の各項目はオブジェクトにしてください。")
        name, student_id = user.get("username"), user.get("student_id")
        if not valid_username(name):
            raise ConfigurationError("ユーザーネームは空白だけや制御文字を含まない1～60文字にしてください。")
        if not isinstance(student_id, str) or not student_id or not student_id.isascii() or not student_id.isalnum():
            raise ConfigurationError("ユーザーの student_id は半角英数字にしてください。")
        if name in names or student_id in ids:
            raise ConfigurationError("ユーザーネームと学生番号は重複できません。")
        names.add(name)
        ids.add(student_id)
    default = config.get("default_user", "")
    if not isinstance(default, str) or (default and default not in {u["username"] for u in get_users(config)}):
        raise ConfigurationError("default_user は登録済みのユーザーネームにしてください。")


def valid_username(value):
    return isinstance(value, str) and 1 <= len(value) <= 60 and value == value.strip() and all(not unicodedata.category(c).startswith("C") for c in value)


def get_users(config):
    users = [dict(user) for user in config.get("users", [])]
    legacy = config.get("student_id", "").strip()
    if legacy and not any(u["student_id"] == legacy for u in users):
        name = legacy
        while any(u["username"] == name for u in users):
            name += " (既存)"
        users.insert(0, {"username": name, "student_id": legacy})
    return users


def select_user(config, username=None):
    users = get_users(config)
    selected = username if username is not None else config.get("default_user")
    if selected:
        for user in users:
            if user["username"] == selected:
                return user
        raise ConfigurationError(f"ユーザー「{selected}」は未登録です。att user list で確認してください。")
    return users[0] if users else None
