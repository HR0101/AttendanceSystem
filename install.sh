#!/bin/bash
# macOS 用。管理者権限なしでセットアップし、att を登録する。
set -euo pipefail

tool_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bin_dir="$HOME/.local/bin"
command_path="$bin_dir/att"
path_line='export PATH="$HOME/.local/bin:$PATH"'

fail() {
    echo "セットアップできません: $*" >&2
    exit 1
}

[ "$(uname -s)" = Darwin ] || fail "macOS が必要です。"
command -v python3 >/dev/null 2>&1 || fail "Python 3.10 以降をインストールしてから再実行してください。"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail "Python 3.10 以降が必要です。"

case "${SHELL##*/}" in
    zsh) profile="${ZDOTDIR:-$HOME}/.zshrc" ;;
    bash) profile="$HOME/.bash_profile" ;;
    *) fail "対応シェルは zsh / bash です。ログインシェルを確認してください。" ;;
esac

if [ -e "$command_path" ] || [ -L "$command_path" ]; then
    if [ -L "$command_path" ]; then
        link_target=$(readlink "$command_path" 2>/dev/null || true)
        if [ "$link_target" != "$tool_dir/att" ]; then
            if [ ! -e "$command_path" ]; then
                fail "${command_path} はリンク先が見つからない壊れたシンボリックリンクです（リンク先: ${link_target}）。ls -l \"${command_path}\" で確認し、不要なら rm \"${command_path}\" を実行してから bash install.sh を再実行してください。"
            fi
            fail "${command_path} は別の場所（${link_target}）へのシンボリックリンクです。ls -l \"${command_path}\" で確認し、このフォルダへ切り替える場合は rm \"${command_path}\" を実行してから bash install.sh を再実行してください。"
        fi
    else
        fail "${command_path} が既に存在します。別のコマンドでないか ls -l \"${command_path}\" で確認し、不要な場合のみ rm \"${command_path}\" を実行してから bash install.sh を再実行してください。"
    fi
fi
lock_path="$tool_dir/requirements.lock"
if [ -L "$lock_path" ] || { [ -e "$lock_path" ] && [ ! -f "$lock_path" ]; }; then
    fail "requirements.lock が通常のファイルではありません。内容を確認してから再実行してください。"
fi
if [ ! -e "$lock_path" ]; then
    for rel in "src" "src/__init__.py" "src/dependencies.py" "src/dependency_lock.py" "src/updater.py" "requirements.txt"; do
        if [ -L "$tool_dir/$rel" ]; then
            fail "requirements.lock の復元に使う $rel がシンボリックリンクです。配布元から完全なファイルを取得して再実行してください。"
        fi
    done
    if [ ! -d "$tool_dir/src" ] \
        || [ ! -f "$tool_dir/src/__init__.py" ] \
        || [ ! -f "$tool_dir/src/dependencies.py" ] \
        || [ ! -f "$tool_dir/src/dependency_lock.py" ] \
        || [ ! -f "$tool_dir/src/updater.py" ] \
        || [ ! -f "$tool_dir/requirements.txt" ]; then
        fail "requirements.lock が見つかりません。配布元から完全なファイルを取得して再実行してください。"
    fi
    # 更新用ZIPには requirements.lock が含まれないため、検証済みの埋め込みロック情報から復元する。
    PYTHONPATH="" PYTHONNOUSERSITE=1 python3 -c '
import os
from pathlib import Path
import re
import sys
import tempfile

tool_dir = Path(sys.argv[1])
sys.path = [str(tool_dir), *(p for p in sys.path if p and p != ".")]
for rel in ("src", "src/__init__.py", "src/dependencies.py", "src/dependency_lock.py", "src/updater.py", "requirements.txt"):
    target = tool_dir / rel
    if target.is_symlink() or (not target.is_dir() if rel == "src" else not target.is_file()):
        raise SystemExit(1)
lock_path = tool_dir / "requirements.lock"
if lock_path.is_symlink() or lock_path.exists():
    raise SystemExit(1)

from src.dependencies import read_spec
from src.updater import UpdateError

try:
    spec = read_spec(tool_dir)
except UpdateError as exc:
    print(exc, file=sys.stderr)
    raise SystemExit(1)

entries, buf = [], []
for raw in spec["text"].splitlines():
    line = raw.strip()
    if not buf and (not line or line.startswith("#")):
        continue
    if buf and line.startswith("#"):
        raise SystemExit(1)
    if line.endswith("\\"):
        buf.append(line[:-1].strip())
        continue
    buf.append(line)
    entries.append(" ".join(part for part in buf if part))
    buf.clear()
if buf or not entries:
    raise SystemExit(1)
for entry in entries:
    parts = entry.split("--hash=")
    if len(parts) < 2:
        raise SystemExit(1)
    head = parts[0].strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?==[A-Za-z0-9.+!_*-]+(?:\s*;.*)?$", head):
        raise SystemExit(1)
    if re.search(r"(?:^|\s)-", head):
        raise SystemExit(1)
    for digest in parts[1:]:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest.strip()):
            raise SystemExit(1)

fd, tmp_name = tempfile.mkstemp(dir=tool_dir, prefix=".requirements.lock.")
try:
    with os.fdopen(fd, "wb") as stream:
        stream.write(spec["text"].encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(tmp_name, 0o644)
    if lock_path.is_symlink() or lock_path.exists():
        raise SystemExit(1)
    os.replace(tmp_name, lock_path)
finally:
    if os.path.exists(tmp_name):
        os.unlink(tmp_name)
' "$tool_dir" || fail "requirements.lock を検証・復元できませんでした。配布元から完全なファイルを取得して再実行してください。"
fi
[ -f "$lock_path" ] && [ ! -L "$lock_path" ] && [ -s "$lock_path" ] \
    || fail "requirements.lock が見つからないか不正です。配布元から完全なファイルを取得して再実行してください。"

echo "Python 環境とブラウザを準備しています…"
if [ ! -e "$tool_dir/.venv" ] && [ ! -L "$tool_dir/.venv" ]; then
    python3 -m venv "$tool_dir/.venv"
elif [ ! -d "$tool_dir/.venv" ]; then
    fail ".venv が正しいディレクトリではありません。rm -rf \"${tool_dir}/.venv\" を実行してから bash install.sh を再実行してください。"
fi
[ -x "$tool_dir/.venv/bin/python" ] \
    || fail ".venv が不完全または破損しています。rm -rf \"${tool_dir}/.venv\" を実行してから bash install.sh を再実行してください。"
"$tool_dir/.venv/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || fail ".venv の Python は 3.10 以降である必要があります（または .venv が破損しています）。rm -rf \"${tool_dir}/.venv\" を実行してから bash install.sh を再実行してください。"
# ロックファイルに記録したハッシュと一致する wheel だけを導入する。
"$tool_dir/.venv/bin/python" -m pip install --no-input --require-hashes --only-binary=:all: -r "$tool_dir/requirements.lock"
unset PLAYWRIGHT_DOWNLOAD_HOST PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT
"$tool_dir/.venv/bin/python" -m playwright install chromium

if [ ! -e "$tool_dir/config.json" ] && [ ! -L "$tool_dir/config.json" ]; then
    (umask 077; cp "$tool_dir/config.example.json" "$tool_dir/config.json")
fi

chmod u+x "$tool_dir/att"
mkdir -p "$bin_dir"
if [ ! -L "$command_path" ]; then
    ln -s "$tool_dir/att" "$command_path"
fi

if ! grep -Fqx "$path_line" "$profile" 2>/dev/null; then
    mkdir -p "$(dirname -- "$profile")"
    printf '\n# 出席ツール: att コマンド\n%s\n' "$path_line" >> "$profile"
fi

echo "セットアップが完了しました。"
echo "新しいターミナルを開き、att --ui を実行してください。"
echo "このターミナルですぐ使う場合:"
printf '  source %q\n' "$profile"
echo "初回は att user add で自分の認証情報を登録できます。"
echo "このフォルダは削除・移動せず、そのまま保管してください: $tool_dir"
