"""browser_pool と open_page の分離・共有・フォールバック・後片付けの検証。"""

import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import src.browser as browser_mod
from src.browser import browser_pool, open_page

CONFIG = {"browser": {"headless": True, "timeout_ms": 15000}}


class BrowserPoolTests(unittest.TestCase):
    def test_browser_pool_shares_single_server_isolates_contexts_and_cleans_up(self):
        real_popen = subprocess.Popen
        spawned = []
        temp_dirs = []

        def tracking_popen(cmd, *args, **kwargs):
            proc = real_popen(cmd, *args, **kwargs)
            spawned.append((cmd, kwargs, proc))
            if "--config" in cmd:
                temp_dirs.append(Path(cmd[cmd.index("--config") + 1]).parent)
            return proc

        barrier = threading.Barrier(2)
        results = {}
        errors = []

        def worker(name: str):
            try:
                with open_page(CONFIG) as page:
                    page.route(
                        "https://attendance.example.test/*",
                        lambda route: route.fulfill(
                            status=200,
                            body=f"<!doctype html><html><body>{name}</body></html>",
                            content_type="text/html; charset=utf-8",
                        ),
                    )
                    page.context.add_cookies([
                        {"name": "session_user", "value": name, "url": "https://attendance.example.test"}
                    ])
                    page.goto("https://attendance.example.test/check")
                    page.evaluate("value => localStorage.setItem('who', value)", name)
                    barrier.wait(timeout=10)
                    cookies = {c["name"]: c["value"] for c in page.context.cookies("https://attendance.example.test")}
                    stored = page.evaluate("localStorage.getItem('who')")
                    results[name] = (cookies.get("session_user"), stored)
            except Exception as exc:
                errors.append(exc)

        from playwright.sync_api import BrowserType

        with patch("src.browser.subprocess.Popen", side_effect=tracking_popen), \
                patch.object(BrowserType, "launch", side_effect=AssertionError("共有プール時は launch へフォールバックしない")):
            with browser_pool():
                threads = [threading.Thread(target=worker, args=(name,)) for name in ("user_a", "user_b")]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=20)
                server_spawns = [entry for entry in spawned if "launch-server" in entry[0]]
                self.assertEqual(len(server_spawns), 1, "共有 Chromium サーバーは 1 プロセスのみ起動する")
                cmd, kwargs, proc = server_spawns[0]
                self.assertIn("launch-server", cmd)
                self.assertTrue(kwargs.get("start_new_session"))
                config_file = Path(cmd[cmd.index("--config") + 1])
                self.assertEqual(temp_dirs[0].stat().st_mode & 0o777, 0o700)
                self.assertEqual(config_file.stat().st_mode & 0o777, 0o600)
                self.assertIsNone(proc.poll())

        self.assertEqual(errors, [])
        self.assertEqual(results, {"user_a": ("user_a", "user_a"), "user_b": ("user_b", "user_b")})
        self.assertIsNotNone(proc.poll(), "プール終了後にサーバープロセスが停止していること")
        self.assertFalse(temp_dirs[0].exists(), "一時ディレクトリが削除されていること")
        with self.assertRaises(OSError):
            os.killpg(proc.pid, 0)

    def test_browser_pool_is_lazy_when_open_page_not_called(self):
        with patch("src.browser.subprocess.Popen") as popen:
            with browser_pool():
                pass
        popen.assert_not_called()

    def test_browser_pool_falls_back_to_launch_when_server_exits(self):
        real_popen = subprocess.Popen
        spawned_dirs = []

        def failing_popen(cmd, *args, **kwargs):
            if "--config" in cmd:
                spawned_dirs.append(Path(cmd[cmd.index("--config") + 1]).parent)
            return real_popen(
                [sys.executable, "-c", "import sys; sys.exit(1)"],
                stdin=kwargs.get("stdin"),
                stdout=kwargs.get("stdout"),
                stderr=kwargs.get("stderr"),
                start_new_session=kwargs.get("start_new_session", True),
            )

        fake_page = Mock()
        fake_context = Mock()
        fake_context.new_page.return_value = fake_page
        fake_browser = Mock()
        fake_browser.new_context.return_value = fake_context
        fake_pw = Mock()
        fake_pw.chromium.launch.return_value = fake_browser
        fake_sync_cm = Mock(__enter__=Mock(return_value=fake_pw), __exit__=Mock(return_value=False))

        with patch("src.browser.subprocess.Popen", side_effect=failing_popen) as popen, \
                patch("playwright.sync_api.sync_playwright", return_value=fake_sync_cm):
            with browser_pool():
                with open_page(CONFIG) as page1:
                    self.assertIs(page1, fake_page)
                with open_page(CONFIG) as page2:
                    self.assertIs(page2, fake_page)

        self.assertEqual(popen.call_count, 1)
        fake_pw.chromium.connect.assert_not_called()
        self.assertEqual(fake_pw.chromium.launch.call_count, 2)
        self.assertFalse(spawned_dirs[0].exists())

    def test_browser_pool_kills_timed_out_server_and_falls_back(self):
        real_popen = subprocess.Popen
        procs = []
        spawned_dirs = []

        def hanging_popen(cmd, *args, **kwargs):
            if "--config" in cmd:
                spawned_dirs.append(Path(cmd[cmd.index("--config") + 1]).parent)
            proc = real_popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                stdin=kwargs.get("stdin"),
                stdout=kwargs.get("stdout"),
                stderr=kwargs.get("stderr"),
                start_new_session=kwargs.get("start_new_session", True),
            )
            procs.append(proc)
            return proc

        fake_pw = Mock()
        fake_pw.chromium.launch.return_value = Mock(new_context=Mock(return_value=Mock(new_page=Mock(return_value=Mock()))))
        fake_sync_cm = Mock(__enter__=Mock(return_value=fake_pw), __exit__=Mock(return_value=False))

        with patch.object(browser_mod, "_SERVER_START_TIMEOUT", 0.2), \
                patch("src.browser.subprocess.Popen", side_effect=hanging_popen), \
                patch("playwright.sync_api.sync_playwright", return_value=fake_sync_cm):
            with browser_pool():
                with open_page(CONFIG):
                    pass

        self.assertEqual(len(procs), 1)
        self.assertIsNotNone(procs[0].poll())
        self.assertFalse(spawned_dirs[0].exists())
        fake_pw.chromium.launch.assert_called_once_with(headless=True)

    def test_browser_pool_falls_back_when_headless_differs_and_shares_when_timeout_differs(self):
        fake_pw = Mock()
        fake_pw.chromium.connect.return_value = Mock(new_context=Mock(return_value=Mock(new_page=Mock(return_value=Mock()))))
        fake_pw.chromium.launch.return_value = Mock(new_context=Mock(return_value=Mock(new_page=Mock(return_value=Mock()))))
        fake_sync_cm = Mock(__enter__=Mock(return_value=fake_pw), __exit__=Mock(return_value=False))

        with patch.object(browser_mod._BrowserPool, "_start_server", return_value="ws://127.0.0.1:44444/tok"), \
                patch("playwright.sync_api.sync_playwright", return_value=fake_sync_cm):
            with browser_pool():
                pool = browser_mod._current_pool()
                with patch.object(pool, "_proc", Mock(poll=Mock(return_value=None))):
                    with open_page({"browser": {"headless": True, "timeout_ms": 10000}}):
                        pass
                    with open_page({"browser": {"headless": True, "timeout_ms": 25000}}):
                        pass
                    with open_page({"browser": {"headless": False, "timeout_ms": 10000}}):
                        pass

        self.assertEqual(fake_pw.chromium.connect.call_count, 2)
        fake_pw.chromium.launch.assert_called_once_with(headless=False)

    def test_open_page_falls_back_to_launch_when_connect_fails(self):
        fake_page = Mock()
        fake_context = Mock(new_page=Mock(return_value=fake_page))
        fake_browser = Mock(new_context=Mock(return_value=fake_context))
        fake_pw = Mock()
        fake_pw.chromium.connect.side_effect = RuntimeError("connect failed")
        fake_pw.chromium.launch.return_value = fake_browser
        fake_sync_cm = Mock(__enter__=Mock(return_value=fake_pw), __exit__=Mock(return_value=False))

        with patch.object(browser_mod._BrowserPool, "_start_server", return_value="ws://127.0.0.1:44444/tok"), \
                patch("playwright.sync_api.sync_playwright", return_value=fake_sync_cm):
            with browser_pool():
                pool = browser_mod._current_pool()
                with patch.object(pool, "_proc", Mock(poll=Mock(return_value=None))):
                    with open_page(CONFIG) as page:
                        self.assertIs(page, fake_page)

        fake_pw.chromium.connect.assert_called_once()
        fake_pw.chromium.launch.assert_called_once_with(headless=True)
        fake_context.close.assert_called_once_with()
        fake_browser.close.assert_called_once_with()

    def test_open_page_closes_context_and_browser_on_exception(self):
        fake_context = Mock()
        fake_context.new_page.return_value = Mock()
        fake_browser = Mock()
        fake_browser.new_context.return_value = fake_context
        fake_pw = Mock()
        fake_pw.chromium.launch.return_value = fake_browser
        fake_sync_cm = Mock(__enter__=Mock(return_value=fake_pw), __exit__=Mock(return_value=False))

        with patch("playwright.sync_api.sync_playwright", return_value=fake_sync_cm):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with open_page(CONFIG):
                    raise RuntimeError("boom")

        fake_context.close.assert_called_once_with()
        fake_browser.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
