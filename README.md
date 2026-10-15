# 出席ツール

macOS で、本人が受講する授業の出席画面を確認し、出席登録を行う Python ツールです。大学・授業の規則で自動化が許可されている場合に利用してください。追加認証や想定外の画面が現れた場合は停止します。

## 動作環境

- macOS、Python 3.10 以降
- zsh または bash（自動セットアップ対応）
- インターネット接続と対象大学の出席システムを利用できるアカウント
- 初回セットアップ用のターミナル

GUIはターミナル内で動作します。幅62文字・高さ24行以上が必要で、幅106文字以上では操作ガイドと今回の操作履歴も表示します。

## 使い方

### ダウンロード・初回セットアップ

Python 3.10以降を用意し、次のコマンドで取得・セットアップします。

```sh
git clone https://github.com/HR0101/AttendanceSystem.git
cd AttendanceSystem
bash install.sh
```

ZIPで取得する場合は、[GitHubリポジトリ](https://github.com/HR0101/AttendanceSystem)の「Code → Download ZIP」からダウンロード・展開し、フォルダ内の [install.command](install.command) をダブルクリックしてください。展開したフォルダで `bash install.sh` を実行する方法も使えます。

セットアップは仮想環境・依存ライブラリ・Chromiumを導入し、初回のみ `config.json` を作成します。`att` コマンドの登録とPATH設定も自動で行うため、エイリアスやPATHを手動で設定する必要はありません。管理者権限は不要で、既存の設定ファイルや同名の別コマンドは上書きしません。

### セットアップ後の起動

新しいターミナルを開き、次を実行してください。`att` はどのフォルダからでも使えます。

```sh
att user add             # 自分の認証情報を登録
att --ui                 # GUIを開く
att --check 642          # 指定教室の出席状態を確認（登録しない）
```

今のターミナルですぐ使いたい場合や `att: command not found` が出る場合は、セットアップ完了時に表示される `source` コマンドを実行してください。通常のzsh環境では `source ~/.zshrc`、bashでは `source ~/.bash_profile` です。

セットアップは `~/.local/bin/att` にリンクを作り、zshでは `~/.zshrc`（`ZDOTDIR` 設定時はその配下）、bashでは `~/.bash_profile` にPATH設定を追加します。ダウンロードしたフォルダは削除・移動せず保管してください。移動する場合は `~/.local/bin/att` のリンクだけを削除し、移動先で `bash install.sh` を再実行してください。

### 詳しい操作方法

セットアップ、出席登録、ユーザー管理、自動実行のコマンドとGUIの操作は、[コマンド・操作早見表（COMMANDS.md）](COMMANDS.md)にまとめています。

- [初回セットアップ](COMMANDS.md#setup)
- [出席確認・登録](COMMANDS.md#attendance)
- [ユーザー管理](COMMANDS.md#users)
- [自動出席の予定・登録時刻の指定](COMMANDS.md#schedule)
- [開発用テスト](COMMANDS.md#tests)

<a id="configuration"></a>

## 設定ファイル

初回起動時に学生番号・ユーザーネーム・パスワードを入力します。学生番号とユーザーネームはローカルの `config.json`、パスワードは macOS Keychain の `UniversityAttendanceTool` に保存します。これらはGitの追跡対象外です。`config.json` を共有したり、ログを公開したりしないでください。

ユーザーネームは表示・選択用で、大学へのログインには学生番号を使います。名前と学生番号の重複登録はできません。既存の `student_id` 設定も利用でき、初めは学生番号をユーザーネームとして扱います。

`config.json` の `dry_run` を `true` にすると確認のみになります。初期値は `false` です。`browser.headless` は初期値が `true` で、`false` にするとブラウザを表示します。自動実行には `true` が必要です。

### 教室番号

入力番号はQRの出席URL用の番号です。`642` は `/attendance/class_room/642` に、7号館の `731` は `/attendance/class_room/7301` に変換します。QR番号と授業情報の教室名は異なる場合があります。例えばQRのURLが `/attendance/class_room/435`、授業教室が432の場合も `435` を指定してください。GUIでは入力値を「QR番号」、大学側の表示を「教室名」として表示します。

### 自動実行の予定

予定は `schedule.json` に保存します。[schedule.example.json](schedule.example.json)をひな形にしてJSONを編集するか、GUIで授業ごとに設定できます。

| 項目 | 内容 |
| --- | --- |
| `active_from` / `active_until` | 授業期間（YYYY-MM-DD） |
| `blackout_dates` | 休講日の配列（YYYY-MM-DD） |
| `courses` | 授業設定の配列 |
| `weekday` | 授業の曜日（mon～sun） |
| `start_time` | 大学の画面と照合する授業開始時刻（HH:MM） |
| `room` | QR用の教室番号 |
| `course_name` | 大学の出席画面と一致する授業名 |
| `register_time` | 登録時刻。範囲指定時は範囲開始（HH:MM） |
| `register_until` | 範囲終了（HH:MM）。省略・空欄なら固定時刻 |
| `enabled` | 授業ごとの有効／無効。省略時はtrue |

登録時刻と範囲終了を両方省略・空欄にすると、授業開始5分後に実行します。範囲指定の入力例は[コマンド・操作早見表](COMMANDS.md#schedule)を参照してください。

<a id="automation"></a>

## 自動実行について

自動実行はmacOSのユーザー用LaunchAgentを使い、既定ユーザーで行います。GUIで今回使うユーザーを切り替えても、自動実行のユーザーは変わりません。GUIを閉じても動作しますが、Macの終了中は動きません。事前に一度手動でログインし、Keychainにパスワードを保存してください。

固定時刻のみの予定では、スリープ復帰等による遅延は登録予定時刻から25分まで受け付けます。範囲指定を含む予定では範囲内の各分に確認し、選んだ登録時刻の分だけ出席処理を行います。この場合、固定時刻の予定も含めて遅延した分の出席は行いません。登録時刻にMacを起動・ログイン・スリープ解除しておいてください。

設定した学期外・休講日は対象外です。指定した出席URL、授業名、開始時刻を確認できない場合は停止します。

## 判定とログ

すでに出席済みの場合は再登録しません。履修未登録の警告や受付対象なしの表示があれば、出席ボタンを押さずに結果を表示します。「現在、出席できる授業はありません。」という表示だけでは、時間外か授業がないかは判定できません。登録後も出席済み表示を確認します。結果はターミナルとローカルの `logs/` に記録します。

GUIでは授業情報の取得時に登録せず、画面で登録するときに再度授業を照合します。出席済み・履修未登録・受付対象なしの場合や `dry_run` が有効な場合は登録できません。

大学側でURLや画面が変更された場合、公式画面を確認して `config.json` を更新してください。`login_url` と `top_url` には同じホスト名を指定します。

## ライセンス

MIT License。詳しくは [LICENSE](LICENSE) を参照してください。
