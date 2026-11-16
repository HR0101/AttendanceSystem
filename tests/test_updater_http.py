"""Issue #10〜#12: 解除時刻、ETag、配布元とリダイレクトの検証。"""

from datetime import datetime, timezone
from email.utils import format_datetime
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

import test_updater as fixtures
from src import updater as u


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


class UpdateHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.UpdaterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.current = fixtures.manifest_for(self.fixture.archive(), fixtures.VERSION)

    def test_etag_saved_and_304_reuses_validated_manifest(self):
        with patch.object(u, "urlopen", return_value=Response(json.dumps(self.current).encode(), {"ETag": '"first"'})):
            self.assertIn("最新版", u.prepare_update(self.fixture.base, force=True))
        state = u.read_json(self.fixture.base / ".updates/check.json")
        self.assertEqual(state["etag"], '"first"')
        self.assertEqual(state["manifest"], self.current)
        error = HTTPError(u.MANIFEST_URL, 304, "Not Modified", {}, io.BytesIO())
        with patch.object(u, "urlopen", side_effect=error) as request:
            self.assertIn("最新版", u.prepare_update(self.fixture.base, force=True))
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0].get_header("If-none-match"), '"first"')
        self.assertTrue(error.fp.closed)
        self.assertEqual(u.fingerprint(self.fixture.base), self.fixture.original)

    def test_new_etag_and_manifest_replace_old_cache_and_download_pinned_zip(self):
        u.write_json(self.fixture.base / ".updates/check.json", {"etag": '"old"', "manifest": self.current})
        data = self.fixture.archive()
        with patch.object(u, "urlopen", side_effect=[Response(json.dumps(fixtures.manifest_for(data)).encode(), {"etag": '"new"'}), Response(data)]) as request:
            self.assertIn("ダウンロード", u.prepare_update(self.fixture.base, force=True))
        state = u.read_json(self.fixture.base / ".updates/check.json")
        self.assertEqual(state["etag"], '"new"')
        self.assertEqual(state["manifest"]["version"], fixtures.NEXT)
        self.assertEqual(request.call_args_list[0].args[0].get_header("If-none-match"), '"old"')
        self.assertIsNone(request.call_args_list[1].args[0].get_header("If-none-match"))
        self.assertIn("/v" + fixtures.NEXT + "/", request.call_args_list[1].args[0].full_url)

    def test_304_without_cache_retries_unconditionally_once(self):
        error = HTTPError(u.MANIFEST_URL, 304, "Not Modified", {}, io.BytesIO())
        with patch.object(u, "urlopen", side_effect=[error, Response(json.dumps(self.current).encode())]) as request:
            self.assertIn("最新版", u.prepare_update(self.fixture.base, force=True))
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(call.args[0].get_header("If-none-match") is None for call in request.call_args_list))
        with patch.object(u, "urlopen", side_effect=HTTPError(u.MANIFEST_URL, 304, "Not Modified", {}, io.BytesIO())) as request:
            with self.assertRaisesRegex(u.UpdateError, "キャッシュがありません"):
                u.prepare_update(self.fixture.base, force=True)
            self.assertEqual(request.call_count, 2)

    def test_invalid_cached_etag_or_manifest_is_not_sent(self):
        for changes in ({"etag": 'bad\r\nheader', "manifest": self.current}, {"etag": '"old"', "manifest": []},
                        {"etag": '"bad\x00tag"', "manifest": self.current}, {"etag": '"タグ"', "manifest": self.current}):
            with self.subTest(changes=changes):
                u.write_json(self.fixture.base / ".updates/check.json", changes)
                with patch.object(u, "urlopen", return_value=Response(json.dumps(self.current).encode())) as request:
                    u.prepare_update(self.fixture.base, force=True)
                self.assertIsNone(request.call_args.args[0].get_header("If-none-match"))
                self.assertNotIn("etag", u.read_json(self.fixture.base / ".updates/check.json"))

    def test_rate_limit_messages_distinguish_permission_rejection_and_show_reset(self):
        now = 1800000000
        with patch.object(u.time, "time", return_value=now):
            limited = HTTPError(u.MANIFEST_URL, 403, "", {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(now + 300)}, None)
            self.assertEqual(u.rate_limit_retry_at(limited), now + 300)
            message = u.update_failure_message(limited)
            self.assertIn("利用制限中", message)
            self.assertIn(datetime.fromtimestamp(now + 300).astimezone().strftime("%H:%M:%S"), message)
            rejected = HTTPError(u.MANIFEST_URL, 403, "", {}, None)
            self.assertIsNone(u.rate_limit_retry_at(rejected))
            self.assertIn("アクセスが拒否", u.update_failure_message(rejected))
            self.assertNotIn("制限", u.update_failure_message(rejected))

    def test_retry_after_seconds_http_date_and_invalid_headers(self):
        now = 1800000000
        cases = [({"Retry-After": "120"}, now + 120),
                 ({"Retry-After": format_datetime(datetime.fromtimestamp(now + 240, timezone.utc), usegmt=True)}, now + 240),
                 ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(now + 300), "Retry-After": "600"}, now + 600),
                 ({"Retry-After": "invalid"}, now + 60),
                 ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9" * 100}, now + 60),
                 ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(now - 1)}, now + 60)]
        with patch.object(u.time, "time", return_value=now):
            for headers, expected in cases:
                with self.subTest(headers=headers):
                    self.assertEqual(u.rate_limit_retry_at(HTTPError(u.MANIFEST_URL, 429, "", headers, None)), expected)

    def test_background_waits_until_reset_instead_of_retrying_early_or_waiting_an_hour(self):
        now = 1800000000
        error = HTTPError(u.MANIFEST_URL, 403, "", {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(now + 300)}, None)
        with patch.object(u.time, "time", return_value=now) as clock:
            with patch.object(u, "fetch", side_effect=error) as request:
                with self.assertRaises(HTTPError):
                    u.prepare_update(self.fixture.base, force=True)
                self.assertIsNone(u.prepare_update(self.fixture.base))
                request.assert_called_once()
            self.assertEqual(u.read_json(self.fixture.base / ".updates/check.json")["retry_at"], now + 300)
            clock.return_value = now + 301
            with patch.object(u, "fetch", return_value=json.dumps(self.current).encode()) as request:
                self.assertIn("最新版", u.prepare_update(self.fixture.base))
                request.assert_called_once()

    def test_zip_download_rate_limit_also_records_retry_time(self):
        error = HTTPError(u.MANIFEST_URL, 429, "", {"Retry-After": "180"}, None)
        with patch.object(u.time, "time", return_value=1800000000), \
                patch.object(u, "fetch", side_effect=[json.dumps(fixtures.manifest_for(self.fixture.archive())).encode(), error]):
            with self.assertRaises(HTTPError):
                u.prepare_update(self.fixture.base, force=True)
        self.assertEqual(u.read_json(self.fixture.base / ".updates/check.json")["retry_at"], 1800000180)

    def test_initial_urls_and_every_redirect_reject_untrusted_hosts_and_http(self):
        invalid = ("http://github.com/file", "https://github.com.evil.test/file", "https://api.github.com/file",
                   "https://user:pass@github.com/file", "https://github.com:444/file", "https://[bad", "file:///tmp/file")
        handler = u.DownloadRedirectHandler()
        for url in invalid:
            with self.subTest(url=url):
                with patch.object(u, "urlopen") as request, self.assertRaises(u.UpdateError):
                    u.fetch(url, 100, u.time.monotonic() + 10)
                request.assert_not_called()
                with self.assertRaises(u.UpdateError):
                    handler.redirect_request(Request(u.MANIFEST_URL), None, 302, "Found", {}, url)
        for host in u.DOWNLOAD_HOSTS:
            url = f"https://{host}/file?signature=example"
            with self.subTest(host=host):
                redirected = handler.redirect_request(Request(u.MANIFEST_URL), None, 302, "Found", {}, url)
                self.assertEqual(redirected.full_url, url)


if __name__ == "__main__":
    unittest.main()
