#!/bin/bash
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 が見つかりません。"
  read -r -p "Enter キーで終了します。"
  exit 1
fi
if [ -x .venv/bin/python ]; then
  .venv/bin/python main.py
else
  python3 main.py
fi
status=$?
if [ "$status" -ne 0 ]; then
  echo "処理に失敗しました。logs フォルダを確認してください。"
fi
read -r -p "Enter キーで終了します。"
exit "$status"
