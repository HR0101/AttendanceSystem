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

    def test_changed_course_after_redirect_still_stops(self):
        with self.assertRaisesRegex(AttendanceError, "予定した授業名"):
            self.run_with_mocked_page(attendance_html(), expected_course="別の授業", redirect_to_top=True)

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

    def run_login_with_mocked_page(self, failed_login_html, *, logger=None):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        login_form = """<!doctype html><html lang="ja"><body>
            <p>学籍番号とパスワードを入力してください。</p>
            <form method="post" action="/attendance/login">
              <input id="userid" name="username">
              <input id="password" name="password" type="password">
              <button name="login" disabled>ログイン</button>
            </form>
            <script>
              document.querySelector('#password').addEventListener('keyup',
                () => document.querySelector('button[name=login]').disabled = false);
            </script>
        </body></html>"""

        def serve(route):
            url = route.request.url
            if route.request.method == "POST" and url == CONFIG["login_url"]:
                route.fulfill(status=200, body=failed_login_html, content_type="text/html; charset=utf-8")
            elif url.endswith("/attendance/class_room/7301"):
                route.fulfill(
                    status=200,
                    body=login_form + "<script>history.replaceState(null, '', '/attendance/login');</script>",
                    content_type="text/html; charset=utf-8",
                )
            else:
                route.abort()

        page.route("**/*", serve)
        return run_attendance(page, CONFIG, "TEST001", "secret-pw", "731", "7301", logger or Mock())

    def test_error_modal_classification_and_pii_isolation(self):
        cases = (
            (
                '<div id="errorModal"><div class="dialog_title">メンテナンス</div>'
                '<div class="dialog_body">SECRET_ID_001 現在システムメンテナンス中です。</div></div>',
                "メンテナンス",
            ),
            (
                '<div id="errorModal"><h4>利用制限</h4>'
                '<p>ログイン試行回数の上限に達したためアカウントがロックされています。</p></div>',
                "ロック",
            ),
            (
                '<div id="errorModal"><div class="modal-title">セッション切れ</div>'
                '<div class="modal-body">セッションがタイムアウトしました。</div></div>',
                "セッション",
            ),
            (
                '<div id="errorModal"><div class="dialog_body">出席受付時間外のため登録できません。</div></div>',
                "受付時間外",
            ),
            (
                '<div id="errorModal"><div class="dialog_body">アクセス権限がありません。</div></div>',
                "アクセス権限",
            ),
        )
        for modal_html, expected_word in cases:
            with self.subTest(expected_word=expected_word):
                logger = Mock()
                page = self.browser.new_page()
                self.addCleanup(page.close)
                page.route(
                    "**/*",
                    lambda route, *, body=attendance_html() + modal_html: route.fulfill(
                        status=200, body=body, content_type="text/html; charset=utf-8"
                    ),
                )
                with self.assertRaises(AttendanceError) as ctx:
                    run_attendance(page, CONFIG, "TEST001", "dummy", "731", "7301", logger)
                self.assertIn(expected_word, str(ctx.exception))
                self.assertNotIn("SECRET_ID_001", str(ctx.exception))
                logged = repr(logger.mock_calls)
                self.assertNotIn("SECRET_ID_001", logged)
                self.assertNotIn("現在システムメンテナンス中", logged)

    def test_error_modal_unknown_text_never_leaks_and_works_without_login_header(self):
        logger = Mock()
        page = self.browser.new_page()
        self.addCleanup(page.close)
        sentinel = "LEAK_STUDENT_9999_SECRET_TOKEN"
        html = f'<div id="errorModal"><div class="dialog_title">謎エラー</div><div class="dialog_body">{sentinel}</div></div>'
        page.route(
            "**/*",
            lambda route: route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8"),
        )
        with self.assertRaises(AttendanceError) as ctx:
            run_attendance(page, CONFIG, "TEST001", "dummy", "731", "7301", logger)
        self.assertEqual(str(ctx.exception), "出席画面にエラーが表示されています。")
        self.assertNotIn(sentinel, str(ctx.exception))
        self.assertNotIn("謎エラー", str(ctx.exception))
        self.assertNotIn(sentinel, repr(logger.mock_calls))

    def test_post_registration_error_modal_is_classified_without_leaking(self):
        html = attendance_html()
        values = require_target(self.page_for(html))
        html = html.replace('<button id="attend">', '<button type="button" id="attend" onclick="document.querySelector(\'#confirmModal\').hidden=false">')
        html += '<div id="confirmModal" hidden><div class="dialog_body">テスト授業 2-3限 731講義室 10:00 12:00</div><form method="post"><button id="ok_confirmModal">登録</button></form></div>'

        with self.assertRaisesRegex(AttendanceError, "受付時間外"):
            self.run_with_mocked_page(
                html,
                expected_values=values,
                posted_html='<div id="errorModal"><p>受付終了のため登録できません。</p></div>',
            )

        sentinel = "SENSITIVE_POST_ERROR_XYZ"
        with self.assertRaises(AttendanceError) as ctx:
            self.run_with_mocked_page(
                html,
                expected_values=values,
                posted_html=f'<div id="errorModal"><p>{sentinel}</p></div>',
            )
        self.assertEqual(str(ctx.exception), "登録結果を確認できません。")
        self.assertNotIn(sentinel, str(ctx.exception))

        with self.assertRaisesRegex(AttendanceError, "セッション"):
            self.run_with_mocked_page(
                html,
                expected_values=values,
                posted_html=(
                    "<script>history.replaceState(null, '', '/attendance/login');</script>"
                    '<div class="alert">セッションがタイムアウトしました。</div>'
                ),
            )

    def test_login_failure_classification_and_safety(self):
        with self.assertRaisesRegex(AttendanceError, "メンテナンス"):
            self.run_login_with_mocked_page(
                '<div id="errorModal"><div class="dialog_title">保守作業</div><div class="dialog_body">現在メンテナンス中です。</div></div>'
            )

        with self.assertRaisesRegex(AttendanceError, "ロック"):
            self.run_login_with_mocked_page(
                '<div class="error">パスワード連続失敗によりアカウントがロックされています。</div>'
            )

        with self.assertRaisesRegex(AttendanceError, "セッション"):
            self.run_login_with_mocked_page(
                '<div role="alert">セッションがタイムアウトしました。再度ログインしてください。</div>'
            )

        with self.assertRaisesRegex(AttendanceError, "メンテナンス"):
            self.run_login_with_mocked_page(
                '<p>学籍番号とパスワードを入力してください。</p><h2>メンテナンスのお知らせ</h2>'
            )

        sentinel = "RAW_LOGIN_SECRET_ID_777"
        logger = Mock()
        with self.assertRaises(AttendanceError) as ctx:
            self.run_login_with_mocked_page(
                f'<div id="errorModal" hidden><div class="dialog_body">メンテナンス中です</div></div>'
                f'<p>学籍番号とパスワードを入力してください。</p>'
                f'<div class="error">{sentinel}</div>',
                logger=logger,
            )
        self.assertEqual(str(ctx.exception), "ログインを確認できません。IDまたはパスワードを確認してください。")
        self.assertNotIn(sentinel, str(ctx.exception))
        self.assertNotIn(sentinel, repr(logger.mock_calls))


if __name__ == "__main__":
    unittest.main()
