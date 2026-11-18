"""授業予定の検証と macOS LaunchAgent の管理。"""

import fcntl
import hashlib
import json
import os
import plistlib
import re
import stat
import shlex
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import private_file_lock, read_private_text
from .config import ConfigurationError, load_config
from .credentials import CredentialError, has_stored_password
from .room import RoomError, parse_room
from .users import select_user


LABEL = "local.attendance.tool"
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
WEEKDAY_LABELS = "月火水木金土日"
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
        raw = json.loads(read_private_text(path, ScheduleError, "schedule.json"))
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
    _check_registration_conflicts(courses)
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
    with private_file_lock(path):
        _atomic_write_bytes(path, data.encode("utf-8"), 0o600)
    return schedule


def update_schedule(path: Path, mutator, default_raw: dict | None = None, *, writer=None) -> dict:
    """排他ロック下で schedule.json を読み込み、mutator の変更を検証・保存する。"""
    save = writer or save_schedule
    with private_file_lock(path):
        if isinstance(path, Path) and (path.is_symlink() or path.exists()):
            current = schedule_data(load_schedule(path))
        elif default_raw is not None:
            current = schedule_data(validate_schedule(default_raw))
        else:
            raise ScheduleError("schedule.json がありません。")
        updated = mutator(current)
        return save(path, updated)


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


def _uses_exact_timing(courses: list[dict]) -> bool:
    """範囲指定を含む有効な授業があるときは遅延許容を行わない。"""
    return any(c.get("enabled", True) and c.get("register_until") for c in courses)


def _registration_window(course: dict, exact: bool) -> tuple[int, int]:
    """有効な授業が自動登録の対象になる週内分範囲 [start, end] を返す。"""
    run_minute = _registration_week_minute(course)
    if not exact:
        return run_minute, run_minute + (LAST_ALLOWED_MINUTE - START_DELAY_MINUTES)
    if course.get("register_until"):
        hour, minute = map(int, course["register_until"].split(":"))
        return run_minute, WEEKDAYS[course["weekday"]] * 1440 + hour * 60 + minute
    return run_minute, run_minute


def _split_week_window(start: int, end: int) -> list[tuple[int, int]]:
    """週またぎを含む登録ウィンドウを [0, WEEK_MINUTES - 1] の区間列へ分割する。"""
    start_mod = start % WEEK_MINUTES
    end_mod = end % WEEK_MINUTES
    if start_mod <= end_mod:
        return [(start_mod, end_mod)]
    return [(start_mod, WEEK_MINUTES - 1), (0, end_mod)]


def _format_week_minute(week_minute: int) -> tuple[int, str]:
    day, minute_of_day = divmod(week_minute % WEEK_MINUTES, 1440)
    return day, f"{minute_of_day // 60:02}:{minute_of_day % 60:02}"


def _window_description(course: dict, start: int, end: int) -> str:
    start_day, start_hhmm = _format_week_minute(start)
    end_day, end_hhmm = _format_week_minute(end)
    if start % WEEK_MINUTES == end % WEEK_MINUTES:
        span = f"{WEEKDAY_LABELS[start_day]} {start_hhmm}"
    elif start_day == end_day:
        span = f"{WEEKDAY_LABELS[start_day]} {start_hhmm}～{end_hhmm}"
    else:
        span = f"{WEEKDAY_LABELS[start_day]} {start_hhmm}～{WEEKDAY_LABELS[end_day]} {end_hhmm}"
    return f"{course['course_name']} ({span})"


def _check_registration_conflicts(courses: list[dict]) -> None:
    """有効な授業間で due_course() の受付ウィンドウが重ならないかを検証する。"""
    exact = _uses_exact_timing(courses)
    segments = []
    for index, course in enumerate(courses):
        if not course.get("enabled", True):
            continue
        start, end = _registration_window(course, exact)
        label = _window_description(course, start, end)
        for seg_start, seg_end in _split_week_window(start, end):
            segments.append((seg_start, seg_end, index, label))
    segments.sort()
    for prev, curr in zip(segments, segments[1:]):
        if curr[0] <= prev[1]:
            raise ScheduleError(f"受付時間が重なります: {prev[3]} と {curr[3]}")


def next_scheduled_run(schedule: dict, now: datetime | None = None) -> tuple[datetime, dict] | None:
    """有効な授業予定・期間・休講日を踏まえた次回の実行予定日時と授業を返す。"""
    current = datetime.now() if now is None else now
    active_from = schedule["active_from"]
    active_until = schedule["active_until"]
    blackout_dates = schedule["blackout_dates"]
    if active_from > active_until:
        return None
    # 日付またぎの登録時刻（例: 日曜23:58開始→月曜00:03実行）も前日分の授業日から拾う。
    start_search = max(active_from, current.date() - timedelta(days=1))
    if start_search > active_until:
        return None
    best = None
    for course in schedule["courses"]:
        if not course.get("enabled", True):
            continue
        target_weekday = WEEKDAYS[course["weekday"]]
        offset = (target_weekday - start_search.weekday()) % 7
        candidate_date = start_search + timedelta(days=offset)
        while candidate_date <= active_until:
            if candidate_date not in blackout_dates:
                run_minute = _registration_week_minute(course, candidate_date)
                rollover = run_minute // 1440 - target_weekday
                hour, minute = divmod(run_minute % 1440, 60)
                run_date = candidate_date + timedelta(days=rollover)
                run_at = datetime(run_date.year, run_date.month, run_date.day, hour, minute, tzinfo=current.tzinfo)
                if run_at >= current:
                    if best is None or run_at < best[0]:
                        best = (run_at, course)
                    break
            candidate_date += timedelta(days=7)
    return best


def _course_date_for(course: dict, now: datetime) -> date:
    days_since = (now.weekday() - WEEKDAYS[course["weekday"]]) % 7
    return now.date() - timedelta(days=days_since)


def _matching_courses(schedule: dict, now: datetime, *, grace_minutes: int = 0) -> list[tuple[dict, date]]:
    if grace_minutes < 0:
        raise ValueError("grace_minutes must be non-negative")
    now_minute = now.weekday() * 1440 + now.hour * 60 + now.minute
    # 範囲内の各分に起動する場合、同じ授業を繰り返し処理しない。
    exact = _uses_exact_timing(schedule["courses"])
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


@contextmanager
def _schedule_runs_lock(log_dir: Path):
    """logs/.schedule-runs.lock で排他ロックを保持する。"""
    lock_path = log_dir / ".schedule-runs.lock"
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise ScheduleError("logs/.schedule-runs.lock が通常のファイルではないため処理できません。")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ScheduleError("logs/.schedule-runs.lock を開けません。") from exc
    with os.fdopen(fd, "a+", encoding="utf-8") as lock_file:
        if not stat.S_ISREG(os.fstat(lock_file.fileno()).st_mode):
            raise ScheduleError("logs/.schedule-runs.lock が通常のファイルではないため処理できません。")
        os.fchmod(lock_file.fileno(), 0o600)
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _claim_path(log_dir: Path, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return log_dir / f".schedule-claim-{digest}.lock"


def _is_claim_active(log_dir: Path, key: str, *, shared: bool) -> bool:
    """対象授業・日付の進行中予約ロックが他プロセスに保持されているか確認する。"""
    claim_path = _claim_path(log_dir, key)
    if claim_path.is_symlink():
        raise ScheduleError("予約ロックファイルがシンボリックリンクのため処理できません。")
    try:
        fd = os.open(claim_path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ScheduleError("予約ロックファイルを開けません。") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ScheduleError("予約ロックファイルが通常のファイルではないため処理できません。")
        mode = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, mode)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _acquire_claim(log_dir: Path, key: str) -> tuple[Path, int, int] | None:
    """進行中予約ロックを非ブロッキングで取得する。先着がいれば None を返す。"""
    claim_path = _claim_path(log_dir, key)
    if claim_path.is_symlink() or (claim_path.exists() and not claim_path.is_file()):
        raise ScheduleError("予約ロックファイルが通常のファイルではないため処理できません。")
    try:
        fd = os.open(claim_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ScheduleError("予約ロックファイルを開けません。") from exc
    keep_open = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ScheduleError("予約ロックファイルが通常のファイルではないため処理できません。")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        os.utime(fd, None)
        claim_ino = os.fstat(fd).st_ino
        keep_open = True
        return claim_path, fd, claim_ino
    finally:
        if not keep_open:
            os.close(fd)


def _release_claim(base: Path, claim_path: Path, claim_fd: int, claim_ino: int) -> None:
    """進行中予約ロックを解放し、対応するロックファイルを削除する。"""
    closed = False
    try:
        log_dir = _ensure_log_dir(base)
        with _schedule_runs_lock(log_dir):
            try:
                if not claim_path.is_symlink() and os.lstat(claim_path).st_ino == claim_ino:
                    claim_path.unlink()
            except OSError:
                pass
            try:
                fcntl.flock(claim_fd, fcntl.LOCK_UN)
            finally:
                os.close(claim_fd)
                closed = True
    except ScheduleError:
        pass
    finally:
        if not closed:
            try:
                fcntl.flock(claim_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(claim_fd)


def _prune_stale_claims(log_dir: Path, now: datetime) -> None:
    """保持期間を過ぎた未使用の予約ロックファイルを掃除する。"""
    cutoff_ts = (now - timedelta(days=RECEIPT_RETENTION_DAYS)).timestamp()
    for path in log_dir.glob(".schedule-claim-*.lock"):
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_mtime >= cutoff_ts:
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            try:
                if os.lstat(path).st_ino == os.fstat(fd).st_ino:
                    path.unlink()
            except OSError:
                pass
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(fd)


def _select_pending_course(
    log_dir: Path | None, schedule: dict, now: datetime, *, shared_probe: bool
) -> tuple[dict | None, date | None, dict[str, dict], bool]:
    cutoff = now.date() - timedelta(days=RECEIPT_RETENTION_DAYS)
    if log_dir is None:
        runs, dirty = {}, False
    else:
        runs, dirty = _load_schedule_runs(log_dir / "schedule-runs.json", cutoff)
    matches = _matching_courses(schedule, now, grace_minutes=EXACT_GRACE_MINUTES)
    pending = []
    for course, course_date in matches:
        key = _run_key(course["weekday"], course["start_time"], course_date)
        if key in runs:
            continue
        if log_dir is not None and _is_claim_active(log_dir, key, shared=shared_probe):
            continue
        pending.append((course, course_date))
    if len(pending) > 1:
        raise ScheduleError("受付時間が重なる授業があります。自動登録を停止しました。")
    if not pending:
        return None, None, runs, dirty
    return pending[0][0], pending[0][1], runs, dirty


@contextmanager
def claim_scheduled_run(base: Path, schedule: dict, now: datetime):
    """LaunchAgent実行向けに実行枠を一時予約し、成功時のみ当日記録を確定する。"""
    log_dir = _ensure_log_dir(base)
    runs_path = log_dir / "schedule-runs.json"
    cutoff = now.date() - timedelta(days=RECEIPT_RETENTION_DAYS)
    claim: tuple[Path, int, int] | None = None
    course = None
    course_date = None
    key = None
    with _schedule_runs_lock(log_dir):
        _prune_stale_claims(log_dir, now)
        course, course_date, runs, dirty = _select_pending_course(log_dir, schedule, now, shared_probe=False)
        if course is None:
            if dirty and runs_path.exists():
                _save_schedule_runs(runs_path, runs)
        else:
            key = _run_key(course["weekday"], course["start_time"], course_date)
            claim = _acquire_claim(log_dir, key)
            if claim is None:
                course = None
    if course is None or claim is None:
        yield None, lambda: None
        return
    claim_path, claim_fd, claim_ino = claim
    committed = False
    released = False

    def commit() -> None:
        nonlocal committed
        if released:
            raise ScheduleError("予約解除後は実行記録を確定できません。")
        if committed:
            return
        verified_log_dir = _ensure_log_dir(base)
        with _schedule_runs_lock(verified_log_dir):
            latest_runs, _ = _load_schedule_runs(verified_log_dir / "schedule-runs.json", cutoff)
            latest_runs[key] = {
                "weekday": course["weekday"],
                "start_time": course["start_time"],
                "course_date": course_date.isoformat(),
                "recorded_at": now.isoformat(timespec="seconds"),
            }
            _save_schedule_runs(verified_log_dir / "schedule-runs.json", latest_runs)
        committed = True

    try:
        yield course, commit
    finally:
        released = True
        _release_claim(base, claim_path, claim_fd, claim_ino)


def due_scheduled_course(base: Path, schedule: dict, now: datetime, *, record: bool = True) -> dict | None:
    """LaunchAgent実行向けの対象授業を判定し、record=True なら同日の重複実行防止記録を保存する。"""
    if not record:
        log_dir = base / "logs"
        if log_dir.is_symlink():
            raise ScheduleError("logs がシンボリックリンクのため処理できません。")
        if log_dir.exists() and not log_dir.is_dir():
            raise ScheduleError("logs がフォルダではないため処理できません。")
        course, _, _, _ = _select_pending_course(log_dir if log_dir.exists() else None, schedule, now, shared_probe=True)
        return course
    with claim_scheduled_run(base, schedule, now) as (course, commit):
        if course is not None:
            commit()
        return course


def calendar_intervals(courses: list[dict]) -> list[dict]:
    intervals = []
    seen = set()
    for course in courses:
        if not course.get("enabled", True):
            continue
        run_minute, end_minute = _registration_window(course, exact=True)
        for candidate in range(run_minute, end_minute + 1):
            weekday, minute_of_day = divmod(candidate % WEEK_MINUTES, 1440)
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
    if _today() > schedule["active_until"]:
        raise ScheduleError("設定した授業期間は終了しています。")
    executable = base / "att"
    path = agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not _owned_agent(path, executable)):
        raise ScheduleError(_agent_conflict_message(path, executable, action="install"))
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
