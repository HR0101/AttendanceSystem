"""機密情報を出力しない、限定された実行ログ。"""

import logging
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path

from .config import tighten_fd_mode


def _ensure_private_entry(path: Path, *, directory: bool, mode: int, flags: int, label: str) -> float:
    if path.is_symlink():
        raise OSError(f"{label} にシンボリックリンクは使用できません。")
    fd = os.open(path, flags | os.O_NOFOLLOW, mode)
    try:
        st = os.fstat(fd)
        valid = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
        if not valid:
            raise OSError(f"{label} の種別を確認してください。")
        tighten_fd_mode(fd, mode)
        return st.st_mtime
    finally:
        os.close(fd)


def setup_logging(base: Path) -> logging.Logger:
    log_dir = base / "logs"
    if log_dir.is_symlink():
        raise OSError("logs ディレクトリにシンボリックリンクは使用できません。")
    log_dir.mkdir(mode=0o700, exist_ok=True)
    _ensure_private_entry(log_dir, directory=True, mode=0o700, flags=os.O_RDONLY, label="logs ディレクトリ")
    cutoff = datetime.now().timestamp() - timedelta(days=30).total_seconds()
    for old in log_dir.glob("attendance-*.log"):
        mtime = _ensure_private_entry(old, directory=False, mode=0o600, flags=os.O_RDONLY, label=old.name)
        if mtime < cutoff:
            old.unlink()
    logfile = log_dir / f"attendance-{datetime.now():%Y-%m-%d}.log"
    _ensure_private_entry(logfile, directory=False, mode=0o600, flags=os.O_CREAT | os.O_WRONLY | os.O_APPEND, label=logfile.name)
    logger = logging.getLogger("attendance_tool")
    logger.setLevel(logging.INFO)
    for existing in list(logger.handlers):
        existing.close()
    logger.handlers.clear()
    handler = logging.FileHandler(logfile, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger
