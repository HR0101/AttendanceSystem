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

最後の `642` は自分の教室番号に置き換えてください。最初の起動時に学生番号・ユーザーネーム・パスワードを入力します。学生番号とユーザーネームはローカルの `config.json`、パスワードは macOS Keychain の `UniversityAttendanceTool` に保存されます。これらは Git の追跡対象外です。`config.json` を共有したり、ログを公開したりしないでください。

Git を使わない場合は GitHub の **Code → Download ZIP** から取得し、展開したフォルダで `git clone` と `cd` を除くセットアップコマンドを実行できます。

## 手動で使う

ターミナルで `./att --ui` を実行すると、出席確認画面が開きます。画面は `--ui` を指定したときだけ開き、`./att 教室番号` は従来どおり出席登録まで自動で実行します。
教室番号を入力して Enter を押すと、大学の画面から授業名・時限・教室名・開始／終了時刻・出席状態を取得します。
Tab または上下キーで「出席で登録する」を選び、Enter を押して確認すると登録します。
R で更新、Q で終了できます。通信中は完了までお待ちください。
画面の大きさは幅62文字・高さ24行以上にしてください。

```sh
./att --ui 642      # 指定教室の確認画面を開く
./att --ui --check  # 登録を無効にして画面を確認
./att --ui --demo 642 # 大学に接続せず、サンプル画面を操作
```

初回の認証情報は画面が開く前に入力します。確認時には登録せず、画面の登録操作時に再度授業を照合します。
出席済み・履修未登録・受付対象なしの場合や `dry_run` が有効な場合は登録できません。

従来のコマンドでも使えます。

```sh
./att --check 642  # 状態の確認のみ
./att 642          # 出席登録まで実行
```

教室番号を省略すると入力を求めます。`./attendance` も同じコマンドです。`start.command` をダブルクリックして起動することもできます。どのフォルダからでも `att` と呼びたい場合は、`att` を PATH に含まれるフォルダへシンボリックリンクしてください。

入力番号はQRの出席URLを指定する番号です。`642` は `/attendance/class_room/642` に、7号館の `731` は `/attendance/class_room/7301` に変換します。QR番号と授業情報の教室名は異なる場合があります。例えばQRのURLが `/attendance/class_room/435`、授業教室が432の場合も `435` を指定してください。GUIの「QR番号」には入力値、「教室名」には大学の画面に表示された授業教室を表示します。登録時は指定URLと授業情報を照合します。

`config.json` の `dry_run` を `true` にすると確認のみになります。初期値は `false` なので、対象授業と履修状態が確認できれば登録ボタンを押します。`browser.headless` は初期値が `true` です。画面を見ながら確認する場合は `false` に変更してください。

## 複数ユーザーとユーザーネーム

```sh
./att user add はるか --student-id K1234567 # パスワードを入力して追加
./att user list                          # 一覧（* は既定ユーザー）
./att user use はるか                    # 既定ユーザーを切り替え
./att user rename はるか Haruka          # 表示名を変更
./att 642 --user Haruka                  # 指定ユーザーで自動登録
./att --ui 642 --user Haruka             # 指定ユーザーのGUIを開く
```

ユーザーネームは表示・選択用の名前です。大学へのログインには学生番号を使います。
`user add` だけでも対話形式で登録できます。名前に空白がある場合は引用符で囲んでください。
ユーザーネームと学生番号の重複登録はできません。パスワードはユーザーごとにKeychainへ保存し、設定ファイルには書き込みません。
`--user` を省略した場合は既定ユーザーで実行します。自動実行も既定ユーザーを使うので、`user use` による切替は自動実行にも反映されます。
既存の `student_id` 設定はそのまま利用でき、最初は学生番号をユーザーネームとして扱います。`user list` で確認し、`user rename` で名前を付けられます。

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

`install` は macOS のユーザー用 LaunchAgent を登録します。授業開始5分後に起動し、開始30分を超えたら登録しません。設定した学期外や休講日も対象外です。指定した出席URL、授業名、開始時刻を確認できない場合は停止します。予定の `room` にはQR用の番号を指定してください。Mac が終了している間は動きません。

## 判定とログ

すでに出席済みの場合は再登録しません。履修未登録の警告や受付対象なしの表示があれば、出席ボタンを押さずに結果を表示します。「現在、出席できる授業はありません。」という表示だけでは、時間外か授業がないかは判定できません。登録後も出席済み表示を確認します。結果はターミナルとローカルの `logs/` に記録されます。

大学側で URL や画面が変更された場合、公式画面を確認して `config.json` を更新してください。`login_url` と `top_url` には同じホスト名を指定します。

## 開発用テスト

依存パッケージと Chromium をセットアップした後、`.venv/bin/python -m unittest discover -s tests` で実行できます。テストには匿名の模擬画面を使用し、大学システムにはアクセスしません。

## ライセンス

MIT License。詳しくは [LICENSE](LICENSE) を参照してください。
