"""授業予定の検証と macOS LaunchAgent の管理。"""

import json
import hashlib
import os
import plistlib
import shlex
import subprocess
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import ConfigurationError, load_config
from .credentials import CredentialError, has_stored_password
from .room import RoomError, parse_room
from .users import select_user


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


def _today() -> date:
    return date.today()


def _as_date(value) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _read_plist(path: Path) -> dict | None:
    try:
        plist = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, TypeError):
        return None
    return plist if isinstance(plist, dict) else None


def _plist_registered_path(plist: dict) -> str | None:
    args = plist.get("ProgramArguments")
    if isinstance(args, list) and args and isinstance(args[0], str):
        return args[0]
    working_dir = plist.get("WorkingDirectory")
    return working_dir if isinstance(working_dir, str) else None


def _owned_agent(path: Path, executable: Path) -> bool:
    plist = _read_plist(path)
    return plist is not None and plist.get("Label") == LABEL and plist.get("ProgramArguments") == [str(executable), "--scheduled"]


def _normalize_intervals(raw) -> list[tuple[int, int, int]] | None:
    if raw is None:
        return []
    items = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else None
    if items is None:
        return None
    result = []
    for item in items:
        if not isinstance(item, dict) or set(item.keys()) != {"Weekday", "Hour", "Minute"}:
            return None
        values = (item["Weekday"], item["Hour"], item["Minute"])
        if any(type(v) is not int for v in values):
            return None
        result.append(values)
    return sorted(result)


def _launchctl_loaded() -> bool:
    try:
        result = subprocess.run(["launchctl", "print", _target()], capture_output=True, check=False)
    except OSError:
        return False
    return result.returncode == 0


def _repair_command(path: Path, *, include_install: bool) -> str:
    quoted = shlex.quote(str(path))
    cmd = f"launchctl bootout {_target()} と rm {quoted}"
    return f"{cmd} を実行してから att schedule install を実行してください。" if include_install else f"{cmd} を実行してください。"


def _agent_conflict_message(path: Path, executable: Path, *, action: str) -> str:
    """_owned_agent 不一致時に、Label 一致・パス相違を区別した修復案内を返す。"""
    plist = _read_plist(path)
    target = _target()
    quoted = shlex.quote(str(path))
    verb = "上書き" if action == "install" else "削除"
    if plist is None:
        return f"同名のLaunchAgent（{path}）を読み込めないため{verb}しません。確認: plutil -p {quoted}"
    label = plist.get("Label")
    registered = _plist_registered_path(plist) or "不明"
    if label == LABEL:
        step = "修復" if action == "install" else "解除"
        return (
            f"別の場所のフォルダで登録された LaunchAgent が残っているため{verb}しません"
            f"（Label: {LABEL}、登録先: {registered}、現在: {executable}）。"
            f"確認: launchctl print {target} ／ {step}: {_repair_command(path, include_install=action == 'install')}"
        )
    found_label = label if isinstance(label, str) and label else "不明"
    return (
        f"同名のLaunchAgentはこのツールのものではありません（Label: {found_label}、登録先: {registered}）。"
        f"{verb}しません。確認: plutil -p {quoted}"
    )


def install_agent(base: Path, schedule: dict) -> Path:
    if not calendar_intervals(schedule["courses"]):
        raise ScheduleError("有効な授業を設定してから自動実行を開始してください。")
    if _today() > schedule["active_until"]:
        raise ScheduleError("設定した授業期間は終了しています。")
    executable = base / "att"
    path = agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not _owned_agent(path, executable):
        raise ScheduleError(_agent_conflict_message(path, executable, action="install"))
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
    executable = base / "att"
    if not _owned_agent(path, executable):
        raise ScheduleError(_agent_conflict_message(path, executable, action="remove"))
    subprocess.run(["launchctl", "bootout", _target()], capture_output=True, check=False)
    path.unlink()
    return True


def agent_installed(base: Path) -> bool:
    path = agent_path()
    if not path.exists() or not _owned_agent(path, base / "att"):
        return False
    return _launchctl_loaded()


def agent_status(base: Path, *, installed: bool | None = None) -> dict:
    """LaunchAgent plist と登録パス・読み込み状態・起動時刻を調べる。"""
    path = agent_path()
    executable = base / "att"
    target = _target()
    quoted = shlex.quote(str(path))
    status = {
        "path": path,
        "target": target,
        "expected_path": str(executable),
        "exists": path.exists(),
        "readable": False,
        "owned": False,
        "loaded": bool(installed) if installed is not None else False,
        "label": None,
        "registered_path": None,
        "intervals": None,
        "state": "uninstalled",
        "guidance": None,
    }
    if not status["exists"]:
        if installed:
            status["state"] = "installed"
        return status
    plist = _read_plist(path)
    if plist is None:
        status["state"] = "unreadable"
        status["guidance"] = f"plutil -p {quoted} で内容を確認してください。"
        return status
    status["readable"] = True
    label = plist.get("Label")
    status["label"] = label if isinstance(label, str) else None
    status["registered_path"] = _plist_registered_path(plist)
    status["owned"] = label == LABEL and plist.get("ProgramArguments") == [str(executable), "--scheduled"]
    if not status["owned"]:
        if label == LABEL:
            status["state"] = "path_mismatch"
            status["guidance"] = _repair_command(path, include_install=True)
        else:
            status["state"] = "foreign"
            status["guidance"] = f"plutil -p {quoted} で内容を確認してください。"
        return status
    loaded = _launchctl_loaded() if installed is None else bool(installed)
    status["loaded"] = loaded
    status["state"] = "installed" if loaded else "unloaded"
    status["intervals"] = _normalize_intervals(plist.get("StartCalendarInterval"))
    if not loaded:
        status["guidance"] = "att schedule install を実行して LaunchAgent を開始してください。"
    return status


def schedule_diagnostics(
    base: Path,
    schedule: dict | None,
    *,
    installed: bool | None = None,
    schedule_error: str | None = None,
    today: date | None = None,
) -> dict:
    """LaunchAgent との同期状態および自動実行の前提条件を診断する。"""
    today = today or _today()
    agent = agent_status(base, installed=installed)
    in_sync = None
    enabled_courses = 0
    total_courses = 0
    active_from = None
    active_until = None
    period_valid = None
    if schedule is not None:
        courses = schedule.get("courses", [])
        total_courses = len(courses)
        enabled_courses = sum(1 for c in courses if c.get("enabled", True))
        active_from = _as_date(schedule.get("active_from"))
        active_until = _as_date(schedule.get("active_until"))
        if active_until is not None:
            period_valid = today <= active_until
        try:
            expected_intervals = _normalize_intervals(calendar_intervals(courses))
        except (ScheduleError, KeyError, TypeError, ValueError):
            expected_intervals = None
        if agent["owned"] and expected_intervals is not None:
            in_sync = agent["intervals"] == expected_intervals

    config = None
    config_error = None
    try:
        config = load_config(base / "config.json")
    except (ConfigurationError, OSError) as exc:
        config_error = str(exc)

    headless = config["browser"]["headless"] if config is not None else None
    user = None
    user_error = None
    if config is not None:
        try:
            user = select_user(config)
        except ConfigurationError as exc:
            user_error = str(exc)

    keychain_ok = None
    keychain_error = None
    if user is not None:
        try:
            keychain_ok = has_stored_password(user["student_id"])
        except CredentialError as exc:
            keychain_ok = False
            keychain_error = str(exc)

    return {
        "today": today,
        "agent": agent,
        "schedule_error": schedule_error,
        "total_courses": total_courses,
        "enabled_courses": enabled_courses,
        "active_from": active_from if active_from is not None else (schedule.get("active_from") if schedule else None),
        "active_until": active_until if active_until is not None else (schedule.get("active_until") if schedule else None),
        "period_valid": period_valid,
        "in_sync": in_sync,
        "config_ok": config is not None,
        "config_error": config_error,
        "headless": headless,
        "user": user,
        "user_error": user_error,
        "keychain_ok": keychain_ok,
        "keychain_error": keychain_error,
    }


def format_schedule_diagnostics(diag: dict) -> list[str]:
    """schedule_diagnostics の結果を人間が読める行一覧に整形する。"""
    agent = diag["agent"]
    state_labels = {
        "installed": "有効",
        "unloaded": "停止中（LaunchAgent 未読み込み）",
        "uninstalled": "未登録",
        "path_mismatch": f"パス不一致（別フォルダで登録済み / Label: {agent['label']}）",
        "foreign": f"競合（別の LaunchAgent が存在 / Label: {agent['label'] or '不明'}）",
        "unreadable": f"エラー（plist を読み込めません: {agent['path']}）",
    }
    lines = [f"自動実行: {state_labels[agent['state']]}"]
    if agent["state"] == "path_mismatch":
        lines.append(f"  登録先パス: {agent['registered_path'] or '不明'}（現在: {agent['expected_path']}）")
    elif agent["state"] == "foreign":
        lines.append(f"  登録先パス: {agent['registered_path'] or '不明'}")
    if agent["guidance"]:
        lines.append(f"  修復・確認: {agent['guidance']}")

    if diag["schedule_error"]:
        lines.append(f"予定ファイル: NG（{diag['schedule_error']}）")
    else:
        lines.append(
            f"予定: 有効 {diag['enabled_courses']} 件 / 全 {diag['total_courses']} 件、"
            f"期間: {diag['active_from']}～{diag['active_until']}"
        )
        if agent["owned"]:
            if diag["in_sync"]:
                lines.append("LaunchAgent 同期: 一致（schedule.json と同じ起動時刻です）")
            else:
                lines.append("LaunchAgent 同期: 設定ずれ: att schedule install を実行してください（plist と schedule.json が一致しません）")

    lines.append("前提条件:")
    if not diag["config_ok"]:
        lines.append(f"  config.json: NG（{diag['config_error']}）")
        lines.append("  browser.headless: 未確認（config.json を確認してください）")
        lines.append("  既定ユーザー: 未確認（config.json を確認してください）")
        lines.append("  Keychain パスワード: 未確認（config.json を確認してください）")
    else:
        lines.append("  config.json: OK")
        lines.append(
            "  browser.headless: OK（true）"
            if diag["headless"]
            else "  browser.headless: NG（false: 自動実行には true にしてください）"
        )
        if diag["user_error"]:
            lines.append(f"  既定ユーザー: NG（{diag['user_error']}）")
            lines.append("  Keychain パスワード: 未確認（既定ユーザーを確認してください）")
        elif diag["user"] is None:
            lines.append("  既定ユーザー: NG（未設定: att user add または手動ログインで登録してください）")
            lines.append("  Keychain パスワード: 未確認（既定ユーザーが未設定です）")
        else:
            user = diag["user"]
            lines.append(f"  既定ユーザー: OK（{user['username']} / {user['student_id']}）")
            if diag["keychain_error"]:
                lines.append(f"  Keychain パスワード: NG（{diag['keychain_error']}）")
            elif diag["keychain_ok"]:
                lines.append("  Keychain パスワード: OK（保存済み）")
            else:
                lines.append("  Keychain パスワード: NG（未保存: 手動で一度ログインするか att user password で設定してください）")

    if diag["schedule_error"] is None:
        if diag["period_valid"] is True:
            note = "（開始前）" if isinstance(diag["active_from"], date) and diag["today"] < diag["active_from"] else ""
            lines.append(f"  授業期間（終了日 {diag['active_until']}）: OK{note}")
        elif diag["period_valid"] is False:
            lines.append(f"  授業期間（終了日 {diag['active_until']}）: NG（期限切れです。期間を更新してください）")
        lines.append(
            f"  有効な授業数: OK（{diag['enabled_courses']}件）"
            if diag["enabled_courses"] > 0
            else "  有効な授業数: NG（0件: 有効な授業を設定してください）"
        )
    return lines
