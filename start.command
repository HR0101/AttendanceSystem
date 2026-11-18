#!/bin/bash
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/python ]; then
  ".venv/bin/python" main.py --ui "$@"
elif command -v python3 >/dev/null 2>&1; then
  python3 main.py --ui "$@"
else
  echo "Python 3 が見つかりません。"
  read -r -p "Enter キーで終了します。"
  exit 1
fi
status=$?
if [ "$status" -ne 0 ]; then
  echo "処理に失敗しました。logs フォルダを確認してください。"
fi
read -r -p "Enter キーで終了します。"
exit "$status"
