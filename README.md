# 出席ツール

macOS で、本人が受講する授業の出席画面を確認し、出席登録を行う Python ツールです。大学・授業の規則で自動化が許可されている場合に利用してください。追加認証や想定外の画面が現れた場合は停止します。

## 動作環境

- macOS、Python 3.10 以降
- インターネット接続と対象大学の出席システムを利用できるアカウント
- 初回セットアップ用のターミナル

## セットアップ

```sh
git clone https://github.com/HR0101/AttendanceSystem.git
cd AttendanceSystem
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp config.example.json config.json
./att --check 642
```

最後の `642` は自分の教室番号に置き換えてください。最初の起動時に学生番号とパスワードを入力します。学生番号はローカルの `config.json`、パスワードは macOS Keychain の `UniversityAttendanceTool` に保存されます。これらは Git の追跡対象外です。`config.json` を共有したり、ログを公開したりしないでください。

Git を使わない場合は GitHub の **Code → Download ZIP** から取得し、展開したフォルダで `git clone` と `cd` を除くセットアップコマンドを実行できます。

## 手動で使う

```sh
./att --check 642  # 状態の確認のみ
./att 642          # 出席登録まで実行
```

教室番号を省略すると入力を求めます。`./attendance` も同じコマンドです。`start.command` をダブルクリックして起動することもできます。どのフォルダからでも `att` と呼びたい場合は、`att` を PATH に含まれるフォルダへシンボリックリンクしてください。

教室番号 `642` は `/attendance/class_room/642` に、7号館の `731` は `/attendance/class_room/7301` に変換します。画面の教室番号が指定値と一致しなければ停止します。

`config.json` の `dry_run` を `true` にすると確認のみになります。初期値は `false` なので、対象授業と履修状態が確認できれば登録ボタンを押します。`browser.headless` は初期値が `true` です。画面を見ながら確認する場合は `false` に変更してください。

## 曜日・時刻で自動実行

`schedule.example.json` を `schedule.json` にコピーし、授業期間、休講日、授業を設定します。次は `courses` の記入例です。授業名は出席画面の表示と一致させてください。

```json
{
  "active_from": "2026-09-18",
  "active_until": "2026-12-21",
  "blackout_dates": ["2026-11-23"],
  "courses": [
    {
      "weekday": "mon",
      "start_time": "10:00",
      "room": "731",
      "course_name": "画面に表示される授業名"
    }
  ]
}
```

日付と授業は自分の学期に合わせて変更してください。`weekday` は `mon` から `sun` です。事前に一度手動でログインし、Keychain にパスワードを保存してください。

```sh
./att schedule status
./att schedule install
./att schedule remove
```

`install` は macOS のユーザー用 LaunchAgent を登録します。授業開始5分後に起動し、開始30分を超えたら登録しません。設定した学期外や休講日も対象外です。教室番号、授業名、開始時刻が画面と一致しない場合は停止します。Mac が終了している間は動きません。

## 判定とログ

すでに出席済みの場合は再登録しません。履修未登録の警告や受付対象なしの表示があれば、出席ボタンを押さずに結果を表示します。「現在、出席できる授業はありません。」という表示だけでは、時間外か授業がないかは判定できません。登録後も出席済み表示を確認します。結果はターミナルとローカルの `logs/` に記録されます。

大学側で URL や画面が変更された場合、公式画面を確認して `config.json` を更新してください。ログイン画面と出席画面のホスト名が異なる構成にも対応しています。

## 開発用テスト

依存パッケージと Chromium をセットアップした後、`.venv/bin/python -m unittest discover -s tests` で実行できます。テストには匿名の模擬画面を使用し、大学システムにはアクセスしません。

## ライセンス

MIT License。詳しくは [LICENSE](LICENSE) を参照してください。
