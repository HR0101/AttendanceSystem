"""リリース署名 (ECDSA P-256) の生成・検証およびマニフェスト/ZIP改ざん拒否を検証する。"""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
