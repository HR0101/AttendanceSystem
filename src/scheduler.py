"""授業予定の検証と macOS LaunchAgent の管理。"""

import fcntl
import json
import hashlib
import os
import plistlib
import re
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
EXACT_GRACE_MINUTES = 2
RECEIPT_RETENTION_DAYS = 14
MAX_LAUNCHAGENT_LOG_BYTES = 256 * 1024


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


def _atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """一時ファイル経由で同期書き込みし、シンボリックリンクを上書きしない。"""
    if path.is_symlink():
        raise ScheduleError(f"{path.name} がシンボリックリンクのため保存できません。")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _ensure_log_dir(base: Path) -> Path:
    """base/logs が安全な専用ディレクトリ (0o700) であることを保証する。"""
    log_dir = base / "logs"
    if log_dir.is_symlink():
        raise ScheduleError("logs がシンボリックリンクのため処理できません。")
    if log_dir.exists() and not log_dir.is_dir():
        raise ScheduleError("logs がフォルダではないため処理できません。")
    log_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(log_dir, 0o700)
    return log_dir


def _sanitize_log_text(text: str, limit: int = 200) -> str:
    cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    cleaned = re.sub(r"://[^/\s@]+@", "://***@", cleaned)
    cleaned = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", cleaned)
    return " ".join(cleaned.split())[:limit]


def _ensure_launchagent_log(log_dir: Path) -> Path:
    """logs/launchagent.log を 0o600 で準備し、肥大化時は1世代ローテーションする。"""
    log_path = log_dir / "launchagent.log"
    if log_path.is_symlink() or (log_path.exists() and not log_path.is_file()):
        raise ScheduleError("logs/launchagent.log が通常のファイルではないため処理できません。")
    if log_path.exists() and log_path.stat().st_size > MAX_LAUNCHAGENT_LOG_BYTES:
        backup = log_dir / "launchagent.log.1"
        if not backup.is_symlink() and (not backup.exists() or backup.is_file()):
            os.replace(log_path, backup)
            os.chmod(backup, 0o600)
    if not log_path.exists():
        fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    os.chmod(log_path, 0o600)
    return log_path


def _append_launchagent_log(log_dir: Path, message: str) -> None:
    try:
        log_path = _ensure_launchagent_log(log_dir)
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        entry = f"{timestamp} {_sanitize_log_text(message)}\n"
        fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(entry)
    except (OSError, ScheduleError):
        pass


def save_schedule(path: Path, raw: dict) -> dict:
    schedule = validate_schedule(raw)
    data = json.dumps(schedule_data(schedule), ensure_ascii=False, indent=2) + "\n"
    _atomic_write_bytes(path, data.encode("utf-8"), 0o600)
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


def _course_date_for(course: dict, now: datetime) -> date:
    days_since = (now.weekday() - WEEKDAYS[course["weekday"]]) % 7
    return now.date() - timedelta(days=days_since)


def _matching_courses(schedule: dict, now: datetime, *, grace_minutes: int = 0) -> list[tuple[dict, date]]:
    if grace_minutes < 0:
        raise ValueError("grace_minutes must be non-negative")
    now_minute = now.weekday() * 1440 + now.hour * 60 + now.minute
    # 範囲内の各分に起動する場合、同じ授業を繰り返し処理しない。
    exact = any(c.get("enabled", True) and c.get("register_until") for c in schedule["courses"])
    max_elapsed = grace_minutes if exact else LAST_ALLOWED_MINUTE - START_DELAY_MINUTES
    matches = []
    for course in schedule["courses"]:
        if not course.get("enabled", True):
            continue
        course_date = _course_date_for(course, now)
        run_minute = _registration_week_minute(course, course_date)
        elapsed = (now_minute - run_minute) % WEEK_MINUTES
        if elapsed > max_elapsed:
            continue
        if schedule["active_from"] <= course_date <= schedule["active_until"] and course_date not in schedule["blackout_dates"]:
            matches.append((course, course_date))
    return matches


def due_course(schedule: dict, now: datetime, *, grace_minutes: int = 0) -> dict | None:
    """現在時刻に登録対象の授業があれば返す。grace_minutes は exact モードの分境界猶予。"""
    matches = _matching_courses(schedule, now, grace_minutes=grace_minutes)
    if len(matches) > 1:
        raise ScheduleError("受付時間が重なる授業があります。自動登録を停止しました。")
    return matches[0][0] if matches else None


def _run_key(weekday: str, start_time: str, course_date: date) -> str:
    return f"{weekday}|{start_time}|{course_date.isoformat()}"


def _load_schedule_runs(runs_path: Path, cutoff: date) -> tuple[dict[str, dict], bool]:
    """実行記録を読み込み、保持期限切れや不正な項目を除外する。"""
    if runs_path.is_symlink():
        raise ScheduleError("logs/schedule-runs.json がシンボリックリンクのため処理できません。")
    if runs_path.exists() and not runs_path.is_file():
        raise ScheduleError("logs/schedule-runs.json が通常のファイルではないため処理できません。")
    if not runs_path.exists():
        return {}, False
    try:
        fd = os.open(runs_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, True
    except OSError as exc:
        raise ScheduleError("logs/schedule-runs.json を読み込めません。") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("runs"), dict):
        return {}, True
    valid: dict[str, dict] = {}
    dirty = False
    for key, entry in raw["runs"].items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            dirty = True
            continue
        weekday = entry.get("weekday")
        start_time = entry.get("start_time")
        course_date_raw = entry.get("course_date")
        if weekday not in WEEKDAYS or not _valid_time(start_time) or not isinstance(course_date_raw, str):
            dirty = True
            continue
        try:
            course_date = date.fromisoformat(course_date_raw)
        except ValueError:
            dirty = True
            continue
        if key != _run_key(weekday, start_time, course_date) or course_date < cutoff:
            dirty = True
            continue
        valid[key] = entry
    return valid, dirty


def _save_schedule_runs(runs_path: Path, runs: dict[str, dict]) -> None:
    payload = json.dumps({"runs": runs}, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_bytes(runs_path, payload.encode("utf-8"), 0o600)


def due_scheduled_course(base: Path, schedule: dict, now: datetime) -> dict | None:
    """LaunchAgent実行向けの対象授業を判定し、同日の重複実行を防ぐ。"""
    log_dir = _ensure_log_dir(base)
    lock_path = log_dir / ".schedule-runs.lock"
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise ScheduleError("logs/.schedule-runs.lock が通常のファイルではないため処理できません。")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ScheduleError("logs/.schedule-runs.lock を開けません。") from exc
    with os.fdopen(fd, "a+", encoding="utf-8") as lock_file:
        os.fchmod(lock_file.fileno(), 0o600)
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            runs_path = log_dir / "schedule-runs.json"
            cutoff = now.date() - timedelta(days=RECEIPT_RETENTION_DAYS)
            runs, dirty = _load_schedule_runs(runs_path, cutoff)
            matches = _matching_courses(schedule, now, grace_minutes=EXACT_GRACE_MINUTES)
            pending = [
                (course, course_date)
                for course, course_date in matches
                if _run_key(course["weekday"], course["start_time"], course_date) not in runs
            ]
            if len(pending) > 1:
                raise ScheduleError("受付時間が重なる授業があります。自動登録を停止しました。")
            if not pending:
                if dirty and runs_path.exists():
                    _save_schedule_runs(runs_path, runs)
                return None
            course, course_date = pending[0]
            key = _run_key(course["weekday"], course["start_time"], course_date)
            runs[key] = {
                "weekday": course["weekday"],
                "start_time": course["start_time"],
                "course_date": course_date.isoformat(),
                "recorded_at": now.isoformat(timespec="seconds"),
            }
            _save_schedule_runs(runs_path, runs)
            return course
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


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


def _sanitize_launchctl_stderr(stderr) -> str:
    if isinstance(stderr, bytes):
        text = stderr.decode("utf-8", errors="replace")
    elif isinstance(stderr, str):
        text = stderr
    else:
        return ""
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return _sanitize_log_text(first_line, limit=160)


def _format_bootstrap_error(result) -> str:
    summary = _sanitize_launchctl_stderr(getattr(result, "stderr", None))
    code = getattr(result, "returncode", "unknown")
    return f"code={code}: {summary}" if summary else f"code={code}"


def _rollback_agent(path: Path, previous_data: bytes | None) -> str:
    """bootstrap 失敗時に以前の plist 復元と再登録（または新規作成物の削除）を試みる。"""
    try:
        if previous_data is None:
            path.unlink(missing_ok=True)
            return "新規作成ファイルを削除済み"
        _atomic_write_bytes(path, previous_data, 0o600)
        restored = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True, check=False)
        if restored.returncode == 0:
            return "以前の設定へ復元済み"
        return f"設定ファイルは復元、再登録失敗({_format_bootstrap_error(restored)})"
    except (OSError, ScheduleError):
        return "以前の設定の復元に失敗"


def install_agent(base: Path, schedule: dict) -> Path:
    if not calendar_intervals(schedule["courses"]):
        raise ScheduleError("有効な授業を設定してから自動実行を開始してください。")
    if date.today() > schedule["active_until"]:
        raise ScheduleError("設定した授業期間は終了しています。")
    executable = base / "att"
    path = agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not _owned_agent(path, executable)):
        raise ScheduleError("同名のLaunchAgentが存在します。上書きしません。")
    log_dir = _ensure_log_dir(base)
    launchagent_log = _ensure_launchagent_log(log_dir)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(executable), "--scheduled"],
        "WorkingDirectory": str(base),
        "StartCalendarInterval": calendar_intervals(schedule["courses"]),
        "StandardOutPath": str(launchagent_log),
        "StandardErrorPath": str(launchagent_log),
        "RunAtLoad": False,
        "Umask": 0o077,
    }
    data = plistlib.dumps(plist)
    previous_data = path.read_bytes() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=".attendance-", suffix=".plist", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if previous_data is not None:
            subprocess.run(["launchctl", "bootout", _target()], capture_output=True, check=False)
        os.replace(temporary, path)
        try:
            result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True, check=False)
            detail = _format_bootstrap_error(result) if result.returncode else None
        except OSError as exc:
            detail = type(exc).__name__
        if detail is not None:
            rollback_note = _rollback_agent(path, previous_data)
            _append_launchagent_log(log_dir, f"ERROR launchctl bootstrap failed ({detail}; {rollback_note})")
            raise ScheduleError(f"LaunchAgentを開始できません（{detail}、{rollback_note}）。macOSのログイン状態を確認してください。")
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
