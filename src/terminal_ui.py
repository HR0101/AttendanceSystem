"""標準ライブラリだけで動く出席確認画面。"""

import contextlib
import curses
import inspect
import io
import os
import queue
import sys
import termios
import threading
import time
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

from .config import ConfigurationError, load_config
from .credentials import CredentialError, get_credentials
from .logging_utils import setup_logging
from .room import RoomError, normalize_room_digits, parse_room
from .scheduler import agent_installed
from .verification import AttendanceError
from .terminal_layout import Canvas, fit_text, init_colors, no_color_enabled, prefix_text, text_width, wrap_text
from .users import change_student_id_config, get_users, remove_user_config, select_user, ui_selection, valid_username
from .text_input import TextCursor
from .unsaved_changes import SavePrompt
from .schedule_ui import SchedulePanel
from .updater import available_message, cached_manifest, managed_by_homebrew

_UNSET = object()


class _WorkerLogger:
    """並列実行時のログに匿名ワーカー識別子を付与する。"""

    def __init__(self, logger, label: str):
        self._logger = logger
        self._label = label

    def _emit(self, method: str, msg, *args, **kwargs):
        getattr(self._logger, method)(f"{self._label} {msg}", *args, **kwargs)

    def debug(self, msg, *args, **kwargs):
        self._emit("debug", msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._emit("info", msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._emit("warning", msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._emit("error", msg, *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        self._emit("exception", msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        self._emit("critical", msg, *args, **kwargs)

    def log(self, level, msg, *args, **kwargs):
        self._logger.log(level, f"{self._label} {msg}", *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._logger, name)

PASSWORD_TTL_SECONDS = 300


LOG_TABS = (
    ("attendance", "1:実行ログ"),
    ("launchagent", "2:自動実行"),
    ("updater", "3:更新"),
)
MAX_LOG_BYTES = 64 * 1024
MAX_LOG_LINES = 200


def _read_log_tail(path: Path, *, max_bytes: int = MAX_LOG_BYTES, max_lines: int = MAX_LOG_LINES) -> list[str]:
    """ログファイルの末尾だけを安全に読み込む。"""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        offset = max(0, size - max_bytes)
        stream.seek(offset)
        raw = stream.read(max_bytes)
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if offset > 0 and len(lines) > 1:
        # 途中バイトから読んだ先頭の不完全な行を除外する。
        lines = lines[1:]
    return list(deque(lines, maxlen=max_lines))


def _load_log_tab(base: Path | None, demo: bool, tab_index: int) -> tuple[str, list[str], str]:
    """指定タブのログ末尾を読み込む。デモ時は実ファイルに触れない。"""
    kind = LOG_TABS[tab_index][0]
    if demo:
        if kind == "attendance":
            return (
                "logs/attendance-demo.log (デモ)",
                [
                    "2026-11-19 10:05:00,000 INFO 起動 version=0.2.8 (デモ)",
                    "2026-11-19 10:05:01,200 INFO 段階=ブラウザ起動",
                    "2026-11-19 10:05:03,400 INFO 結果=デモ: 未出席 所要時間=3.4秒",
                    "※ デモモードでは実ファイルのログを読み込みません。",
                ],
                "",
            )
        if kind == "launchagent":
            return (
                "logs/launchagent.log (デモ)",
                [
                    "デモモードでは自動実行ログ（logs/launchagent.log）を読み込みません。",
                    "通常起動では LaunchAgent 実行時の出力を確認できます。",
                ],
                "",
            )
        return (
            ".updates/updater.log (デモ)",
            [
                "デモモードでは更新ログ（.updates/updater.log）を読み込みません。",
                "通常起動では更新確認の失敗履歴を確認できます。",
            ],
            "",
        )
    if base is None:
        return "ログ", [], "設定フォルダを確認できないため、ログを読み込めません。"

    fixed = {
        "launchagent": (base / "logs" / "launchagent.log", "logs/launchagent.log", "自動実行ログ"),
        "updater": (base / ".updates" / "updater.log", ".updates/updater.log", "更新ログ"),
    }
    try:
        if kind == "attendance":
            log_dir = base / "logs"
            if log_dir.is_symlink() or not log_dir.is_dir():
                return "logs/attendance-*.log", [], "実行ログ（logs/attendance-*.log）はまだありません。"
            candidates = [
                entry for entry in log_dir.iterdir()
                if entry.name.startswith("attendance-") and entry.name.endswith(".log")
                and not entry.is_symlink() and entry.is_file()
            ]
            if not candidates:
                return "logs/attendance-*.log", [], "実行ログ（logs/attendance-*.log）はまだありません。"
            target = max(candidates, key=lambda entry: entry.name)
            source = f"logs/{target.name}"
        else:
            target, source, label = fixed[kind]
            if target.parent.is_symlink() or target.is_symlink() or not target.exists():
                return source, [], f"{label}（{source}）はまだありません。"
            if not target.is_file():
                return source, [], f"{source} は通常のファイルではありません。"
        lines = _read_log_tail(target)
    except OSError:
        fallback = "logs/attendance-*.log" if kind == "attendance" else fixed[kind][1]
        return fallback, [], f"{fallback} を読み込めませんでした（アクセス権限を確認してください）。"

    if not any(line.strip() for line in lines):
        return source, [], f"{source} にログの記録はまだありません。"
    return source, lines, ""


@contextlib.contextmanager
def _disable_xon_flow_control():
    """端末のソフトウェアフロー制御 (IXON) を一時解除し、Ctrl+S を画面へ渡す。"""
    try:
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
    except (AttributeError, io.UnsupportedOperation, ValueError, OSError, termios.error):
        yield
        return
    if not (old_attrs[0] & termios.IXON):
        yield
        return
    new_attrs = list(old_attrs)
    new_attrs[0] &= ~termios.IXON
    try:
        termios.tcsetattr(fd, termios.TCSANOW, new_attrs)
    except (OSError, termios.error):
        yield
        return
    try:
        yield
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old_attrs)
        except (OSError, termios.error):
            pass


def _validate_username_field(value, users, *, current_student_id=None, label="ユーザーネーム", allow_empty=False):
    """ユーザーネームの入力規則と重複を検証し、案内文を返す。"""
    if not value:
        return None if allow_empty else f"{label}を入力してください（1～60文字）。"
    if not value.strip():
        return f"{label}は空白だけにできません。1～60文字で入力してください。"
    if value != value.strip():
        return f"{label}の前後に空白は使えません。"
    if any(unicodedata.category(c).startswith("C") for c in value):
        return f"{label}に制御文字は使えません。"
    if len(value) > 60:
        return f"{label}は60文字以内で入力してください。"
    if any(u["username"] == value and u["student_id"] != current_student_id for u in users):
        return "そのユーザーネームは登録済みです。"
    return None


def _validate_student_id_field(value, users, *, current_user=None, label="学籍番号", allow_empty=False):
    """学籍番号の全角文字・空白・記号・重複を検証し、案内文を返す。"""
    if not value:
        return None if allow_empty else f"{label}を半角英数字で入力してください。"
    has_space = any(c.isspace() for c in value)
    has_fullwidth = any(unicodedata.east_asian_width(c) in ("F", "W") for c in value)
    if has_fullwidth and has_space:
        return f"{label}に全角文字・空白は使えません。半角英数字で入力してください。"
    if has_fullwidth:
        return f"{label}に全角文字は使えません。半角英数字で入力してください。"
    if has_space:
        return f"{label}に空白は使えません。半角英数字で入力してください。"
    if not value.isascii() or not value.isalnum():
        return f"{label}は半角英数字のみで入力してください（記号不可）。"
    if current_user is not None:
        if value == current_user["student_id"]:
            return "現在と異なる学籍番号を入力してください。"
        if any(u["student_id"] == value and u["username"] != current_user["username"] for u in users):
            return "その学籍番号は登録済みです。"
    elif any(u["student_id"] == value for u in users):
        return "その学籍番号は登録済みです。"
    return None


class AttendanceScreen:
    def __init__(self, config, student_id, password, logger, room="", demo=False, base=None, *, restore_selection=True, password_ttl=PASSWORD_TTL_SECONDS):
        self.password_ttl = password_ttl
        self._password = ""
        self._password_deadline = None
        self.config, self.student_id, self.password = config, student_id, password
        self.logger, self.room, self.demo = logger, room, demo
        self.room_editor = TextCursor(room)
        self.values = {}
        self.attended = self.unenrolled = self.busy = self.confirm = False
        self.selected = 0
        self.message = "教室番号を入力して、Enter で授業を確認してください。"
        self.events = queue.Queue()
        self.updated = "未確認"
        self.base = base
        manifest = cached_manifest(base) if base and not demo and not os.environ.get("ATTENDANCE_NO_UPDATE") else None
        self.update_version = manifest["version"] if manifest and available_message(manifest) else None
        self.update_check_after = time.monotonic() + 1
        self.history = deque(maxlen=8)
        self.checked = self.failed = self.cancelled = False
        self.cancel_event = None
        self.modal = None
        self.user_index = 0
        self.selected_user_ids = ui_selection(config, student_id) if restore_selection else {student_id}
        if student_id not in self.selected_user_ids and self.selected_user_ids:
            active = next(user for user in get_users(config) if user["student_id"] in self.selected_user_ids)
            self.student_id = active["student_id"]
            self.clear_password()
            self.config["_active_username"] = active["username"]
        self.pending_user_ids = set()
        self.user_results = {}
        self.form_values = []
        self.form_initial_values = ()
        self.form_parent_users = False
        self.back_after_save = False
        self.submitted_form = None
        self.save_prompt = SavePrompt()
        self.form_editors = []
        self.password_visible = False
        self.form_index = 0
        self.form_error = ""
        self.confirm_selection = 0
        self._default_agent_active = _UNSET
        self.confirm_scroll = 0
        self.demo_state = "unattended"
        self.demo_attended = set()
        self.message_error = False
        self._account_notice = False
        self.use_colors = not no_color_enabled()
        self.schedule_panel = SchedulePanel(self)
        self.log_tab = 0
        self.log_offset = 0
        self.log_max_offset = None
        self.log_page_rows = 10
        self.log_parent = None
        self.log_source = ""
        self.log_lines = []
        self.log_empty_message = ""

    @property
    def password(self):
        """期限切れ後の参照ではメモリ上のパスワードを消去して空文字を返す。"""
        self.expire_password()
        return self._password

    @password.setter
    def password(self, value):
        if value and self.password_ttl > 0:
            self._password = value
            self._password_deadline = time.monotonic() + self.password_ttl
        else:
            self.clear_password()

    def clear_password(self):
        """保持中のパスワードと有効期限を即時消去する。"""
        self._password = ""
        self._password_deadline = None

    def expire_password(self):
        """有効期限を過ぎた保持パスワードを消去する。"""
        if self._password and self._password_deadline is not None and time.monotonic() >= self._password_deadline:
            self.clear_password()

    @staticmethod
    def _wipe_form_data(values, editors):
        if values:
            values[:] = [""] * len(values)
            values.clear()
        for editor in editors or ():
            editor.text = ""
            editor.position = 0

    def clear_sensitive_state(self):
        """画面終了・破棄時にパスワードと入力フォームの保持領域を消去する。"""
        self.clear_password()
        self._wipe_form_data(self.form_values, self.form_editors)
        if self.submitted_form is not None:
            self._wipe_form_data(self.submitted_form[1], self.submitted_form[2])
            self.submitted_form = None
        self.form_values = []
        self.form_editors = []
        self.form_initial_values = ()
        self.save_prompt.reset()
        self.password_visible = False

    def clear_target(self):
        self.values = {}
        self.user_results = {}
        self.attended = self.unenrolled = self.checked = self.failed = self.cancelled = self.confirm = False
        self.confirm_scroll = 0
        self.message_error = self._account_notice = False
        self.updated = "未確認"

    def request_cancel(self):
        """出席確認・登録などのネットワーク処理を安全な境界で中断する。"""
        if not self.busy or self.cancel_event is None or self.cancel_event.is_set():
            return False
        self.cancel_event.set()
        self.message = "中断しています…"
        return True

    @staticmethod
    def _run_attendance(fn, *args, cancel_event=None, **kwargs):
        # 古いテスト用モック（side_effect 関数を含む）が cancel_event を受け取らない場合だけ省略する。
        side = getattr(fn, "side_effect", None)
        target = side if callable(side) and not (isinstance(side, type) and issubclass(side, BaseException)) else fn
        try:
            parameters = inspect.signature(target).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        if any(p.name == "cancel_event" or p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters):
            kwargs["cancel_event"] = cancel_event
        return fn(*args, **kwargs)

    def selected_users(self):
        users = get_users(self.config)
        if not users:
            users = [{"username": self.config.get("_active_username", self.student_id), "student_id": self.student_id}]
        return [user for user in users if user["student_id"] in self.selected_user_ids]

    def registration_targets(self):
        users = self.selected_users()
        if len(users) == 1:
            return users if self.values and not (self.attended or self.unenrolled or self.failed) else []
        return [user for user in users if (result := self.user_results.get(user["student_id"], {})).get("values")
                and not (result.get("attended") or result.get("unenrolled") or result.get("failed"))]

    def status(self):
        if self.busy:
            if self.cancel_event is not None and self.cancel_event.is_set():
                return "中断しています…", 3, "安全に停止できる位置で終了します"
            detail = "Esc/Qで中断できます" if self.cancel_event is not None else "完了までお待ちください"
            return "|/-\\"[int(time.monotonic() * 8) % 4] + " 処理中", 1, detail
        if len(self.selected_user_ids) > 1 and self.checked:
            total = len(self.selected_user_ids)
            attended = sum(result.get("attended", False) for result in self.user_results.values())
            cancelled = sum(result.get("cancelled", False) for result in self.user_results.values())
            errors = sum(result.get("failed", False) for result in self.user_results.values())
            ready = len(self.registration_targets())
            if attended == total:
                return f"全員出席済み（{total}人）", 2, "Uでユーザーごとの結果を確認できます"
            if self.cancelled and cancelled:
                title = f"処理を中断しました（登録可能 {ready}人 / 選択 {total}人）" if ready else "処理を中断しました"
                detail = f"出席済み {attended}人 / 中断 {cancelled}人 / エラー {errors}人"
                return title, 3, detail
            title = f"登録可能 {ready}人 / 選択 {total}人" if ready else "ユーザーごとの結果を確認してください"
            detail = f"出席済み {attended}人 / 対象外 {total - attended - ready - errors}人 / エラー {errors}人"
            if self.config.get("dry_run"):
                detail = "確認のみモードです。Uで個別の結果を確認できます"
            return title, 3 if errors else 4, detail
        if self.cancelled:
            return "処理を中断しました", 3, "R で再確認できます"
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
        return bool(self.registration_targets()) and not (self.busy or self.config.get("dry_run", False))

    def start(self, register=False):
        if self.busy:
            return
        try:
            room, route = parse_room(self.room)
        except RoomError as exc:
            self.message = str(exc)
            self.message_error = True
            self._account_notice = False
            return
        if register and not self.can_register():
            return
        targets = self.registration_targets() if register else self.selected_users()
        if not targets:
            return
        expected = {user["student_id"]: dict(self.user_results[user["student_id"]]["values"]
                    if len(self.selected_user_ids) > 1 else self.values) for user in targets} if register else {}
        if not register:
            self.clear_target()
        cancel_event = threading.Event()
        self.cancel_event = cancel_event
        self.busy, self.confirm, self.confirm_scroll = True, False, 0
        self.failed = self.cancelled = False
        self.message_error = self._account_notice = False
        self.message = f"{len(targets)}人の出席登録中…" if register else f"{len(targets)}人の授業情報を取得中…"
        # 古い授業情報は、新しい取得に成功するまで登録に利用しない。
        self.values = {}
        self.selected = 1
        for user in targets:
            self.user_results[user["student_id"]] = {"values": {}, "attended": False, "unenrolled": False,
                                                    "failed": False, "cancelled": False, "result": "処理中…"}

        def work_user(index, user):
            from .attendance import AttendanceCancelled
            student_id = user["student_id"]
            values_expected = expected.get(student_id, {})
            # 複数人並列処理時は PII を含まない番号でスレッドごとのログを追跡可能にする。
            worker_logger = _WorkerLogger(self.logger, f"対象={index}/{len(targets)}") if len(targets) > 1 else self.logger

            password = ""
            if cancel_event.is_set():
                worker_logger.info("画面操作中断")
                self.events.put(("user_cancelled", (student_id, "中断しました")))
                return
            try:
                if self.demo:
                    if cancel_event.is_set():
                        raise AttendanceCancelled("ユーザー操作により中断しました。")
                    if self.demo_state == "error":
                        raise AttendanceError("デモの通信エラーです。Rで再確認できます。")
                    if register:
                        if cancel_event.is_set():
                            raise AttendanceCancelled("ユーザー操作により中断しました。")
                        self.demo_attended.add((student_id, room))
                    attended = self.demo_state == "attended" or (student_id, room) in self.demo_attended
                    unenrolled = self.demo_state == "unenrolled"
                    values = {} if self.demo_state == "empty" else {"授業名": "システムズエンジニアリング（デモ）", "時限": "2-3限", "教室名": f"{'432' if room == '435' else room}講義室", "開始時刻": "10:00", "終了時刻": "12:00"}
                    self.events.put(("user_snapshot", (student_id, values, attended, unenrolled)))
                    result = "出席受付対象なし（デモ）" if not values else "履修登録なし（デモ）" if unenrolled else "デモ: 出席済み" if attended else "デモ: 未出席（大学には接続していません）"
                else:
                    from .attendance import run_attendance
                    from .browser import open_page
                    config = dict(self.config, dry_run=not register)
                    password = self.password if student_id == self.student_id else ""
                    if not password:
                        if self.base is None:
                            raise CredentialError("設定ファイルの場所を確認できません。")
                        _, password = get_credentials(config, self.base / "config.json", interactive=False, username=user["username"])
                    if cancel_event.is_set():
                        raise AttendanceCancelled("ユーザー操作により中断しました。")
                    with open_page(config) as page:
                        if cancel_event.is_set():
                            raise AttendanceCancelled("ユーザー操作により中断しました。")
                        result = self._run_attendance(
                            run_attendance,
                            page, config, student_id, password, room, route, worker_logger,
                            values_expected.get("授業名"), values_expected.get("開始時刻"),
                            on_snapshot=lambda values, attended, unenrolled: self.events.put(
                                ("user_snapshot", (student_id, values, attended, unenrolled))),
                            expected_values=values_expected if register else None,
                            cancel_event=cancel_event,
                        )
                worker_logger.info("画面操作 結果=%s", result)
                self.events.put(("user_result", (student_id, result)))
            except AttendanceCancelled:
                self.logger.info("画面操作中断")
                self.events.put(("user_cancelled", (student_id, "中断しました")))
            except (ConfigurationError, CredentialError, AttendanceError, RoomError) as exc:
                worker_logger.error("画面操作停止 種別=%s 理由=%s", type(exc).__name__, exc)
                self.events.put(("user_error", (student_id, str(exc))))
            except Exception as exc:
                worker_logger.error("画面操作停止 種別=%s", type(exc).__name__)
                self.events.put(("user_error", (student_id, "通信または画面状態を確認してください。詳細は logs を参照してください。")))
            finally:
                del password

        def work():
            if len(targets) == 1:
                work_user(1, targets[0])
            else:
                if self.demo:
                    pool_cm = nullcontext()
                else:
                    from .browser import browser_pool
                    pool_cm = browser_pool()
                # 単一 Chromium サーバーだけを共有し、各スレッドで独立した BrowserContext を使う。
                with pool_cm, ThreadPoolExecutor(max_workers=min(4, len(targets))) as executor:
                    list(executor.map(work_user, range(1, len(targets) + 1), targets))
            self.events.put(("complete", register))

        threading.Thread(target=work, daemon=True).start()

    def complete_attendance(self, register):
        users = self.selected_users()
        results = [self.user_results[user["student_id"]] for user in users]
        self.values = next((dict(result["values"]) for result in results if result["values"] and not result["failed"]), {})
        for result in results:
            if result["values"] and result["values"] != self.values:
                result.update(values={}, failed=True, attended=False, unenrolled=False,
                              result="授業情報が他のユーザーと異なります。Rで再確認してください。")
        cancelled_any = any(result.get("cancelled", False) for result in results)
        self.cancel_event = None
        self.attended = bool(results) and all(result["attended"] for result in results)
        self.unenrolled = bool(results) and all(result["unenrolled"] for result in results)
        self.failed = any(result["failed"] for result in results)
        self.cancelled = cancelled_any
        self.message_error = self.failed
        self._account_notice = False
        self.checked, self.busy = True, False
        self.updated = datetime.now().strftime("%H:%M:%S")
        if len(users) == 1:
            self.message = results[0]["result"]
        elif cancelled_any:
            attended = sum(result["attended"] for result in results)
            self.message = f"処理を中断しました（出席済み {attended}/{len(users)}人）。Uで個別結果を確認できます。"
        else:
            attended = sum(result["attended"] for result in results)
            self.message = f"{'一括登録' if register else '確認'}完了: 出席済み {attended}/{len(users)}人。Uで個別結果を確認できます。"
        self.history.appendleft((self.updated, self.room, prefix_text("error", self.message) if self.failed else self.message))

    def poll(self):
        self.expire_password()
        if (self.base and not self.demo and not os.environ.get("ATTENDANCE_NO_UPDATE")
                and time.monotonic() >= self.update_check_after):
            self.update_check_after = time.monotonic() + 1
            manifest = cached_manifest(self.base)
            self.update_version = manifest["version"] if manifest and available_message(manifest) else None
        self.schedule_panel.poll()
        while True:
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "snapshot":
                self.values, self.attended, self.unenrolled = data
                self.checked = True
            elif kind == "user_snapshot":
                student_id, values, attended, unenrolled = data
                self.user_results[student_id].update(values=dict(values), attended=attended, unenrolled=unenrolled)
            elif kind in ("user_result", "user_error", "user_cancelled"):
                student_id, result = data
                state = self.user_results[student_id]
                state["result"] = result
                if kind == "user_error":
                    state.update(values={}, failed=True, attended=False, unenrolled=False, cancelled=False)
                elif kind == "user_cancelled":
                    state.update(values={}, failed=False, attended=False, unenrolled=False, cancelled=True)
                elif result in ("出席登録成功", "出席済み", "デモ: 出席済み"):
                    state["attended"] = True
                elif result.startswith("履修登録なし"):
                    state["unenrolled"] = True
                if len(self.selected_user_ids) > 1:
                    name = next((user["username"] for user in self.selected_users() if user["student_id"] == student_id), student_id)
                    entry = prefix_text("error", f"{name}: {result}") if kind == "user_error" else f"{name}: {result}"
                    self.history.appendleft((datetime.now().strftime("%H:%M:%S"), self.room, entry))
            elif kind == "complete":
                self.complete_attendance(data)
            elif kind == "account":
                config, student_id, password, action, selection = data
                draft, old_config = self.submitted_form, self.config
                self.submitted_form = None
                if action == "default":
                    self.clear_target()
                if action in ("delete", "student_id"):
                    removed = {u["student_id"] for u in get_users(self.config)} - {u["student_id"] for u in get_users(config)}
                    self.demo_attended = {entry for entry in self.demo_attended if entry[0] not in removed}
                    self.clear_target()
                config["dry_run"] = self.config.get("dry_run", False)
                self.config, self.student_id = config, student_id
                if password is not None:
                    self.password = password
                if selection is not None:
                    self.selected_user_ids = selection
                    self.pending_user_ids = set(selection)
                self.busy = False
                self.modal = None
                if draft is not None:
                    if draft[5]:
                        pending = set(draft[6])
                        users = get_users(config)
                        valid = {user["student_id"] for user in users}
                        for previous in get_users(old_config):
                            if previous["student_id"] in pending and previous["student_id"] not in valid:
                                pending.remove(previous["student_id"])
                                replacement = next((user for user in users if user["username"] == previous["username"]), None)
                                if replacement:
                                    pending.add(replacement["student_id"])
                        pending.intersection_update(valid)
                        if not get_users(old_config):
                            pending = set(self.selected_user_ids)
                        if self.back_after_save or pending != self.selected_user_ids:
                            self.modal, self.pending_user_ids = "users", pending
                    self._wipe_form_data(draft[1], draft[2])
                    del draft
                self.back_after_save = False
                self.form_parent_users = False
                self.message = {"switch": f"{len(self.selected_user_ids)}人のユーザーを選択しました。", "add": "ユーザーを登録しました。Uで選択できます。", "rename": "ユーザーネームを変更しました。", "password": "保存パスワードを更新しました。", "default": f"既定ユーザーとこの画面の出席対象を「{config['_active_username']}」に切り替えました。", "delete": "ユーザーを削除しました。", "student_id": "学籍番号を変更しました。授業情報を再確認してください。"}[action]
                self.message_error = False
                self._account_notice = True
                self.user_index = min(self.user_index, max(0, len(get_users(config)) - 1))
                if action == "delete" and not get_users(config):
                    self.modal = "users"
                    self.message += " Aで新しいユーザーを追加できます。"
                if action in ("switch", "default") and self.room:
                    self.start()
            elif kind == "account_error":
                self.busy = False
                self.back_after_save = False
                if self.submitted_form is not None:
                    draft = self.submitted_form
                    (self.modal, self.form_values, self.form_editors, self.form_index, self.form_initial_values,
                     self.form_parent_users, self.pending_user_ids) = draft
                    self.submitted_form = None
                    self.password_visible = False
                self.form_error = data
                self.message = data
                self.message_error = True
                self._account_notice = False
            else:
                self.busy = False
                self.message = data
                self.updated = datetime.now().strftime("%H:%M:%S")
                self._account_notice = False
                if kind == "error":
                    self.values = {}
                    self.failed = True
                    self.message_error = True
                else:
                    self.message_error = False
                    if data in ("出席登録成功", "出席済み", "デモ: 出席済み"):
                        self.attended = True
                self.history.appendleft((self.updated, self.room, prefix_text("error", self.message) if self.failed else self.message))

    def account_action(self, action, name, new_name="", student_id="", password=""):
        if self.busy:
            password = ""
            return
        if action == "switch":
            self.clear_target()
            self.clear_password()
        self.busy = True
        self.form_error = ""
        self.message = "ユーザー情報を処理中…"
        self.message_error = False
        self._account_notice = True
        self.submitted_form = ((self.modal, list(self.form_values), list(self.form_editors), self.form_index,
                                self.form_initial_values, self.form_parent_users, set(self.pending_user_ids))
                               if self.modal in ("add", "rename", "password", "student_id") and self.form_values else None)
        # 処理中は入力を表示せず、失敗時だけ編集へ戻す。
        self.form_values = []
        self.form_editors = []
        self.form_initial_values = ()
        self.password_visible = False
        self.modal = "users"

        def work():
            nonlocal password
            submitted_password = password
            password = ""
            activated = None
            current_password = None
            try:
                from .user_commands import activate_default_user, add_user, change_password, change_student_id, remove_user, rename_user
                target = select_user(self.config, name) if action in ("delete", "student_id") else None
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
                        config["ui_selected_users"] = [select_user(config, name)["student_id"]]
                    elif action == "password":
                        select_user(config, name)
                        if not submitted_password:
                            raise CredentialError("新しいパスワードを入力してください。")
                    elif action == "delete":
                        config = remove_user_config(config, name)
                    elif action == "student_id":
                        config = change_student_id_config(config, name, student_id)
                else:
                    if self.base is None:
                        raise CredentialError("設定ファイルの場所を確認できません。")
                    if action == "add":
                        config = add_user(self.base, name, student_id, submitted_password)
                    elif action == "rename":
                        config = rename_user(self.base, name, new_name)
                    elif action == "default":
                        config, active_id, active_password = activate_default_user(self.base, name)
                        activated = active_id, active_password
                        del active_password
                    elif action == "password":
                        config = change_password(self.base, name, submitted_password)
                    elif action == "delete":
                        config = remove_user(self.base, name)
                    elif action == "student_id":
                        config = change_student_id(self.base, name, student_id)
                    else:
                        config = load_config(self.base / "config.json")
                current_id = activated[0] if activated is not None else self.student_id
                if activated is not None:
                    current_password = activated[1]
                if action in ("switch", "default"):
                    if self.demo:
                        current_id, current_password = select_user(config, name)["student_id"], ""
                    elif activated is None:
                        current_id, current_password = get_credentials(config, self.base / "config.json", interactive=False, username=name)
                elif action == "password" and not self.demo and select_user(config, name)["student_id"] == current_id:
                    current_password = submitted_password
                selected = {current_id} if action in ("switch", "default") else None
                if action in ("delete", "student_id"):
                    selected = set(self.selected_user_ids)
                    if target["student_id"] in selected:
                        selected.remove(target["student_id"])
                        if action == "student_id":
                            selected.add(student_id)
                    if action == "student_id" and current_id == target["student_id"]:
                        current_id = student_id
                    users = get_users(config)
                    selected.intersection_update(user["student_id"] for user in users)
                    if not selected and users:
                        selected.add(select_user(config)["student_id"])
                    if current_id not in selected:
                        current_id = next((user["student_id"] for user in users if user["student_id"] in selected), "")
                        current_password = ""
                elif action == "add" and not current_id:
                    current_id, current_password = student_id, "" if self.demo else submitted_password
                    selected = {student_id}
                if action == "switch":
                    self.persist_selection(config, selected)
                config["_active_username"] = next((u["username"] for u in get_users(config) if u["student_id"] == current_id), current_id)
                self.events.put(("account", (config, current_id, current_password, action, selected)))
            except (ConfigurationError, CredentialError, OSError) as exc:
                self.events.put(("account_error", str(exc) or "ユーザー情報を処理できません。"))
            except Exception:
                self.events.put(("account_error", "ユーザー情報を処理できません。Keychain の状態を確認してください。"))
            finally:
                del submitted_password, activated, current_password

        try:
            threading.Thread(target=work, daemon=True).start()
        except BaseException:
            password = ""
            raise

    def open_users(self):
        self.expire_password()
        self.modal = "users"
        self.form_error = ""
        self._default_agent_active = _UNSET
        self.pending_user_ids = set(self.selected_user_ids)
        users = get_users(self.config)
        self.user_index = next((i for i, u in enumerate(users) if u["student_id"] in self.selected_user_ids), 0)

    def apply_user_selection(self):
        if not self.pending_user_ids:
            self.form_error = "出席するユーザーを1人以上選択してください。"
            return
        users = [user for user in get_users(self.config) if user["student_id"] in self.pending_user_ids]
        if len(users) == 1 and users[0]["student_id"] != self.student_id:
            self.account_action("switch", users[0]["username"])
            return
        try:
            self.persist_selection(self.config, self.pending_user_ids)
        except (ConfigurationError, CredentialError, OSError) as exc:
            self.form_error = f"選択を保存できません: {exc}"
            return
        if self.pending_user_ids == self.selected_user_ids:
            self.modal = None
            return
        self.selected_user_ids = set(self.pending_user_ids)
        if self.student_id not in self.selected_user_ids:
            self.clear_password()
        self.clear_target()
        self.modal = None
        self.message = f"{len(users)}人のユーザーを選択しました。QR番号を入力して確認してください。"
        if self.room:
            self.start()

    def persist_selection(self, config, selected):
        if self.demo:
            values = [user["student_id"] for user in get_users(config) if user["student_id"] in selected]
        else:
            if self.base is None:
                raise CredentialError("設定ファイルの場所を確認できません。")
            from .user_commands import save_ui_selection
            values = save_ui_selection(self.base, selected)["ui_selected_users"]
        config["ui_selected_users"] = values

    def open_form(self, action):
        users = get_users(self.config)
        if action in ("rename", "password", "student_id") and not users:
            return
        self.form_parent_users = self.modal == "users"
        self.back_after_save = False
        self.modal = action
        self.form_index = 0
        self.form_error = ""
        self.form_values = (["", "", ""] if action == "add" else ["", ""] if action == "password"
                            else [users[self.user_index]["student_id"]] if action == "student_id"
                            else [users[self.user_index]["username"]])
        self.form_editors = [TextCursor(value) for value in self.form_values]
        self.form_initial_values = tuple(self.form_values)
        self.password_visible = False

    def _automation_active(self):
        """LaunchAgentの自動実行状態を安全に確認する。デモや参照不可時はFalse/Noneを返す。"""
        if self.demo or not isinstance(self.base, Path):
            return False
        try:
            return bool(agent_installed(self.base))
        except Exception:
            return None

    def default_switch_impact(self, target_user, *, agent_active=_UNSET):
        """既定ユーザー変更時の現在値・出席対象・自動実行への具体的な影響文を返す。"""
        if agent_active is _UNSET:
            agent_active = self._automation_active()
        target_label = f"{target_user['username']} ({target_user['student_id']})"
        try:
            current_default = select_user(self.config) if self.config.get("default_user") else None
        except ConfigurationError:
            current_default = None
        same_default = (
            current_default is not None
            and current_default["username"] == target_user["username"]
            and current_default["student_id"] == target_user["student_id"]
        )
        if current_default is None:
            default_line = f"既定ユーザー: 未設定 → {target_label}"
        elif same_default:
            default_line = f"既定ユーザー: {target_label}（変更なし）"
        else:
            current_label = f"{current_default['username']} ({current_default['student_id']})"
            default_line = f"既定ユーザー: {current_label} → {target_label}"

        selected_count = len(self.selected_users()) if self.selected_user_ids else 0
        if (same_default and self.selected_user_ids == {target_user["student_id"]}
                and self.student_id == target_user["student_id"]):
            target_line = f"現在の出席対象（1人選択中）と保存済みの選択は {target_label} のままです。"
        elif selected_count == 0:
            target_line = f"現在の出席対象（未選択）と保存済みの選択も {target_label} の1人のみに切り替わります。"
        else:
            target_line = f"現在の出席対象（{selected_count}人選択中）と保存済みの選択も {target_label} の1人のみに切り替わります。"

        automation_state = "有効中" if agent_active is True else "停止中" if agent_active is False else "状態未確認"
        if same_default:
            automation_line = f"自動実行（{automation_state}）は引き続き {target_label} で実行されます。"
        else:
            automation_line = f"自動実行（{automation_state}）も今後は {target_label} で実行されます。"
        return (default_line, target_line, automation_line)

    def _confirm_default_user(self, username):
        self._default_agent_active = _UNSET
        self.account_action("default", username)

    def open_logs(self, parent=None):
        self.modal = "logs"
        self.log_parent = parent
        self.log_tab = 0
        self.reload_logs()

    def reload_logs(self):
        self.log_source, self.log_lines, self.log_empty_message = _load_log_tab(self.base, self.demo, self.log_tab)
        self.log_offset = 0
        self.log_max_offset = None

    def discard_modal(self):
        if self.modal == "logs":
            self.modal = self.log_parent
            self.log_parent = None
            return
        parent = self.modal in ("user_result", "delete", "default") or (
            self.modal in ("add", "rename", "password", "student_id") and self.form_parent_users)
        self._wipe_form_data(self.form_values, self.form_editors)
        self.form_values = []
        self.form_editors = []
        self.form_initial_values = ()
        self.form_parent_users = False
        self.password_visible = False
        self.form_error = ""
        self.back_after_save = False
        self._default_agent_active = _UNSET
        if self.modal == "users":
            self.pending_user_ids = set(self.selected_user_ids)
        self.modal = "users" if parent else None

    def save_form_and_back(self):
        self.back_after_save = True
        self.submit_account_form()

    def request_modal_back(self):
        self.password_visible = False
        if self.modal == "users" and self.pending_user_ids != self.selected_user_ids:
            self.save_prompt.open("ユーザーの選択", save=self.apply_user_selection, discard=self.discard_modal)
        elif self.modal in ("add", "rename", "password", "student_id") and tuple(self.form_values) != self.form_initial_values:
            self.save_prompt.open("ユーザー情報の編集", save=self.save_form_and_back, discard=self.discard_modal)
        elif self.modal == "default":
            users = get_users(self.config)
            if not users:
                self.discard_modal()
                return
            user = users[min(self.user_index, len(users) - 1)]
            if (self.config.get("default_user") != user["username"] or self.student_id != user["student_id"]
                    or self.selected_user_ids != {user["student_id"]}):
                if self._default_agent_active is _UNSET:
                    self._default_agent_active = self._automation_active()
                self.save_prompt.open(
                    "既定ユーザーの変更",
                    save=lambda: self._confirm_default_user(user["username"]),
                    discard=self.discard_modal,
                    details=self.default_switch_impact(user, agent_active=self._default_agent_active),
                )
            else:
                self.discard_modal()
        else:
            self.discard_modal()

    def validate_account_field(self, index, *, allow_empty=False):
        """現在のユーザーフォームで指定項目の入力を検証し、エラー文を返す。"""
        users = get_users(self.config)
        value = self.form_values[index]
        if self.modal == "add":
            if index == 0:
                return _validate_username_field(value, users, allow_empty=allow_empty)
            if index == 1:
                return _validate_student_id_field(value, users, label="学生番号", allow_empty=allow_empty)
            if index == 2:
                return None if (value or allow_empty) else "パスワードを入力してください。"
        elif self.modal == "rename":
            current = users[self.user_index]
            return _validate_username_field(
                value, users, current_student_id=current["student_id"],
                label="新しいユーザーネーム", allow_empty=allow_empty,
            )
        elif self.modal == "student_id":
            current = users[self.user_index]
            return _validate_student_id_field(
                value, users, current_user=current, label="学籍番号", allow_empty=allow_empty,
            )
        elif self.modal == "password":
            password, confirmation = self.form_values
            if index == 0:
                if not password:
                    return None if allow_empty else "新しいパスワードを入力してください。"
                # 確認欄が入力済みで異なる場合だけ移動時にも不一致を案内する。
                if allow_empty and confirmation and password != confirmation:
                    return "確認用のパスワードが一致しません。"
                return None
            if index == 1:
                if not confirmation:
                    return None if allow_empty else "確認用のパスワードを入力してください。"
                if password != confirmation:
                    return "確認用のパスワードが一致しません。"
        return None

    def first_invalid_account_field(self):
        """項目順に入力を検証し、最初の (項目番号, エラー文) を返す。"""
        for index in range(len(self.form_values)):
            error = self.validate_account_field(index, allow_empty=False)
            if error:
                return index, error
        return None

    def submit_account_form(self):
        invalid = self.first_invalid_account_field()
        if invalid is not None:
            self.form_index, self.form_error = invalid
            self.back_after_save = False
            return
        if self.modal == "add":
            name, student_id, password = self.form_values
            self.account_action("add", name, student_id=student_id, password=password)
        elif self.modal == "password":
            password, _ = self.form_values
            users = get_users(self.config)
            self.account_action("password", users[self.user_index]["username"], password=password)
        elif self.modal == "student_id":
            users = get_users(self.config)
            self.account_action("student_id", users[self.user_index]["username"], student_id=self.form_values[0])
        else:
            users = get_users(self.config)
            self.account_action("rename", users[self.user_index]["username"], new_name=self.form_values[0])

    def handle_modal(self, key):
        if self.save_prompt.active:
            self.save_prompt.handle(key)
            return
        if self.modal == "schedule":
            self.schedule_panel.handle(key)
            return
        if key == "\x1b" or (key in ("q", "Q") and self.modal not in ("add", "rename", "password", "student_id")):
            self.request_modal_back()
            return
        enter = key in ("\n", "\r", curses.KEY_ENTER)
        if self.modal == "help":
            if enter or key in ("h", "H", "?"):
                self.modal = None
            return
        if self.modal == "logs":
            if key in ("\t", curses.KEY_RIGHT):
                self.log_tab = (self.log_tab + 1) % len(LOG_TABS)
                self.reload_logs()
            elif key in (curses.KEY_BTAB, curses.KEY_LEFT):
                self.log_tab = (self.log_tab - 1) % len(LOG_TABS)
                self.reload_logs()
            elif key in ("1", "2", "3"):
                self.log_tab = int(key) - 1
                self.reload_logs()
            elif key in ("r", "R"):
                self.reload_logs()
            elif key == curses.KEY_UP:
                self.log_offset = self.log_offset + 1 if self.log_max_offset is None else min(self.log_max_offset, self.log_offset + 1)
            elif key == curses.KEY_DOWN:
                self.log_offset = max(0, self.log_offset - 1)
            elif key == curses.KEY_PPAGE:
                step = max(1, self.log_page_rows - 1)
                self.log_offset = self.log_offset + step if self.log_max_offset is None else min(self.log_max_offset, self.log_offset + step)
            elif key == curses.KEY_NPAGE:
                step = max(1, self.log_page_rows - 1)
                self.log_offset = max(0, self.log_offset - step)
            elif key == curses.KEY_HOME:
                self.log_offset = self.log_max_offset if self.log_max_offset is not None else 10**9
            elif key == curses.KEY_END:
                self.log_offset = 0
            return
        if self.modal == "users":
            users = get_users(self.config)
            if key in (curses.KEY_UP, curses.KEY_DOWN) and users:
                self.user_index = (self.user_index + (1 if key == curses.KEY_DOWN else -1)) % len(users)
            elif key == " " and users:
                student_id = users[self.user_index]["student_id"]
                self.pending_user_ids.symmetric_difference_update({student_id})
                self.form_error = ""
            elif enter and users:
                self.apply_user_selection()
            elif key in ("v", "V") and users:
                self.modal = "user_result"
            elif key in ("a", "A"):
                self.open_form("add")
            elif key in ("n", "N"):
                self.open_form("rename")
            elif key in ("p", "P"):
                self.open_form("password")
            elif key in ("i", "I"):
                self.open_form("student_id")
            elif key in ("x", "X") and users:
                self.modal = "delete"
                self.confirm_selection = 0
            elif key in ("d", "D") and users:
                self.modal = "default"
                self.confirm_selection = 0
                self._default_agent_active = self._automation_active()
            return
        if self.modal == "default":
            if key in ("\t", curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_BTAB):
                self.confirm_selection = 1 - self.confirm_selection
            elif enter:
                if self.confirm_selection:
                    users = get_users(self.config)
                    self._default_agent_active = _UNSET
                    if users:
                        self.account_action("default", users[min(self.user_index, len(users) - 1)]["username"])
                else:
                    self._default_agent_active = _UNSET
                    self.modal = "users"
            return
        if self.modal == "delete":
            if key in ("\t", curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_BTAB):
                self.confirm_selection = 1 - self.confirm_selection
            elif enter:
                if self.confirm_selection:
                    self.account_action("delete", get_users(self.config)[self.user_index]["username"])
                else:
                    self.discard_modal()
            return
        if self.modal in ("add", "rename", "password", "student_id"):
            if key in (curses.KEY_F2, "\x14") and self.modal in ("add", "password"):
                self.password_visible = not self.password_visible
            elif key == "\x13" or (enter and self.form_index == len(self.form_values) - 1):
                self.submit_account_form()
            elif enter or key in ("\t", curses.KEY_DOWN, curses.KEY_UP, curses.KEY_BTAB):
                leaving = self.form_index
                self.form_index = (self.form_index + (-1 if key in (curses.KEY_UP, curses.KEY_BTAB) else 1)) % len(self.form_values)
                self.form_error = self.validate_account_field(leaving, allow_empty=True) or ""
            else:
                if len(self.form_editors) != len(self.form_values):
                    self.form_editors = [TextCursor(value) for value in self.form_values]
                limit = 256 if self.modal == "password" or (self.modal == "add" and self.form_index == 2) else 60
                self.form_values[self.form_index] = self.form_editors[self.form_index].edit(self.form_values[self.form_index], key, limit=limit)

    def display_status_title(self, title=None, tone=None):
        """色覚や NO_COLOR 環境でも状態種別を区別できるよう表示用接頭辞を付ける。"""
        if title is None or tone is None:
            title, tone, _ = self.status()
        if tone == 3:
            kind = "warning" if self.unenrolled and not self.failed else "error"
        elif tone == 4:
            kind = "warning"
        elif tone == 2:
            kind = "success"
        else:
            kind = ""
        return prefix_text(kind, title)

    def draw(self, screen):
        screen.erase()
        canvas = Canvas(screen, use_colors=self.use_colors)
        height, width = canvas.height, canvas.width
        if height < 24 or width < 62:
            canvas.put(1, 1, "画面を広げてください（幅62 × 高さ24以上）。", curses.A_BOLD)
            canvas.put(3, 1, self.display_status_title())
            busy_hint = ("中断しています…" if self.cancel_event is not None and self.cancel_event.is_set()
                         else "処理中… Esc/Q: 中断" if self.cancel_event is not None
                         else "処理中… 完了までお待ちください。")
            canvas.put(5, 1, busy_hint if self.busy else "Q: 終了")
            if self.update_version:
                update_action = "brew upgrade attendance" if managed_by_homebrew() else "att update"
                canvas.put(7, 1, prefix_text("update", f"更新あり: v{self.update_version}"), curses.A_BOLD)
                canvas.put(8, 1, "内容: att update --notes")
                canvas.put(9, 1, f"適用: {update_action}")
            if self.save_prompt.active:
                canvas.put(11 if self.update_version else 7, 1, prefix_text("warning", "未保存の変更があります。画面を広げてください。"), curses.A_BOLD)
            screen.refresh()
            return

        wide = width >= 106
        total = min(width - 4, 112 if wide else 78)
        left = (width - total) // 2
        main_width = total - 32 if wide else total
        if self.update_version:
            update_action = "brew upgrade attendance" if managed_by_homebrew() else "att update"
            canvas.put(0, left, prefix_text("update", f"アップデートがあります: v{self.update_version}"), canvas.color(4) | curses.A_BOLD, total)
            canvas.put(4, left, f"内容: att update --notes / 適用: {update_action}", canvas.color(4), total)
        name = self.config.get("_active_username", self.student_id)
        default = select_user(self.config)
        is_default = default and default["student_id"] == self.student_id
        canvas.put(1, left, "ATTENDANCE", canvas.color(1) | curses.A_BOLD)
        canvas.put(1, left + 13, "/ 出席確認", curses.A_BOLD)
        mode = "[DEMO / 接続なし]" if self.demo else "[確認のみ]" if self.config.get("dry_run") else "[出席登録]"
        canvas.put(1, left + total - 20, mode, canvas.color(4) | curses.A_BOLD, 20)
        if len(self.selected_user_ids) > 1:
            names = "、".join(user["username"] for user in self.selected_users())
            canvas.put(2, left, f"選択中 {len(self.selected_user_ids)}人: {names}", curses.A_BOLD, main_width - 1)
            canvas.put(3, left, "U: ユーザー選択・個別結果  S: 予定  L: ログ", curses.A_DIM, main_width - 1)
        else:
            canvas.put(2, left, f"{name}  {'[既定]' if is_default else '[今回のユーザー]'}", curses.A_BOLD, main_width - 1)
            canvas.put(3, left, f"学生番号 {self.student_id}  U: ユーザー  S: 予定  L: ログ", curses.A_DIM, main_width - 1)
        canvas.button(5, left, f"QR番号 [ {self.room or '___'} ]", self.selected == 0)
        if self.selected == 0:
            cursor = self.room_editor.sync(self.room)
            caret = self.room[cursor:cursor + 1] or "_"
            canvas.put(5, left + 1 + text_width("QR番号 [ ") + text_width(self.room[:cursor]),
                       caret, canvas.color(1) | curses.A_REVERSE | curses.A_UNDERLINE | curses.A_BOLD, max(1, text_width(caret)))
        canvas.button(5, left + 28, "[ 更新 R ]", self.selected == 1, not self.busy)
        title, tone, detail = self.status()
        banner_attr = canvas.color(tone) | curses.A_REVERSE
        canvas.fill(7, left, main_width, 2, banner_attr)
        canvas.put(7, left + 2, self.display_status_title(title, tone), banner_attr | curses.A_BOLD, main_width - 4)
        canvas.put(8, left + 2, detail, banner_attr, main_width - 4)

        canvas.box(10, left, main_width, 7, "授業情報", canvas.color(1))
        course = self.values.get("授業名", "授業情報はまだ取得されていません")
        course_lines = wrap_text(course, main_width - 6)
        for index, line in enumerate(course_lines[:2]):
            canvas.put(11 + index, left + 3, line, curses.A_BOLD, main_width - 6)
        canvas.put(14, left + 3, "教室  " + self.values.get("教室名", "--"), width=(main_width - 6) // 2)
        canvas.put(14, left + main_width // 2, "時限  " + self.values.get("時限", "--"), width=main_width // 2 - 3)
        canvas.put(15, left + 3, f"時間  {self.values.get('開始時刻', '--:--')} ～ {self.values.get('終了時刻', '--:--')}", width=main_width - 6)

        label = "[ 出席済み ]" if self.attended and self.values else "[ 出席で登録する ]"
        if len(self.selected_user_ids) > 1 and not self.attended:
            label = f"[ {len(self.registration_targets())}人を出席登録 ]"
        canvas.button(18, left, label, self.selected == 2, self.can_register())
        canvas.put(18, left + 27, f"最終確認 {self.updated}", curses.A_DIM, main_width - 27)
        canvas.button(19, left, "[ スケジュール登録 S ]", self.selected == 3, not self.busy)
        canvas.put(19, left + 30, "曜日・時刻・教室を設定", curses.A_DIM, main_width - 30)
        is_error_message = self.message_error or (self.failed and not self._account_notice)
        display_message = prefix_text("error", self.message) if is_error_message else self.message
        message_attr = (canvas.color(3) | curses.A_BOLD) if is_error_message else canvas.color(1)
        for index, line in enumerate(wrap_text(display_message, main_width - 2)[:2]):
            canvas.put(20 + index, left + 1, line, message_attr, main_width - 2)

        if wide:
            side = left + main_width + 2
            canvas.box(5, side, 30, 7, "操作ガイド", canvas.color(1))
            for row, text in enumerate(("Enter  選択項目を実行", "R 更新 / U ユーザー", "S      自動出席の予定", "L      ログを表示", "H ヘルプ / Q 終了"), 6):
                canvas.put(row, side + 2, text, width=26)
            canvas.box(13, side, 30, min(height - 17, 14), "今回の操作履歴", canvas.color(1))
            if not self.history:
                canvas.put(15, side + 2, "まだ操作はありません", curses.A_DIM, 26)
            for index, (stamp, qr, message) in enumerate(list(self.history)[:max(0, (min(height - 17, 14) - 2) // 3)]):
                row = 14 + index * 3
                canvas.put(row, side + 2, f"{stamp}  QR {qr}", curses.A_DIM, 26)
                canvas.put(row + 1, side + 2, message, width=26)
        elif height >= 29:
            canvas.box(23, left, main_width, height - 26, "今回の操作履歴", canvas.color(1))
            for index, (stamp, qr, message) in enumerate(list(self.history)[:height - 28]):
                canvas.put(24 + index, left + 2, f"{stamp}  QR {qr}  {message}", width=main_width - 4)
        footer = ("Tab 選択 Enter 実行 R 更新 U ユーザー S 予定 L ログ H ヘルプ Q 終了" if total >= 68
                  else "Tab選択 Enter実行 R更新 U管理 S予定 Lログ Hヘルプ Q終了")
        if self.busy and self.cancel_event is not None:
            footer = "中断しています… 安全な位置で停止します" if self.cancel_event.is_set() else "処理中… Esc / Q / Ctrl+C: 中断"
        elif self.save_prompt.active:

            footer = "↑↓/Tab: 選択 Enter: 決定 Esc/Q: 編集に戻る"
        elif self.modal:
            editing = self.modal in ("add", "rename", "password", "student_id") or (
                self.modal == "schedule" and self.schedule_panel.mode in ("add", "edit", "period"))
            footer = "Esc: 戻る・保存確認  " + ("Ctrl+S: 保存  q/Q: 文字入力" if editing else "Q: 戻る")
        canvas.put(height - 2, left, footer, curses.A_DIM, total)
        if self.demo and height >= 26:
            canvas.put(height - 3, left, "デモ: 1 未出席 / 2 出席済 / 3 受付なし / 4 履修なし / 5 エラー", curses.A_DIM, total)

        if self.confirm:
            self.draw_confirmation(canvas)
        elif self.modal:
            self.draw_modal(canvas)
        if self.save_prompt.active:
            self.save_prompt.draw(canvas)
        screen.refresh()

    def open_confirmation(self):
        """出席登録の確認画面を初期状態（戻る選択・先頭スクロール）で開く。"""
        self.confirm = True
        self.confirm_selection = 0
        self.confirm_scroll = 0

    def confirmation_layout(self, canvas_height, canvas_width):
        """確認画面の枠サイズと1ページあたりの表示人数を返す。"""
        width = min(canvas_width - 6, 74)
        height = min(canvas_height - 2, 24)
        per_page = max(1, (height - 12) // 2)
        return width, height, per_page

    def _confirmation_status(self, user, single_user):
        """確認画面に表示するユーザーごとの対象可否と理由を返す。"""
        result = self.user_results.get(user["student_id"], {})
        if single_user:
            failed = self.failed
            unenrolled = self.unenrolled
            attended = self.attended
            has_values = bool(self.values)
            raw_result = result.get("result", "")
            checked = self.checked or bool(result)
        else:
            failed = bool(result.get("failed"))
            unenrolled = bool(result.get("unenrolled"))
            attended = bool(result.get("attended"))
            has_values = bool(result.get("values"))
            raw_result = result.get("result", "")
            checked = bool(result)
        if failed:
            reason = "授業情報不一致" if "授業情報が他のユーザーと異なります" in raw_result else "確認失敗"
            return False, reason
        if unenrolled:
            return False, "未履修（履修登録なし）"
        if attended:
            return False, "出席済み"
        if not has_values:
            return False, "受付対象なし" if checked and raw_result != "処理中…" else "未確認"
        if self.config.get("dry_run"):
            return False, "確認のみモード"
        return True, "登録対象"

    def confirmation_entries(self):
        """選択中全ユーザーの登録対象可否と理由の一覧を返す。"""
        users = self.selected_users()
        single_user = len(users) == 1
        entries = []
        for user in users:
            eligible, reason = self._confirmation_status(user, single_user)
            entries.append({
                "user": user,
                "username": user["username"],
                "student_id": user["student_id"],
                "eligible": eligible,
                "reason": reason,
            })
        return entries

    @staticmethod
    def _format_confirmation_user(prefix, username, student_id, inner_width):
        """学籍番号を欠落させずに長いユーザー名を枠内へ収める。"""
        suffix = f" / {student_id}"
        available_for_name = inner_width - text_width(prefix) - text_width(suffix)
        if available_for_name < 4:
            return fit_text(f"{prefix}{username}{suffix}", inner_width)
        if text_width(username) > available_for_name:
            # Ambiguous幅文字の「…」が端末設定により2桁表示されても枠を越えないよう2桁控える。
            username = fit_text(username, max(1, available_for_name - 2)) + "…"
        return fit_text(f"{prefix}{username}{suffix}", inner_width)

    def handle_confirmation(self, key, height=24, width=80):
        """確認画面の一覧スクロール・ボタン選択・決定・取消を処理する。"""
        total = len(self.selected_users())
        _, _, per_page = self.confirmation_layout(height, width)
        max_scroll = max(0, total - per_page)
        self.confirm_scroll = max(0, min(self.confirm_scroll, max_scroll))
        can_reg = self.can_register()
        if key in ("\t", curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_BTAB):
            self.confirm_selection = (1 - self.confirm_selection) if can_reg else 0
        elif key == curses.KEY_UP:
            self.confirm_scroll = max(0, self.confirm_scroll - 1)
        elif key == curses.KEY_DOWN:
            self.confirm_scroll = min(max_scroll, self.confirm_scroll + 1)
        elif key == curses.KEY_PPAGE:
            self.confirm_scroll = max(0, self.confirm_scroll - per_page)
        elif key == curses.KEY_NPAGE:
            self.confirm_scroll = min(max_scroll, self.confirm_scroll + per_page)
        elif key == curses.KEY_HOME:
            self.confirm_scroll = 0
        elif key == curses.KEY_END:
            self.confirm_scroll = max_scroll
        elif key in ("\n", "\r", curses.KEY_ENTER):
            if self.confirm_selection == 1 and can_reg:
                self.start(register=True)
            else:
                self.confirm = False
                self.confirm_scroll = 0
        elif key == "\x1b":
            self.confirm = False
            self.confirm_scroll = 0

    def draw_confirmation(self, canvas):
        width, height, per_page = self.confirmation_layout(canvas.height, canvas.width)
        left = (canvas.width - width) // 2
        top = max(1, (canvas.height - height) // 2)
        inner = width - 6
        canvas.box(top, left, width, height, prefix_text("warning", "出席登録の確認"), canvas.color(4))

        entries = self.confirmation_entries()
        eligible_entries = [entry for entry in entries if entry["eligible"]]
        total = len(entries)
        eligible_count = len(eligible_entries)
        excluded_count = total - eligible_count

        max_scroll = max(0, total - per_page)
        self.confirm_scroll = max(0, min(self.confirm_scroll, max_scroll))
        visible = entries[self.confirm_scroll:self.confirm_scroll + per_page]

        count_text = f"登録対象 {eligible_count}人 / 対象外 {excluded_count}人"
        start = self.confirm_scroll + 1 if visible else 0
        end = self.confirm_scroll + len(visible)
        page_text = f"表示 {start}-{end} / {total}人" if total else "表示 0 / 0人"
        page_width = text_width(page_text)
        canvas.put(top + 1, left + 3, count_text, curses.A_BOLD, max(1, inner - page_width - 2))
        canvas.put(top + 1, left + 3 + inner - page_width, page_text, curses.A_DIM, page_width)

        if eligible_entries:
            names = "、".join(entry["username"] for entry in eligible_entries)
            if text_width("対象: " + names) > inner:
                preview = "対象: " + fit_text(names, max(1, inner - 8)) + "…"
            else:
                preview = "対象: " + names
            canvas.put(top + 2, left + 3, preview, curses.A_BOLD, inner)
        else:
            canvas.put(top + 2, left + 3, "対象: なし（登録できるユーザーはいません）", canvas.color(3) | curses.A_BOLD, inner)

        canvas.put(top + 3, left + 3, "授業名", curses.A_DIM, 6)
        canvas.put(top + 3, left + 11, self.values.get("授業名", "--"), curses.A_BOLD, inner - 8)
        half = inner // 2
        canvas.put(top + 4, left + 3, "教室名", curses.A_DIM, 6)
        canvas.put(top + 4, left + 11, self.values.get("教室名", "--"), curses.A_BOLD, max(1, half - 9))
        canvas.put(top + 4, left + 3 + half, "時限", curses.A_DIM, 4)
        canvas.put(top + 4, left + 3 + half + 6, self.values.get("時限", "--"), curses.A_BOLD, max(1, inner - half - 6))
        canvas.put(top + 5, left + 3, "時間", curses.A_DIM, 4)
        time_range = f"{self.values.get('開始時刻', '--:--')} ～ {self.values.get('終了時刻', '--:--')}"
        canvas.put(top + 5, left + 11, time_range, curses.A_BOLD, inner - 8)

        list_header = "選択ユーザー一覧（↑↓/PgUp/PgDnで移動）" if total > per_page else "選択ユーザー一覧"
        canvas.put(top + 6, left + 3, list_header, canvas.color(1), inner)
        for index, entry in enumerate(visible):
            row = top + 7 + index * 2
            prefix = "[+] " if entry["eligible"] else "[-] "
            user_line = self._format_confirmation_user(prefix, entry["username"], entry["student_id"], inner)
            user_attr = (canvas.color(2) | curses.A_BOLD) if entry["eligible"] else curses.A_BOLD
            canvas.put(row, left + 3, user_line, user_attr, inner)
            reason_line = "    登録対象" if entry["eligible"] else f"    対象外: {entry['reason']}"
            reason_attr = canvas.color(2) if entry["eligible"] else canvas.color(3 if entry["reason"] in ("確認失敗", "授業情報不一致") else 4)
            canvas.put(row + 1, left + 3, reason_line, reason_attr, inner)

        canvas.put(top + height - 5, left + 3, f"{eligible_count}人を登録します（対象外 {excluded_count}人）。", width=inner)
        can_reg = self.can_register()
        if not can_reg:
            note = "確認のみモードのため登録できません。" if self.config.get("dry_run") else "登録できる対象ユーザーがいません。"
            canvas.put(top + height - 4, left + 3, note, canvas.color(3), inner)
        elif total > per_page:
            canvas.put(top + height - 4, left + 3, f"↑↓/PgUp/PgDn でスクロール（{start}-{end} / {total}人）", curses.A_DIM, inner)

        focus_register = can_reg and self.confirm_selection == 1
        canvas.button(top + height - 3, left + 3, "[ 戻る ]", not focus_register)
        canvas.button(top + height - 3, left + 20, "[ 出席登録 ]", focus_register, enabled=can_reg)
        canvas.put(top + height - 2, left + 3, "Tab/←→ 選択  Enter 決定  Esc 戻る", curses.A_DIM, inner)

    def draw_logs(self, canvas):
        width = min(canvas.width - 6, 108)
        height = min(canvas.height - 4, 22)
        left = (canvas.width - width) // 2
        top = (canvas.height - height) // 2
        inner_width = max(10, width - 6)
        canvas.box(top, left, width, height, "ログ確認", canvas.color(1))
        x = left + 3
        for index, (_, label) in enumerate(LOG_TABS):
            tab = f" {label} "
            attr = (canvas.color(1) | curses.A_REVERSE | curses.A_BOLD) if index == self.log_tab else curses.A_DIM
            canvas.put(top + 1, x, tab, attr, left + width - 3 - x)
            x += text_width(tab) + 1
        wrapped = [part for line in self.log_lines for part in wrap_text(line, inner_width)] if self.log_lines else []
        body_rows = max(1, height - 6)
        max_offset = max(0, len(wrapped) - body_rows)
        self.log_max_offset = max_offset
        self.log_page_rows = body_rows
        self.log_offset = min(self.log_offset, max_offset)
        if wrapped:
            start = max_offset - self.log_offset
            end = min(len(wrapped), start + body_rows)
            pos = f"{'末尾 ' if self.log_offset == 0 else ''}{start + 1}-{end}/{len(wrapped)}行"
            canvas.put(top + 2, left + 3, f"{self.log_source} ({pos})", curses.A_DIM, inner_width)
            for row, line in enumerate(wrapped[start:end], top + 3):
                attr = canvas.color(3) if " ERROR " in line or "失敗" in line else 0
                canvas.put(row, left + 3, line, attr, inner_width)
        else:
            canvas.put(top + 2, left + 3, self.log_source, curses.A_DIM, inner_width)
            for row, line in enumerate(wrap_text(self.log_empty_message, inner_width)[:body_rows], top + 4):
                canvas.put(row, left + 3, line, curses.A_DIM, inner_width)
        canvas.put(top + height - 2, left + 3, "Tab/1-3:切替 ↑↓/PgUpDn:移動 R:更新 Esc/Q:戻る", curses.A_DIM, inner_width)

    def draw_modal(self, canvas):
        if self.modal == "schedule":
            self.schedule_panel.draw(canvas)
            return
        if self.modal == "logs":
            self.draw_logs(canvas)
            return
        width = min(canvas.width - 6, 72)
        left = (canvas.width - width) // 2
        height = min(canvas.height - 4, 20)
        top = (canvas.height - height) // 2
        title = {"users": "ユーザー選択・管理", "user_result": "ユーザーごとの結果", "add": "ユーザー登録", "rename": "ユーザーネームの変更", "password": "保存パスワードの変更", "student_id": "学籍番号の変更", "delete": "ユーザーの削除", "default": "既定ユーザーを変更", "help": "操作ヘルプ"}[self.modal]
        canvas.box(top, left, width, height, title, canvas.color(1))
        if self.modal == "users":
            users = get_users(self.config)
            default = select_user(self.config)
            canvas.put(top + 2, left + 3, f"Space 選択/解除  Enter 決定（{len(self.pending_user_ids)}人）", width=width - 6)
            count = max(1, (height - 10) // 2)
            self.user_index = min(self.user_index, max(0, len(users) - 1))
            first = max(0, self.user_index - count + 1)
            for index, user in enumerate(users[first:first + count], first):
                row = top + 4 + (index - first) * 2
                tags = " [既定]" if user == default else ""
                marker = "[x]" if user["student_id"] in self.pending_user_ids else "[ ]"
                attr = curses.A_REVERSE if index == self.user_index else 0
                canvas.fill(row, left + 2, width - 4, 1, attr)
                canvas.put(row, left + 3, f"{marker} {user['username']} / {user['student_id']}{tags}", attr, width - 6)
                entry = self.user_results.get(user["student_id"], {})
                result = entry.get("result", "未確認")
                if entry.get("failed"):
                    result = prefix_text("error", result)
                    result_attr = canvas.color(3) | curses.A_BOLD
                else:
                    result_attr = curses.A_DIM
                canvas.put(row + 1, left + 7, result, result_attr, width - 10)
            canvas.put(top + height - 6, left + 3, "A 追加  N 名前変更  P パスワード変更", canvas.color(1), width - 6)
            canvas.put(top + height - 5, left + 3, "I 学籍番号変更  X 削除", canvas.color(1), width - 6)
            canvas.put(top + height - 4, left + 3, "D 既定に設定  V 結果の詳細", canvas.color(1), width - 6)
            error_text = prefix_text("error", self.form_error) if self.form_error else ""
            canvas.put(top + height - 3, left + 3, error_text or "Esc/Qで戻る（変更があれば保存確認）。", (canvas.color(3) | curses.A_BOLD) if self.form_error else curses.A_DIM, width - 6)
        elif self.modal == "user_result":
            user = get_users(self.config)[self.user_index]
            canvas.put(top + 2, left + 3, f"{user['username']} / {user['student_id']}", curses.A_BOLD, width - 6)
            result = self.user_results.get(user["student_id"], {})
            details = result.get("result", "未確認です。ユーザーを選択して授業情報を取得してください。")
            if result.get("failed"):
                details = prefix_text("error", details)
            if result.get("values"):
                details += "\n\n" + "\n".join(f"{label}: {value}" for label, value in result["values"].items())
            for index, line in enumerate(wrap_text(details, width - 6)[:height - 7]):
                attr = (canvas.color(3) | curses.A_BOLD) if result.get("failed") and index == 0 else 0
                canvas.put(top + 4 + index, left + 3, line, attr, width - 6)
        elif self.modal == "delete":
            user = get_users(self.config)[self.user_index]
            canvas.put(top + 2, left + 3, user["username"], curses.A_BOLD, width - 6)
            canvas.put(top + 3, left + 3, f"学籍番号: {user['student_id']}", curses.A_BOLD, width - 6)
            details = "このアプリの登録と保存パスワードを削除します。\n大学側のアカウントは削除しません。"
            if len(get_users(self.config)) == 1:
                details += "\n最後のユーザーです。削除後はAで追加できます。"
            elif user == select_user(self.config):
                details += "\n既定ユーザーは残った一覧の先頭に切り替わります。"
            for index, line in enumerate(wrap_text(details, width - 6)):
                canvas.put(top + 5 + index, left + 3, line, width=width - 6)
            canvas.button(top + height - 5, left + 3, "[ キャンセル ]", self.confirm_selection == 0)
            canvas.button(top + height - 5, left + 29, "[ 削除 ]", self.confirm_selection == 1)
            canvas.put(top + height - 3, left + 3, "Tab/←→で選択し、Enterで決定します。", curses.A_DIM, width - 6)
        elif self.modal in ("add", "rename", "password", "student_id"):
            fields = (("ユーザーネーム", "学生番号", "パスワード") if self.modal == "add" else
                      ("新しいパスワード", "新しいパスワード（確認）") if self.modal == "password" else
                      ("新しい学籍番号",) if self.modal == "student_id" else ("新しいユーザーネーム",))
            if self.modal in ("password", "student_id"):
                user = get_users(self.config)[self.user_index]
                canvas.put(top + 2, left + 3, f"{user['username']} / {user['student_id']}", width=width - 6)
                detail = "保存パスワードを新番号へ引き継ぎます。" if self.modal == "student_id" else "大学側で変更済みのパスワードを入力します。"
                canvas.put(top + 10, left + 3, detail, curses.A_DIM, width - 6)
            for index, label in enumerate(fields):
                row = top + 3 + index * 3
                canvas.put(row, left + 3, label, curses.A_DIM)
                value = self.form_values[index] if index < len(self.form_values) else ""
                if len(self.form_editors) != len(self.form_values):
                    self.form_editors = [TextCursor(field) for field in self.form_values]
                masked = (self.modal == "password" or (self.modal == "add" and index == 2)) and not self.password_visible
                self.form_editors[index].draw(canvas, row + 1, left + 4, value, width - 8, active=index == self.form_index, masked=masked)
            guide = "Ctrl+S/最後Enter: 保存"
            if self.modal in ("add", "password"):
                guide += "  F2/Ctrl+T: " + ("隠す" if self.password_visible else "パスワード表示")
            canvas.put(top + height - 5, left + 3, guide, canvas.color(1), width - 6)
            footer_text = prefix_text("error", self.form_error) if self.form_error else ("デモの変更は保存されません。" if self.demo else "パスワードはKeychainに保存されます。")
            footer_attr = (canvas.color(3) | curses.A_BOLD) if self.form_error else curses.A_DIM
            for offset, line in enumerate(wrap_text(footer_text, width - 6)[:2]):
                canvas.put(top + height - 4 + offset, left + 3, line, footer_attr, width - 6)
        elif self.modal == "default":
            users = get_users(self.config)
            if users:
                user = users[min(self.user_index, len(users) - 1)]
                if self._default_agent_active is _UNSET:
                    self._default_agent_active = self._automation_active()
                canvas.put(top + 2, left + 3, prefix_text("warning", f"変更先: {user['username']} ({user['student_id']})"), curses.A_BOLD, width - 6)
                paragraphs = [wrap_text(entry, width - 6) for entry in self.default_switch_impact(user, agent_active=self._default_agent_active)]
                row, limit = top + 4, top + height - 6
                spaced = sum(len(lines) for lines in paragraphs) + max(0, len(paragraphs) - 1) <= limit - row
                for lines in paragraphs:
                    for line in lines:
                        if row < limit:
                            canvas.put(row, left + 3, line, width=width - 6)
                            row += 1
                    if spaced and row < limit:
                        row += 1
            canvas.button(top + height - 5, left + 3, "[ キャンセル ]", self.confirm_selection == 0)
            canvas.button(top + height - 5, left + 25, "[ 切り替える ]", self.confirm_selection == 1)
            canvas.put(top + height - 3, left + 3, "Tab/←→で選択し、Enterで決定します。", curses.A_DIM, width - 6)
        else:
            guide = (
                "入力: ←→で移動 / Home・End / Delete",
                "Tab・↑↓: 項目  Ctrl+S: 保存  Enter: 決定",
                "R: 更新  Esc/Q: 処理中断  U: ユーザー  S: 予定 L: ログ",
                "ユーザー: Spaceで複数選択、Enterで保存",
                "登録確認: Tab・←→で選択、↑↓/PgUp/PgDn一覧",
                "パスワード: F2 または Ctrl+T で表示/非表示",
                "GUIの選択を保存。既定の変更は U → D。",
                "ユーザーごとの結果は U → V で確認。ログは L。",
            )
            for index, line in enumerate(guide):
                for offset, part in enumerate(wrap_text(line, width - 6)):
                    canvas.put(top + 2 + index * 2 + offset, left + 3, part, width=width - 6)
        guide = "Esc: 戻る・保存確認  Tab/↑↓: 項目  Ctrl+S: 保存" if self.modal in ("add", "rename", "password", "student_id") else "Esc/Q: 戻る"
        canvas.put(top + height - 2, left + 3, guide, curses.A_DIM, width - 6)

    def run(self, screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.use_colors = init_colors()
        screen.keypad(True)
        screen.timeout(100)
        try:
            with _disable_xon_flow_control():
                return self._run_loop(screen)
        finally:
            self.clear_sensitive_state()

    def _run_loop(self, screen):
        if self.room:
            self.start()
        while True:
            self.poll()
            self.draw(screen)
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                if self.busy and (self.request_cancel() or self.cancel_event is None):
                    continue
                raise
            if self.busy:
                if key in ("\x1b", "q", "Q", "\x03"):
                    self.request_cancel()
                continue
            if key == curses.KEY_RESIZE:
                continue
            height, width = screen.getmaxyx()
            if height < 24 or width < 62:
                if self.save_prompt.active:
                    if key in ("\x1b", "q", "Q"):
                        self.save_prompt.handle(key)
                    continue
                if key in ("q", "Q", "\x03"):
                    if self.modal:
                        self.handle_modal("\x1b")
                    elif self.confirm:
                        self.confirm = False
                        self.confirm_scroll = 0
                    else:
                        return 0
                elif key == "\x1b":
                    if self.modal:
                        self.handle_modal(key)
                    elif self.confirm:
                        self.confirm = False
                        self.confirm_scroll = 0
                continue
            if self.save_prompt.active:
                self.save_prompt.handle(key)
                continue
            if self.confirm:
                self.handle_confirmation(key, height, width)
                continue
            if self.modal:
                self.handle_modal(key)
                continue
            if key in ("q", "Q", "\x03"):
                return 0
            if key in ("\t", curses.KEY_DOWN, curses.KEY_UP, curses.KEY_BTAB):
                self.selected = (self.selected + (-1 if key in (curses.KEY_UP, curses.KEY_BTAB) else 1)) % 4
            elif key in ("r", "R"):
                self.start()
            elif key in ("u", "U"):
                self.open_users()
            elif key in ("s", "S"):
                self.schedule_panel.open()
            elif key in ("l", "L"):
                self.open_logs()
            elif key in ("h", "H", "?"):
                self.modal = "help"
            elif self.demo and self.selected != 0 and key in ("1", "2", "3", "4", "5"):
                self.demo_state = {"1": "unattended", "2": "attended", "3": "empty", "4": "unenrolled", "5": "error"}[key]
                for student_id in self.selected_user_ids:
                    self.demo_attended.discard((student_id, self.room))
                self.start()
            elif key in ("\n", "\r", curses.KEY_ENTER):
                if self.selected == 3:
                    self.schedule_panel.open()
                elif self.selected == 2:
                    if self.can_register():
                        self.open_confirmation()
                else:
                    self.start()
            elif self.selected == 0:
                if isinstance(key, str):
                    key = normalize_room_digits(key)
                edited = self.room_editor.edit(self.room, key, limit=4, accepts=lambda value: value.isascii() and value.isdecimal())
                if edited != self.room:
                    self.room = edited
                    self.clear_target()
                    self.message = "QR番号を入力して Enter で確認してください。"


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
            initial = username
            if initial is None:
                selected = ui_selection(config)
                default = select_user(config)
                primary = default if default and default["student_id"] in selected else next((user for user in get_users(config) if user["student_id"] in selected), None)
                initial = primary["username"] if primary else None
            student_id, password = get_credentials(config, base / "config.json", username=initial)
            if check:
                config["dry_run"] = True
        try:
            ui = AttendanceScreen(config, student_id, password, logger, room or "", demo, base, restore_selection=username is None)
        finally:
            del password
        try:
            return curses.wrapper(ui.run)
        finally:
            ui.clear_sensitive_state()
    except (ConfigurationError, CredentialError, RoomError, curses.error) as exc:
        print(f"画面を起動できません: {exc}", file=sys.stderr)
        return 1
    except EOFError:
        print("入力を中断しました。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("処理を中断しました。", file=sys.stderr)
        return 130
