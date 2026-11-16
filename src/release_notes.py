"""利用者向けの更新内容。CIと配布JSONで同じ形式を使う。"""

import re

MAX_NOTES_BYTES = 16 * 1024
SECTIONS = ("変更内容", "利用者の操作")


def validate_notes(text, version):
    if (not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_NOTES_BYTES
            or any(ord(char) < 32 and char not in "\n\t" or 127 <= ord(char) <= 159 for char in text)):
        raise ValueError("更新内容は16KiB以内の、制御文字を含まないUTF-8テキストにしてください。")
    if text.splitlines()[0].strip() != f"# v{version}":
        raise ValueError(f"更新内容の先頭を # v{version} にしてください。")
    if re.search(r"\b(?:TODO|TBD)\b|記入してください", text, re.IGNORECASE):
        raise ValueError("更新内容の未記入箇所を埋めてください。")
    for title in SECTIONS:
        match = re.search(rf"^## {title}\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
        if not match or not re.search(r"^- \S.+", match[1], re.MULTILINE):
            raise ValueError(f"更新内容に「## {title}」と具体的な箇条書きを記載してください。")
    return text


def read_notes(root, version):
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("更新内容のバージョンが不正です。")
    path = root / "updates" / f"{version}.md"
    if path.is_symlink() or (root / "updates").is_symlink():
        raise ValueError("更新内容は通常のファイルにしてください。")
    try:
        return validate_notes(path.read_text(encoding="utf-8"), version)
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{path.relative_to(root)} に利用者向けの更新内容を記載してください。") from exc
