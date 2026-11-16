"""個人設定を含めず更新用ZIPとJSONを作る。 python scripts/build_release.py v0.2.3"""

import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.updater import ASSET_NAME, MANIFEST_NAME, REQUIRED, allowed, atomic_write, managed_files, validate_manifest, version_tuple
from src.version import VERSION
from src.dependencies import read_spec


def build(tag, output):
    if version_tuple(tag) != version_tuple(VERSION):
        raise ValueError("タグと src/version.py の VERSION を揃えてください。")
    names = managed_files(ROOT)
    if not REQUIRED.issubset(names) or not all(allowed(name) for name in names):
        raise ValueError("更新ファイルの構成が不正です。")
    spec = read_spec(ROOT)
    if (ROOT / "requirements.lock").read_text(encoding="utf-8") != spec["text"]:
        raise ValueError("配布用ロック情報が古いため scripts/generate_dependency_lock.py を実行してください。")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(names):
            if (ROOT / name).is_symlink():
                raise ValueError("シンボリックリンクは配布できません。")
            archive.write(ROOT / name, name)
    data = output.read_bytes()
    manifest = validate_manifest({
        "schema_version": 1,
        "version": VERSION,
        "archive": {"name": ASSET_NAME, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)},
    })
    atomic_write(output.parent / MANIFEST_NAME, (json.dumps(manifest, indent=2) + "\n").encode(), 0o644)
    return output


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("使い方: python scripts/build_release.py vX.Y.Z")
    print(build(sys.argv[1], ROOT / "dist" / ASSET_NAME))
    print(ROOT / "dist" / MANIFEST_NAME)
