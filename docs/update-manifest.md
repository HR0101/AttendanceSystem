# APIを使用しない更新情報の取得

v0.2.2から、利用者の更新確認はGitHub Releasesの添付JSONを直接取得する。GitHub REST APIへのアクセスやAPIへのフォールバックは行わない。

## 取得経路

1. `https://github.com/HR0101/AttendanceSystem/releases/latest/download/attendance-update.json` を取得する。
2. JSONを検証する。v0.2.3以降の通常起動は、新しい版がある場合に `att update --notes` と `att update` の案内を表示して終わる。ETagを保存し、304応答では検証済みJSONを再利用する。
3. 利用者が `att update` を実行した時だけ、その版に固定した `releases/download/vX.Y.Z/attendance-update.zip` を取得する。
4. 実際のZIPサイズとSHA-256がJSONに一致することを確認する。
5. 既存のファイル構成・Python構文・内部バージョン・依存関係・起動確認を通した後、終了時に適用する。

`latest` がZIP取得前に別の版へ切り替わっても、取得するZIPはJSONの版番号に固定される。JSONが取得できない場合や形式が不正な場合は失敗として扱い、APIを呼び直さない。

v0.2.3以降は取得開始時とすべてのリダイレクトで、HTTPSと正確なホスト名を検証する。許可するホストは `github.com`、`objects.githubusercontent.com`、`release-assets.githubusercontent.com`、`github-releases.githubusercontent.com`。HTTPへの変更、認証情報付きURL、別ポート、似た名前の第三者ホストは拒否する。

403の利用制限ヘッダーや429を検出すると、`X-RateLimit-Reset` と `Retry-After` から再試行時刻を計算する。手動コマンドでは原因と時刻を表示し、通常起動ではログだけに残す。通常起動の確認はその時刻まで待ち、時刻を過ぎたら通常の1時間待機を追加せず再確認する。通常の403拒否は利用制限と区別する。

## JSONの形式

```json
{
  "schema_version": 1,
  "version": "0.2.3",
  "release_notes": "# v0.2.3\n\n## 変更内容\n\n- 更新通知を追加しました。\n\n## 利用者の操作\n\n- att update で更新してください。\n",
  "archive": {
    "name": "attendance-update.zip",
    "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "size": 54000
  }
}
```

上の `sha256` と `size` は説明用の仮の値。`scripts/build_release.py` が実際のZIPから値を計算する。対応する形式バージョンは整数の1、版番号はプレフィックスのない安定版の3整数形式に限定する。ZIP名は固定で、任意の取得先URLをJSONから受け取らない。

JSONは更新用ZIPの外に置く。v0.2.1以前はZIP内の未知のファイルを拒否するが、別アセットとして添付するJSONは旧版のZIP検証に影響しない。ZIPとJSONはHTTPSで同じ配布元から取得するため、SHA-256は破損・取り違えを検出するものであり、独立した発行者署名ではない。

v0.2.3以降のビルドは `updates/X.Y.Z.md` を必須とし、同じテキストを `release_notes` とGitHub Releaseの説明に使う。16KiB以内、先頭の版番号、変更内容・利用者の操作の具体的な箇条書きを検証する。古いリリースにはこのフィールドがないため、受信側では省略を許可し、`att update --notes` でリリースページへ案内する。未知のフィールドを許可する既存の形式バージョン1との互換性を保つ。

## 配布

`python3 scripts/check_update_notes.py` で更新内容を確認し、`python3 scripts/build_release.py v0.2.3` でZIPとJSONを同時に生成する。CIは通常ブランチでは既定ブランチから、PRではベースコミットからの差分に現在版の更新ファイルが含まれることも確認する。既定ブランチへのpushでは変更前コミットと比較する。タグではファイルと版・タグの一致を検証する。配布CIは既存の読み取り専用テストジョブと公開ジョブの分離を維持し、両アセットをドラフトへ添付した後で公開・latest指定する。新しい依存ライブラリは不要。

JSONがない最新リリースは、配布準備が不足している場合とリリース自体がない場合を区別できない。そのため404を「最新版」として扱わず、JSONが見つからないことを表示する。

## 旧版からの初回移行

旧版の更新プログラムはAPI経路を使用する。API制限がかかっていない回線なら従来の `att update` でv0.2.2以降へ移行できる。403が続く環境では、Git導入なら既存フォルダで `git pull --ff-only`、ZIP導入ならソースZIPから `src/`・`main.py`・起動スクリプトを既存フォルダへ反映する。`config.json`・`schedule.json`・Keychain・ログは保持する。以降の更新確認はJSON経路を使用する。

## 検証

`tests/test_updater_manifest.py` は、APIへのリクエストに403を返す環境でJSONとZIPだけから更新できることを検証する。JSON取得失敗時にもAPIへ戻らないこと、形式・サイズ・SHA-256・ZIP名・版番号の検証、配布用JSONの生成とZIP外への配置を確認する。既存の更新・復元・個人設定保持テストも新しいJSON形式で実行する。

2026-11-16のローカル検証では、更新関連79テストと全体160テストが成功した。生成したZIPはv0.2.1の検証処理でも受け入れられ、配布対象のPythonコードは3.10の構文でも検証した。

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_updater*.py'
```
