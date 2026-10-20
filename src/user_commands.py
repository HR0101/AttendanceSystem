"""登録・一覧・既定ユーザー切替・表示名と保存パスワードの変更。"""

import argparse
import getpass

from .config import ConfigurationError, load_config
from .credentials import CredentialError, SERVICE, _save_private_json, keychain_backend
from .users import get_users, select_user, valid_username


def add_user(base, name, student_id, password):
    path = base / "config.json"
    config = load_config(path)
    users = get_users(config)
    default = select_user(config)
    if not valid_username(name):
        raise CredentialError("ユーザーネームは制御文字を含まない1～60文字にしてください。")
    if not student_id or not student_id.isascii() or not student_id.isalnum():
        raise CredentialError("学生番号は半角英数字で入力してください。")
    if any(u["username"] == name or u["student_id"] == student_id for u in users):
        raise CredentialError("そのユーザーネームまたは学生番号は登録済みです。")
    if not password:
        raise CredentialError("パスワードが入力されていません。")
    backend = keychain_backend()
    try:
        backend.set_password(SERVICE, student_id, password)
        if backend.get_password(SERVICE, student_id) != password:
            raise CredentialError("Keychain への保存を確認できません。")
    except CredentialError:
        raise
    except Exception as exc:
        raise CredentialError("Keychain の操作に失敗しました。") from exc
    config["users"] = users + [{"username": name, "student_id": student_id}]
    config["default_user"] = default["username"] if default else name
    _save_private_json(path, config)
    return config


def use_user(base, name):
    path = base / "config.json"
    config = load_config(path)
    user = select_user(config, name)
    config["users"] = get_users(config)
    config["default_user"] = user["username"]
    _save_private_json(path, config)
    return config


def rename_user(base, name, new_name):
    path = base / "config.json"
    config = load_config(path)
    user = select_user(config, name)
    default = select_user(config)
    users = get_users(config)
    if not valid_username(new_name):
        raise CredentialError("ユーザーネームは制御文字を含まない1～60文字にしてください。")
    if any(u["username"] == new_name and u["student_id"] != user["student_id"] for u in users):
        raise CredentialError("そのユーザーネームは登録済みです。")
    for entry in users:
        if entry["student_id"] == user["student_id"]:
            entry["username"] = new_name
    if default == user:
        config["default_user"] = new_name
    config["users"] = users
    _save_private_json(path, config)
    return config


def change_password(base, name, password):
    """大学側で変更済みのパスワードをKeychainへ保存する。"""
    config = load_config(base / "config.json")
    user = select_user(config, name)
    if user is None:
        raise CredentialError("登録ユーザーがありません。att user add で登録してください。")
    if not password:
        raise CredentialError("新しいパスワードが入力されていません。")
    backend = keychain_backend()
    try:
        backend.set_password(SERVICE, user["student_id"], password)
        if backend.get_password(SERVICE, user["student_id"]) != password:
            raise CredentialError("Keychain への保存を確認できません。")
    except CredentialError:
        raise
    except Exception as exc:
        raise CredentialError("Keychain の操作に失敗しました。") from exc
    return config


def user_command(base, argv):
    parser = argparse.ArgumentParser(prog="att user", description="利用者を管理します。")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="登録ユーザーの一覧")
    add = commands.add_parser("add", help="ユーザーを追加")
    add.add_argument("username", nargs="?", help="表示用のユーザーネーム")
    add.add_argument("--student-id", help="学生番号")
    use = commands.add_parser("use", help="通常実行・自動実行の既定ユーザーを変更")
    use.add_argument("username")
    rename = commands.add_parser("rename", help="ユーザーネームを変更")
    rename.add_argument("username")
    rename.add_argument("new_name")
    password_command = commands.add_parser("password", help="Keychainに保存したパスワードを更新")
    password_command.add_argument("username", nargs="?", help="省略時は既定ユーザー")
    args = parser.parse_args(argv)
    try:
        path = base / "config.json"
        config = load_config(path)
        users = get_users(config)
        default = select_user(config)
        if args.action == "list":
            if not users:
                print("登録ユーザーなし。att user add で登録してください。")
            for user in users:
                mark = "*" if user == default else " "
                print(f"{mark} {user['username']}  学生番号: {user['student_id']}")
            if users:
                print("* 既定ユーザー（通常実行・自動実行）")
            return 0
        if args.action == "add":
            name = (args.username if args.username is not None else input("ユーザーネーム: ")).strip()
            student_id = (args.student_id if args.student_id is not None else input("学生番号: ")).strip()
            if not valid_username(name):
                raise CredentialError("ユーザーネームは制御文字を含まない1～60文字にしてください。")
            if not student_id or not student_id.isascii() or not student_id.isalnum():
                raise CredentialError("学生番号は半角英数字で入力してください。")
            if any(u["username"] == name or u["student_id"] == student_id for u in users):
                raise CredentialError("そのユーザーネームまたは学生番号は登録済みです。表示名の変更には att user rename を使ってください。")
            password = getpass.getpass("パスワード (macOS Keychain に保存): ")
            try:
                add_user(base, name, student_id, password)
            finally:
                del password
            message = f"ユーザー「{name}」を登録しました。"
        elif args.action == "password":
            user = select_user(config, args.username)
            if user is None:
                raise CredentialError("登録ユーザーがありません。att user add で登録してください。")
            print(f"ユーザー「{user['username']}」（学生番号: {user['student_id']}）の保存パスワードを更新します。")
            print("大学側で変更済みのパスワードを入力してください。")
            password = getpass.getpass("新しいパスワード: ")
            confirmation = ""
            try:
                if not password:
                    raise CredentialError("新しいパスワードが入力されていません。")
                confirmation = getpass.getpass("新しいパスワード（確認）: ")
                if password != confirmation:
                    raise CredentialError("確認用のパスワードが一致しません。")
                change_password(base, user["username"], password)
            finally:
                del password, confirmation
            message = f"ユーザー「{user['username']}」の保存パスワードを更新しました。起動中の別画面には再起動後に反映されます。"
        elif args.action == "use":
            use_user(base, args.username)
            message = f"既定ユーザーを「{args.username}」に変更しました。"
        else:
            rename_user(base, args.username, args.new_name)
            message = f"ユーザーネームを「{args.new_name}」に変更しました。"
        print(message)
        return 0
    except (EOFError, KeyboardInterrupt):
        print("ユーザー設定の変更を取り消しました。")
        return 1
    except (ConfigurationError, CredentialError, OSError) as exc:
        print(f"ユーザー設定を変更できません: {exc}")
        return 1
    except Exception:
        print("ユーザー設定を変更できません。Keychain の状態を確認してください。")
        return 1
