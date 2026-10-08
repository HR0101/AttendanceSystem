"""標準ライブラリだけで動く出席確認画面。"""

import curses
import queue
import sys
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

from .config import ConfigurationError, load_config
from .credentials import CredentialError, get_credentials
from .logging_utils import setup_logging
from .room import RoomError, parse_room
from .verification import AttendanceError


def fit_text(text: str, width: int) -> str:
    """日本語の表示幅を考慮して端末の右端で切る。"""
    result, used = [], 0
    for char in text.replace("\n", " "):
        if unicodedata.category(char).startswith("C"):
            continue
        size = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result)


class AttendanceScreen:
    def __init__(self, config, student_id, password, logger, room="", demo=False):
        self.config, self.student_id, self.password = config, student_id, password
        self.logger, self.room, self.demo = logger, room, demo
        self.values = {}
        self.attended = self.unenrolled = self.busy = self.confirm = False
        self.selected = 0
        self.message = "教室番号を入力して、Enter で授業を確認してください。"
        self.events = queue.Queue()
        self.updated = "未確認"

    def can_register(self):
        return bool(self.values) and not (self.attended or self.unenrolled or self.config.get("dry_run", False))

    def start(self, register=False):
        try:
            room, route = parse_room(self.room)
        except RoomError as exc:
            self.message = str(exc)
            return
        if register and not self.can_register():
            return
        self.busy, self.confirm = True, False
        self.message = "出席登録中…" if register else "ログインして授業情報を取得中…"
        expected = dict(self.values) if register else {}
        # 古い授業情報は、新しい取得に成功するまで登録に利用しない。
        self.values = {}
        self.selected = 1

        def work():
            try:
                if self.demo:
                    values = {"授業名": "サンプル授業（デモ）", "時限": "2-3限", "教室名": f"{room}講義室", "開始時刻": "10:00", "終了時刻": "12:00"}
                    self.events.put(("snapshot", (values, register or self.attended, False)))
                    self.events.put(("result", "デモ: 出席済み" if register or self.attended else "デモ: 未出席（大学には接続していません）"))
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
            else:
                self.busy = False
                self.message = data
                self.updated = datetime.now().strftime("%H:%M:%S")
                if kind == "error":
                    self.values = {}
                elif data in ("出席登録成功", "出席済み", "デモ: 出席済み"):
                    self.attended = True

    def draw(self, screen):
        screen.erase()
        height, width = screen.getmaxyx()
        def put(y, x, text, attr=0):
            if 0 <= y < height - 1 and 0 <= x < width - 1:
                try:
                    screen.addstr(y, x, fit_text(text, width - x - 1), attr)
                except curses.error:
                    pass

        if height < 24 or width < 62:
            put(0, 0, "画面を広げてください（幅62 × 高さ24以上）。")
            put(2, 0, "Q: 終了" if not self.busy else "処理中… 完了までお待ちください。")
            screen.refresh()
            return
        left = max(2, (width - 72) // 2)
        rule = "─" * min(68, width - left - 2)
        put(1, left, "出席システム  /  出席確認", curses.A_BOLD | curses.color_pair(1))
        put(2, left, "DEMO · 大学への接続なし" if self.demo else f"{self.config.get('_active_username', self.student_id)} / 学籍番号：{self.student_id}")
        put(3, left, rule)
        put(5, left, f"QR番号： [ {self.room or '___'} ]", curses.A_REVERSE if self.selected == 0 else 0)
        put(5, left + 30, "[ 授業を確認 / 更新 ]", curses.A_REVERSE if self.selected == 1 else 0)
        for row, label in enumerate(("授業名", "時限", "教室名", "開始時刻", "終了時刻"), 8):
            put(row, left + 2, label, curses.A_BOLD)
            put(row, left + 16, self.values.get(label, "──"))
        status = "確認中…" if self.busy else ("出席済" if self.attended and self.values else "未出席" if self.values else "未確認")
        put(14, left + 2, f"出席状態      {status}", curses.A_BOLD | curses.color_pair(2 if self.attended else 1))
        label = "[ 出席済 ]" if self.attended else "[ 出席で登録する ]"
        if not self.can_register() and not self.attended:
            label = "[ 出席で登録する（現在は利用不可） ]"
        put(16, left + 2, label, curses.A_REVERSE if self.selected == 2 else curses.A_BOLD)
        put(18, left, rule)
        put(19, left, self.message, curses.color_pair(1))
        put(20, left, f"履修登録なし：登録できません。" if self.unenrolled else f"確認のみの設定が有効です。" if self.config.get("dry_run") else f"最終更新：{self.updated}")
        put(22, left, "Tab / ↑↓: 選択   Enter: 実行   R: 更新   Q: 終了", curses.A_DIM)
        if self.confirm:
            put(16, left + 2, "この授業に出席登録しますか？ Enter: 登録 / Esc: 戻る", curses.A_REVERSE)
        screen.refresh()

    def run(self, screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
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
            if key in ("q", "Q", "\x03"):
                return 0
            if key == curses.KEY_RESIZE:
                continue
            height, width = screen.getmaxyx()
            if height < 24 or width < 62:
                continue
            if self.confirm:
                if key in ("\n", "\r", curses.KEY_ENTER):
                    self.start(register=True)
                elif key == "\x1b":
                    self.confirm = False
                continue
            if key in ("\t", curses.KEY_DOWN, curses.KEY_UP, curses.KEY_BTAB):
                self.selected = (self.selected + (-1 if key in (curses.KEY_UP, curses.KEY_BTAB) else 1)) % 3
            elif key in ("r", "R"):
                self.start()
            elif key in ("\n", "\r", curses.KEY_ENTER):
                if self.selected == 2:
                    if self.can_register():
                        self.confirm = True
                else:
                    self.start()
            elif self.selected == 0:
                if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    self.room = self.room[:-1]
                    self.values = {}
                    self.attended = self.unenrolled = False
                elif isinstance(key, str) and key in "0123456789" and len(self.room) < 3:
                    self.room += key
                    self.values = {}
                    self.attended = self.unenrolled = False


def launch_ui(base: Path, room=None, demo=False, check=False, username=None) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("画面モードはターミナルで ./att --ui を実行してください。", file=sys.stderr)
        return 1
    try:
        if room is not None:
            room, _ = parse_room(room)
        logger = setup_logging(base)
        if demo:
            config, student_id, password = {"dry_run": check}, "DEMO001", ""
        else:
            config = load_config(base / "config.json")
            student_id, password = get_credentials(config, base / "config.json", username=username)
            if check:
                config["dry_run"] = True
        ui = AttendanceScreen(config, student_id, password, logger, room or "", demo)
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
