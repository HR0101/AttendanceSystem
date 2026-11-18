"""授業ごとの自動出席予定を編集するターミナル画面。"""

import copy
import curses
import queue
import threading
from datetime import date
from contextlib import nullcontext

from .config import ConfigurationError, load_config, private_file_lock
from .room import RoomError, parse_room
from .scheduler import (
    WEEKDAYS, ScheduleError, agent_installed, calendar_intervals, install_agent,
    load_schedule, next_scheduled_run, registration_time, remove_agent, save_schedule,
    schedule_data, valid_time, update_schedule, validate_schedule,
    schedule_digest, schedule_lock,
)
from .terminal_layout import prefix_text, fit_text, text_width, wrap_text
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
        self.confirm_selection = 0
        self.start_preview = None
        self.message = ""
        self.undo_state = None
        self.message_level = "info"
        self.events = queue.Queue()

    def _reset_start_confirmation(self):
        self.confirm_selection = 0
        self.start_preview = None

    def work(self, operation):
        self.owner.busy = True
        self.message = "処理中…"
        self.message_level = "info"

        def run():
            try:
                result = operation()
                self.events.put((result, None))
            except Exception as exc:
                # launchctlやファイル操作の失敗も画面に残し、再操作できるようにする。
                from .config import ConfigurationError
                message = (str(exc) or "予定を処理できませんでした。") if isinstance(exc, (ScheduleError, ConfigurationError, OSError)) else "予定を処理できませんでした。"
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
            self.message_level = "error"
            if self.data is None:
                self.undo_state = None
            return
        if not isinstance(result, dict):
            result = {"data": result[0], "active": result[1], "message": result[2],
                      "preview": result[3] if len(result) > 3 else None}
        self.data, self.active, self.message = result["data"], result["active"], result["message"]
        if "undo_state" in result:
            self.undo_state = result["undo_state"]
        self.fields, self.initial_fields, self.editors = [], (), []
        self._reset_start_confirmation()
        self.index = min(result.get("index", self.index), max(0, len(self.data["courses"]) - 1))
        self.start_preview = result.get("preview")
        self.mode = "confirm_start" if self.start_preview else "list"
        self.message_level = "warning" if ("反映に失敗" in self.message or "反映に失敗しました" in self.message) else "info"

    def open(self, *, clear_undo=False):
        if hasattr(self.owner, "expire_password"):
            self.owner.expire_password()
        self._reset_start_confirmation()
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
        restore_active = bool(state.get("restore_active"))
        should_sync = active or restore_active
        current_data = self.data
        current_index = self.index
        target_index = state.get("index", current_index)
        label = state.get("label", "直前の予定変更")

        def restore():
            if self.owner.demo:
                restored_active = bool(calendar_intervals(restored["courses"])) if should_sync else False
                if should_sync and not active and restored_active:
                    message = f"デモ: {label}を元に戻し、自動実行を再開しました（変更は保存されません）。"
                else:
                    message = f"デモ: {label}を元に戻しました（変更は保存されません）。"
                return {
                    "data": restored,
                    "active": restored_active,
                    "message": message,
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
            synced = self._sync_agent(base, schedule) if should_sync else True
            installed = agent_installed(base)
            if should_sync and not synced:
                action = "Iで停止→再開" if active else "Iで開始"
                message = f"予定ファイルのみ保存済み。自動実行への反映に失敗しました。{action}またはRで再読込してください。"
            elif should_sync and not active and installed:
                message = f"{label}を元に戻し、自動実行を再開しました。"
            elif should_sync:
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

    def persist(self, candidate, mutator=None, default_raw=None, *, undo_label="予定の変更"):
        # 入力エラーはフォームを閉じずに表示する。
        try:
            candidate = schedule_data(validate_schedule(candidate))
        except ScheduleError as exc:
            self.message = str(exc)
            self.message_level = "error"
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

        def make_undo(disk_sha256, *, restore_active=False):
            if changed and undo_label is not None:
                return {
                    "data": before_data,
                    "disk_sha256": disk_sha256,
                    "label": undo_label,
                    "index": before_index,
                    "restore_active": restore_active,
                }
            return None

        def save():
            nonlocal before_data, changed, candidate
            schedule = validate_schedule(candidate)
            has_enabled = bool(calendar_intervals(schedule["courses"]))
            if self.owner.demo:
                demo_active = bool(active and has_enabled)
                restore_active = bool(active and not has_enabled)
                new_undo = make_undo(None, restore_active=restore_active) if changed else previous_undo
                message = "デモ: 変更は保存されません（Zで直前の変更を元に戻せます）。" if new_undo else "デモ: 変更は保存されません。"
                return {"data": candidate, "active": demo_active, "message": message, "undo_state": new_undo}
            base = self.owner.base
            if base is None:
                raise ScheduleError("設定ファイルの場所を確認できません。")
            path = base / "schedule.json"
            with schedule_lock(path, exclusive=True):
                before_data = schedule_data(load_schedule(path)) if path.exists() else copy.deepcopy(default_raw or self.data)
                if mutator is None:
                    schedule = save_schedule(path, candidate)
                else:
                    schedule = update_schedule(path, mutator, default_raw=default_raw, writer=save_schedule)
                candidate = schedule_data(schedule)
                has_enabled = bool(calendar_intervals(schedule["courses"]))
                changed = before_data is not None and candidate != before_data
                disk_sha256 = schedule_digest(path)
            synced = self._sync_agent(base, schedule) if active else True
            installed = agent_installed(base)
            restore_active = bool(active and synced and not installed and not has_enabled)
            if changed:
                new_undo = make_undo(disk_sha256, restore_active=restore_active)
            elif previous_undo is not None and disk_sha256 == previous_undo.get("disk_sha256"):
                new_undo = previous_undo
            else:
                new_undo = None
            undo_hint = "（Zで直前の変更を元に戻せます）" if new_undo else ""
            if active and not synced:
                message = f"保存済み。自動実行への反映に失敗。Iで停止→再開してください。{undo_hint}"
            elif active:
                message = f"予定を保存し、自動実行に反映しました。{undo_hint}"
            else:
                message = f"予定を保存しました。Iで自動実行を開始できます。{undo_hint}"
            return {"data": candidate, "active": installed, "message": message, "undo_state": new_undo}
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
                    return {"data": current, "active": True, "message": "デモ: 自動実行の状態は保存されません。",
                            "undo_state": {**self.undo_state, "restore_active": False} if self.undo_state else None}
                save_schedule(base / "schedule.json", current)
                install_agent(base, preview["schedule"])
                return {"data": current, "active": True, "message": "自動実行を開始しました。GUIを閉じても動作します。",
                        "undo_state": {**self.undo_state, "restore_active": False} if self.undo_state else None}
        self.work(start)

    def toggle_agent(self):
        if not self.active:
            self.prepare_start()
            return
        candidate = copy.deepcopy(self.data)

        cleared_undo = {**self.undo_state, "restore_active": False} if self.undo_state else None

        def stop():
            if self.owner.demo:
                return {"data": candidate, "active": False, "message": "デモ: 自動実行の状態は保存されません。", "undo_state": cleared_undo}
            remove_agent(self.owner.base)
            return {"data": candidate, "active": False, "message": "自動実行を停止しました。", "undo_state": cleared_undo}
        self.work(stop)

    def form(self, mode):
        self.mode, self.field_index, self.message, self.message_level = mode, 0, "", "info"
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
        self.mode, self.fields, self.editors, self.message, self.message_level = "list", [], [], "", "info"
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
        base_data = copy.deepcopy(self.data)
        candidate = copy.deepcopy(base_data)
        values = [value.strip() for value in self.fields]
        if self.mode == "period":
            blackouts = [s.strip() for s in values[2].replace("、", ",").split(",") if s.strip()]
            candidate.update(active_from=values[0], active_until=values[1], blackout_dates=blackouts)

            def mutator(current):
                current.update(active_from=values[0], active_until=values[1], blackout_dates=list(blackouts))
                return current
            undo_label = "期間・休講日の変更"
        else:
            name, day, start, room, register, until = values
            day = _normalize_weekday(day)
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
                undo_label = "授業の編集"
            else:
                candidate["courses"].append(course)

                def mutator(current):
                    current["courses"].append(copy.deepcopy(course))
                    return current
                undo_label = "授業の追加"
        self.persist(candidate, mutator=mutator, default_raw=base_data, undo_label=undo_label)

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
            if key in ("\t", curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_BTAB):
                self.confirm_selection = 1 - self.confirm_selection
            elif enter:
                if self.confirm_selection == 0:
                    self.discard_form()
                    return
                base_data = copy.deepcopy(self.data)
                index = self.index
                before_course = copy.deepcopy(base_data["courses"][index])
                candidate = copy.deepcopy(base_data)
                del candidate["courses"][index]

                def mutator(current):
                    idx = _find_course_index(current["courses"], before_course, index)
                    del current["courses"][idx]
                    return current
                self.persist(candidate, mutator=mutator, default_raw=base_data, undo_label="授業の削除")
            return
        if self.mode in ("add", "edit", "period"):
            if key == "\x13" or (enter and self.field_index == len(self.fields) - 1):
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
            self.mode, self.confirm_selection, self.message = "delete", 0, ""
            self.message_level = "info"
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
            self.persist(candidate, mutator=mutator, default_raw=base_data, undo_label="授業の有効/無効切替")
        elif key in ("p", "P"):
            self.form("period")
        elif key in ("i", "I"):
            self.toggle_agent()
        elif key in ("l", "L"):
            self.owner.open_logs(parent="schedule")
        elif key in ("z", "Z", "\x1a"):
            self.undo()
        elif key in ("r", "R"):
            self.open(clear_undo=True)

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
        put(1, "自動実行を開始しますか？", curses.A_BOLD | canvas.color(4))
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
        canvas.box(top, left, width, height, "自動出席の予定", canvas.color(1))

        def put(row, text, attr=0):
            canvas.put(top + row, left + 3, text, attr, width - 6)

        if self.data is None:
            if self.owner.busy:
                put(3, "予定を読み込んでいます…")
            else:
                put(3, prefix_text("error", "予定を読み込めませんでした。Rで再試行。"), canvas.color(3) | curses.A_BOLD)
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
            put(height - 6, "A 追加  Enter 編集  D 削除  T 有効/無効", canvas.color(1))
            put(height - 5, "P 期間・休講日  I 自動実行の開始/停止  L ログ", canvas.color(1))
        elif self.mode == "confirm_start" and self.start_preview:
            self._draw_confirm_start(canvas, top, left, width, height)
        elif self.mode == "delete":
            course = self.data["courses"][self.index]
            timing = registration_time(course) + (f"～{course['register_until']}" if course.get("register_until") else "")
            timing_text = timing if course.get("register_time") else f"{timing}（開始5分後）"
            for offset, line in enumerate(wrap_text(course["course_name"], width - 6)[:2]):
                put(2 + offset, line, curses.A_BOLD)
            put(5, f"曜日: {DAYS[course['weekday']]}曜  開始時刻: {course['start_time']}  QR番号: {course['room']}")
            put(6, f"登録時刻: {timing_text}")
            put(8, "[注意] この授業の自動出席予定を削除します。")
            put(9, "予定ファイルから削除し、自動実行中なら反映します。", curses.A_DIM)
            canvas.button(top + height - 6, left + 3, "[ キャンセル ]", self.confirm_selection == 0)
            canvas.button(top + height - 6, left + 25, "[ 削除する ]", self.confirm_selection == 1)
            put(height - 5, "Tab/←→: 選択  Enter: 決定", curses.A_DIM)
        else:
            labels = ("開始日 YYYY-MM-DD", "終了日 YYYY-MM-DD", "休講日 YYYY-MM-DD（複数はカンマ区切り）") if self.mode == "period" else ("授業名（出席画面と一致）", "曜日 mon～sun または 月～日", "授業開始時刻 HH:MM", "QR番号（例: 642、731、7301）", "登録時刻/範囲開始 HH:MM（空欄:開始5分後）", "範囲終了 HH:MM（空欄:固定時刻）")
            for i, label in enumerate(labels):
                row = 2 + i * (2 if len(labels) == 6 else 3)
                put(row, label, curses.A_DIM)
                if len(self.editors) != len(self.fields):
                    self.editors = [TextCursor(value) for value in self.fields]
                self.editors[i].draw(canvas, top + row + 1, left + 3, self.fields[i], width - 6, active=i == self.field_index)
            put(height - 5, "Tab/↑↓:項目 ←→:編集 Ctrl+S/最後Enter:保存", canvas.color(1))
        # 長いエラーでも2行で読み取れるようにする。
        display_message = prefix_text(self.message_level, self.message)
        message_attr = (canvas.color(3) | curses.A_BOLD if self.message_level == "error"
                        else canvas.color(4) | curses.A_BOLD if self.message_level == "warning" else canvas.color(1))
        for offset, line in enumerate(wrap_text(display_message, width - 6)[:2]):
            put(height - 4 + offset, line, message_attr)
        footer = (
            "Esc/Q:戻る R:再読込" if self.mode == "list"
            else "Tab/←→: 選択  Enter: 決定  Esc/Q: キャンセル" if self.mode == "confirm_start"
            else "Esc/Q: キャンセルして戻る" if self.mode == "delete"
            else "Esc: 戻る・保存確認  Ctrl+S: 保存  Ctrl+U: 全消去"
        )
        if self.mode == "list" and self.data is not None:
            footer += "  " + ("Z: 直前の変更を元に戻す" if self.undo_state else "(Z 元に戻す: 不可)")
        put(height - 2, footer, curses.A_DIM)
