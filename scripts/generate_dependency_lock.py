"""旧版が受け取れるPythonファイルとして、配布用ロック情報を生成する。"""

import hashlib
from pathlib import Path


def generate(root):
    lock = (root / "requirements.lock").read_text(encoding="utf-8")
    input_hash = hashlib.sha256((root / "requirements.txt").read_bytes()).hexdigest()
    target = root / "src/dependency_lock.py"
    lines = "".join(f"    {line!r}\n" for line in lock.splitlines(keepends=True))
    target.write_text('"""生成ファイル。scripts/generate_dependency_lock.py で更新する。"""\n\n'
                      f'REQUIREMENTS_SHA256 = {input_hash!r}\nLOCK_TEXT = (\n{lines})\n', encoding="utf-8")
    return target


if __name__ == "__main__":
    print(generate(Path(__file__).resolve().parents[1]))
