# 依存パッケージの自動更新（v0.2.3）

## 利用者の動作

更新版のロック情報が変わると、更新の準備中に固定版のパッケージとChromiumをダウンロードする。`pip install --require-hashes --only-binary=:all:`、`pip check`、Chromiumの導入、ブラウザの起動確認を通してから、終了時にアプリ本体と実行環境を一緒に切り替える。

準備が失敗した場合、現在のソースと実行環境は変更しない。切替中の失敗・中断、明示的な `att update --rollback` ではソースと依存環境を一緒に戻す。`config.json`、`schedule.json`、Keychain、ログ、元の `.venv` は更新対象外。

v0.2.2からv0.2.3へ最初に更新する時は `requirements.txt` を変更していないため旧クライアントでも受信できる。旧クライアントは依存環境の準備機能を持たないため、移行後の最初の通常起動または `att update` で専用環境を作る。ヘルプ・バージョン表示・デモ・ロールバックでは準備を行わない。

## ロック情報の配布

`requirements.lock` は従来どおり更新ZIPへ直接追加しない。v0.2.2以前がZIP内の未知の `.lock` ファイルを拒否するため、同じテキストを `src/dependency_lock.py` の文字列として同梱する。

```sh
uv pip compile requirements.txt --universal --generate-hashes --python-version 3.10 --exclude-newer "14 days" --no-header -o requirements.lock
python3 scripts/generate_dependency_lock.py
```

生成ファイルには `requirements.txt` のSHA-256も含める。受信側はデータをASTで読み、任意のPythonコードとして実行しない。入力ファイルとの対応を確認し、配布ビルドではロックファイルと同梱テキストの一致も確認する。CIのハッシュ付き導入・脆弱性監査と、書き込み権限を持つ公開ジョブの分離は維持する。

## 環境と切替の記録

- `.updates/environments/<UUID>/`: パッケージごとの専用venv。Chromiumもこの中の `browsers/` に置く。
- `.updates/runtime.json`: 使用する環境IDとロックのハッシュ。
- `.updates/pending/metadata.json`: 検証済み更新版の環境ID。
- `.updates/transaction/runtime.json`: 切替前の環境。ソースの復元記録と一緒に保存する。
- `.updates/dependencies.log`: 導入・整合性確認・ブラウザ起動の段階と結果。

venvには絶対パスが含まれるため、候補環境は最終配置先で作成し、その後フォルダを移動しない。同じロックとPython・プラットフォームの環境は再利用する。元の環境は復元用に保持し、準備に失敗した候補だけを削除する。タイムアウトや中断時は、Playwrightの子プロセスも停止してから未完成の環境を削除する。

`att`、`attendance`、直接の `python main.py`、既存LaunchAgentのいずれから起動しても、アプリ読込前に選択した環境のPythonへ再実行する。切替は実行ロックを排他的に取得して行い、待機後にロック情報を読み直す。準備時に実行中の別の授業処理やそのパッケージを変更しない。

実行環境の参照はUUID・ロックハッシュ・Python・プラットフォーム・実際の配置先・準備完了記録で検証する。任意のパスやシンボリックリンクを実行先として受け入れない。復元処理で必要な検証は標準ライブラリだけで実行でき、パッケージやアプリのモジュールが壊れていても復元記録を読み取れる。

## Issueとの対応

- #10: 403の利用制限を通常の拒否と区別し、解除時刻を表示・保存して、その時刻まで自動確認を抑止する。
- #11: API呼び出しゼロを維持したまま公開JSONのETagを保存し、304時は検証済みのキャッシュを使う。キャッシュ不正時は条件なしで取得し直す。
- #12: v0.2.2で実装したJSON方式に、取得開始・リダイレクト先のホストとHTTPSの検証を追加する。APIへのフォールバックは行わない。

## 検証

`test_updater_dependencies.py` は、各準備段階の失敗、環境参照の検証、ソースと環境の一括切替・復元、古い復元記録との互換性を確認する。ローカルwheelによる実pipテストでは、ハッシュ不一致による停止、パッケージ版の変更、アプリの再実行、ロールバックをネットワークなしで確認する。

実際の `requirements.lock` を使った隔離環境でもダウンロードとハッシュ検証を行い、Playwright 1.64.1・keyring 25.7.0などの導入、対応するChromiumの導入・起動を確認した。実行結果とログは `dist/dependency-live-20261116-153313/` に保存した。この検証は現在使用中の `.venv` と実際の個人設定を変更していない。

公開中のv0.2.2のJSONにも新しい取得処理で2回アクセスし、`github.com` → `release-assets.githubusercontent.com` のリダイレクト検証、ETag保存、2回目の304とキャッシュ再利用を確認した。GitHub APIは呼び出していない。今回の更新ZIPは、v0.2.2の検証処理でも受信可能であることを確認した。
