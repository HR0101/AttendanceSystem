"""ユーザー管理のCLIと、UIから使う設定・Keychainの保存操作。"""

import argparse
import getpass

from .config import ConfigurationError, load_config, private_file_lock
from .credentials import CredentialError, SERVICE, _save_private_json, keychain_backend
from .users import (
    change_student_id_config, get_users, remove_user_config, select_user,
    valid_student_id, valid_username,
)


def add_user(base, name, student_id, password):
    path = base / "config.json"
    with private_file_lock(path):
        config = load_config(path)
        users = get_users(config)
        default = select_user(config)
        if not valid_username(name):
            raise CredentialError("ユーザーネームは制御文字を含まない1～60文字にしてください。")
        if not valid_student_id(student_id):
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
    with private_file_lock(path):
        config = load_config(path)
        user = select_user(config, name)
        config["users"] = get_users(config)
        config["default_user"] = user["username"]
        _save_private_json(path, config)
        return config


def activate_default_user(base, name):
    """GUIの既定と出席対象を一緒に変更する。認証失敗なら設定を保存しない。"""
    path = base / "config.json"
    with private_file_lock(path):
        config = load_config(path)
        user = select_user(config, name)
        student_id = user["student_id"]
        password = _saved_password(keychain_backend(), student_id)
        if not password:
            raise CredentialError("保存パスワードがありません。ユーザー管理のPで設定してから、もう一度Dで切り替えてください。")
        config["users"] = get_users(config)
        config["default_user"] = user["username"]
        config["ui_selected_users"] = [student_id]
        _save_private_json(path, config)
        return config, student_id, password


def save_ui_selection(base, student_ids):
    path = base / "config.json"
    with private_file_lock(path):
        config = load_config(path)
        users = get_users(config)
        requested = set(student_ids)
        if not requested or not requested.issubset({user["student_id"] for user in users}):
            raise CredentialError("ユーザー一覧が変更されています。画面を開き直して選択してください。")
        config["ui_selected_users"] = [user["student_id"] for user in users if user["student_id"] in requested]
        _save_private_json(path, config)
        return config


def rename_user(base, name, new_name):
    path = base / "config.json"
    with private_file_lock(path):
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
    path = base / "config.json"
    with private_file_lock(path):
        config = load_config(path)
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


def _set_saved_password(backend, student_id, password):
    try:
        if password is None:
            if backend.get_password(SERVICE, student_id) is not None:
                backend.delete_password(SERVICE, student_id)
        else:
            backend.set_password(SERVICE, student_id, password)
        if backend.get_password(SERVICE, student_id) != password:
            raise CredentialError("Keychain の変更を確認できません。")
    except CredentialError:
        raise
    except Exception as exc:
        raise CredentialError("Keychain の操作に失敗しました。") from exc


def _save_account_change(path, config, backend, before, after):
    try:
        for student_id, password in after.items():
            _set_saved_password(backend, student_id, password)
        _save_private_json(path, config)
    except BaseException:
        try:
            # 元の番号を先に復元してから、新番号のコピーを取り除く。
            for student_id, password in before.items():
                _set_saved_password(backend, student_id, password)
        except Exception as exc:
            raise CredentialError("設定変更を完了できず、保存認証情報の復元にも失敗しました。Keychainの状態を確認してください。") from exc
        raise
    return config


def _saved_password(backend, student_id):
    try:
        return backend.get_password(SERVICE, student_id)
    except Exception as exc:
        raise CredentialError("Keychain の操作に失敗しました。") from exc


def remove_user(base, name):
    path = base / "config.json"
    with private_file_lock(path):
        original = load_config(path)
        user = select_user(original, name)
        config = remove_user_config(original, name)
        backend = keychain_backend()
        password = _saved_password(backend, user["student_id"])
        return _save_account_change(path, config, backend, {user["student_id"]: password}, {user["student_id"]: None})


def change_student_id(base, name, student_id):
    path = base / "config.json"
    with private_file_lock(path):
        original = load_config(path)
        user = select_user(original, name)
        config = change_student_id_config(original, name, student_id)
        if user["student_id"] == student_id:
            return original
        backend = keychain_backend()
        password = _saved_password(backend, user["student_id"])
        if _saved_password(backend, student_id) is not None:
            raise CredentialError("その学籍番号の認証情報がKeychainにあります。上書きせずに変更を中止しました。")
        return _save_account_change(path, config, backend,
                                    {user["student_id"]: password, student_id: None},
                                    {student_id: password, user["student_id"]: None})


def _confirm_user_removal(user, users, default):
    """削除対象と影響を表示し、明示的な yes 応答時のみ True を返す。"""
    print(f"ユーザー「{user['username']}」（学生番号: {user['student_id']}）を削除します。")
    print("このアプリの登録とKeychainの保存パスワードを削除します（大学側のアカウントは削除しません）。")
    if len(users) == 1:
        print("最後のユーザーです。削除後は att user add で再登録できます。")
    elif user == default:
        next_default = next(entry["username"] for entry in users if entry["student_id"] != user["student_id"])
        print(f"既定ユーザーのため、削除後は「{next_default}」が既定ユーザーになります。")
    return input("削除しますか？ [y/N]: ").strip().lower() in ("y", "yes")


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
    student_id_command = commands.add_parser("student-id", help="学籍番号を変更（保存パスワードを引き継ぐ）")
    student_id_command.add_argument("username", help="対象のユーザーネーム")
    student_id_command.add_argument("new_student_id", help="新しい学籍番号（半角英数字）")
    remove = commands.add_parser("remove", help="登録ユーザーと保存パスワードを削除")
    remove.add_argument("username", help="削除するユーザーネーム")
    remove.add_argument("-y", "--yes", action="store_true", help="確認プロンプトなしで削除")
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
            if not valid_student_id(student_id):
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
        elif args.action == "rename":
            rename_user(base, args.username, args.new_name)
            message = f"ユーザーネームを「{args.new_name}」に変更しました。"
        elif args.action == "student-id":
            user = select_user(config, args.username)
            change_student_id(base, user["username"], args.new_student_id)
            if user["student_id"] == args.new_student_id:
                message = f"ユーザー「{user['username']}」の学籍番号はすでに {args.new_student_id} です。"
            else:
                message = f"ユーザー「{user['username']}」の学籍番号を {user['student_id']} から {args.new_student_id} に変更しました。起動中の別画面には再起動後に反映されます。"
        elif args.action == "remove":
            user = select_user(config, args.username)
            if not args.yes and not _confirm_user_removal(user, users, default):
                print("ユーザーの削除を取り消しました。")
                return 1
            updated = remove_user(base, user["username"])
            remaining = get_users(updated)
            if not remaining:
                message = f"ユーザー「{user['username']}」（学生番号: {user['student_id']}）を削除しました。登録ユーザーはなくなりました。再度利用する場合は att user add で登録してください。"
            elif user == default:
                message = f"ユーザー「{user['username']}」（学生番号: {user['student_id']}）を削除しました。既定ユーザーを「{updated['default_user']}」に切り替えました。"
            else:
                message = f"ユーザー「{user['username']}」（学生番号: {user['student_id']}）を削除しました。"
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
