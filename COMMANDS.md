# コマンド・操作早見表

[READMEに戻る](README.md)

コマンドはプロジェクトのフォルダ内で実行します。例の `642`・ユーザーネーム・学生番号は自分のものに置き換えてください。教室番号には、QRの出席URL用の番号を指定します。

<a id="setup"></a>

## 初回セットアップ

```sh
git clone https://github.com/HR0101/AttendanceSystem.git
cd AttendanceSystem
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp config.example.json config.json
./att --check 642
```

最後のコマンドで初回の認証情報を入力します。ZIPで取得した場合は、展開したフォルダで `git clone` と `cd` を除くコマンドを実行してください。

<a id="attendance"></a>

## 出席確認・登録

```sh
./att --ui                # GUIを開く
./att --ui 642            # 指定教室の確認画面を開く
./att --ui --check        # GUIで登録を無効にする
./att --ui --demo 642     # 大学に接続せずGUIを試す
./att --check 642         # 状態を確認する（登録しない）
./att 642                 # 出席登録まで実行する
./att                     # 教室番号を対話入力して登録する
```

`./attendance` でも同じコマンドを使えます。`start.command` のダブルクリックでも起動できます。

### GUIの基本操作

| キー | 操作 |
| --- | --- |
| 数字 / Backspace | QR番号の入力 / 削除 |
| Tab / ↑↓ | 項目を選択 |
| Enter | 授業情報の取得・選択項目の実行 |
| R | 授業情報を更新 |
| U | ユーザー管理 |
| S | 自動出席の予定 |
| H / ? | ヘルプ |
| Q | 終了 |
| Tab / ←→ → Enter | 出席登録の確認画面で選択・決定 |
| Esc | 確認・入力をキャンセル |

デモではQR番号の入力欄以外を選んだ状態で、1:未出席、2:出席済み、3:受付対象なし、4:履修未登録、5:通信エラーを試せます。デモ中の変更は保存されません。

<a id="users"></a>

## ユーザー管理

```sh
./att user add                            # 対話形式でユーザーを追加
./att user add はるか --student-id K1234567 # パスワードを入力して追加
./att user list                           # 一覧（* は既定ユーザー）
./att user use はるか                     # 通常実行・自動実行の既定ユーザーを変更
./att user rename はるか Haruka           # 表示名を変更
./att 642 --user Haruka                   # 指定ユーザーで出席登録
./att --ui 642 --user Haruka              # 指定ユーザーでGUIを開く
```

名前に空白がある場合は引用符で囲みます。`--user` を省略すると既定ユーザーを使います。

### GUIのユーザー管理（U）

| キー | 操作 |
| --- | --- |
| ↑↓ → Enter | 今回のGUIで使うユーザーを切り替え |
| A | ユーザーを追加 |
| N | ユーザーネームを変更 |
| D → Enter | 通常実行・自動実行の既定ユーザーを変更 |

フォームはTab・↑↓で移動し、最後の項目でEnterを押すと保存します。Escで取り消せます。

<a id="schedule"></a>

## 自動出席の予定

```sh
./att --ui                # 起動後、Sで予定画面を開く
./att schedule status     # 自動実行の状態を確認
./att schedule install    # 自動実行を開始・設定を反映
./att schedule remove     # 自動実行を停止
```

### GUIの予定管理（S）

| キー | 操作 |
| --- | --- |
| A | 授業を追加 |
| ↑↓ → Enter | 選んだ授業を編集 |
| T | 選んだ授業を有効 / 無効にする |
| D → Enter | 選んだ授業を削除 |
| P | 授業期間・休講日を設定 |
| I | 自動実行全体を開始 / 停止 |
| R | 予定を再読込 |
| Esc | 戻る・キャンセル |
| Tab / ↑↓ | 入力項目を移動 |
| Ctrl+U | 現在の入力欄を空にする |
| 最後の項目でEnter | 保存 |

授業名は出席画面と一致させます。曜日は `mon`～`sun` または「月」～「日」、時刻は `14:00` の形式です。休講日を複数指定する場合は `2026-11-03,2026-11-23` のように入力します。
授業情報を取得してからAで追加すると、授業名・開始時刻・QR番号が入力済みになります。

| 登録方法 | 登録時刻 / 範囲開始 | 範囲終了 |
| --- | --- | --- |
| 固定時刻 | `14:05` | 空欄 |
| 範囲内から日ごとに選択 | `14:05` | `14:15` |
| 授業開始5分後 | 空欄 | 空欄 |

一覧の `14:00→14:05～14:15` は「授業開始→登録時刻の範囲」です。範囲は同日内で、両端を含め1分単位で選びます。同じ授業・日付では再起動しても選択は変わりません。別の日でも同じ時刻になる場合があります。

授業ごとの有効／無効と、自動実行全体の開始／停止は別です。自動実行中の保存は起動時刻にも反映され、有効な授業がなくなると停止します。反映に失敗した場合はIで停止・再開してください。
自動実行は既定ユーザーで行います。GUIを閉じても動作しますが、Macは起動・ログイン・スリープ解除しておいてください。遅延時の扱いは[READMEの自動実行について](README.md#automation)を参照してください。

### JSONを直接編集する場合

```sh
cp schedule.example.json schedule.json # 初めて予定ファイルを作る場合のみ
# schedule.json に授業期間・休講日・授業を設定
./att schedule install                # 編集した起動時刻を反映
```

既存の `schedule.json` がある場合はコピーせず、そのファイルを編集してください。設定項目は[READMEの設定ファイル](README.md#configuration)を参照してください。

## ヘルプ

```sh
./att --help
./att user --help
./att schedule --help
```

<a id="tests"></a>

## 開発用テスト

```sh
.venv/bin/python -m unittest discover -s tests
```

テストは匿名の模擬画面を使い、大学システムにはアクセスしません。
