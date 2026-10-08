"""標準ライブラリだけで動く出席確認画面。"""

import curses
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from .config import ConfigurationError, load_config
from .credentials import CredentialError, get_credentials
from .logging_utils import setup_logging
from .room import RoomError, parse_room
from .verification import AttendanceError
from .terminal_layout import Canvas, color, fit_text, text_width, wrap_text
from .users import get_users, select_user, valid_username


class AttendanceScreen:
    def __init__(self, config, student_id, password, logger, room="", demo=False, base=None):
        self.config, self.student_id, self.password = config, student_id, password
        self.logger, self.room, self.demo = logger, room, demo
        self.values = {}
        self.attended = self.unenrolled = self.busy = self.confirm = False
        self.selected = 0
        self.message = "教室番号を入力して、Enter で授業を確認してください。"
        self.events = queue.Queue()
        self.updated = "未確認"
        self.base = base
        self.history = deque(maxlen=8)
        self.checked = self.failed = False
        self.modal = None
        self.user_index = 0
        self.form_values = []
        self.form_index = 0
        self.form_error = ""
        self.confirm_selection = 0
        self.demo_state = "unattended"
        self.demo_attended = set()

    def clear_target(self):
        self.values = {}
        self.attended = self.unenrolled = self.checked = self.failed = self.confirm = False
        self.updated = "未確認"

    def status(self):
        if self.busy:
            return "|/-\\"[int(time.monotonic() * 8) % 4] + " 処理中", 1, "完了までお待ちください"
        if self.failed:
            return "確認できませんでした", 3, "R で再試行できます"
        if self.unenrolled:
            return "履修登録を確認してください", 3, "出席登録は利用できません"
        if self.attended and self.values:
            return "出席済み", 2, "この授業の出席は登録されています"
        if self.values:
            return "未出席", 4, "確認のみモードです" if self.config.get("dry_run") else "授業情報を確認して出席登録できます"
        if self.checked:
            return "現在の受付対象なし", 4, "この時間に出席できる授業はありません"
        return "授業を確認しましょう", 1, "QR番号を入力して Enter を押してください"

    def can_register(self):
        return bool(self.values) and not (self.busy or self.attended or self.unenrolled or self.config.get("dry_run", False))

    def start(self, register=False):
        try:
            room, route = parse_room(self.room)
        except RoomError as exc:
            self.message = str(exc)
            return
        if register and not self.can_register():
            return
        self.busy, self.confirm = True, False
        self.failed = False
        self.message = "出席登録中…" if register else "ログインして授業情報を取得中…"
        expected = dict(self.values) if register else {}
        # 古い授業情報は、新しい取得に成功するまで登録に利用しない。
        self.values = {}
        self.selected = 1

        def work():
            try:
                if self.demo:
                    if self.demo_state == "error":
                        raise AttendanceError("デモの通信エラーです。Rで再確認できます。")
                    if register:
                        self.demo_attended.add((self.student_id, room))
                    attended = self.demo_state == "attended" or (self.student_id, room) in self.demo_attended
                    unenrolled = self.demo_state == "unenrolled"
                    values = {} if self.demo_state == "empty" else {"授業名": "システムズエンジニアリング（デモ）", "時限": "2-3限", "教室名": f"{'432' if room == '435' else room}講義室", "開始時刻": "10:00", "終了時刻": "12:00"}
                    self.events.put(("snapshot", (values, attended, unenrolled)))
                    result = "出席受付対象なし（デモ）" if not values else "履修登録なし（デモ）" if unenrolled else "デモ: 出席済み" if attended else "デモ: 未出席（大学には接続していません）"
                    self.events.put(("result", result))
                else:
                    from .attendance import run_attendance
                    from .browser import open_page
                    config = dict(self.config, dry_run=not register)
                    with open_page(config) as page:
                        result = run_attendance(
                            page, config, self.student_id, self.password, room, route, self.logger,
                            expected.get("授業名"), expected.get("開始時刻"),
                            on_snapshot=lambda values, attended, unenrolled: self.events.put(
                                ("snapshot", (values, attended, unenrolled))),
                            expected_values=expected if register else None,
                        )
                    self.logger.info("画面操作 結果=%s", result)
                    self.events.put(("result", result))
            except (ConfigurationError, CredentialError, AttendanceError, RoomError) as exc:
                self.logger.error("画面操作停止 種別=%s", type(exc).__name__)
                self.events.put(("error", str(exc)))
            except Exception as exc:
                self.logger.error("画面操作停止 種別=%s", type(exc).__name__)
                self.events.put(("error", "通信または画面状態を確認してください。詳細は logs を参照してください。"))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        while True:
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "snapshot":
                self.values, self.attended, self.unenrolled = data
                self.checked = True
            elif kind == "account":
                config, student_id, password, action = data
                config["dry_run"] = self.config.get("dry_run", False)
                self.config, self.student_id, self.password = config, student_id, password
                self.busy = False
                self.modal = None
                self.message = {"switch": "ユーザーを切り替えました。", "add": "ユーザーを登録しました。Uで選択できます。", "rename": "ユーザーネームを変更しました。", "default": "既定ユーザーを変更しました。通常実行・自動実行に反映されます。"}[action]
                if action == "switch" and self.room:
                    self.start()
            elif kind == "account_error":
                self.busy = False
                self.form_error = data
                self.message = data
            else:
                self.busy = False
                self.message = data
                self.updated = datetime.now().strftime("%H:%M:%S")
                if kind == "error":
                    self.values = {}
                    self.failed = True
                elif data in ("出席登録成功", "出席済み", "デモ: 出席済み"):
                    self.attended = True
                self.history.appendleft((self.updated, self.room, self.message))

    def account_action(self, action, name, new_name="", student_id="", password=""):
        if self.busy:
            return
        if action == "switch":
            self.clear_target()
        self.busy = True
        self.form_error = ""
        self.message = "ユーザー情報を処理中…"
        # パスワード入力は送信後すぐにフォームから消す。
        self.form_values = []
        self.modal = "users"

        def work():
            try:
                from .user_commands import add_user, rename_user, use_user
                if self.demo:
                    config = dict(self.config, users=get_users(self.config))
                    if action == "add":
                        if not valid_username(name) or not student_id or not student_id.isascii() or not student_id.isalnum():
                            raise CredentialError("ユーザーネームと半角英数字の学生番号を入力してください。")
                        if any(u["username"] == name or u["student_id"] == student_id for u in config["users"]):
                            raise CredentialError("その名前または学生番号は登録済みです。")
                        config["users"].append({"username": name, "student_id": student_id})
                    elif action == "rename":
                        if not valid_username(new_name) or any(u["username"] == new_name and u["username"] != name for u in config["users"]):
                            raise CredentialError("そのユーザーネームは使えません。")
                        for user in config["users"]:
                            if user["username"] == name:
                                user["username"] = new_name
                        if config.get("default_user") == name:
                            config["default_user"] = new_name
                    elif action == "default":
                        config["default_user"] = select_user(config, name)["username"]
                else:
                    if self.base is None:
                        raise CredentialError("設定ファイルの場所を確認できません。")
                    if action == "add":
                        config = add_user(self.base, name, student_id, password)
                    elif action == "rename":
                        config = rename_user(self.base, name, new_name)
                    elif action == "default":
                        config = use_user(self.base, name)
                    else:
                        config = load_config(self.base / "config.json")
                current_id, current_password = self.student_id, self.password
                if action == "switch":
                    if self.demo:
                        current_id, current_password = select_user(config, name)["student_id"], ""
                    else:
                        current_id, current_password = get_credentials(config, self.base / "config.json", interactive=False, username=name)
                config["_active_username"] = next((u["username"] for u in get_users(config) if u["student_id"] == current_id), current_id)
                self.events.put(("account", (config, current_id, current_password, action)))
            except (ConfigurationError, CredentialError, OSError) as exc:
                self.events.put(("account_error", str(exc)))
            except Exception:
                self.events.put(("account_error", "ユーザー情報を処理できません。Keychain の状態を確認してください。"))

        threading.Thread(target=work, daemon=True).start()

    def open_users(self):
        self.modal = "users"
        self.form_error = ""
        users = get_users(self.config)
        self.user_index = next((i for i, u in enumerate(users) if u["student_id"] == self.student_id), 0)

    def open_form(self, action):
        users = get_users(self.config)
        if action == "rename" and not users:
            return
        self.modal = action
        self.form_index = 0
        self.form_error = ""
        self.form_values = ["", "", ""] if action == "add" else [users[self.user_index]["username"]]

    def handle_modal(self, key):
        if key == "\x1b":
            self.form_values = []
            self.modal = None
            return
        enter = key in ("\n", "\r", curses.KEY_ENTER)
        if self.modal == "help":
            if enter or key in ("h", "H", "?"):
                self.modal = None
            return
        if self.modal == "users":
            users = get_users(self.config)
            if key in (curses.KEY_UP, curses.KEY_DOWN) and users:
                self.user_index = (self.user_index + (1 if key == curses.KEY_DOWN else -1)) % len(users)
            elif enter and users:
                self.account_action("switch", users[self.user_index]["username"])
            elif key in ("a", "A"):
                self.open_form("add")
            elif key in ("n", "N"):
                self.open_form("rename")
            elif key in ("d", "D") and users:
                self.modal = "default"
            return
        if self.modal == "default":
            if enter:
                users = get_users(self.config)
                self.account_action("default", users[self.user_index]["username"])
            return
        if self.modal in ("add", "rename"):
            if enter and self.form_index == len(self.form_values) - 1:
                if self.modal == "add":
                    name, student_id, password = self.form_values
                    if not valid_username(name.strip()) or not student_id.strip() or not student_id.strip().isascii() or not student_id.strip().isalnum() or not password:
                        self.form_error = "名前・半角英数字の学生番号・パスワードを入力してください。"
                        return
                    if any(u["username"] == name.strip() or u["student_id"] == student_id.strip() for u in get_users(self.config)):
                        self.form_error = "その名前または学生番号は登録済みです。"
                        return
                    self.account_action("add", self.form_values[0].strip(), student_id=self.form_values[1].strip(), password=self.form_values[2])
                else:
                    users = get_users(self.config)
                    new_name = self.form_values[0].strip()
                    if not valid_username(new_name):
                        self.form_error = "新しいユーザーネームを1～60文字で入力してください。"
                        return
                    if any(u["username"] == new_name and u["student_id"] != users[self.user_index]["student_id"] for u in users):
                        self.form_error = "そのユーザーネームは登録済みです。"
                        return
                    self.account_action("rename", users[self.user_index]["username"], new_name=self.form_values[0].strip())
            elif enter or key in ("\t", curses.KEY_DOWN, curses.KEY_UP, curses.KEY_BTAB):
                self.form_index = (self.form_index + (-1 if key in (curses.KEY_UP, curses.KEY_BTAB) else 1)) % len(self.form_values)
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                self.form_values[self.form_index] = self.form_values[self.form_index][:-1]
            elif key == "\x15":
                self.form_values[self.form_index] = ""
            elif isinstance(key, str) and key.isprintable():
                limit = 256 if self.modal == "add" and self.form_index == 2 else 60
                if len(self.form_values[self.form_index]) < limit:
                    self.form_values[self.form_index] += key

    def draw(self, screen):
        screen.erase()
        canvas = Canvas(screen)
        height, width = canvas.height, canvas.width
        if height < 24 or width < 62:
            canvas.put(1, 1, "画面を広げてください（幅62 × 高さ24以上）。", curses.A_BOLD)
            canvas.put(3, 1, self.status()[0])
            canvas.put(5, 1, "Q: 終了" if not self.busy else "処理中… 完了までお待ちください。")
            screen.refresh()
            return

        wide = width >= 106
        total = min(width - 4, 112 if wide else 78)
        left = (width - total) // 2
        main_width = total - 32 if wide else total
        name = self.config.get("_active_username", self.student_id)
        default = select_user(self.config)
        is_default = default and default["student_id"] == self.student_id
        canvas.put(1, left, "ATTENDANCE", color(1) | curses.A_BOLD)
        canvas.put(1, left + 13, "/ 出席確認", curses.A_BOLD)
        mode = "DEMO / 接続なし" if self.demo else "確認のみ" if self.config.get("dry_run") else "出席登録"
        canvas.put(1, left + total - 20, mode, color(4), 20)
        canvas.put(2, left, f"{name}  {'[既定]' if is_default else '[今回のユーザー]'}", curses.A_BOLD, main_width - 1)
        canvas.put(3, left, f"学生番号 {self.student_id}    U: ユーザー管理", curses.A_DIM, main_width - 1)
        canvas.button(5, left, f"QR番号 [ {self.room or '___'} ]", self.selected == 0)
        canvas.button(5, left + 28, "[ 更新 R ]", self.selected == 1, not self.busy)
        title, tone, detail = self.status()
        canvas.fill(7, left, main_width, 2, color(tone) | curses.A_REVERSE)
        canvas.put(7, left + 2, title, color(tone) | curses.A_REVERSE | curses.A_BOLD, main_width - 4)
        canvas.put(8, left + 2, detail, color(tone) | curses.A_REVERSE, main_width - 4)

        canvas.box(10, left, main_width, 7, "授業情報", color(1))
        course = self.values.get("授業名", "授業情報はまだ取得されていません")
        course_lines = wrap_text(course, main_width - 6)
        for index, line in enumerate(course_lines[:2]):
            canvas.put(11 + index, left + 3, line, curses.A_BOLD, main_width - 6)
        canvas.put(14, left + 3, "教室  " + self.values.get("教室名", "--"), width=(main_width - 6) // 2)
        canvas.put(14, left + main_width // 2, "時限  " + self.values.get("時限", "--"), width=main_width // 2 - 3)
        canvas.put(15, left + 3, f"時間  {self.values.get('開始時刻', '--:--')} ～ {self.values.get('終了時刻', '--:--')}", width=main_width - 6)

        label = "[ 出席済み ]" if self.attended and self.values else "[ 出席で登録する ]"
        canvas.button(18, left, label, self.selected == 2, self.can_register())
        canvas.put(18, left + 27, f"最終確認 {self.updated}", curses.A_DIM, main_width - 27)
        for index, line in enumerate(wrap_text(self.message, main_width - 2)[:2]):
            canvas.put(20 + index, left + 1, line, color(3 if self.failed else 1), main_width - 2)

        if wide:
            side = left + main_width + 2
            canvas.box(5, side, 30, 7, "操作ガイド", color(1))
            for row, text in enumerate(("Enter  選択項目を実行", "R      授業を更新", "U      ユーザー管理", "H      ヘルプ", "Q      終了"), 6):
                canvas.put(row, side + 2, text, width=26)
            canvas.box(13, side, 30, min(height - 17, 14), "今回の操作履歴", color(1))
            if not self.history:
                canvas.put(15, side + 2, "まだ操作はありません", curses.A_DIM, 26)
            for index, (stamp, qr, message) in enumerate(list(self.history)[:max(0, (min(height - 17, 14) - 2) // 3)]):
                row = 14 + index * 3
                canvas.put(row, side + 2, f"{stamp}  QR {qr}", curses.A_DIM, 26)
                canvas.put(row + 1, side + 2, message, width=26)
        elif height >= 29:
            canvas.box(23, left, main_width, height - 26, "今回の操作履歴", color(1))
            for index, (stamp, qr, message) in enumerate(list(self.history)[:height - 28]):
                canvas.put(24 + index, left + 2, f"{stamp}  QR {qr}  {message}", width=main_width - 4)
        canvas.put(height - 2, left, "Tab/↑↓ 選択  Enter 実行  R 更新  U ユーザー  H ヘルプ  Q 終了", curses.A_DIM, total)
        if self.demo and height >= 26:
            canvas.put(height - 3, left, "デモ: 1 未出席 / 2 出席済 / 3 受付なし / 4 履修なし / 5 エラー", curses.A_DIM, total)

        if self.confirm:
            self.draw_confirmation(canvas)
        elif self.modal:
            self.draw_modal(canvas)
        screen.refresh()

    def draw_confirmation(self, canvas):
        width = min(canvas.width - 6, 68)
        left, top = (canvas.width - width) // 2, max(1, (canvas.height - 15) // 2)
        canvas.box(top, left, width, 15, "出席登録の確認", color(4))
        canvas.put(top + 2, left + 3, self.config.get("_active_username", self.student_id), curses.A_BOLD, width - 6)
        for index, label in enumerate(("授業名", "教室名", "時限", "開始時刻", "終了時刻"), 4):
            canvas.put(top + index, left + 3, label, curses.A_DIM)
            canvas.put(top + index, left + 15, self.values.get(label, "--"), curses.A_BOLD, width - 18)
        canvas.put(top + 10, left + 3, "このユーザーで、上記の授業に出席登録します。", width=width - 6)
        canvas.button(top + 12, left + 3, "[ 戻る ]", self.confirm_selection == 0)
        canvas.button(top + 12, left + 20, "[ 出席登録 ]", self.confirm_selection == 1)
        canvas.put(top + 13, left + 3, "Tab/←→ 選択  Enter 決定  Esc 戻る", curses.A_DIM, width - 6)

    def draw_modal(self, canvas):
        width = min(canvas.width - 6, 72)
        left = (canvas.width - width) // 2
        height = min(canvas.height - 4, 20)
        top = (canvas.height - height) // 2
        title = {"users": "ユーザー管理", "add": "ユーザー登録", "rename": "ユーザーネームの変更", "default": "既定ユーザーを変更", "help": "操作ヘルプ"}[self.modal]
        canvas.box(top, left, width, height, title, color(1))
        if self.modal == "users":
            users = get_users(self.config)
            default = select_user(self.config)
            canvas.put(top + 2, left + 3, "Enterで今回のユーザーを切り替えます。", width=width - 6)
            count = height - 9
            self.user_index = min(self.user_index, max(0, len(users) - 1))
            first = max(0, self.user_index - count + 1)
            for index, user in enumerate(users[first:first + count], first):
                row = top + 4 + index - first
                tags = (" [使用中]" if user["student_id"] == self.student_id else "") + (" [既定]" if user == default else "")
                attr = curses.A_REVERSE if index == self.user_index else 0
                canvas.fill(row, left + 2, width - 4, 1, attr)
                canvas.put(row, left + 3, f"{user['username']} / {user['student_id']}{tags}", attr, width - 6)
            canvas.put(top + height - 4, left + 3, "A 追加  N 名前変更  D 既定に設定", color(1), width - 6)
            canvas.put(top + height - 3, left + 3, self.form_error or "既定ユーザーは通常実行・自動実行に使われます。", color(3) if self.form_error else curses.A_DIM, width - 6)
        elif self.modal in ("add", "rename"):
            fields = ("ユーザーネーム", "学生番号", "パスワード") if self.modal == "add" else ("新しいユーザーネーム",)
            for index, label in enumerate(fields):
                row = top + 3 + index * 3
                canvas.put(row, left + 3, label, curses.A_DIM)
                value = self.form_values[index] if index < len(self.form_values) else ""
                if self.modal == "add" and index == 2:
                    value = "*" * len(value)
                shortened = False
                while text_width(value) > width - 10:
                    value = value[1:]
                    shortened = True
                if shortened:
                    value = "<" + value
                attr = curses.A_REVERSE if index == self.form_index else 0
                canvas.fill(row + 1, left + 3, width - 6, 1, attr)
                canvas.put(row + 1, left + 4, value + ("_" if index == self.form_index else ""), attr, width - 8)
            canvas.put(top + height - 4, left + 3, "最後の項目で Enter を押すと保存します。", color(1), width - 6)
            canvas.put(top + height - 3, left + 3, self.form_error or ("デモの変更は保存されません。" if self.demo else "パスワードはKeychainに保存されます。"), color(3) if self.form_error else curses.A_DIM, width - 6)
        elif self.modal == "default":
            users = get_users(self.config)
            name = users[self.user_index]["username"]
            canvas.put(top + 3, left + 3, name, curses.A_BOLD, width - 6)
            for index, line in enumerate(wrap_text("このユーザーを通常実行・時刻による自動実行の既定ユーザーにします。", width - 6)):
                canvas.put(top + 6 + index, left + 3, line)
            canvas.put(top + 11, left + 3, "Enter: 既定ユーザーに設定", color(4))
        else:
            guide = ("QR番号: 数字入力 / Backspaceで削除", "Tab・↑↓: 項目を選択   Enter: 決定", "R: 状態を再取得   U: ユーザー管理", "登録確認: Tab・←→で選択してEnter", "QR番号と授業の教室名は異なる場合があります。", "GUIでのユーザー切替は今回の画面だけに反映。", "既定の変更はユーザー管理の D から。", "通信中は完了までお待ちください。")
            for index, line in enumerate(guide):
                for offset, part in enumerate(wrap_text(line, width - 6)):
                    canvas.put(top + 2 + index * 2 + offset, left + 3, part, width=width - 6)
        canvas.put(top + height - 2, left + 3, "Esc: 閉じる" + ("  Tab/↑↓: 項目選択" if self.modal in ("add", "rename") else ""), curses.A_DIM, width - 6)

    def run(self, screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            background = -1
            try:
                curses.use_default_colors()
            except curses.error:
                background = curses.COLOR_BLACK
            for number, foreground in enumerate((curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW), 1):
                curses.init_pair(number, foreground, background)
        screen.keypad(True)
        screen.timeout(100)
        if self.room:
            self.start()
        while True:
            self.poll()
            self.draw(screen)
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if self.busy:
                continue
            if key == curses.KEY_RESIZE:
                continue
            height, width = screen.getmaxyx()
            if height < 24 or width < 62:
                if key in ("q", "Q", "\x03"):
                    return 0
                continue
            if self.confirm:
                if key in ("\t", curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_BTAB):
                    self.confirm_selection = 1 - self.confirm_selection
                if key in ("\n", "\r", curses.KEY_ENTER):
                    if self.confirm_selection == 1:
                        self.start(register=True)
                    else:
                        self.confirm = False
                elif key == "\x1b":
                    self.confirm = False
                continue
            if self.modal:
                self.handle_modal(key)
                continue
            if key in ("q", "Q", "\x03"):
                return 0
            if key in ("\t", curses.KEY_DOWN, curses.KEY_UP, curses.KEY_BTAB):
                self.selected = (self.selected + (-1 if key in (curses.KEY_UP, curses.KEY_BTAB) else 1)) % 3
            elif key in ("r", "R"):
                self.start()
            elif key in ("u", "U"):
                self.open_users()
            elif key in ("h", "H", "?"):
                self.modal = "help"
            elif self.demo and self.selected != 0 and key in ("1", "2", "3", "4", "5"):
                self.demo_state = {"1": "unattended", "2": "attended", "3": "empty", "4": "unenrolled", "5": "error"}[key]
                self.demo_attended.discard((self.student_id, self.room))
                self.start()
            elif key in ("\n", "\r", curses.KEY_ENTER):
                if self.selected == 2:
                    if self.can_register():
                        self.confirm = True
                        self.confirm_selection = 0
                else:
                    self.start()
            elif self.selected == 0:
                if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    self.room = self.room[:-1]
                    self.clear_target()
                    self.message = "QR番号を入力して Enter で確認してください。"
                elif isinstance(key, str) and key in "0123456789" and len(self.room) < 3:
                    self.room += key
                    self.clear_target()
                    self.message = "QR番号を入力して Enter で確認してください。"
                elif key == "\x15":
                    self.room = ""
                    self.clear_target()


def launch_ui(base: Path, room=None, demo=False, check=False, username=None) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("画面モードはターミナルで ./att --ui を実行してください。", file=sys.stderr)
        return 1
    try:
        if room is not None:
            room, _ = parse_room(room)
        logger = setup_logging(base)
        if demo:
            config = {"dry_run": check, "users": [{"username": "デモユーザー", "student_id": "DEMO001"}, {"username": "ゲスト", "student_id": "DEMO002"}], "default_user": "デモユーザー", "_active_username": "デモユーザー"}
            student_id, password = "DEMO001", ""
        else:
            config = load_config(base / "config.json")
            student_id, password = get_credentials(config, base / "config.json", username=username)
            if check:
                config["dry_run"] = True
        ui = AttendanceScreen(config, student_id, password, logger, room or "", demo, base)
        try:
            return curses.wrapper(ui.run)
        finally:
            ui.password = ""
            del password
    except (ConfigurationError, CredentialError, RoomError, curses.error) as exc:
        print(f"画面を起動できません: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
