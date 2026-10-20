"""個人設定を含めず更新用ZIPを作る。 python scripts/build_release.py v0.2.0"""

from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.updater import ASSET_NAME, REQUIRED, allowed, managed_files, version_tuple
from src.version import VERSION


def build(tag, output):
    if version_tuple(tag) != version_tuple(VERSION):
        raise ValueError("タグと src/version.py の VERSION を揃えてください。")
    names = managed_files(ROOT)
    if not REQUIRED.issubset(names) or not all(allowed(name) for name in names):
        raise ValueError("更新ファイルの構成が不正です。")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(names):
            if (ROOT / name).is_symlink():
                raise ValueError("シンボリックリンクは配布できません。")
            archive.write(ROOT / name, name)
    return output


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("使い方: python scripts/build_release.py vX.Y.Z")
    print(build(sys.argv[1], ROOT / "dist" / ASSET_NAME))
