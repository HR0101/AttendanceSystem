"""授業予定の検証と macOS LaunchAgent の管理。"""

import json
import hashlib
import os
import plistlib
import subprocess
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from .room import RoomError, parse_room


LABEL = "local.attendance.tool"
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
WEEK_MINUTES = 7 * 24 * 60
START_DELAY_MINUTES = 5
LAST_ALLOWED_MINUTE = 30


class ScheduleError(Exception):
    pass


def _valid_time(value) -> bool:
    return (isinstance(value, str) and len(value) == 5 and value[2] == ":"
            and value[:2].isascii() and value[3:].isascii()
            and value[:2].isdecimal() and value[3:].isdecimal()
            and int(value[:2]) < 24 and int(value[3:]) < 60)


def load_schedule(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScheduleError("schedule.json がありません。") from exc
    except json.JSONDecodeError as exc:
        raise ScheduleError(f"schedule.json の {exc.lineno} 行目を確認してください。") from exc
    return validate_schedule(raw)


def validate_schedule(raw: dict) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("courses"), list):
        raise ScheduleError("schedule.json の courses は配列にしてください。")
    try:
        active_from = date.fromisoformat(raw["active_from"])
        active_until = date.fromisoformat(raw["active_until"])
        blackout_dates = frozenset(date.fromisoformat(value) for value in raw.get("blackout_dates", []))
    except (KeyError, TypeError, ValueError) as exc:
        raise ScheduleError("授業期間と休講日は YYYY-MM-DD 形式で設定してください。") from exc
    if active_from > active_until:
        raise ScheduleError("授業期間の開始日が終了日より後です。")
    courses = []
    occupied = set()
    for number, item in enumerate(raw["courses"], 1):
        if not isinstance(item, dict):
            raise ScheduleError(f"{number} 件目の授業設定を確認してください。")
        weekday = item.get("weekday")
        start_time = item.get("start_time")
        course_name = item.get("course_name")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ScheduleError(f"{number} 件目の enabled は true または false にしてください。")
        if weekday not in WEEKDAYS:
            raise ScheduleError(f"{number} 件目の weekday は mon～sun にしてください。")
        if not _valid_time(start_time):
            raise ScheduleError(f"{number} 件目の start_time は HH:MM にしてください。")
        register_time = item.get("register_time", "")
        if register_time != "" and not _valid_time(register_time):
            raise ScheduleError(f"{number} 件目の register_time（登録時刻）は HH:MM にしてください。")
        register_until = item.get("register_until", "")
        if register_until != "" and (not _valid_time(register_until) or not register_time or register_until < register_time):
            raise ScheduleError(f"{number} 件目の範囲終了は、登録時刻以降の HH:MM を指定してください（日付またぎ不可）。")
        if not isinstance(course_name, str) or not course_name.strip():
            raise ScheduleError(f"{number} 件目の course_name に画面と一致する授業名を設定してください。")
        try:
            room, route = parse_room(item.get("room", ""))
        except (RoomError, AttributeError) as exc:
            raise ScheduleError(f"{number} 件目の教室番号を確認してください。") from exc
        key = (weekday, start_time)
        if enabled and key in occupied:
            raise ScheduleError("同じ曜日・開始時刻に複数の授業は設定できません。")
        if enabled:
            occupied.add(key)
        courses.append({"weekday": weekday, "start_time": start_time, "course_name": course_name.strip(), "room": room, "route": route, "enabled": enabled, "register_time": register_time, "register_until": register_until})
    return {"active_from": active_from, "active_until": active_until, "blackout_dates": blackout_dates, "courses": courses}


def schedule_data(schedule: dict) -> dict:
    """保存・編集用のJSON形式に戻す。内部用のrouteは保存しない。"""
    return {
        "active_from": schedule["active_from"].isoformat(),
        "active_until": schedule["active_until"].isoformat(),
        "blackout_dates": sorted(day.isoformat() for day in schedule["blackout_dates"]),
        "courses": [dict({key: course[key] for key in
                         ("weekday", "start_time", "room", "course_name")},
                         enabled=course.get("enabled", True), register_time=course.get("register_time", ""), register_until=course.get("register_until", ""))
                    for course in schedule["courses"]],
    }


def save_schedule(path: Path, raw: dict) -> dict:
    schedule = validate_schedule(raw)
    data = json.dumps(schedule_data(schedule), ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".schedule-", suffix=".json", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return schedule


def _start_week_minute(course: dict) -> int:
    hour, minute = map(int, course["start_time"].split(":"))
    return WEEKDAYS[course["weekday"]] * 1440 + hour * 60 + minute


def _registration_week_minute(course: dict, course_date: date | None = None) -> int:
    if not course.get("register_time"):
        return _start_week_minute(course) + START_DELAY_MINUTES
    hour, minute = map(int, course["register_time"].split(":"))
    run = WEEKDAYS[course["weekday"]] * 1440 + hour * 60 + minute
    if course.get("register_until") and course_date is not None:
        end_hour, end_minute = map(int, course["register_until"].split(":"))
        span = end_hour * 60 + end_minute - hour * 60 - minute + 1
        # 同じ授業・日付なら再起動しても同じ分を選び、日ごとに分散させる。
        seed = json.dumps([course_date.isoformat(), course["weekday"], course["start_time"], course["room"], course["course_name"], course["register_time"], course["register_until"]], ensure_ascii=False)
        run += int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest(), "big") % span
    return run


def registration_time(course: dict, course_date: date | None = None) -> str:
    minute_of_day = _registration_week_minute(course, course_date) % 1440
    return f"{minute_of_day // 60:02}:{minute_of_day % 60:02}"


def due_course(schedule: dict, now: datetime) -> dict | None:
    now_minute = now.weekday() * 1440 + now.hour * 60 + now.minute
    # 範囲内の各分に起動する場合、同じ授業を繰り返し処理しない。
    exact = any(c.get("enabled", True) and c.get("register_until") for c in schedule["courses"])
    matches = []
    for course in schedule["courses"]:
        run_minute = _registration_week_minute(course, now.date())
        elapsed = (now_minute - run_minute) % WEEK_MINUTES
        if not course.get("enabled", True) or elapsed > (0 if exact else LAST_ALLOWED_MINUTE - START_DELAY_MINUTES):
            continue
        # スリープ復帰や日付またぎでも、予定した授業の日付で期間・休講日を判定する。
        rollover = run_minute // 1440 - WEEKDAYS[course["weekday"]]
        course_date = (now - timedelta(minutes=elapsed, days=rollover)).date()
        if schedule["active_from"] <= course_date <= schedule["active_until"] and course_date not in schedule["blackout_dates"]:
            matches.append(course)
    if len(matches) > 1:
        raise ScheduleError("受付時間が重なる授業があります。自動登録を停止しました。")
    return matches[0] if matches else None


def calendar_intervals(courses: list[dict]) -> list[dict]:
    intervals = []
    seen = set()
    for course in courses:
        if not course.get("enabled", True):
            continue
        run_minute = _registration_week_minute(course) % WEEK_MINUTES
        end_minute = run_minute
        if course.get("register_until"):
            hour, minute = map(int, course["register_until"].split(":"))
            end_minute = WEEKDAYS[course["weekday"]] * 1440 + hour * 60 + minute
        for candidate in range(run_minute, end_minute + 1):
            weekday, minute_of_day = divmod(candidate, 1440)
            hour, minute = divmod(minute_of_day, 60)
            key = ((weekday + 1) % 7, hour, minute)
            if key not in seen:
                intervals.append(dict(zip(("Weekday", "Hour", "Minute"), key)))
                seen.add(key)
    return intervals


def agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def _owned_agent(path: Path, executable: Path) -> bool:
    try:
        plist = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, TypeError):
        return False
    return plist.get("Label") == LABEL and plist.get("ProgramArguments") == [str(executable), "--scheduled"]


def install_agent(base: Path, schedule: dict) -> Path:
    if not calendar_intervals(schedule["courses"]):
        raise ScheduleError("有効な授業を設定してから自動実行を開始してください。")
    if date.today() > schedule["active_until"]:
        raise ScheduleError("設定した授業期間は終了しています。")
    executable = base / "att"
    path = agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not _owned_agent(path, executable):
        raise ScheduleError("同名のLaunchAgentが存在します。上書きしません。")
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(executable), "--scheduled"],
        "WorkingDirectory": str(base),
        "StartCalendarInterval": calendar_intervals(schedule["courses"]),
        "RunAtLoad": False,
        "Umask": 0o077,
    }
    data = plistlib.dumps(plist)
    fd, temporary = tempfile.mkstemp(prefix=".attendance-", suffix=".plist", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        if path.exists():
            subprocess.run(["launchctl", "bootout", _target()], capture_output=True, check=False)
        os.replace(temporary, path)
        result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True, check=False)
        if result.returncode:
            raise ScheduleError("LaunchAgentを開始できません。macOSのログイン状態を確認してください。")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def remove_agent(base: Path) -> bool:
    path = agent_path()
    if not path.exists():
        return False
    if not _owned_agent(path, base / "att"):
        raise ScheduleError("同名のLaunchAgentはこのツールのものではありません。削除しません。")
    subprocess.run(["launchctl", "bootout", _target()], capture_output=True, check=False)
    path.unlink()
    return True


def agent_installed(base: Path) -> bool:
    path = agent_path()
    if not path.exists() or not _owned_agent(path, base / "att"):
        return False
    result = subprocess.run(["launchctl", "print", _target()], capture_output=True, check=False)
    return result.returncode == 0
