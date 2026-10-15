#!/bin/bash
# Finder からダブルクリックしてセットアップする。
/bin/bash "$(dirname -- "$0")/install.sh"
status=$?
if [ "$status" -ne 0 ]; then
    echo "セットアップに失敗しました。上のエラーを確認してください。"
fi
read -r -p "Enter キーで終了します。"
exit "$status"
