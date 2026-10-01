"""出席ツールの実行入口。"""

import sys
import time
from pathlib import Path

from src.config import ConfigurationError, load_config
from src.credentials import CredentialError, get_credentials
from src.logging_utils import setup_logging
from src.verification import AttendanceError
from src.room import RoomError, ask_room


VERSION = "0.1.0"


def main() -> int:
    base = Path(__file__).resolve().parent
    started = time.monotonic()
    logger = setup_logging(base)
    logger.info("起動 version=%s", VERSION)
    try:
        config = load_config(base / "config.json")
        room, route = ask_room()
        student_id, password = get_credentials(config, base / "config.json")
        try:
            from src.browser import open_page
            from src.attendance import run_attendance
            logger.info("段階=ブラウザ起動")
            with open_page(config) as page:
                result = run_attendance(page, config, student_id, password, room, route, logger)
        finally:
            del password
        logger.info("結果=%s 所要時間=%.1f秒", result, time.monotonic() - started)
        print(f"結果: {result}")
        return 0
    except (ConfigurationError, CredentialError, AttendanceError, RoomError) as exc:
        logger.error("失敗 種別=%s", type(exc).__name__)
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
