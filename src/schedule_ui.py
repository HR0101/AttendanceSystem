"""授業ごとの自動出席予定を編集するターミナル画面。"""

import copy
import curses
import queue
import threading
from datetime import date

from .config import load_config
from .room import RoomError, parse_room
from .scheduler import (
    WEEKDAYS, ScheduleError, agent_installed, calendar_intervals, install_agent,
    load_schedule, registration_time, remove_agent, save_schedule, schedule_data, valid_time, validate_schedule,
)
from .terminal_layout import color
from .text_input import TextCursor
from .users import select_user


DAYS = dict(zip(WEEKDAYS, "月火水木金土日"))


def _parse_iso_date(value: str) -> date | None:
    """厳密な YYYY-MM-DD 形式の日付を検証して返す。"""
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        return None
    if not (value[:4].isascii() and value[:4].isdecimal()
            and value[5:7].isascii() and value[5:7].isdecimal()
            and value[8:10].isascii() and value[8:10].isdecimal()):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _normalize_weekday(value: str) -> str | None:
    """曜日入力（mon～sun または 月～日）を内部キーへ正規化する。"""
    day = value.strip()
    if not day:
        return None
    normalized = next((key for key, label in DAYS.items() if day in (label, label + "曜", label + "曜日")), day.lower())
    return normalized if normalized in WEEKDAYS else None


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
        else:
            self.data, self.active, self.message = result
            self.mode = "list"
            self.fields = []
            self.initial_fields = ()
            self.editors = []
            self.index = min(self.index, max(0, len(self.data["courses"]) - 1))

    def open(self):
        self.owner.modal = "schedule"
        self.mode = "list"
        self.fields = []
        self.initial_fields = ()
        self.editors = []
        self.data = None

        def read():
            base = self.owner.base
            if self.owner.demo:
                data = schedule_data(load_schedule(base / "schedule.json")) if base and (base / "schedule.json").exists() else self.empty_data()
                return data, False, "デモ: 変更は保存されません。"
            if base is None:
                raise ScheduleError("設定ファイルの場所を確認できません。")
            data = schedule_data(load_schedule(base / "schedule.json")) if (base / "schedule.json").exists() else self.empty_data()
            return data, agent_installed(base), "Aで授業を追加できます。"
        self.work(read)

    @staticmethod
    def empty_data():
        today = date.today()
        return {"active_from": today.isoformat(), "active_until": date(today.year, 12, 31).isoformat(), "blackout_dates": [], "courses": []}

    def persist(self, candidate):
        # 入力エラーはフォームを閉じずに表示する。
        try:
            candidate = schedule_data(validate_schedule(candidate))
        except ScheduleError as exc:
            self.message = str(exc)
            return
        active = self.active

        def save():
            schedule = validate_schedule(candidate)
            if self.owner.demo:
                return candidate, active, "デモ: 変更は保存されません。"
            base = self.owner.base
            save_schedule(base / "schedule.json", candidate)
            if active:
                try:
                    if calendar_intervals(schedule["courses"]):
                        install_agent(base, schedule)
                    else:
                        remove_agent(base)
                except (ScheduleError, OSError):
                    return candidate, agent_installed(base), "保存済み。自動実行への反映に失敗。Iで停止→再開してください。"
            return candidate, agent_installed(base), "予定を保存し、自動実行に反映しました。" if active else "予定を保存しました。Iで自動実行を開始できます。"
        self.work(save)

    def toggle_agent(self):
        active, candidate = self.active, copy.deepcopy(self.data)

        def toggle():
            if self.owner.demo:
                return candidate, not active, "デモ: 自動実行の状態は保存されません。"
            base = self.owner.base
            if active:
                remove_agent(base)
                return candidate, False, "自動実行を停止しました。"
            config = load_config(base / "config.json")
            if not config["browser"]["headless"]:
                raise ScheduleError("自動実行には browser.headless を true にしてください。")
            if select_user(config) is None:
                raise ScheduleError("ユーザー管理で既定ユーザーを登録してください。")
            schedule = validate_schedule(candidate)
            # 予定ファイルが存在しない状態では開始しない。
            save_schedule(base / "schedule.json", candidate)
            install_agent(base, schedule)
            return candidate, True, "自動実行を開始しました。GUIを閉じても動作します。"
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

    def _validate_period_field(self, index, *, allow_empty=False):
        raw = self.fields[index]
        stripped = raw.strip()
        if index == 0:
            if not stripped:
                return None if (allow_empty and raw == "") else "開始日は半角の YYYY-MM-DD 形式で入力してください（例: 2026-09-18）。"
            if _parse_iso_date(stripped) is None:
                return "開始日は半角の YYYY-MM-DD 形式で入力してください（例: 2026-09-18）。"
            return None
        if index == 1:
            if not stripped:
                return None if (allow_empty and raw == "") else "終了日は半角の YYYY-MM-DD 形式で入力してください（例: 2026-12-21）。"
            end_date = _parse_iso_date(stripped)
            if end_date is None:
                return "終了日は半角の YYYY-MM-DD 形式で入力してください（例: 2026-12-21）。"
            start_date = _parse_iso_date(self.fields[0].strip())
            if start_date is not None and start_date > end_date:
                return "終了日は開始日以降の日付（YYYY-MM-DD）にしてください。"
            return None
        if index == 2:
            tokens = [s.strip() for s in raw.replace("、", ",").split(",") if s.strip()]
            if any(_parse_iso_date(token) is None for token in tokens):
                return "休講日は半角の YYYY-MM-DD 形式で入力してください（複数はカンマ区切り）。"
        return None

    def _validate_course_field(self, index, *, allow_empty=False):
        raw = self.fields[index]
        stripped = raw.strip()
        if index == 0:
            if not stripped:
                return None if (allow_empty and raw == "") else "出席画面と一致する授業名を入力してください。"
            return None
        if index == 1:
            if not stripped:
                return None if (allow_empty and raw == "") else "曜日は mon～sun または 月～日 で入力してください。"
            if _normalize_weekday(stripped) is None:
                return "曜日は半角の mon～sun または 月～日 で入力してください。"
            return None
        if index == 2:
            if not stripped:
                return None if (allow_empty and raw == "") else "授業開始時刻は HH:MM 形式で入力してください（例: 14:00）。"
            if not valid_time(stripped):
                return "授業開始時刻は半角の HH:MM 形式で入力してください（例: 14:00）。"
            day = _normalize_weekday(self.fields[1])
            courses = (self.data or {}).get("courses", [])
            enabled = True if self.mode == "add" else courses[self.index].get("enabled", True)
            if day and enabled:
                for i, course in enumerate(courses):
                    if self.mode == "edit" and i == self.index:
                        continue
                    if course.get("enabled", True) and course.get("weekday") == day and course.get("start_time") == stripped:
                        return "同じ曜日・開始時刻に複数の授業は設定できません。"
            return None
        if index == 3:
            if not stripped:
                return None if (allow_empty and raw == "") else "QR番号は半角数字3桁で入力してください（例: 642、731）。"
            try:
                parse_room(stripped)
            except RoomError:
                return "QR番号は半角数字3桁で入力してください（例: 642、731）。"
            return None
        if index == 4:
            if stripped:
                if not valid_time(stripped):
                    return "登録時刻/範囲開始は半角の HH:MM 形式で入力してください（空欄可）。"
                return None
            # 範囲終了だけが正しいHH:MMで入っている場合は、保存時に開始時刻欄へ案内する。
            if not allow_empty and valid_time(self.fields[5].strip()):
                return "範囲終了を指定する場合は、登録時刻/範囲開始（HH:MM）も入力してください。"
            return None
        if index == 5 and stripped:
            if not valid_time(stripped):
                return "範囲終了は登録時刻以降の半角 HH:MM 形式で入力してください（日付またぎ不可）。"
            reg_time = self.fields[4].strip()
            if not reg_time:
                return "範囲終了を指定する場合は、登録時刻/範囲開始（HH:MM）も入力してください。"
            if valid_time(reg_time) and stripped < reg_time:
                return "範囲終了は登録時刻以降の半角 HH:MM を指定してください（日付またぎ不可）。"
        return None

    def validate_field(self, index, *, allow_empty=False):
        """現在の予定フォームで指定項目の入力を検証し、エラー文を返す。"""
        if self.mode == "period":
            return self._validate_period_field(index, allow_empty=allow_empty)
        if self.mode in ("add", "edit"):
            return self._validate_course_field(index, allow_empty=allow_empty)
        return None

    def first_invalid_field(self):
        """項目順に入力を検証し、最初の (項目番号, エラー文) を返す。"""
        for index in range(len(self.fields)):
            error = self.validate_field(index, allow_empty=False)
            if error:
                return index, error
        return None

    def submit(self):
        invalid = self.first_invalid_field()
        if invalid is not None:
            self.field_index, self.message = invalid
            return
        candidate = copy.deepcopy(self.data)
        values = [value.strip() for value in self.fields]
        if self.mode == "period":
            candidate.update(active_from=values[0], active_until=values[1], blackout_dates=[s.strip() for s in values[2].replace("、", ",").split(",") if s.strip()])
        else:
            name, day, start, room, register, until = values
            day = _normalize_weekday(day)
            course = {"course_name": name, "weekday": day, "start_time": start, "room": room, "enabled": True, "register_time": register, "register_until": until}
            if self.mode == "edit":
                course["enabled"] = candidate["courses"][self.index].get("enabled", True)
                candidate["courses"][self.index] = course
            else:
                candidate["courses"].append(course)
        self.persist(candidate)

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
                self.open()
            return
        courses = self.data["courses"]
        if self.mode == "delete":
            if enter:
                candidate = copy.deepcopy(self.data)
                del candidate["courses"][self.index]
                self.persist(candidate)
            return
        if self.mode in ("add", "edit", "period"):
            if enter and self.field_index == len(self.fields) - 1:
                self.submit()
            elif enter or key in ("\t", curses.KEY_DOWN, curses.KEY_UP, curses.KEY_BTAB):
                leaving = self.field_index
                self.field_index = (self.field_index + (-1 if key in (curses.KEY_UP, curses.KEY_BTAB) else 1)) % len(self.fields)
                self.message = self.validate_field(leaving, allow_empty=True) or ""
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
            self.persist(candidate)
        elif key in ("p", "P"):
            self.form("period")
        elif key in ("i", "I"):
            self.toggle_agent()
        elif key in ("r", "R"):
            self.open()

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
        put(height - 2, "Esc/Q: 戻る  R: 再読込" if self.mode == "list" else "Esc: 戻る・保存確認  Ctrl+U: 全消去", curses.A_DIM)
