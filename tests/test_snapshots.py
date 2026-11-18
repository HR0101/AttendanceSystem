"""匿名の画面データで、対象の照合と安全な停止を確認する。"""

import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from src.attendance import run_attendance
from src.room import RoomError, parse_room
from src.verification import (
    AttendanceError, enrollment_warning, is_attended, no_active_class,
    require_student_identity, require_target,
)


CONFIG = {
    "login_url": "https://attendance.example.test/attendance/login",
    "top_url": "https://attendance.example.test/attendance/top",
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
        self.assertEqual(require_target(page)["授業名"], "テスト授業")
        self.assertFalse(is_attended(page))
        self.assertFalse(enrollment_warning(page))

    def test_attended_page(self):
        page = self.page_for(attendance_html(attended=True))
        require_target(page)
        self.assertTrue(is_attended(page))

    def test_unenrolled_page(self):
        page = self.page_for(attendance_html(unenrolled=True))
        require_target(page)
        self.assertTrue(enrollment_warning(page))
        self.assertFalse(is_attended(page))

    def test_no_active_class_message(self):
        page = self.page_for('<div class="panel active">現在、出席できる授業はありません。</div>')
        self.assertTrue(no_active_class(page))
        page.set_content('<div class="panel active">画面の仕様が変更されました。</div>')
        self.assertFalse(no_active_class(page))

    def run_with_mocked_page(self, html, expected_course=None, expected_start=None, *, room="731", route_id="7301", config=None, on_snapshot=None, expected_values=None, posted_html=None, redirect_to_top=False):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        attempted_posts = []

        def serve(route):
            if route.request.method != "GET":
                attempted_posts.append(route.request.url)
                if posted_html is not None:
                    route.fulfill(status=200, body=posted_html, content_type="text/html; charset=utf-8")
                else:
                    route.abort()
            elif route.request.url.endswith(f"/attendance/class_room/{route_id}"):
                if redirect_to_top:
                    # Playwright のHTTPリダイレクトは後続リクエストに route が
                    # 適用されないため、通信せずに遷移後のURLを再現する。
                    route.fulfill(status=200, body=html + "<script>history.replaceState(null, '', '/attendance/top');</script>", content_type="text/html; charset=utf-8")
                else:
                    route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
            elif redirect_to_top and route.request.url == CONFIG["top_url"]:
                route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
            else:
                route.abort()

        page.route("**/*", serve)
        result = run_attendance(
            page, CONFIG if config is None else config, "TEST001", "dummy", room, route_id, Mock(),
            expected_course, expected_start,
            on_snapshot=on_snapshot, expected_values=expected_values,
        )
        return result, attempted_posts

    def test_unenrolled_page_never_clicks_attendance(self):
        result, posts = self.run_with_mocked_page(attendance_html(unenrolled=True))
        self.assertEqual(result, "履修登録なし（出席操作なし）")
        self.assertEqual(posts, [])

    def test_scheduled_course_mismatch_stops_before_click(self):
        with self.assertRaises(AttendanceError):
            self.run_with_mocked_page(attendance_html(), "別の授業", "10:00")

    def test_login_id_letter_prefix_matches_displayed_student_id(self):
        page = self.page_for(attendance_html().replace("TEST001", "99X0000"))
        for prefix in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz":
            with self.subTest(prefix=prefix):
                require_student_identity(page, prefix + "99X0000")
        require_student_identity(page, "99X0000")
        for login_id in ("K99X0001", "T99X0001", "Y99X0001", "199X0000", "KT99X0000"):
            with self.subTest(login_id=login_id), self.assertRaises(AttendanceError):
                require_student_identity(page, login_id)

    def test_qr_number_can_differ_from_classroom(self):
        html = attendance_html().replace("７３１講義室", "４３２講義室")
        snapshot = Mock()
        result, posts = self.run_with_mocked_page(html, room="435", route_id="435", config=dict(CONFIG, dry_run=True), on_snapshot=snapshot)
        self.assertEqual(result, "未出席（確認のみ）")
        self.assertEqual(snapshot.call_args.args[0]["教室名"], "432講義室")
        self.assertEqual(posts, [])

    def test_registration_with_different_qr_and_classroom(self):
        html = attendance_html().replace("７３１講義室", "４３２講義室")
        values = require_target(self.page_for(html))
        html = html.replace('<button id="attend">', '<button type="button" id="attend" onclick="document.querySelector(\'#confirmModal\').hidden=false">')
        html += '<div id="confirmModal" hidden><div class="dialog_body">テスト授業 2-3限 432講義室 10:00 12:00</div><form method="post"><button id="ok_confirmModal">登録</button></form></div>'
        posted_html = attendance_html(attended=True).replace("７３１講義室", "４３２講義室")
        result, posts = self.run_with_mocked_page(html, room="435", route_id="435", expected_values=values, posted_html=posted_html)
        self.assertEqual(result, "出席登録成功")
        self.assertEqual(posts, [CONFIG["top_url"].replace("/top", "/class_room/435")])

    def test_changed_classroom_stops_before_registration(self):
        html = attendance_html().replace("７３１講義室", "４３２講義室")
        values = require_target(self.page_for(html))
        changed = html.replace("４３２講義室", "４３３講義室")
        with self.assertRaisesRegex(AttendanceError, "授業情報が変更"):
            self.run_with_mocked_page(changed, room="435", route_id="435", expected_values=values)

    def test_attended_classroom_redirected_to_top_updates_snapshot(self):
        html = attendance_html(attended=True).replace("７３１講義室", "４３２講義室")
        snapshot = Mock()
        result, posts = self.run_with_mocked_page(html, room="435", route_id="435", on_snapshot=snapshot, redirect_to_top=True)
        self.assertEqual(result, "出席済み")
        self.assertEqual(snapshot.call_args.args[0]["教室名"], "432講義室")
        self.assertTrue(snapshot.call_args.args[1])
        self.assertEqual(posts, [])

    def test_unattended_classroom_redirected_to_top_can_be_checked(self):
        snapshot = Mock()
        result, posts = self.run_with_mocked_page(attendance_html(), config=dict(CONFIG, dry_run=True), on_snapshot=snapshot, redirect_to_top=True)
        self.assertEqual(result, "未出席（確認のみ）")
        self.assertFalse(snapshot.call_args.args[1])
        self.assertEqual(posts, [])

    def test_redirect_to_top_without_login_still_re_navigates(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        expected_url = CONFIG["top_url"].replace("/top", "/class_room/7301")
        visited = []

        def serve(route):
            visited.append(route.request.url)
            if route.request.url == expected_url:
                body = attendance_html() + ("<script>history.replaceState(null, '', '/attendance/top');</script>" if len(visited) == 1 else "")
                route.fulfill(status=200, body=body, content_type="text/html; charset=utf-8")
            else:
                route.abort()

        page.route("**/*", serve)
        logger = Mock()
        with patch.object(page, "goto", wraps=page.goto) as goto_spy:
            result = run_attendance(page, dict(CONFIG, dry_run=True), "TEST001", "dummy", "731", "7301", logger)
        self.assertEqual(result, "未出席（確認のみ）")
        self.assertEqual(goto_spy.call_count, 2)
        self.assertEqual(visited, [expected_url, expected_url])
        self.assertIn("段階=教室画面を再表示", [call.args[0] for call in logger.info.call_args_list])

    def test_changed_course_after_redirect_still_stops(self):
        with self.assertRaisesRegex(AttendanceError, "予定した授業名"):
            self.run_with_mocked_page(attendance_html(), expected_course="別の授業", redirect_to_top=True)

    def test_already_logged_in_skips_redundant_second_goto(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        get_urls = []

        def serve(route):
            get_urls.append((route.request.method, route.request.url))
            if route.request.method == "GET" and route.request.url.endswith("/attendance/class_room/7301"):
                route.fulfill(status=200, body=attendance_html(), content_type="text/html; charset=utf-8")
            else:
                route.abort()

        page.route("**/*", serve)
        logger = Mock()
        with patch.object(page, "goto", wraps=page.goto) as goto_spy:
            result = run_attendance(page, dict(CONFIG, dry_run=True), "TEST001", "dummy", "731", "7301", logger)
        expected_url = CONFIG["top_url"].replace("/top", "/class_room/7301")
        self.assertEqual(result, "未出席（確認のみ）")
        goto_spy.assert_called_once_with(expected_url, wait_until="domcontentloaded")
        self.assertEqual(get_urls, [("GET", expected_url)])
        logged_stages = [call.args[0] for call in logger.info.call_args_list]
        self.assertNotIn("段階=教室画面を再表示", logged_stages)

    def test_redirect_or_login_re_navigates_to_room_url(self):
        expected_url = CONFIG["top_url"].replace("/top", "/class_room/7301")
        for post_login_action in ("/attendance/top", "/attendance/class_room/7301"):
            with self.subTest(post_login_action=post_login_action):
                page = self.browser.new_page()
                self.addCleanup(page.close)
                authenticated = False
                get_urls = []

                login_html = f"""<!doctype html><html lang="ja"><body>
                    <script>history.replaceState(null, '', '/attendance/login');</script>
                    <form method="post" action="{post_login_action}">
                      <input id="userid" name="username">
                      <input id="password" name="password">
                      <button name="login" disabled>ログイン</button>
                    </form>
                    <script>document.querySelector('#password').addEventListener('keyup',
                      () => document.querySelector('button[name=login]').disabled = false);</script>
                </body></html>"""

                def serve(route):
                    nonlocal authenticated
                    if route.request.method == "POST" and route.request.url.endswith(post_login_action):
                        authenticated = True
                        route.fulfill(status=200, body=attendance_html(), content_type="text/html; charset=utf-8")
                    elif route.request.method == "GET" and route.request.url == expected_url:
                        get_urls.append(route.request.url)
                        body = attendance_html() if authenticated else login_html
                        route.fulfill(status=200, body=body, content_type="text/html; charset=utf-8")
                    else:
                        route.abort()

                page.route("**/*", serve)
                logger = Mock()
                with patch.object(page, "goto", wraps=page.goto) as goto_spy:
                    result = run_attendance(page, dict(CONFIG, dry_run=True), "TEST001", "dummy", "731", "7301", logger)
                self.assertEqual(result, "未出席（確認のみ）")
                self.assertEqual(goto_spy.call_count, 2)
                self.assertEqual(get_urls, [expected_url, expected_url])
                logged_stages = [call.args[0] for call in logger.info.call_args_list]
                self.assertIn("段階=教室画面を再表示", logged_stages)

    def test_wrong_url_stops(self):
        from src.attendance import _require_known_page
        page = Mock(url=CONFIG["top_url"].replace("/top", "/class_room/432"))
        with self.assertRaises(AttendanceError):
            _require_known_page(page, CONFIG, "435")
        page.url = CONFIG["top_url"]
        _require_known_page(page, CONFIG, "435")
        page.url = "https://unexpected.test/attendance/top"
        with self.assertRaises(AttendanceError):
            _require_known_page(page, CONFIG, "435")

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
