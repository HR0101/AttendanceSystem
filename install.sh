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
# ロックファイルがあれば、記録したハッシュと一致するパッケージだけを導入する。
if [ -f "$tool_dir/requirements.lock" ]; then
    "$tool_dir/.venv/bin/python" -m pip install --require-hashes -r "$tool_dir/requirements.lock"
else
    echo "警告: requirements.lock がないため、ハッシュを検証せずに requirements.txt から導入します。" >&2
    echo "      最新版を取得すると、検証付きで導入できます。" >&2
    "$tool_dir/.venv/bin/python" -m pip install -r "$tool_dir/requirements.txt"
fi
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
