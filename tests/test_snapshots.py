"""提供された保存画面で、対象の照合と状態判定を確認する。"""

import unittest
import re
from unittest.mock import Mock
from pathlib import Path

from playwright.sync_api import sync_playwright

from src.room import RoomError, parse_room
from src.attendance import run_attendance
from src.verification import AttendanceError, enrollment_warning, is_attended, no_active_class, require_student_identity, require_target


ROOT = Path(__file__).resolve().parents[1]
class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def page_for(self, filename):
        page = self.browser.new_page()
        html = (ROOT / filename).read_text(encoding="utf-8").split('<div id="gemini-ai">')[0]
        page.set_content(html)
        self.addCleanup(page.close)
        return page

    def test_unattended_snapshot(self):
        page = self.page_for("出席確認画面未出席.html")
        self.assertEqual(require_target(page, "731")["授業名"], "感性情報処理 情工3年")
        self.assertFalse(is_attended(page))
        self.assertFalse(enrollment_warning(page))

    def test_attended_snapshot(self):
        page = self.page_for("出席確認画面出席済み.html")
        require_target(page, "731")
        self.assertTrue(is_attended(page))
        self.assertFalse(enrollment_warning(page))

    def test_unenrolled_snapshot(self):
        page = self.page_for("出席確認画面履修登録なし.html")
        require_target(page, "731")
        self.assertTrue(enrollment_warning(page))
        self.assertFalse(is_attended(page))

    def test_no_active_class_message(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_content('<div class="panel active">現在、出席できる授業はありません。</div>')
        self.assertTrue(no_active_class(page))
        page.set_content('<div class="panel active">画面の仕様が変更されました。</div>')
        self.assertFalse(no_active_class(page))

    def test_unenrolled_page_never_clicks_attendance(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        html = (ROOT / "出席確認画面履修登録なし.html").read_text(encoding="utf-8")
        html = re.sub(r"学籍番号：[^ ]+ でログイン中", "学籍番号：TEST001 でログイン中", html)
        attempted_posts = []

        def serve(route):
            if route.request.method != "GET":
                attempted_posts.append(route.request.url)
                route.abort()
            elif route.request.url.endswith("/attendance/class_room/7301"):
                route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
            else:
                route.abort()

        page.route("**/*", serve)
        config = {
            "login_url": "https://attendance.is.chibatech.ac.jp/attendance/login",
            "top_url": "https://attendance.is.it-chiba.ac.jp/attendance/top",
            "dry_run": False,
        }
        result = run_attendance(page, config, "TEST001", "dummy", "731", "7301", Mock())
        self.assertEqual(result, "履修登録なし（出席操作なし）")
        self.assertEqual(attempted_posts, [])

    def test_scheduled_course_mismatch_stops_before_click(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        html = (ROOT / "出席確認画面未出席.html").read_text(encoding="utf-8")
        html = re.sub(r"学籍番号：[^ ]+ でログイン中", "学籍番号：TEST001 でログイン中", html)
        attempted_posts = []

        def serve(route):
            if route.request.method != "GET":
                attempted_posts.append(route.request.url)
                route.abort()
            elif route.request.url.endswith("/attendance/class_room/7301"):
                route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
            else:
                route.abort()

        page.route("**/*", serve)
        config = {
            "login_url": "https://attendance.is.chibatech.ac.jp/attendance/login",
            "top_url": "https://attendance.is.it-chiba.ac.jp/attendance/top",
            "dry_run": False,
        }
        with self.assertRaises(AttendanceError):
            run_attendance(page, config, "TEST001", "dummy", "731", "7301", Mock(), "別の授業", "10:00")
        self.assertEqual(attempted_posts, [])

    def test_login_id_k_prefix_matches_displayed_student_id(self):
        page = self.page_for("出席確認画面履修登録なし.html")
        require_student_identity(page, "K24G1111")
        require_student_identity(page, "24G1111")
        with self.assertRaises(AttendanceError):
            require_student_identity(page, "K24G1112")

    def test_wrong_room_stops(self):
        page = self.page_for("出席確認画面未出席.html")
        with self.assertRaises(AttendanceError):
            require_target(page, "642")

    def test_login_snapshot(self):
        page = self.page_for("出席システムログイン.html")
        self.assertEqual(page.locator("#userid[name=username]").count(), 1)
        self.assertEqual(page.locator("#password[name=password]").count(), 1)
        self.assertEqual(page.locator("button[name=login]").count(), 1)

    def test_login_button_requires_keyup(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.goto((ROOT / "出席システムログイン.html").as_uri())
        page.locator("#userid").fill("TEST001")
        page.locator("#password").fill("dummy")
        self.assertTrue(page.locator("button[name=login]").is_disabled())
        page.locator("#password").press("End")
        self.assertFalse(page.locator("button[name=login]").is_disabled())

    def test_room_url_numbers(self):
        self.assertEqual(parse_room("642"), ("642", "642"))
        self.assertEqual(parse_room("731"), ("731", "7301"))
        self.assertEqual(parse_room("732"), ("732", "7302"))
        self.assertEqual(parse_room("741"), ("741", "7401"))
        with self.assertRaises(RoomError):
            parse_room("73a")


if __name__ == "__main__":
    unittest.main()
