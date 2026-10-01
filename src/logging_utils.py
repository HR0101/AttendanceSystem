"""機密情報を出力しない、限定された実行ログ。"""

import logging
from datetime import datetime, timedelta
from pathlib import Path


def setup_logging(base: Path) -> logging.Logger:
    log_dir = base / "logs"
    log_dir.mkdir(mode=0o700, exist_ok=True)
    for old in log_dir.glob("attendance-*.log"):
        if datetime.now().timestamp() - old.stat().st_mtime > timedelta(days=30).total_seconds():
            old.unlink()
    logfile = log_dir / f"attendance-{datetime.now():%Y-%m-%d}.log"
    logfile.touch(mode=0o600, exist_ok=True)
    logger = logging.getLogger("attendance_tool")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(logfile, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger
