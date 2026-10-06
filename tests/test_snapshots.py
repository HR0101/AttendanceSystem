"""匿名の画面データで、対象の照合と安全な停止を確認する。"""

import unittest
from unittest.mock import Mock

from playwright.sync_api import sync_playwright

from src.attendance import run_attendance
from src.room import RoomError, parse_room
from src.verification import (
    AttendanceError, enrollment_warning, is_attended, no_active_class,
    require_student_identity, require_target,
)


CONFIG = {
    "login_url": "https://attendance.is.chibatech.ac.jp/attendance/login",
    "top_url": "https://attendance.is.it-chiba.ac.jp/attendance/top",
    "dry_run": False,
}


def attendance_html(*, attended=False, unenrolled=False):
    warning = (
        '<p class="message">システム上に上記授業の履修が登録されていないため仮登録となります。'
        '履修が確定すると出席が確定します。</p>' if unenrolled else ""
    )
    button = '<button disabled>出席済</button>' if attended else '<button id="attend">出席で登録する</button>'
    return f"""<!doctype html><html lang="ja"><body>
        <div class="style_text_login_user">学籍番号：TEST001 でログイン中</div>
        <div class="panel active"><div class="main"><div class="container">
          <table class="table_list">
            <tr><td>授業名：</td><td>テスト授業</td></tr>
            <tr><td>時限：</td><td>2-3限</td></tr>
            <tr><td>教室名：</td><td>７３１講義室</td></tr>
            <tr><td>開始時刻：</td><td>10:00</td></tr>
            <tr><td>終了時刻：</td><td>12:00</td></tr>
          </table>
          {warning}<form id="attendForm">{button}</form>
        </div></div></div>
    </body></html>"""


class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def page_for(self, html):
        page = self.browser.new_page()
        page.set_content(html)
        self.addCleanup(page.close)
        return page

    def test_unattended_page(self):
        page = self.page_for(attendance_html())
        self.assertEqual(require_target(page, "731")["授業名"], "テスト授業")
        self.assertFalse(is_attended(page))
        self.assertFalse(enrollment_warning(page))

    def test_attended_page(self):
        page = self.page_for(attendance_html(attended=True))
        require_target(page, "731")
        self.assertTrue(is_attended(page))

    def test_unenrolled_page(self):
        page = self.page_for(attendance_html(unenrolled=True))
        require_target(page, "731")
        self.assertTrue(enrollment_warning(page))
        self.assertFalse(is_attended(page))

    def test_no_active_class_message(self):
        page = self.page_for('<div class="panel active">現在、出席できる授業はありません。</div>')
        self.assertTrue(no_active_class(page))
        page.set_content('<div class="panel active">画面の仕様が変更されました。</div>')
        self.assertFalse(no_active_class(page))

    def run_with_mocked_page(self, html, expected_course=None, expected_start=None):
        page = self.browser.new_page()
        self.addCleanup(page.close)
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
        result = run_attendance(
            page, CONFIG, "TEST001", "dummy", "731", "7301", Mock(),
            expected_course, expected_start,
        )
        return result, attempted_posts

    def test_unenrolled_page_never_clicks_attendance(self):
        result, posts = self.run_with_mocked_page(attendance_html(unenrolled=True))
        self.assertEqual(result, "履修登録なし（出席操作なし）")
        self.assertEqual(posts, [])

    def test_scheduled_course_mismatch_stops_before_click(self):
        with self.assertRaises(AttendanceError):
            self.run_with_mocked_page(attendance_html(), "別の授業", "10:00")

    def test_login_id_k_prefix_matches_displayed_student_id(self):
        page = self.page_for(attendance_html().replace("TEST001", "99X0000"))
        require_student_identity(page, "K99X0000")
        require_student_identity(page, "99X0000")
        with self.assertRaises(AttendanceError):
            require_student_identity(page, "K99X0001")

    def test_wrong_room_stops(self):
        page = self.page_for(attendance_html())
        with self.assertRaises(AttendanceError):
            require_target(page, "642")

    def test_login_button_requires_keyup(self):
        page = self.page_for("""<input id="userid" name="username">
            <input id="password" name="password">
            <button name="login" disabled>ログイン</button>
            <script>document.querySelector('#password').addEventListener('keyup',
              () => document.querySelector('button[name=login]').disabled = false);</script>""")
        self.assertEqual(page.locator("#userid[name=username]").count(), 1)
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
