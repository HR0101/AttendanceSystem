"""個人設定を含めず更新用ZIPとJSONを作る。 python scripts/build_release.py v0.2.3"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.dependencies import read_spec
from src.release_notes import read_notes
from src.signing import extract_public_key_pem, sign_bytes, sign_manifest
from src.updater import ASSET_NAME, MANIFEST_NAME, REQUIRED, allowed, atomic_write, managed_files, validate_manifest, version_tuple
from src.version import VERSION


def resolve_signing_key(signing_key=None):
    """引数または環境変数 RELEASE_SIGNING_KEY_FILE から署名鍵パスを解決する。"""
    if signing_key is not None:
        if not str(signing_key).strip():
            raise ValueError("署名鍵ファイルのパスを指定してください。")
        return Path(signing_key)
    env_path = os.environ.get("RELEASE_SIGNING_KEY_FILE", "")
    if env_path.strip():
        return Path(env_path)
    return None


def read_signing_key(key_path):
    """署名鍵ファイルを検証して読み込む。"""
    if key_path.is_symlink() or not key_path.is_file():
        raise ValueError("署名鍵ファイルが見つからないか通常のファイルではありません。")
    pem = key_path.read_text(encoding="utf-8")
    if not pem.strip():
        raise ValueError("署名鍵ファイルが空です。")
    extract_public_key_pem(pem)
    return pem


def build(tag, output, *, signing_key=None):
    if version_tuple(tag) != version_tuple(VERSION):
        raise ValueError("タグと src/version.py の VERSION を揃えてください。")
    names = managed_files(ROOT)
    if not REQUIRED.issubset(names) or not all(allowed(name) for name in names):
        raise ValueError("更新ファイルの構成が不正です。")
    spec = read_spec(ROOT)
    if (ROOT / "requirements.lock").read_text(encoding="utf-8") != spec["text"]:
        raise ValueError("配布用ロック情報が古いため scripts/generate_dependency_lock.py を実行してください。")
    notes = read_notes(ROOT, VERSION)
    key_path = resolve_signing_key(signing_key)
    private_key_pem = read_signing_key(key_path) if key_path is not None else None
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(names):
            if (ROOT / name).is_symlink():
                raise ValueError("シンボリックリンクは配布できません。")
            archive.write(ROOT / name, name)
    data = output.read_bytes()
    manifest = {
        "schema_version": 1,
        "version": VERSION,
        "release_notes": notes,
        "archive": {"name": ASSET_NAME, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)},
    }
    sig_path = Path(str(output) + ".sig")
    if private_key_pem is not None:
        manifest["signature"] = sign_manifest(manifest, private_key_pem)
        atomic_write(sig_path, sign_bytes(data, private_key_pem), 0o644)
    else:
        sig_path.unlink(missing_ok=True)
    manifest = validate_manifest(manifest)
    atomic_write(output.parent / MANIFEST_NAME, (json.dumps(manifest, indent=2) + "\n").encode(), 0o644)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description="個人設定を含めず更新用ZIPとJSONを作る。")
    parser.add_argument("tag", help="リリースタグ (vX.Y.Z)")
    parser.add_argument("--signing-key", help="ECDSA P-256 秘密鍵 PEM ファイルのパス")
    args = parser.parse_args(argv)
    archive = build(args.tag, ROOT / "dist" / ASSET_NAME, signing_key=args.signing_key)
    print(archive)
    print(archive.parent / MANIFEST_NAME)
    if resolve_signing_key(args.signing_key) is not None:
        print(Path(str(archive) + ".sig"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
