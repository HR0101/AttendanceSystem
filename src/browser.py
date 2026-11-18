"""Playwright のライフサイクル。"""

import json
import os
import secrets
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

_SERVER_START_TIMEOUT = 15.0
_MAX_WS_LINE_BYTES = 4096
_POOL_LOCK = threading.Lock()
_POOL_STACK: list["_BrowserPool"] = []


def _server_key(config: dict) -> tuple[bool, tuple[tuple[str, object], ...]]:
    browser_cfg = config["browser"]
    extra = tuple(sorted((key, value) for key, value in browser_cfg.items() if key not in ("headless", "timeout_ms")))
    return bool(browser_cfg["headless"]), extra


def _stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        # まだ回収していない間だけプロセスグループを停止し、PID再利用への誤送信を防ぐ。
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
    finally:
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass


def _read_ws_endpoint(proc: subprocess.Popen, expected_ws_path: str, timeout: float) -> str | None:
    if proc.stdout is None:
        return None
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while b"\n" not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if not selector.select(timeout=remaining):
                return None
            chunk = os.read(fd, 4096)
            if not chunk:
                return None
            buffer.extend(chunk)
            if len(buffer) > _MAX_WS_LINE_BYTES and b"\n" not in buffer:
                return None
    try:
        line = buffer.split(b"\n", 1)[0].decode("utf-8", errors="strict").strip()
        parsed = urlparse(line)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "ws"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.path != expected_ws_path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return line


class _BrowserPool:
    """同一設定のワーカー間で単一の Chromium サーバープロセスを共有する。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = False
        self._closed = False
        self._failed = False
        self._server_key: tuple[bool, tuple[tuple[str, object], ...]] | None = None
        self._endpoint: str | None = None
        self._proc: subprocess.Popen | None = None
        self._temp_dir: str | None = None

    def get_endpoint(self, config: dict) -> str | None:
        key = _server_key(config)
        with self._lock:
            if self._closed:
                return None
            if not self._started:
                self._started = True
                self._server_key = key
                self._endpoint = self._start_server(key[0])
                if self._endpoint is None:
                    self._failed = True
                return self._endpoint
            if self._failed or key != self._server_key:
                return None
            if self._proc is None or self._proc.poll() is not None:
                self._failed = True
                return None
            return self._endpoint

    def _start_server(self, headless: bool) -> str | None:
        try:
            from playwright._impl._driver import compute_driver_executable, get_driver_env

            temp_dir = tempfile.mkdtemp(prefix="att-browser-")
            os.chmod(temp_dir, 0o700)
            self._temp_dir = temp_dir
            ws_path = "/" + secrets.token_urlsafe(24)
            config_path = Path(temp_dir) / "launch-server.json"
            fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"headless": headless, "host": "127.0.0.1", "port": 0, "wsPath": ws_path}, handle)
            driver_executable, driver_cli = compute_driver_executable()
            proc = subprocess.Popen(
                [driver_executable, driver_cli, "launch-server", "--browser", "chromium", "--config", str(config_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=get_driver_env(),
                start_new_session=True,
            )
            self._proc = proc
            endpoint = _read_ws_endpoint(proc, ws_path, _SERVER_START_TIMEOUT)
            if endpoint is None or proc.poll() is not None:
                self._cleanup_locked()
                return None
            return endpoint
        except Exception:
            self._cleanup_locked()
            return None

    def _cleanup_locked(self) -> None:
        proc, self._proc = self._proc, None
        temp_dir, self._temp_dir = self._temp_dir, None
        self._endpoint = None
        _stop_process(proc)
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cleanup_locked()


def _current_pool() -> _BrowserPool | None:
    with _POOL_LOCK:
        return _POOL_STACK[-1] if _POOL_STACK else None


@contextmanager
def browser_pool():
    """複数ユーザー処理の間、単一の Chromium サーバーを共有する。"""
    pool = _BrowserPool()
    with _POOL_LOCK:
        _POOL_STACK.append(pool)
    try:
        yield
    finally:
        with _POOL_LOCK:
            if pool in _POOL_STACK:
                _POOL_STACK.remove(pool)
        pool.close()


def _open_browser(playwright, headless: bool, timeout_ms: int, ws_endpoint: str | None):
    if ws_endpoint:
        try:
            return playwright.chromium.connect(ws_endpoint, timeout=timeout_ms)
        except Exception:
            pass
    return playwright.chromium.launch(headless=headless)


@contextmanager
def open_page(config: dict):
    from playwright.sync_api import sync_playwright

    headless = config["browser"]["headless"]
    timeout_ms = config["browser"]["timeout_ms"]
    pool = _current_pool()
    ws_endpoint = pool.get_endpoint(config) if pool is not None else None

    with sync_playwright() as playwright:
        browser = _open_browser(playwright, headless, timeout_ms, ws_endpoint)
        context = None
        try:
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            yield page
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            try:
                browser.close()
            except Exception:
                pass
