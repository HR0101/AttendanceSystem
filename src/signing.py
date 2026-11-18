"""OpenSSL / LibreSSL による ECDSA P-256 署名と検証。標準ライブラリだけを使用する。"""

import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

SIGNATURE_ALGORITHM = "ecdsa-p256-sha256"
MAX_SIGNATURE_BYTES = 128
MAX_SIGNATURE_BASE64_LENGTH = 256

# ECDSA P-256 (id-ecPublicKey + prime256v1) の SPKI DER ヘッダ。
_P256_UNCOMPRESSED_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d03010703420004")
_P256_COMPRESSED_SPKI_PREFIX = bytes.fromhex("3039301306072a8648ce3d020106082a8648ce3d030107032200")
_PEM_PUBLIC_KEY_RE = re.compile(
    r"\A-----BEGIN PUBLIC KEY-----\r?\n([A-Za-z0-9+/=\r\n]+)\r?\n-----END PUBLIC KEY-----\Z"
)


class SignatureError(ValueError):
    """署名形式・公開鍵・暗号署名検証の失敗。"""


def find_openssl():
    """利用可能な OpenSSL / LibreSSL 実行ファイルを返す。"""
    for candidate in ("/usr/bin/openssl", shutil.which("openssl")):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _require_openssl():
    executable = find_openssl()
    if not executable:
        raise SignatureError("署名検証に必要な OpenSSL が見つかりません。")
    return executable


def _run_openssl(args, *, timeout=10):
    executable = _require_openssl()
    try:
        return subprocess.run(
            [executable, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SignatureError("OpenSSL による署名処理を実行できませんでした。") from exc


def _write_secret_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def validate_public_key_pem(pem):
    """ECDSA P-256 の公開鍵 PEM (SubjectPublicKeyInfo) を検証して正規化する。"""
    if not isinstance(pem, str):
        raise SignatureError("公開鍵の形式が不正です。")
    cleaned = pem.strip()
    match = _PEM_PUBLIC_KEY_RE.fullmatch(cleaned)
    if not match:
        raise SignatureError("公開鍵は PEM 形式の ECDSA P-256 公開鍵である必要があります。")
    body = "".join(match.group(1).split())
    try:
        der = base64.b64decode(body, validate=True)
    except ValueError as exc:
        raise SignatureError("公開鍵の Base64 データが不正です。") from exc
    valid_uncompressed = len(der) == 91 and der.startswith(_P256_UNCOMPRESSED_SPKI_PREFIX)
    valid_compressed = (
        len(der) == 59
        and der.startswith(_P256_COMPRESSED_SPKI_PREFIX)
        and der[len(_P256_COMPRESSED_SPKI_PREFIX)] in (0x02, 0x03)
    )
    if not (valid_uncompressed or valid_compressed):
        raise SignatureError("公開鍵は ECDSA P-256 (prime256v1) である必要があります。")
    return cleaned + "\n"


def validate_signature_block(signature):
    """manifest['signature'] の形式を検証し、DER 署名バイト列を返す。"""
    if not isinstance(signature, dict) or set(signature.keys()) != {"algorithm", "value"}:
        raise SignatureError("署名のメタデータ形式が不正です。")
    if signature.get("algorithm") != SIGNATURE_ALGORITHM:
        raise SignatureError("未対応の署名アルゴリズムです。")
    value = signature.get("value")
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_SIGNATURE_BASE64_LENGTH
        or not value.isascii()
        or any(ch.isspace() for ch in value)
    ):
        raise SignatureError("署名データの文字列形式が不正です。")
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise SignatureError("署名データの Base64 が不正です。") from exc
    if base64.b64encode(raw).decode("ascii") != value or not 1 <= len(raw) <= MAX_SIGNATURE_BYTES:
        raise SignatureError("署名データの長さが不正です。")
    return raw


def canonical_manifest_payload(manifest):
    """マニフェストの版番号・配布アーカイブ識別情報から正規化バイト列を作る。"""
    if not isinstance(manifest, dict):
        raise SignatureError("署名対象のマニフェストが不正です。")
    archive = manifest.get("archive")
    if not isinstance(archive, dict):
        raise SignatureError("署名対象のアーカイブ情報が不正です。")
    try:
        canonical = {
            "schema_version": manifest["schema_version"],
            "version": manifest["version"],
            "archive": {
                "name": archive["name"],
                "sha256": archive["sha256"],
                "size": archive["size"],
            },
        }
    except KeyError as exc:
        raise SignatureError("署名対象のマニフェスト項目が不足しています。") from exc
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def extract_public_key_pem(private_key_pem):
    """ECDSA P-256 秘密鍵 PEM から公開鍵 PEM を取り出して検証する。"""
    if not isinstance(private_key_pem, str) or not private_key_pem.strip():
        raise SignatureError("署名用の秘密鍵が空です。")
    normalized = private_key_pem.strip() + "\n"
    with tempfile.TemporaryDirectory(prefix="att-sign-") as temp:
        temp_dir = Path(temp)
        key_path = temp_dir / "private.pem"
        pub_path = temp_dir / "public.pem"
        _write_secret_file(key_path, normalized.encode("utf-8"))
        result = _run_openssl(["ec", "-in", str(key_path), "-pubout", "-out", str(pub_path)])
        if result.returncode != 0 or not pub_path.is_file():
            raise SignatureError("署名鍵を ECDSA 鍵として読み込めませんでした。")
        return validate_public_key_pem(pub_path.read_text(encoding="utf-8"))


def sign_bytes(data, private_key_pem):
    """任意バイト列に対する ECDSA P-256 SHA-256 分離署名 (DER) を生成する。"""
    if not isinstance(data, (bytes, bytearray)):
        raise SignatureError("署名対象データはバイト列である必要があります。")
    if not isinstance(private_key_pem, str) or not private_key_pem.strip():
        raise SignatureError("署名用の秘密鍵が空です。")
    normalized = private_key_pem.strip() + "\n"
    with tempfile.TemporaryDirectory(prefix="att-sign-") as temp:
        temp_dir = Path(temp)
        key_path = temp_dir / "private.pem"
        pub_path = temp_dir / "public.pem"
        data_path = temp_dir / "payload.bin"
        sig_path = temp_dir / "payload.sig"
        _write_secret_file(key_path, normalized.encode("utf-8"))
        pub_check = _run_openssl(["ec", "-in", str(key_path), "-pubout", "-out", str(pub_path)])
        if pub_check.returncode != 0 or not pub_path.is_file():
            raise SignatureError("署名鍵を ECDSA 鍵として読み込めませんでした。")
        validate_public_key_pem(pub_path.read_text(encoding="utf-8"))
        data_path.write_bytes(bytes(data))
        result = _run_openssl(["dgst", "-sha256", "-sign", str(key_path), "-out", str(sig_path), str(data_path)])
        if result.returncode != 0 or not sig_path.is_file():
            raise SignatureError("署名の生成に失敗しました。")
        sig_bytes = sig_path.read_bytes()
    if not 1 <= len(sig_bytes) <= MAX_SIGNATURE_BYTES:
        raise SignatureError("生成された署名データのサイズが不正です。")
    return sig_bytes


def verify_bytes(data, signature, public_key_pem):
    """ECDSA P-256 SHA-256 分離署名 (DER) を検証し、失敗時は SignatureError を送出する。"""
    if not isinstance(data, (bytes, bytearray)):
        raise SignatureError("検証対象データはバイト列である必要があります。")
    if not isinstance(signature, (bytes, bytearray)) or not 1 <= len(signature) <= MAX_SIGNATURE_BYTES:
        raise SignatureError("署名データの形式またはサイズが不正です。")
    normalized_pub = validate_public_key_pem(public_key_pem)
    with tempfile.TemporaryDirectory(prefix="att-verify-") as temp:
        temp_dir = Path(temp)
        pub_path = temp_dir / "public.pem"
        sig_path = temp_dir / "payload.sig"
        data_path = temp_dir / "payload.bin"
        pub_path.write_text(normalized_pub, encoding="utf-8")
        sig_path.write_bytes(bytes(signature))
        data_path.write_bytes(bytes(data))
        result = _run_openssl(
            ["dgst", "-sha256", "-verify", str(pub_path), "-signature", str(sig_path), str(data_path)]
        )
    if result.returncode != 0 or result.stdout.decode("utf-8", errors="replace").strip() != "Verified OK":
        raise SignatureError("署名検証に失敗しました。")


def sign_manifest(manifest, private_key_pem):
    """マニフェスト用の signature メタデータ辞書を生成する。"""
    payload = canonical_manifest_payload(manifest)
    sig_bytes = sign_bytes(payload, private_key_pem)
    return {
        "algorithm": SIGNATURE_ALGORITHM,
        "value": base64.b64encode(sig_bytes).decode("ascii"),
    }
