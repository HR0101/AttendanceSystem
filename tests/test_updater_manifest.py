"""APIが利用できなくても公開JSONだけで更新できることを確認する。"""

import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import zipfile

import test_updater as fixtures
from src import updater as u


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.UpdaterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_build_emits_verifiable_json_outside_legacy_compatible_zip(self):
        from scripts.build_release import build
        archive = build("v" + fixtures.VERSION, Path(self.fixture.temp.name) / u.ASSET_NAME)
        manifest_path = archive.parent / u.MANIFEST_NAME
        manifest = u.validate_manifest(json.loads(manifest_path.read_text()))
        data = archive.read_bytes()
        self.assertEqual(manifest["version"], fixtures.VERSION)
        self.assertEqual(manifest["archive"], {"name": u.ASSET_NAME, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o644)
        with zipfile.ZipFile(archive) as package:
            self.assertNotIn(u.MANIFEST_NAME, package.namelist())
            self.assertTrue(all(u.allowed(name) for name in package.namelist()))
            self.assertTrue(set(self.fixture.private).isdisjoint(package.namelist()))
        self.assertNotIn("api.github.com", manifest_path.read_text())
        self.assertNotIn("private", manifest_path.read_text())

    def test_build_cli_creates_both_release_assets_with_standard_library(self):
        script = self.fixture.base / "scripts/build_release.py"
        script.parent.mkdir()
        shutil.copy2(fixtures.ROOT / "scripts/build_release.py", script)
        result = subprocess.run([sys.executable, str(script), "v" + fixtures.VERSION],
                                cwd=self.fixture.base, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual({Path(line).name for line in lines}, {u.ASSET_NAME, u.MANIFEST_NAME})
        self.assertTrue(all(Path(line).is_file() for line in lines))
        u.validate_manifest(json.loads((self.fixture.base / "dist" / u.MANIFEST_NAME).read_text()))

    def test_update_uses_only_public_json_and_version_pinned_zip_when_api_is_blocked(self):
        data = self.fixture.archive()
        manifest = fixtures.manifest_for(data)
        requests = []
        archive_url = f"https://github.com/{u.REPOSITORY}/releases/download/v{fixtures.NEXT}/{u.ASSET_NAME}"

        def respond(request, timeout):
            requests.append(request)
            if request.full_url.startswith("https://api.github.com/"):
                raise HTTPError(request.full_url, 403, "API rate limit exceeded", {}, None)
            if request.full_url == u.MANIFEST_URL:
                return io.BytesIO(json.dumps(manifest).encode())
            if request.full_url == archive_url:
                return io.BytesIO(data)
            self.fail("Unexpected request: " + request.full_url)

        with patch.object(u, "urlopen", side_effect=respond):
            result = u.prepare_update(self.fixture.base, force=True)
        self.assertIn("ダウンロード", result)
        self.assertEqual([request.full_url for request in requests], [u.MANIFEST_URL, archive_url])
        self.assertEqual([request.get_header("Accept") for request in requests], ["application/json", "application/octet-stream"])
        self.assertTrue(all(request.get_header("Authorization") is None for request in requests))
        self.assertEqual(u.fingerprint(self.fixture.base), self.fixture.original)
        self.fixture.assert_private_preserved()
        self.assertIn(fixtures.NEXT, u.apply_pending(self.fixture.base))
        self.fixture.assert_private_preserved()

    def test_missing_or_blocked_json_never_falls_back_to_api(self):
        for code in (403, 404, 429, 503):
            with self.subTest(status=code), patch.object(u, "urlopen", side_effect=HTTPError(u.MANIFEST_URL, code, "", {}, None)) as request:
                with self.assertRaises((u.UpdateError, HTTPError)):
                    u.prepare_update(self.fixture.base, force=True)
                request.assert_called_once()
                self.assertEqual(request.call_args.args[0].full_url, u.MANIFEST_URL)
        self.assertEqual(u.fingerprint(self.fixture.base), self.fixture.original)

    def test_schema_version_and_archive_sizes_are_strictly_validated(self):
        data = self.fixture.archive()
        for value in (None, True, False, 0, 2, "1", 1.0):
            manifest = fixtures.manifest_for(data)
            manifest["schema_version"] = value
            with self.subTest(schema=value), self.assertRaises(u.UpdateError):
                u.validate_manifest(manifest)
        for size in (None, True, False, 0, -1, "100", 1.5, u.MAX_ARCHIVE + 1):
            manifest = fixtures.manifest_for(data)
            manifest["archive"]["size"] = size
            with self.subTest(size=size), self.assertRaises(u.UpdateError):
                u.validate_manifest(manifest)

    def test_version_and_archive_name_cannot_change_the_download_host_or_path(self):
        data = self.fixture.archive()
        for version in (None, 1, True, "v1.2.3", "1.2.3-rc1", "01.2.3", "1.2.3/../../other", "https://other.test/1.2.3"):
            manifest = fixtures.manifest_for(data)
            manifest["version"] = version
            with self.subTest(version=version), self.assertRaises(u.UpdateError):
                u.validate_manifest(manifest)
        for name in ("other.zip", "../attendance-update.zip", "https://other.test/update.zip", "/tmp/file.zip", None):
            manifest = fixtures.manifest_for(data)
            manifest["archive"]["name"] = name
            with self.subTest(name=name), self.assertRaises(u.UpdateError):
                u.validate_manifest(manifest)

    def test_downloaded_zip_must_match_both_size_and_sha256(self):
        data = self.fixture.archive()
        for mismatch in ("size_small", "size_large", "sha256"):
            manifest = fixtures.manifest_for(data)
            if mismatch == "sha256":
                manifest["archive"]["sha256"] = "0" * 64
                message = "チェックサムが一致"
            else:
                manifest["archive"]["size"] += -1 if mismatch == "size_small" else 1
                message = "サイズが更新情報JSONと一致"
            with self.subTest(mismatch=mismatch), patch.object(u, "fetch", side_effect=[json.dumps(manifest).encode(), data]):
                with self.assertRaisesRegex(u.UpdateError, message):
                    u.prepare_update(self.fixture.base, force=True)
            self.assertFalse((self.fixture.base / ".updates/pending").exists())
            self.assertEqual(u.fingerprint(self.fixture.base), self.fixture.original)

    def test_oversized_manifest_stops_before_downloading_zip(self):
        with patch.object(u, "urlopen", return_value=io.BytesIO(b" " * (u.MAX_MANIFEST + 1))) as request:
            with self.assertRaisesRegex(u.UpdateError, "上限サイズ"):
                u.prepare_update(self.fixture.base, force=True)
            request.assert_called_once()
        self.assertEqual(u.fingerprint(self.fixture.base), self.fixture.original)


if __name__ == "__main__":
    unittest.main()
