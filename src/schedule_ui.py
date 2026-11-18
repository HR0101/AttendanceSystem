"""授業ごとの自動出席予定を編集するターミナル画面。"""

import copy
import curses
import queue
import threading
from datetime import date

from .config import load_config
from .scheduler import (
    WEEKDAYS, ScheduleError, agent_installed, calendar_intervals, install_agent,
    load_schedule, registration_time, remove_agent, save_schedule, schedule_data,
    schedule_digest, schedule_lock, validate_schedule,
)
from .terminal_layout import color
from .text_input import TextCursor
from .users import select_user


DAYS = dict(zip(WEEKDAYS, "月火水木金土日"))


class SchedulePanel:
    def __init__(self, owner):
        self.owner = owner
        self.data = None
        self.active = False
        self.index = 0
        self.mode = "list"
        self.fields = []
        self.initial_fields = ()
        self.editors = []
        self.field_index = 0
        self.message = ""
        self.undo_state = None
        self.events = queue.Queue()

    def work(self, operation):
        self.owner.busy = True
        self.message = "処理中…"

        def run():
            try:
                result = operation()
                self.events.put((result, None))
            except Exception as exc:
                # launchctlやファイル操作の失敗も画面に残し、再操作できるようにする。
                from .config import ConfigurationError
                message = str(exc) if isinstance(exc, (ScheduleError, ConfigurationError, OSError)) else "予定を処理できませんでした。"
                self.events.put((None, message))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        try:
            result, error = self.events.get_nowait()
        except queue.Empty:
            return
        self.owner.busy = False
        if error:
            self.message = error
            if self.data is None:
                self.undo_state = None
        else:
            self.data = result["data"]
            self.active = result["active"]
            self.message = result["message"]
            if "undo_state" in result:
                self.undo_state = result["undo_state"]
            self.mode = "list"
            self.fields = []
            self.initial_fields = ()
            self.editors = []
            target_index = result.get("index", self.index)
            self.index = min(target_index, max(0, len(self.data["courses"]) - 1))

    def open(self, *, clear_undo=False):
        self.owner.modal = "schedule"
        self.mode = "list"
        self.fields = []
        self.initial_fields = ()
        self.editors = []
        self.data = None
        previous_undo = None if (clear_undo or self.owner.demo) else self.undo_state

        def read():
            base = self.owner.base
            if self.owner.demo:
                data = schedule_data(load_schedule(base / "schedule.json")) if base and (base / "schedule.json").exists() else self.empty_data()
                return {"data": data, "active": False, "message": "デモ: 変更は保存されません。", "undo_state": None}
            if base is None:
                raise ScheduleError("設定ファイルの場所を確認できません。")
            path = base / "schedule.json"
            with schedule_lock(path, exclusive=False):
                exists = path.exists()
                data = schedule_data(load_schedule(path)) if exists else self.empty_data()
                digest = schedule_digest(path) if exists else None
            # 再読込(R)時はクリアし、画面を閉じて開き直した時は外部変更がなければ1段階の元に戻すを維持する。
            undo_state = previous_undo if (previous_undo is not None and digest is not None and previous_undo.get("disk_sha256") == digest) else None
            message = "Aで授業を追加できます。Zで直前の変更を元に戻せます。" if undo_state else "Aで授業を追加できます。"
            return {"data": data, "active": agent_installed(base), "message": message, "undo_state": undo_state}
        self.work(read)

    @staticmethod
    def empty_data():
        today = date.today()
        return {"active_from": today.isoformat(), "active_until": date(today.year, 12, 31).isoformat(), "blackout_dates": [], "courses": []}

    @staticmethod
    def _sync_agent(base, schedule) -> bool:
        """有効な授業があれば LaunchAgent を更新し、なければ削除する。失敗時は False を返す。"""
        try:
            if calendar_intervals(schedule["courses"]):
                install_agent(base, schedule)
            else:
                remove_agent(base)
            return True
        except (ScheduleError, OSError):
            return False

    def persist(self, candidate, *, undo_label="予定の変更"):
        # 入力エラーはフォームを閉じずに表示する。
        try:
            candidate = schedule_data(validate_schedule(candidate))
        except ScheduleError as exc:
            self.message = str(exc)
            return
        active = self.active
        before_index = self.index
        before_data = None
        if self.data is not None:
            try:
                before_data = schedule_data(validate_schedule(self.data))
            except ScheduleError:
                before_data = copy.deepcopy(self.data)
        changed = before_data is not None and candidate != before_data
        previous_undo = self.undo_state

        def make_undo(disk_sha256):
            if changed and undo_label is not None:
                return {
                    "data": before_data,
                    "disk_sha256": disk_sha256,
                    "label": undo_label,
                    "index": before_index,
                }
            return None

        def save():
            schedule = validate_schedule(candidate)
            if self.owner.demo:
                new_undo = make_undo(None) if changed else previous_undo
                message = "デモ: 変更は保存されません（Zで直前の変更を元に戻せます）。" if new_undo else "デモ: 変更は保存されません。"
                return {"data": candidate, "active": active, "message": message, "undo_state": new_undo}
            base = self.owner.base
            if base is None:
                raise ScheduleError("設定ファイルの場所を確認できません。")
            path = base / "schedule.json"
            with schedule_lock(path, exclusive=True):
                save_schedule(path, candidate)
                disk_sha256 = schedule_digest(path)
            if changed:
                new_undo = make_undo(disk_sha256)
            elif previous_undo is not None and disk_sha256 == previous_undo.get("disk_sha256"):
                new_undo = previous_undo
            else:
                new_undo = None
            synced = self._sync_agent(base, schedule) if active else True
            installed = agent_installed(base)
            undo_hint = "（Zで直前の変更を元に戻せます）" if new_undo else ""
            if active and not synced:
                message = f"保存済み。自動実行への反映に失敗。Iで停止→再開してください。{undo_hint}"
            elif active:
                message = f"予定を保存し、自動実行に反映しました。{undo_hint}"
            else:
                message = f"予定を保存しました。Iで自動実行を開始できます。{undo_hint}"
            return {"data": candidate, "active": installed, "message": message, "undo_state": new_undo}
        self.work(save)

    def undo(self):
        """直前に成功した予定変更を1段階だけ元に戻す。"""
        if self.undo_state is None:
            self.message = "元に戻せる直前の予定変更はありません。"
            return
        state = self.undo_state
        try:
            restored = schedule_data(validate_schedule(state["data"]))
        except ScheduleError as exc:
            self.undo_state = None
            self.message = f"直前の予定を復元できません（{exc}）。Rで再読込してください。"
            return
        active = self.active
        current_data = self.data
        current_index = self.index
        target_index = state.get("index", current_index)
        label = state.get("label", "直前の予定変更")

        def restore():
            if self.owner.demo:
                return {
                    "data": restored,
                    "active": active,
                    "message": f"デモ: {label}を元に戻しました（変更は保存されません）。",
                    "undo_state": None,
                    "index": target_index,
                }
            base = self.owner.base
            if base is None:
                raise ScheduleError("設定ファイルの場所を確認できません。")
            path = base / "schedule.json"
            expected_sha256 = state.get("disk_sha256")
            try:
                with schedule_lock(path, exclusive=True):
                    if path.is_symlink():
                        return {
                            "data": current_data,
                            "active": active,
                            "message": "schedule.json がシンボリックリンクのため元に戻せません。Rで再読込して確認してください。",
                            "undo_state": None,
                            "index": current_index,
                        }
                    current_sha256 = schedule_digest(path)
                    # 他プロセス・CLIによる更新や削除があれば、古い状態で上書きしない。
                    if expected_sha256 is None or current_sha256 != expected_sha256:
                        return {
                            "data": current_data,
                            "active": active,
                            "message": "他の画面またはCLIで予定が変更されたため元に戻せません。Rで再読込してください。",
                            "undo_state": None,
                            "index": current_index,
                        }
                    schedule = validate_schedule(restored)
                    save_schedule(path, restored)
            except (ScheduleError, OSError) as exc:
                return {
                    "data": current_data,
                    "active": active,
                    "message": f"元に戻せず現在の予定を維持しました。\nZで再試行またはRで再読込してください。({exc})",
                    "undo_state": state,
                    "index": current_index,
                }
            synced = self._sync_agent(base, schedule) if active else True
            installed = agent_installed(base)
            if active and not synced:
                message = "予定ファイルのみ保存済み。自動実行への反映に失敗しました。Iで停止→再開またはRで再読込してください。"
            elif active:
                message = f"{label}を元に戻し、自動実行に反映しました。"
            else:
                message = f"{label}を元に戻しました。"
            return {
                "data": restored,
                "active": installed,
                "message": message,
                "undo_state": None,
                "index": target_index,
            }
        self.work(restore)

    def toggle_agent(self):
        active, candidate = self.active, copy.deepcopy(self.data)

        def toggle():
            if self.owner.demo:
                return {"data": candidate, "active": not active, "message": "デモ: 自動実行の状態は保存されません。"}
            base = self.owner.base
            if base is None:
                raise ScheduleError("設定ファイルの場所を確認できません。")
            if active:
                remove_agent(base)
                return {"data": candidate, "active": False, "message": "自動実行を停止しました。"}
            config = load_config(base / "config.json")
            if not config["browser"]["headless"]:
                raise ScheduleError("自動実行には browser.headless を true にしてください。")
            if select_user(config) is None:
                raise ScheduleError("ユーザー管理で既定ユーザーを登録してください。")
            schedule = validate_schedule(candidate)
            # 予定ファイルが存在しない状態では開始しない。
            path = base / "schedule.json"
            with schedule_lock(path, exclusive=True):
                save_schedule(path, candidate)
            install_agent(base, schedule)
            return {"data": candidate, "active": True, "message": "自動実行を開始しました。GUIを閉じても動作します。"}
        self.work(toggle)

    def form(self, mode):
        self.mode, self.field_index, self.message = mode, 0, ""
        if mode == "period":
            self.fields = [self.data["active_from"], self.data["active_until"], ",".join(self.data["blackout_dates"])]
        elif mode == "edit":
            course = self.data["courses"][self.index]
            self.fields = [course["course_name"], course["weekday"], course["start_time"], course["room"], course.get("register_time", ""), course.get("register_until", "")]
        else:
            self.fields = [self.owner.values.get("授業名", ""), list(WEEKDAYS)[date.today().weekday()], self.owner.values.get("開始時刻", ""), self.owner.room, "", ""]
        self.editors = [TextCursor(value) for value in self.fields]
        self.initial_fields = tuple(self.fields)

    def discard_form(self):
        self.mode, self.fields, self.editors, self.message = "list", [], [], ""
        self.initial_fields = ()

    def request_back(self):
        if self.mode == "list":
            self.owner.modal = None
        elif self.mode in ("add", "edit", "period") and tuple(self.fields) != self.initial_fields:
            self.owner.save_prompt.open("自動出席予定の編集", save=self.submit, discard=self.discard_form)
        else:
            self.discard_form()

    def submit(self):
        candidate = copy.deepcopy(self.data)
        values = [value.strip() for value in self.fields]
        if self.mode == "period":
            candidate.update(active_from=values[0], active_until=values[1], blackout_dates=[s.strip() for s in values[2].replace("、", ",").split(",") if s.strip()])
            undo_label = "期間・休講日の変更"
        else:
            name, day, start, room, register, until = values
            day = next((key for key, label in DAYS.items() if day in (label, label + "曜", label + "曜日")), day.lower())
            course = {"course_name": name, "weekday": day, "start_time": start, "room": room, "enabled": True, "register_time": register, "register_until": until}
            if self.mode == "edit":
                course["enabled"] = candidate["courses"][self.index].get("enabled", True)
                candidate["courses"][self.index] = course
                undo_label = "授業の編集"
            else:
                candidate["courses"].append(course)
                undo_label = "授業の追加"
        self.persist(candidate, undo_label=undo_label)

    def handle(self, key):
        if self.owner.save_prompt.active:
            self.owner.save_prompt.handle(key)
            return
        enter = key in ("\n", "\r", curses.KEY_ENTER)
        if key == "\x1b" or (key in ("q", "Q") and self.mode not in ("add", "edit", "period")):
            self.request_back()
            return
        if self.data is None:
            if key in ("r", "R"):
                self.open(clear_undo=True)
            return
        courses = self.data["courses"]
        if self.mode == "delete":
            if enter:
                candidate = copy.deepcopy(self.data)
                del candidate["courses"][self.index]
                self.persist(candidate, undo_label="授業の削除")
            return
        if self.mode in ("add", "edit", "period"):
            if enter and self.field_index == len(self.fields) - 1:
                self.submit()
            elif enter or key in ("\t", curses.KEY_DOWN, curses.KEY_UP, curses.KEY_BTAB):
                self.field_index = (self.field_index + (-1 if key in (curses.KEY_UP, curses.KEY_BTAB) else 1)) % len(self.fields)
            else:
                if len(self.editors) != len(self.fields):
                    self.editors = [TextCursor(value) for value in self.fields]
                self.fields[self.field_index] = self.editors[self.field_index].edit(self.fields[self.field_index], key, limit=1000)
            return
        if key in (curses.KEY_UP, curses.KEY_DOWN) and courses:
            self.index = (self.index + (1 if key == curses.KEY_DOWN else -1)) % len(courses)
        elif key in ("a", "A"):
            self.form("add")
        elif (enter or key in ("e", "E")) and courses:
            self.form("edit")
        elif key in ("d", "D") and courses:
            self.mode, self.message = "delete", ""
        elif key in ("t", "T") and courses:
            candidate = copy.deepcopy(self.data)
            course = candidate["courses"][self.index]
            course["enabled"] = not course.get("enabled", True)
            self.persist(candidate, undo_label="授業の有効/無効切替")
        elif key in ("p", "P"):
            self.form("period")
        elif key in ("i", "I"):
            self.toggle_agent()
        elif key in ("z", "Z", "\x1a"):
            self.undo()
        elif key in ("r", "R"):
            self.open(clear_undo=True)

    def draw(self, canvas):
        width, height = min(canvas.width - 6, 88), min(canvas.height - 2, 26)
        left, top = (canvas.width - width) // 2, (canvas.height - height) // 2
        canvas.box(top, left, width, height, "自動出席の予定", color(1))

        def put(row, text, attr=0):
            canvas.put(top + row, left + 3, text, attr, width - 6)

        if self.data is None:
            put(3, "予定を読み込んでいます…" if self.owner.busy else "予定を読み込めませんでした。Rで再試行。")
        elif self.mode == "list":
            default = select_user(self.owner.config)
            put(1, f"自動実行: {'有効' if self.active else '停止中'} / ユーザー: {default['username'] if default else '未設定'}", curses.A_BOLD)
            put(2, f"期間 {self.data['active_from']} ～ {self.data['active_until']}")
            put(3, "授業開始→登録時刻 / Mac起動・ログイン中に動作", curses.A_DIM)
            count = height - 11
            first = max(0, self.index - count + 1)
            if not self.data["courses"]:
                put(5, "授業はありません。Aで追加してください。")
            for i, course in enumerate(self.data["courses"][first:first + count], first):
                timing = registration_time(course) + (f"～{course['register_until']}" if course.get("register_until") else "")
                text = f"{'●' if course.get('enabled', True) else '－'} {DAYS[course['weekday']]} {course['start_time']}→{timing} QR{course['room']} {course['course_name']}"
                put(5 + i - first, text, curses.A_REVERSE if i == self.index else 0)
            put(height - 6, "A 追加  Enter 編集  D 削除  T 有効/無効", color(1))
            put(height - 5, "P 期間・休講日  I 自動実行の開始/停止", color(1))
        elif self.mode == "delete":
            put(3, self.data["courses"][self.index]["course_name"], curses.A_BOLD)
            put(6, "この授業の自動出席予定を削除します。")
            put(8, "Enter: 削除する / Esc: 戻る", color(4))
        else:
            labels = ("開始日 YYYY-MM-DD", "終了日 YYYY-MM-DD", "休講日 YYYY-MM-DD（複数はカンマ区切り）") if self.mode == "period" else ("授業名（出席画面と一致）", "曜日 mon～sun または 月～日", "授業開始時刻 HH:MM", "QR番号（例: 642、731）", "登録時刻/範囲開始 HH:MM（空欄:開始5分後）", "範囲終了 HH:MM（空欄:固定時刻）")
            for i, label in enumerate(labels):
                row = 2 + i * (2 if len(labels) == 6 else 3)
                put(row, label, curses.A_DIM)
                if len(self.editors) != len(self.fields):
                    self.editors = [TextCursor(value) for value in self.fields]
                self.editors[i].draw(canvas, top + row + 1, left + 3, self.fields[i], width - 6, active=i == self.field_index)
            put(height - 5, "Tab/↑↓:項目 ←→:編集 最後でEnter:保存", color(1))
        # 長いエラーでも2行で読み取れるようにする。
        from .terminal_layout import wrap_text
        for offset, line in enumerate(wrap_text(self.message, width - 6)[:2]):
            put(height - 4 + offset, line, color(4))
        footer = "Esc/Q: 戻る  R: 再読込" if self.mode == "list" else "Esc: 戻る・保存確認  Ctrl+U: 全消去"
        if self.mode == "list" and self.data is not None:
            footer += "  " + ("Z: 直前の変更を元に戻す" if self.undo_state else "(Z 元に戻す: 不可)")
        put(height - 2, footer, curses.A_DIM)
