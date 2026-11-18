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
    update_schedule, validate_schedule,
)
from .terminal_layout import color
from .text_input import TextCursor
from .users import select_user


DAYS = dict(zip(WEEKDAYS, "月火水木金土日"))


def _course_snapshot(course: dict) -> tuple:
    return (
        course["weekday"],
        course["start_time"],
        course["room"],
        course["course_name"].strip(),
        course.get("enabled", True),
        course.get("register_time", ""),
        course.get("register_until", ""),
    )


def _find_course_index(courses: list[dict], target: dict, hint_index: int) -> int:
    key = _course_snapshot(target)
    if 0 <= hint_index < len(courses) and _course_snapshot(courses[hint_index]) == key:
        return hint_index
    for index, item in enumerate(courses):
        if _course_snapshot(item) == key:
            return index
    raise ScheduleError("対象の授業は他の操作で変更または削除されています。Rで再読込してください。")


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
        if hasattr(self.owner, "expire_password"):
            self.owner.expire_password()
        self.owner.modal = "schedule"
        self.mode = "list"
        self.fields = []
        self.initial_fields = ()
        self.editors = []
        self.data = None

        def read():
            base = self.owner.base
            schedule_path = base / "schedule.json" if base is not None else None
            has_file = schedule_path is not None and (schedule_path.is_symlink() or schedule_path.exists())
            if self.owner.demo:
                data = schedule_data(load_schedule(schedule_path)) if has_file else self.empty_data()
                return data, False, "デモ: 変更は保存されません。"
            if base is None:
                raise ScheduleError("設定ファイルの場所を確認できません。")
            data = schedule_data(load_schedule(schedule_path)) if has_file else self.empty_data()
            return data, agent_installed(base), "Aで授業を追加できます。"
        self.work(read)

    @staticmethod
    def empty_data():
        today = date.today()
        return {"active_from": today.isoformat(), "active_until": date(today.year, 12, 31).isoformat(), "blackout_dates": [], "courses": []}

    def persist(self, candidate, mutator=None, default_raw=None):
        # 入力エラーはフォームを閉じずに表示する。
        try:
            candidate = schedule_data(validate_schedule(candidate))
        except ScheduleError as exc:
            self.message = str(exc)
            return
        active = self.active

        def save():
            if self.owner.demo:
                return candidate, active, "デモ: 変更は保存されません。"
            base = self.owner.base
            path = base / "schedule.json"
            if mutator is None:
                schedule = save_schedule(path, candidate)
            else:
                schedule = update_schedule(path, mutator, default_raw=default_raw, writer=save_schedule)
            saved = schedule_data(schedule)
            if active:
                try:
                    if calendar_intervals(schedule["courses"]):
                        install_agent(base, schedule)
                    else:
                        remove_agent(base)
                except (ScheduleError, OSError):
                    return saved, agent_installed(base), "保存済み。自動実行への反映に失敗。Iで停止→再開してください。"
            return saved, agent_installed(base), "予定を保存し、自動実行に反映しました。" if active else "予定を保存しました。Iで自動実行を開始できます。"
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
            # 予定ファイルが存在しない状態では開始しない。既存の更新も上書きしない。
            schedule = update_schedule(base / "schedule.json", lambda current: current, default_raw=candidate, writer=save_schedule)
            install_agent(base, schedule)
            return schedule_data(schedule), True, "自動実行を開始しました。GUIを閉じても動作します。"
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
        base_data = copy.deepcopy(self.data)
        candidate = copy.deepcopy(base_data)
        values = [value.strip() for value in self.fields]
        if self.mode == "period":
            blackouts = [s.strip() for s in values[2].replace("、", ",").split(",") if s.strip()]
            candidate.update(active_from=values[0], active_until=values[1], blackout_dates=blackouts)

            def mutator(current):
                current.update(active_from=values[0], active_until=values[1], blackout_dates=list(blackouts))
                return current
        else:
            name, day, start, room, register, until = values
            day = next((key for key, label in DAYS.items() if day in (label, label + "曜", label + "曜日")), day.lower())
            course = {"course_name": name, "weekday": day, "start_time": start, "room": room, "enabled": True, "register_time": register, "register_until": until}
            if self.mode == "edit":
                index = self.index
                before_course = copy.deepcopy(base_data["courses"][index])
                course["enabled"] = before_course.get("enabled", True)
                candidate["courses"][index] = course

                def mutator(current):
                    idx = _find_course_index(current["courses"], before_course, index)
                    edited = dict(course, enabled=current["courses"][idx].get("enabled", True))
                    current["courses"][idx] = edited
                    return current
            else:
                candidate["courses"].append(course)

                def mutator(current):
                    current["courses"].append(copy.deepcopy(course))
                    return current
        self.persist(candidate, mutator=mutator, default_raw=base_data)

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
                base_data = copy.deepcopy(self.data)
                index = self.index
                before_course = copy.deepcopy(base_data["courses"][index])
                candidate = copy.deepcopy(base_data)
                del candidate["courses"][index]

                def mutator(current):
                    idx = _find_course_index(current["courses"], before_course, index)
                    del current["courses"][idx]
                    return current
                self.persist(candidate, mutator=mutator, default_raw=base_data)
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
            base_data = copy.deepcopy(self.data)
            index = self.index
            before_course = copy.deepcopy(base_data["courses"][index])
            candidate = copy.deepcopy(base_data)
            course = candidate["courses"][index]
            course["enabled"] = not course.get("enabled", True)

            def mutator(current):
                idx = _find_course_index(current["courses"], before_course, index)
                current["courses"][idx]["enabled"] = not current["courses"][idx].get("enabled", True)
                return current
            self.persist(candidate, mutator=mutator, default_raw=base_data)
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
            labels = ("開始日 YYYY-MM-DD", "終了日 YYYY-MM-DD", "休講日 YYYY-MM-DD（複数はカンマ区切り）") if self.mode == "period" else ("授業名（出席画面と一致）", "曜日 mon～sun または 月～日", "授業開始時刻 HH:MM", "QR番号（例: 642、731、7301）", "登録時刻/範囲開始 HH:MM（空欄:開始5分後）", "範囲終了 HH:MM（空欄:固定時刻）")
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
