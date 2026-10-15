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
    [ -L "$command_path" ] && [ "$(readlink "$command_path")" = "$tool_dir/att" ] \
        || fail "$command_path が既に存在します。内容を確認してから再実行してください。"
fi

echo "Python 環境とブラウザを準備しています…"
if [ ! -d "$tool_dir/.venv" ]; then
    python3 -m venv "$tool_dir/.venv"
fi
[ -x "$tool_dir/.venv/bin/python" ] || fail ".venv が不完全です。.venv を確認してください。"
"$tool_dir/.venv/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || fail ".venv の Python は 3.10 以降である必要があります。"
"$tool_dir/.venv/bin/python" -m pip install -r "$tool_dir/requirements.txt"
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
