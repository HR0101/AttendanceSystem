"""授業予定の検証と macOS LaunchAgent の管理。"""

import json
import os
import plistlib
import subprocess
import tempfile
from datetime import date, datetime
from pathlib import Path

from .room import RoomError, parse_room


LABEL = "local.attendance.tool"
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
WEEK_MINUTES = 7 * 24 * 60
START_DELAY_MINUTES = 5
LAST_ALLOWED_MINUTE = 30


class ScheduleError(Exception):
    pass


def load_schedule(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScheduleError("schedule.json がありません。") from exc
    except json.JSONDecodeError as exc:
        raise ScheduleError(f"schedule.json の {exc.lineno} 行目を確認してください。") from exc
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
        if weekday not in WEEKDAYS:
            raise ScheduleError(f"{number} 件目の weekday は mon～sun にしてください。")
        if not isinstance(start_time, str) or len(start_time) != 5 or start_time[2] != ":":
            raise ScheduleError(f"{number} 件目の start_time は HH:MM にしてください。")
        hour, minute = start_time[:2], start_time[3:]
        if not hour.isascii() or not minute.isascii() or not hour.isdecimal() or not minute.isdecimal() or int(hour) > 23 or int(minute) > 59:
            raise ScheduleError(f"{number} 件目の start_time は HH:MM にしてください。")
        if not isinstance(course_name, str) or not course_name.strip():
            raise ScheduleError(f"{number} 件目の course_name に画面と一致する授業名を設定してください。")
        try:
            room, route = parse_room(item.get("room", ""))
        except (RoomError, AttributeError) as exc:
            raise ScheduleError(f"{number} 件目の教室番号を確認してください。") from exc
        key = (weekday, start_time)
        if key in occupied:
            raise ScheduleError("同じ曜日・開始時刻に複数の授業は設定できません。")
        occupied.add(key)
        courses.append({"weekday": weekday, "start_time": start_time, "course_name": course_name.strip(), "room": room, "route": route})
    return {"active_from": active_from, "active_until": active_until, "blackout_dates": blackout_dates, "courses": courses}


def _start_week_minute(course: dict) -> int:
    hour, minute = map(int, course["start_time"].split(":"))
    return WEEKDAYS[course["weekday"]] * 1440 + hour * 60 + minute


def due_course(schedule: dict, now: datetime) -> dict | None:
    if not schedule["active_from"] <= now.date() <= schedule["active_until"] or now.date() in schedule["blackout_dates"]:
        return None
    now_minute = now.weekday() * 1440 + now.hour * 60 + now.minute
    matches = [course for course in schedule["courses"] if START_DELAY_MINUTES <= (now_minute - _start_week_minute(course)) % WEEK_MINUTES <= LAST_ALLOWED_MINUTE]
    if len(matches) > 1:
        raise ScheduleError("受付時間が重なる授業があります。自動登録を停止しました。")
    return matches[0] if matches else None


def calendar_intervals(courses: list[dict]) -> list[dict]:
    intervals = []
    for course in courses:
        run_minute = (_start_week_minute(course) + START_DELAY_MINUTES) % WEEK_MINUTES
        weekday, minute_of_day = divmod(run_minute, 1440)
        hour, minute = divmod(minute_of_day, 60)
        intervals.append({"Weekday": (weekday + 1) % 7, "Hour": hour, "Minute": minute})
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
    if not schedule["courses"]:
        raise ScheduleError("schedule.json に授業を設定してからインストールしてください。")
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
