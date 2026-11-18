"""リリース署名 (ECDSA P-256) の生成・検証およびマニフェスト/ZIP改ざん拒否を検証する。"""

import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import test_updater as fixtures
from scripts import build_release
from src import signing
from src import updater as u


def _generate_p256_keypair(directory, prefix="release", pkcs8=False):
    priv = directory / f"{prefix}-private.pem"
    pub = directory / f"{prefix}-public.pem"
    openssl = signing.find_openssl()
    assert openssl is not None
    if pkcs8:
        subprocess.run(
            [
                openssl,
                "genpkey",
                "-algorithm",
                "EC",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-pkeyopt",
                "ec_param_enc:named_curve",
                "-out",
                str(priv),
            ],
            check=True,
            capture_output=True,
        )
    else:
        subprocess.run(
            [openssl, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(priv)],
            check=True,
            capture_output=True,
        )
    subprocess.run([openssl, "ec", "-in", str(priv), "-pubout", "-out", str(pub)], check=True, capture_output=True)
    return priv, pub


class ReleaseSigningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.UpdaterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.keydir = Path(self.fixture.temp.name) / "keys"
        self.keydir.mkdir()
        self.priv_path, self.pub_path = _generate_p256_keypair(self.keydir)
        self.priv_pem = self.pub_pem = ""
        self.priv_pem = self.priv_path.read_text(encoding="utf-8")
        self.pub_pem = self.pub_path.read_text(encoding="utf-8")

    def signed_manifest(self, data, version=fixtures.NEXT, priv_pem=None):
        manifest = fixtures.manifest_for(data, version=version)
        manifest["signature"] = signing.sign_manifest(manifest, priv_pem or self.priv_pem)
        return manifest

    def test_sign_and_verify_canonical_manifest_and_raw_bytes_across_key_formats(self):
        priv8_path, pub8_path = _generate_p256_keypair(self.keydir, prefix="pkcs8", pkcs8=True)
        for priv, pub in ((self.priv_pem, self.pub_pem), (priv8_path.read_text(), pub8_path.read_text())):
            payload = b"attendance release archive bytes"
            sig = signing.sign_bytes(payload, priv)
            signing.verify_bytes(payload, sig, pub)

            data = self.fixture.archive()
            manifest = self.signed_manifest(data, priv_pem=priv)
            self.assertTrue(u.verify_manifest_signature(manifest, public_key_pem=pub))
            self.assertTrue(u.verify_manifest_signature(manifest, data, public_key_pem=pub))

            # マニフェストの整形順や release_notes の有無は正規化署名に影響しない。
            reordered = {
                "release_notes": "# v99.0.0\n\n## 変更内容\n\n- 署名テスト。\n\n## 利用者の操作\n\n- att update を実行してください。\n",
                "signature": manifest["signature"],
                "archive": {
                    "size": manifest["archive"]["size"],
                    "sha256": manifest["archive"]["sha256"],
                    "name": manifest["archive"]["name"],
                },
                "version": manifest["version"],
                "schema_version": manifest["schema_version"],
            }
            self.assertTrue(u.verify_manifest_signature(reordered, data, public_key_pem=pub))

    def test_manifest_signature_format_validation(self):
        data = self.fixture.archive()
        base = self.signed_manifest(data)
        u.validate_manifest(base)
        bad_signatures = (
            None,
            [],
            "not-a-dict",
            {"algorithm": signing.SIGNATURE_ALGORITHM},
            {"value": base["signature"]["value"]},
            {"algorithm": signing.SIGNATURE_ALGORITHM, "value": base["signature"]["value"], "extra": 1},
            {"algorithm": "rsa-sha256", "value": base["signature"]["value"]},
            {"algorithm": signing.SIGNATURE_ALGORITHM, "value": ""},
            {"algorithm": signing.SIGNATURE_ALGORITHM, "value": "not!base64"},
            {"algorithm": signing.SIGNATURE_ALGORITHM, "value": base["signature"]["value"] + "\n"},
            {"algorithm": signing.SIGNATURE_ALGORITHM, "value": "A" * 300},
            {"algorithm": signing.SIGNATURE_ALGORITHM, "value": "AQJ="},
        )
        for bad in bad_signatures:
            with self.subTest(bad=bad), self.assertRaises(u.UpdateError):
                u.validate_manifest({**base, "signature": bad})

    def test_verify_manifest_signature_rejects_tampering_missing_and_forged_signatures(self):
        data = self.fixture.archive()
        signed = self.signed_manifest(data)

        # 改ざんされたZIP（サイズ変化および同サイズ内容変化）を拒否する。
        with self.assertRaisesRegex(u.UpdateError, "サイズ"):
            u.verify_manifest_signature(signed, data + b"x", public_key_pem=self.pub_pem)
        same_size_tampered = bytes([data[0] ^ 0xFF]) + data[1:]
        with self.assertRaisesRegex(u.UpdateError, "チェックサム"):
            u.verify_manifest_signature(signed, same_size_tampered, public_key_pem=self.pub_pem)

        # 署名後に版番号やアーカイブメタデータを改ざんしたマニフェストを拒否する。
        tampered_version = {**signed, "version": "100.0.0"}
        with self.assertRaisesRegex(u.UpdateError, "署名検証"):
            u.verify_manifest_signature(tampered_version, public_key_pem=self.pub_pem)

        tampered_sha = {
            **signed,
            "archive": {**signed["archive"], "sha256": hashlib.sha256(same_size_tampered).hexdigest()},
        }
        with self.assertRaisesRegex(u.UpdateError, "署名検証"):
            u.verify_manifest_signature(tampered_sha, same_size_tampered, public_key_pem=self.pub_pem)

        # 別の鍵による偽造署名およびビット反転署名を拒否する。
        other_priv, _ = _generate_p256_keypair(self.keydir, prefix="attacker")
        forged = self.signed_manifest(data, priv_pem=other_priv.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(u.UpdateError, "署名検証"):
            u.verify_manifest_signature(forged, data, public_key_pem=self.pub_pem)

        raw_sig = bytearray(base64.b64decode(signed["signature"]["value"]))
        raw_sig[-1] ^= 0x01
        flipped = {**signed, "signature": {"algorithm": signing.SIGNATURE_ALGORITHM, "value": base64.b64encode(bytes(raw_sig)).decode("ascii")}}
        with self.assertRaisesRegex(u.UpdateError, "署名検証"):
            u.verify_manifest_signature(flipped, data, public_key_pem=self.pub_pem)

        # 公開鍵設定時または require_signature=True 時の署名欠落を拒否する。
        unsigned = fixtures.manifest_for(data)
        with self.assertRaisesRegex(u.UpdateError, "署名がありません"):
            u.verify_manifest_signature(unsigned, data, public_key_pem=self.pub_pem)
        with patch.object(u, "PUBLIC_KEY_PEM", self.pub_pem):
            with self.assertRaisesRegex(u.UpdateError, "署名がありません"):
                u.verify_manifest_signature(unsigned, data)
        with self.assertRaisesRegex(u.UpdateError, "署名がありません"):
            u.verify_manifest_signature(unsigned, data, public_key_pem=self.pub_pem, require_signature=True)
        with self.assertRaisesRegex(u.UpdateError, "公開鍵が設定されていません"):
            u.verify_manifest_signature(unsigned, data, public_key_pem="", require_signature=True)

        # 公開鍵未設定かつ require_signature=False なら従来互換でスキップする。
        self.assertFalse(u.verify_manifest_signature(unsigned, data, public_key_pem="", require_signature=False))

    def test_rejects_non_p256_keys_invalid_pem_and_missing_openssl(self):
        openssl = signing.find_openssl()
        rsa_priv = self.keydir / "rsa-private.pem"
        rsa_pub = self.keydir / "rsa-public.pem"
        p384_priv = self.keydir / "p384-private.pem"
        p384_pub = self.keydir / "p384-public.pem"
        subprocess.run([openssl, "genrsa", "-out", str(rsa_priv), "2048"], check=True, capture_output=True)
        subprocess.run([openssl, "rsa", "-in", str(rsa_priv), "-pubout", "-out", str(rsa_pub)], check=True, capture_output=True)
        subprocess.run([openssl, "ecparam", "-name", "secp384r1", "-genkey", "-noout", "-out", str(p384_priv)], check=True, capture_output=True)
        subprocess.run([openssl, "ec", "-in", str(p384_priv), "-pubout", "-out", str(p384_pub)], check=True, capture_output=True)

        data = self.fixture.archive()
        signed = self.signed_manifest(data)
        for bad_key in (rsa_pub.read_text(), p384_pub.read_text(), "not a pem", self.pub_pem + "\n" + self.pub_pem, 123):
            with self.subTest(bad_key=str(bad_key)[:30]), self.assertRaises(u.UpdateError):
                u.verify_manifest_signature(signed, data, public_key_pem=bad_key)

        with self.assertRaises(signing.SignatureError):
            signing.sign_bytes(b"payload", rsa_priv.read_text())

        with patch.object(signing, "find_openssl", return_value=None):
            with self.assertRaises(signing.SignatureError):
                signing.sign_bytes(b"payload", self.priv_pem)
            with self.assertRaisesRegex(u.UpdateError, "署名検証"):
                u.verify_manifest_signature(signed, data, public_key_pem=self.pub_pem)

    def test_prepare_update_verifies_signature_before_validate_payload(self):
        data = self.fixture.archive()
        signed = self.signed_manifest(data)
        archive_url = f"https://github.com/{u.REPOSITORY}/releases/download/v{fixtures.NEXT}/{u.ASSET_NAME}"

        # 正常な署名では展開・適用まで完了し、余分なHTTP通信をしない。
        requests = []

        def respond(request, timeout):
            requests.append(request.full_url)
            if request.full_url == u.MANIFEST_URL:
                return io.BytesIO(json.dumps(signed).encode())
            if request.full_url == archive_url:
                return io.BytesIO(data)
            raise AssertionError(f"Unexpected request: {request.full_url}")

        with patch.object(u, "PUBLIC_KEY_PEM", self.pub_pem), patch.object(u, "urlopen", side_effect=respond):
            message = u.prepare_update(self.fixture.base, force=True)
        self.assertIn("ダウンロード", message)
        self.assertEqual(requests, [u.MANIFEST_URL, archive_url])
        self.assertIn(fixtures.NEXT, u.apply_pending(self.fixture.base))
        self.fixture.assert_private_preserved()

        # 改ざんケース・署名欠落・偽造署名はいずれも validate_payload の前に拒否する。
        self.setUp()
        data = self.fixture.archive()
        signed = self.signed_manifest(data)
        other_priv, _ = _generate_p256_keypair(self.keydir, prefix="other")
        tampered_zip = bytes([data[0] ^ 0xFF]) + data[1:]
        cases = (
            ("tampered_zip", [json.dumps(signed).encode(), tampered_zip]),
            ("tampered_version", [json.dumps({**signed, "version": "100.0.0"}).encode(), data]),
            ("missing_signature", [json.dumps(fixtures.manifest_for(data)).encode(), data]),
            ("forged_signature", [json.dumps(self.signed_manifest(data, priv_pem=other_priv.read_text())).encode(), data]),
        )
        for name, replies in cases:
            with self.subTest(case=name), patch.object(u, "PUBLIC_KEY_PEM", self.pub_pem), \
                    patch.object(u, "fetch", side_effect=replies), \
                    patch.object(u, "validate_payload") as validate_payload:
                with self.assertRaises(u.UpdateError):
                    u.prepare_update(self.fixture.base, force=True)
                validate_payload.assert_not_called()
                self.assertFalse((self.fixture.base / ".updates/pending").exists())
                self.assertEqual(u.fingerprint(self.fixture.base), self.fixture.original)

    def test_check_update_notes_and_config_override_respect_signature_policy(self):
        data = self.fixture.archive()
        unsigned = fixtures.manifest_for(data)
        with patch.object(u, "PUBLIC_KEY_PEM", self.pub_pem), patch.object(u, "fetch", return_value=json.dumps(unsigned).encode()):
            with self.assertRaisesRegex(u.UpdateError, "署名"):
                u.check_update(self.fixture.base)
            with self.assertRaisesRegex(u.UpdateError, "署名"):
                u.show_update_notes(self.fixture.base)
        self.assertIsNone(u.cached_manifest(self.fixture.base))

        # config.json の update_public_key は PUBLIC_KEY_PEM 未設定時のみ有効で、設定時は弱められない。
        config_path = self.fixture.base / "config.json"
        config_path.write_text(json.dumps({"users": ["private"], "update_public_key": self.pub_pem}))
        with patch.object(u, "fetch", side_effect=[json.dumps(unsigned).encode(), data]):
            with self.assertRaisesRegex(u.UpdateError, "署名"):
                u.prepare_update(self.fixture.base, force=True)
        signed = self.signed_manifest(data)
        with patch.object(u, "fetch", side_effect=[json.dumps(signed).encode(), data]):
            self.assertIn("ダウンロード", u.prepare_update(self.fixture.base, force=True))
        shutil.rmtree(self.fixture.base / ".updates/pending")

        other_priv, other_pub = _generate_p256_keypair(self.keydir, prefix="override")
        config_path.write_text(json.dumps({"users": ["private"], "update_public_key": other_pub.read_text()}))
        forged_for_main = self.signed_manifest(data, priv_pem=other_priv.read_text())
        with patch.object(u, "PUBLIC_KEY_PEM", self.pub_pem), \
                patch.object(u, "fetch", side_effect=[json.dumps(forged_for_main).encode(), data]):
            with self.assertRaisesRegex(u.UpdateError, "署名検証"):
                u.prepare_update(self.fixture.base, force=True)

    def test_build_release_signing_cli_env_var_and_unsigned_fallback(self):
        stage = Path(self.fixture.temp.name) / "dist-build"
        stage.mkdir()
        archive_out = stage / u.ASSET_NAME
        manifest_out = stage / u.MANIFEST_NAME
        sig_out = Path(str(archive_out) + ".sig")

        with patch.object(build_release, "ROOT", fixtures.ROOT):
            built = build_release.build("v" + fixtures.VERSION, archive_out, signing_key=self.priv_path)
        self.assertEqual(built, archive_out)
        self.assertTrue(sig_out.is_file())
        data = archive_out.read_bytes()
        manifest = u.validate_manifest(json.loads(manifest_out.read_text(encoding="utf-8")))
        self.assertEqual(manifest["archive"], {"name": u.ASSET_NAME, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        self.assertTrue(u.verify_manifest_signature(manifest, data, public_key_pem=self.pub_pem, require_signature=True))
        signing.verify_bytes(data, sig_out.read_bytes(), self.pub_pem)
        cli_verify = subprocess.run(
            [signing.find_openssl(), "dgst", "-sha256", "-verify", str(self.pub_path), "-signature", str(sig_out), str(archive_out)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(cli_verify.returncode, 0, cli_verify.stderr)

        # 署名なしビルドに切り替えた場合、古い .sig を残さない。
        with patch.object(build_release, "ROOT", fixtures.ROOT):
            build_release.build("v" + fixtures.VERSION, archive_out)
        self.assertFalse(sig_out.exists())
        self.assertNotIn("signature", json.loads(manifest_out.read_text(encoding="utf-8")))

        # CLI --signing-key と環境変数 RELEASE_SIGNING_KEY_FILE の両方を検証する。
        script = self.fixture.base / "scripts/build_release.py"
        script.parent.mkdir()
        shutil.copy2(fixtures.ROOT / "scripts/build_release.py", script)
        shutil.copy2(fixtures.ROOT / "requirements.lock", self.fixture.base / "requirements.lock")
        shutil.copytree(fixtures.ROOT / "updates", self.fixture.base / "updates")
        for label, extra_args, env_override in (
            ("cli", ["--signing-key", str(self.priv_path)], {}),
            ("env", [], {"RELEASE_SIGNING_KEY_FILE": str(self.priv_path)}),
        ):
            with self.subTest(mode=label):
                env = {**os.environ, **env_override}
                if label == "cli":
                    env.pop("RELEASE_SIGNING_KEY_FILE", None)
                result = subprocess.run(
                    [sys.executable, str(script), "v" + fixtures.VERSION, *extra_args],
                    cwd=self.fixture.base,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = result.stdout.splitlines()
                self.assertEqual({Path(line).name for line in lines}, {u.ASSET_NAME, u.MANIFEST_NAME, u.ASSET_NAME + ".sig"})
                dist_zip = (self.fixture.base / "dist" / u.ASSET_NAME).read_bytes()
                dist_json = json.loads((self.fixture.base / "dist" / u.MANIFEST_NAME).read_text(encoding="utf-8"))
                dist_sig = (self.fixture.base / "dist" / (u.ASSET_NAME + ".sig")).read_bytes()
                self.assertTrue(u.verify_manifest_signature(dist_json, dist_zip, public_key_pem=self.pub_pem, require_signature=True))
                signing.verify_bytes(dist_zip, dist_sig, self.pub_pem)

        # 指定された署名鍵が存在しない・シンボリックリンク・不正な場合は必ず失敗する。
        missing_key = self.keydir / "missing.pem"
        symlink_key = self.keydir / "link.pem"
        symlink_key.symlink_to(self.priv_path)
        for bad in (missing_key, symlink_key, ""):
            with self.subTest(bad_key=bad), patch.object(build_release, "ROOT", fixtures.ROOT), self.assertRaises(ValueError):
                build_release.build("v" + fixtures.VERSION, archive_out, signing_key=bad)

    def test_release_workflow_configures_provenance_and_ephemeral_signing_key_cleanup(self):
        workflow = (fixtures.ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("id-token: write", workflow)
        self.assertIn("attestations: write", workflow)
        self.assertIn("actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8", workflow)
        self.assertIn("dist/attendance-update.zip", workflow)
        self.assertIn("dist/attendance-update.json", workflow)
        self.assertIn("dist/attendance-update.zip.sig", workflow)
        self.assertIn("RELEASE_SIGNING_KEY", workflow)
        self.assertIn("trap cleanup EXIT", workflow)

    def _configure_key(self, mode):
        if mode == "config":
            config_path = self.fixture.base / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["update_public_key"] = self.pub_pem
            encoded = json.dumps(config).encode("utf-8")
            config_path.write_bytes(encoded)
            self.fixture.private["config.json"] = encoded
            return contextlib.nullcontext()
        return patch.object(u, "PUBLIC_KEY_PEM", self.pub_pem)

    def _prepare_unsigned_pending(self, data):
        unsigned = fixtures.manifest_for(data)
        with patch.object(u, "fetch", side_effect=[json.dumps(unsigned).encode(), data]), \
                patch.object(u.subprocess, "run", return_value=Mock(returncode=0)):
            u.prepare_update(self.fixture.base, force=True)
        pending = self.fixture.base / ".updates/pending"
        self.assertTrue((pending / u.ASSET_NAME).is_file())
        self.assertEqual(stat.S_IMODE((pending / u.ASSET_NAME).stat().st_mode), 0o600)
        self.assertEqual(u.read_json(pending / "metadata.json")["manifest"], unsigned)
        return pending, unsigned

    def test_unsigned_pending_is_rejected_and_discarded_when_signing_key_is_configured_later(self):
        for mode in ("config", "module"):
            with self.subTest(mode=mode, action="run_application_rejects_unsigned"):
                self.setUp()
                data = self.fixture.archive()
                pending, unsigned = self._prepare_unsigned_pending(data)
                with self._configure_key(mode), \
                        patch.object(u, "fetch", return_value=json.dumps(unsigned).encode()), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = u.run_application(self.fixture.base, ["update"])
                self.assertEqual(code, 1)
                self.assertFalse(pending.exists())
                self.assertEqual(u.fingerprint(self.fixture.base), self.fixture.original)
                self.fixture.assert_private_preserved()

            with self.subTest(mode=mode, action="prepare_update_replaces_with_signed"):
                self.setUp()
                data = self.fixture.archive()
                pending, _ = self._prepare_unsigned_pending(data)
                signed = self.signed_manifest(data)
                with self._configure_key(mode), \
                        patch.object(u, "fetch", side_effect=[json.dumps(signed).encode(), data]) as fetch:
                    message = u.prepare_update(self.fixture.base, force=True)
                self.assertIn("ダウンロード", message)
                self.assertEqual(fetch.call_count, 2)
                self.assertIn(fixtures.NEXT, u.apply_pending(self.fixture.base))
                self.fixture.assert_private_preserved()

            with self.subTest(mode=mode, action="apply_pending_rejects_and_deletes"):
                self.setUp()
                data = self.fixture.archive()
                pending, _ = self._prepare_unsigned_pending(data)
                with self._configure_key(mode), self.assertRaisesRegex(u.UpdateError, "署名"):
                    u.apply_pending(self.fixture.base)
                self.assertFalse(pending.exists())
                self.assertEqual(u.fingerprint(self.fixture.base), self.fixture.original)
                self.fixture.assert_private_preserved()

            with self.subTest(mode=mode, action="legacy_pending_without_manifest_or_archive"):
                self.setUp()
                data = self.fixture.archive()
                pending, unsigned = self._prepare_unsigned_pending(data)
                metadata = u.read_json(pending / "metadata.json")
                metadata.pop("manifest")
                u.write_json(pending / "metadata.json", metadata)
                (pending / u.ASSET_NAME).unlink()
                with self._configure_key(mode):
                    with self.assertRaises(u.UpdateError):
                        u.apply_pending(self.fixture.base)
                    self.assertFalse(pending.exists())
                    self.assertEqual(u.fingerprint(self.fixture.base), self.fixture.original)

                self.setUp()
                data = self.fixture.archive()
                pending, unsigned = self._prepare_unsigned_pending(data)
                metadata = u.read_json(pending / "metadata.json")
                metadata.pop("manifest")
                u.write_json(pending / "metadata.json", metadata)
                (pending / u.ASSET_NAME).unlink()
                with self._configure_key(mode), patch.object(u, "fetch", return_value=json.dumps(unsigned).encode()):
                    with self.assertRaisesRegex(u.UpdateError, "署名"):
                        u.prepare_update(self.fixture.base, force=True)
                    self.assertFalse(pending.exists())

    def test_signed_pending_is_reused_without_network_and_tampered_pending_is_rejected(self):
        for mode in ("config", "module"):
            with self.subTest(mode=mode, case="reuse_and_apply"):
                self.setUp()
                data = self.fixture.archive()
                signed = self.signed_manifest(data)
                with self._configure_key(mode):
                    with patch.object(u, "fetch", side_effect=[json.dumps(signed).encode(), data]):
                        self.assertIn("ダウンロード", u.prepare_update(self.fixture.base, force=True))
                    pending = self.fixture.base / ".updates/pending"
                    self.assertEqual(stat.S_IMODE((pending / u.ASSET_NAME).stat().st_mode), 0o600)
                    self.assertEqual(u.read_json(pending / "metadata.json")["manifest"], signed)
                    with patch.object(u, "fetch") as fetch:
                        self.assertIn("準備", u.prepare_update(self.fixture.base, force=True))
                        fetch.assert_not_called()
                    self.assertIn(fixtures.NEXT, u.apply_pending(self.fixture.base))
                    self.fixture.assert_private_preserved()

        def apply_tampering(kind, data):
            pending = self.fixture.base / ".updates/pending"
            metadata_path = pending / "metadata.json"
            metadata = u.read_json(metadata_path)
            if kind == "tampered_file_and_digest":
                tampered = b"# tampered main\n"
                (pending / "main.py").write_bytes(tampered)
                metadata["files"]["main.py"] = hashlib.sha256(tampered).hexdigest()
                u.write_json(metadata_path, metadata)
            elif kind == "extra_file":
                (pending / "src/injected.py").write_text("# injected\n", encoding="utf-8")
            elif kind == "metadata_version":
                metadata["version"] = "v100.0.0"
                u.write_json(metadata_path, metadata)
            elif kind == "stripped_signature":
                metadata["manifest"].pop("signature", None)
                u.write_json(metadata_path, metadata)
            elif kind == "tampered_archive_zip":
                (pending / u.ASSET_NAME).write_bytes(self.fixture.archive({"src/injected.py": b"# evil\n"}))
            elif kind == "corrupt_archive_bytes":
                (pending / u.ASSET_NAME).write_bytes(b"not-a-zip")
            elif kind == "archive_symlink":
                outside = Path(self.fixture.temp.name) / "outside.zip"
                outside.write_bytes(data)
                (pending / u.ASSET_NAME).unlink()
                (pending / u.ASSET_NAME).symlink_to(outside)
            return pending

        kinds = (
            "tampered_file_and_digest",
            "extra_file",
            "metadata_version",
            "stripped_signature",
            "tampered_archive_zip",
            "corrupt_archive_bytes",
            "archive_symlink",
        )
        for kind in kinds:
            with self.subTest(tampering=kind, operation="apply_pending"):
                self.setUp()
                data = self.fixture.archive()
                signed = self.signed_manifest(data)
                with self._configure_key("config"):
                    with patch.object(u, "fetch", side_effect=[json.dumps(signed).encode(), data]):
                        u.prepare_update(self.fixture.base, force=True)
                    pending = apply_tampering(kind, data)
                    with self.assertRaises(u.UpdateError):
                        u.apply_pending(self.fixture.base)
                    self.assertFalse(pending.exists())
                    self.assertEqual(u.fingerprint(self.fixture.base), self.fixture.original)
                    self.fixture.assert_private_preserved()

            with self.subTest(tampering=kind, operation="prepare_update_recovers"):
                self.setUp()
                data = self.fixture.archive()
                signed = self.signed_manifest(data)
                with self._configure_key("config"):
                    with patch.object(u, "fetch", side_effect=[json.dumps(signed).encode(), data]):
                        u.prepare_update(self.fixture.base, force=True)
                    apply_tampering(kind, data)
                    with patch.object(u, "fetch", side_effect=[json.dumps(signed).encode(), data]) as fetch:
                        self.assertIn("ダウンロード", u.prepare_update(self.fixture.base, force=True))
                        self.assertEqual(fetch.call_count, 2)
                    self.assertIn(fixtures.NEXT, u.apply_pending(self.fixture.base))
                    self.fixture.assert_private_preserved()

    def test_config_update_public_key_enforced_for_cached_manifest_304_check_update_and_notes(self):
        class _Response(io.BytesIO):
            def __init__(self, body, headers=None):
                super().__init__(body)
                self.headers = headers or {}

        data = self.fixture.archive()
        notes = f"# v{fixtures.NEXT}\n\n## 変更内容\n\n- 署名付きテスト。\n\n## 利用者の操作\n\n- att update で更新してください。\n"
        unsigned = {**fixtures.manifest_for(data), "release_notes": notes}
        signed = {**unsigned, "signature": signing.sign_manifest(unsigned, self.priv_pem)}

        u.write_json(
            self.fixture.base / ".updates/check.json",
            {"checked_at": time.time(), "interval": 86400, "etag": '"unsigned"', "manifest": unsigned},
        )
        with self._configure_key("config"):
            self.assertIsNone(u.cached_manifest(self.fixture.base))
            self.assertIsNone(u.cached_update_message(self.fixture.base))
            with patch.object(u, "fetch") as fetch:
                self.assertIsNone(u.check_update(self.fixture.base))
                fetch.assert_not_called()

            # 304 応答でも config.json の公開鍵に適合しない未署名キャッシュは再利用しない。
            def reject_304(request, timeout):
                self.assertIsNone(request.get_header("If-none-match"))
                raise HTTPError(request.full_url, 304, "Not Modified", {}, io.BytesIO())

            with patch.object(u, "urlopen", side_effect=reject_304):
                with self.assertRaisesRegex(u.UpdateError, "キャッシュがありません"):
                    u.show_update_notes(self.fixture.base)
                with self.assertRaisesRegex(u.UpdateError, "キャッシュがありません"):
                    u.prepare_update(self.fixture.base, force=True)

            # 期限切れの check_update も同様に未署名キャッシュでの 304 再利用・200 受入を拒否する。
            u.write_json(
                self.fixture.base / ".updates/check.json",
                {"checked_at": 0, "interval": 86400, "etag": '"unsigned"', "manifest": unsigned},
            )
            with patch.object(u, "urlopen", side_effect=reject_304):
                with self.assertRaisesRegex(u.UpdateError, "キャッシュがありません"):
                    u.check_update(self.fixture.base)
            self.assertNotIn("manifest", u.read_json(self.fixture.base / ".updates/check.json"))
            u.write_json(self.fixture.base / ".updates/check.json", {"checked_at": 0, "interval": 86400})
            with patch.object(
                u,
                "urlopen",
                side_effect=lambda *args, **kwargs: _Response(json.dumps(unsigned).encode(), {"ETag": '"unsigned-2"'}),
            ):
                with self.assertRaisesRegex(u.UpdateError, "署名"):
                    u.check_update(self.fixture.base)
                with self.assertRaisesRegex(u.UpdateError, "署名"):
                    u.show_update_notes(self.fixture.base)
            self.assertIsNone(u.cached_manifest(self.fixture.base))

            # 署名済みマニフェスト保存後は ETag による 304 再利用が成立する。
            u.write_json(self.fixture.base / ".updates/check.json", {"checked_at": 0, "interval": 86400})
            with patch.object(u, "urlopen", return_value=_Response(json.dumps(signed).encode(), {"ETag": '"signed-v1"'})):
                self.assertIn(fixtures.NEXT, u.check_update(self.fixture.base))
            self.assertEqual(u.cached_manifest(self.fixture.base), signed)

            def reuse_304(request, timeout):
                self.assertEqual(request.get_header("If-none-match"), '"signed-v1"')
                raise HTTPError(request.full_url, 304, "Not Modified", {}, io.BytesIO())

            notes_output = io.StringIO()
            with patch.object(u, "urlopen", side_effect=reuse_304), contextlib.redirect_stdout(notes_output):
                u.show_update_notes(self.fixture.base)
            self.assertIn("署名付きテスト", notes_output.getvalue())


if __name__ == "__main__":
    unittest.main()
