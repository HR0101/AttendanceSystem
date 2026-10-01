"""出席ツールの実行入口。"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from src.config import ConfigurationError, load_config
from src.credentials import CredentialError, get_credentials
from src.logging_utils import setup_logging
from src.verification import AttendanceError
from src.room import RoomError, ask_room, parse_room
from src.scheduler import ScheduleError, agent_installed, due_course, install_agent, load_schedule, remove_agent


VERSION = "0.1.0"


def schedule_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="att schedule", description="自動出席の予定を管理します。")
    parser.add_argument("action", choices=("status", "install", "remove"))
    args = parser.parse_args(argv)
    base = Path(__file__).resolve().parent
    try:
        if args.action == "remove":
            print("自動実行を解除しました。" if remove_agent(base) else "自動実行は登録されていません。")
            return 0
        schedule = load_schedule(base / "schedule.json")
        if args.action == "status":
            state = "有効" if agent_installed(base) else "未登録"
            print(f"自動実行: {state}、予定: {len(schedule['courses'])} 件、期間: {schedule['active_from']}～{schedule['active_until']}")
            return 0
        config = load_config(base / "config.json")
        if not config["browser"]["headless"]:
            raise ScheduleError("自動実行には browser.headless を true にしてください。")
        if not config["student_id"]:
            raise ScheduleError("自動実行の前に手動でログインし、学生番号を保存してください。")
        path = install_agent(base, schedule)
        print(f"自動実行を登録しました: {path}")
        return 0
    except (ScheduleError, ConfigurationError) as exc:
        print(f"自動実行を設定できません: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "schedule":
        return schedule_command(argv[1:])
    parser = argparse.ArgumentParser(prog="att", description="大学の出席画面を確認・登録します。")
    parser.add_argument("room", nargs="?", help="教室番号（例: 642、731）。省略時は入力します。")
    parser.add_argument("--check", action="store_true", help="登録せずに状態だけ確認します。")
    parser.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    base = Path(__file__).resolve().parent
    started = time.monotonic()
    logger = setup_logging(base)
    logger.info("起動 version=%s", VERSION)
    try:
        config = load_config(base / "config.json")
        expected_course = None
        expected_start = None
        if args.scheduled:
            if args.room is not None:
                raise ScheduleError("自動実行では教室番号を直接指定できません。")
            schedule = load_schedule(base / "schedule.json")
            course = due_course(schedule, datetime.now().astimezone())
            if course is None:
                result = "予定時間外（出席操作なし）"
                logger.info("結果=%s", result)
                print(f"結果: {result}")
                return 0
            room, route = course["room"], course["route"]
            expected_course = course["course_name"]
            expected_start = course["start_time"]
            logger.info("自動実行対象 開始=%s 教室=%s", expected_start, room)
        else:
            room, route = parse_room(args.room) if args.room is not None else ask_room()
        if args.check:
            config["dry_run"] = True
        student_id, password = get_credentials(config, base / "config.json", interactive=not args.scheduled)
        try:
            from src.browser import open_page
            from src.attendance import run_attendance
            logger.info("段階=ブラウザ起動")
            with open_page(config) as page:
                result = run_attendance(page, config, student_id, password, room, route, logger, expected_course, expected_start)
        finally:
            del password
        logger.info("結果=%s 所要時間=%.1f秒", result, time.monotonic() - started)
        print(f"結果: {result}")
        return 0
    except (ConfigurationError, CredentialError, AttendanceError, RoomError, ScheduleError) as exc:
        logger.error("失敗 種別=%s 理由=%s", type(exc).__name__, exc)
        print(f"処理を停止しました: {exc}", file=sys.stderr)
    except ModuleNotFoundError as exc:
        logger.error("依存パッケージ不足")
        print(f"依存パッケージがありません: {exc.name}。README.md のセットアップを確認してください。", file=sys.stderr)
    except Exception as exc:
        # Playwright の例外にはURLや画面内容が含まれ得るため本文は記録しない。
        logger.error("失敗 種別=%s", type(exc).__name__)
        if type(exc).__name__ == "TimeoutError":
            print("画面操作がタイムアウトしました。logs の直前の『段階』を確認してください。", file=sys.stderr)
        else:
            print("処理を停止しました。通信または画面状態を確認し、logs を参照してください。", file=sys.stderr)
    logger.info("異常終了 所要時間=%.1f秒", time.monotonic() - started)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
