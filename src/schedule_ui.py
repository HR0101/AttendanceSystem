"""授業ごとの自動出席予定を編集するターミナル画面。"""

import copy
import curses
import queue
import threading
from datetime import date
from contextlib import nullcontext

from .config import ConfigurationError, load_config, private_file_lock
from .scheduler import (
    WEEKDAYS, ScheduleError, agent_installed, calendar_intervals, install_agent,
    load_schedule, next_scheduled_run, registration_time, remove_agent, save_schedule,
    schedule_data, update_schedule, validate_schedule,
)
from .terminal_layout import color, fit_text, text_width, wrap_text
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
        self.confirm_selection = 0
        self.start_preview = None
        self.message = ""
        self.events = queue.Queue()

    def _reset_start_confirmation(self):
        self.confirm_selection = 0
        self.start_preview = None

    def work(self, operation):
        self.owner.busy = True
        self.message = "処理中…"

        def run():
            try:
                result = operation()
                self.events.put((result, None))
            except Exception as exc:
                # launchctlやファイル操作の失敗も画面に残し、再操作できるようにする。
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
            preview = result[3] if len(result) > 3 else None
            self.data, self.active, self.message = result[:3]
            self.fields = []
            self.initial_fields = ()
            self.editors = []
            self.index = min(self.index, max(0, len(self.data["courses"]) - 1))
            if preview is None:
                self.mode = "list"
                self._reset_start_confirmation()
            else:
                self.mode = "confirm_start"
                self.start_preview = preview
                self.confirm_selection = 0

    def open(self):
        if hasattr(self.owner, "expire_password"):
            self.owner.expire_password()
        self.owner.modal = "schedule"
        self.mode = "list"
        self.fields = []
        self.initial_fields = ()
        self.editors = []
        self._reset_start_confirmation()
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

    def _build_start_preview(self, candidate):
        """自動実行開始前の前提条件と次回実行予定を検証してまとめる。"""
        if self.owner.demo:
            config = self.owner.config
        else:
            base = self.owner.base
            if base is None:
                raise ScheduleError("設定ファイルの場所を確認できません。")
            config = load_config(base / "config.json")
            if not config["browser"]["headless"]:
                raise ScheduleError("自動実行には browser.headless を true にしてください。")
        user = select_user(config)
        if user is None:
            raise ScheduleError("ユーザー管理で既定ユーザーを登録してください。")
        schedule = validate_schedule(candidate)
        enabled_courses = [course for course in schedule["courses"] if course.get("enabled", True)]
        if not enabled_courses:
            raise ScheduleError("有効な授業を設定してから自動実行を開始してください。")
        if date.today() > schedule["active_until"]:
            raise ScheduleError("設定した授業期間は終了しています。")
        next_run = next_scheduled_run(schedule)
        if next_run is None:
            raise ScheduleError("期間内に実行できる予定がありません。休講日や授業期間を確認してください。")
        next_at, next_course = next_run
        return {
            "user": user,
            "next_at": next_at,
            "next_course": next_course,
            "active_from": schedule["active_from"].isoformat(),
            "active_until": schedule["active_until"].isoformat(),
            "enabled_count": len(enabled_courses),
            "blackout_count": len(schedule["blackout_dates"]),
            "schedule": schedule,
        }

    def prepare_start(self):
        """自動実行を開始する前に確認画面を開く。"""
        candidate = copy.deepcopy(self.data)
        try:
            preview = self._build_start_preview(candidate)
        except (ScheduleError, ConfigurationError, OSError) as exc:
            self.message = str(exc)
            return
        self.start_preview = preview
        self.confirm_selection = 0
        self.mode = "confirm_start"
        self.message = ""

    @staticmethod
    def _start_preview_changed(expected_preview, preview):
        """確認画面で表示した対象ユーザーや次回実行条件が変わったか判定する。"""
        if expected_preview is None or preview is None:
            return expected_preview is not preview

        def summary(item):
            user = item["user"]
            course = item["next_course"]
            return (
                user.get("username"),
                user.get("student_id"),
                item["next_at"],
                course["weekday"],
                course["start_time"],
                course["room"],
                course["course_name"],
                course.get("register_time") or "",
                course.get("register_until") or "",
                item["active_from"],
                item["active_until"],
                item["enabled_count"],
                item["blackout_count"],
            )

        return summary(expected_preview) != summary(preview)

    def start_agent(self):
        """確認後に予定を保存して LaunchAgent を開始する。"""
        expected_preview = copy.deepcopy(self.start_preview)
        candidate = copy.deepcopy(self.data)
        self.mode = "list"
        self._reset_start_confirmation()

        def start():
            base = self.owner.base
            lock = nullcontext() if self.owner.demo else private_file_lock(base / "schedule.json")
            with lock:
                current = candidate
                if not self.owner.demo and (base / "schedule.json").exists():
                    current = schedule_data(load_schedule(base / "schedule.json"))
                preview = self._build_start_preview(current)
                if expected_preview is not None and self._start_preview_changed(expected_preview, preview):
                    return (current, False,
                            "確認中に既定ユーザーまたは実行条件が変更されました。新しい内容を確認してください。",
                            preview)
                if self.owner.demo:
                    return current, True, "デモ: 自動実行の状態は保存されません。"
                save_schedule(base / "schedule.json", current)
                install_agent(base, preview["schedule"])
                return current, True, "自動実行を開始しました。GUIを閉じても動作します。"
        self.work(start)

    def toggle_agent(self):
        if not self.active:
            self.prepare_start()
            return
        candidate = copy.deepcopy(self.data)

        def stop():
            if self.owner.demo:
                return candidate, False, "デモ: 自動実行の状態は保存されません。"
            remove_agent(self.owner.base)
            return candidate, False, "自動実行を停止しました。"
        self.work(stop)

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
        self._reset_start_confirmation()

    def _cancel_start(self):
        self.mode = "list"
        self._reset_start_confirmation()
        self.message = "自動実行の開始をキャンセルしました。"

    def request_back(self):
        if self.mode == "list":
            self.owner.modal = None
        elif self.mode == "confirm_start":
            self._cancel_start()
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
            elif key in ("l", "L"):
                self.owner.open_logs(parent="schedule")
            return
        courses = self.data["courses"]
        if self.mode == "confirm_start":
            if key in ("\t", curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_BTAB):
                self.confirm_selection = 1 - self.confirm_selection
            elif enter:
                if self.confirm_selection == 1:
                    self.start_agent()
                else:
                    self._cancel_start()
            return
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
        elif key in ("l", "L"):
            self.owner.open_logs(parent="schedule")
        elif key in ("r", "R"):
            self.open()

    def _draw_confirm_start(self, canvas, top, left, width, height):
        inner = width - 6

        def put(row, text, attr=0):
            canvas.put(top + row, left + 3, text, attr, inner)

        preview = self.start_preview
        user = preview["user"]
        suffix = f" ({user['student_id']})"
        prefix = "既定ユーザー: "
        available = max(0, inner - text_width(prefix) - text_width(suffix))
        name = user["username"]
        if text_width(name) > available:
            name = fit_text(name, max(0, available - 1)) + ("…" if available >= 1 else "")
        next_at = preview["next_at"]
        course = preview["next_course"]
        weekday_label = "月火水木金土日"[next_at.weekday()]
        put(1, "自動実行を開始しますか？", curses.A_BOLD | color(4))
        put(2, "GUIを閉じても既定ユーザーで実行します。", curses.A_DIM)
        put(4, f"{prefix}{name}{suffix}", curses.A_BOLD)
        put(5, f"次回実行日時: {next_at:%Y-%m-%d} ({weekday_label}) {next_at:%H:%M}", curses.A_BOLD)
        course_lines = wrap_text(f"次回の授業: QR{course['room']} {course['course_name']}", inner)[:2]
        for offset, line in enumerate(course_lines):
            put(6 + offset, line)
        row = 6 + len(course_lines)
        put(row, f"適用期間: {preview['active_from']} ～ {preview['active_until']}")
        blackout = f"（休講日 {preview['blackout_count']}日）" if preview["blackout_count"] else ""
        put(row + 1, f"有効な予定: {preview['enabled_count']}件{blackout}")
        button_row = min(row + 3, height - 6)
        canvas.button(top + button_row, left + 3, "[ キャンセル ]", self.confirm_selection == 0)
        canvas.button(top + button_row, left + 22, "[ 開始する ]", self.confirm_selection == 1)
        put(button_row + 2, "確定するまで予定や自動実行は変更されません。", curses.A_DIM)

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
            put(height - 5, "P 期間・休講日  I 自動実行の開始/停止  L ログ", color(1))
        elif self.mode == "confirm_start" and self.start_preview:
            self._draw_confirm_start(canvas, top, left, width, height)
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
        for offset, line in enumerate(wrap_text(self.message, width - 6)[:2]):
            put(height - 4 + offset, line, color(4))
        footer = (
            "Esc/Q: 戻る  R: 再読込  L: ログ" if self.mode == "list"
            else "Tab/←→: 選択  Enter: 決定  Esc/Q: キャンセル" if self.mode == "confirm_start"
            else "Esc: 戻る・保存確認  Ctrl+U: 全消去"
        )
        put(height - 2, footer, curses.A_DIM)
