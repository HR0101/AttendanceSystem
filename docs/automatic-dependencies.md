# 依存パッケージの自動更新（v0.2.3）

## 利用者の動作

通常起動では更新情報の確認と通知だけを行う。`att update --notes` で更新内容を表示できる。`att update` の実行時に更新版のロック情報が変わると、準備中に固定版のパッケージとChromiumをダウンロードする。`pip install --require-hashes --only-binary=:all:`、`pip check`、Chromiumの導入、ブラウザの起動確認を通してから、終了時にアプリ本体と実行環境を一緒に切り替える。通常起動や更新内容の表示では適用待ちの更新も適用しない。

準備が失敗した場合、現在のソースと実行環境は変更しない。切替中の失敗・中断、明示的な `att update --rollback` ではソースと依存環境を一緒に戻す。`config.json`、`schedule.json`、Keychain、ログ、元の `.venv` は更新対象外。

v0.2.2からv0.2.3へ最初に更新する時は `requirements.txt` を変更していないため旧クライアントでも受信できる。移行後の通常起動は既存の `.venv` を使い、最初の `att update` で専用環境を作る。ヘルプ・バージョン表示・デモ・更新内容の表示・ロールバックでも準備を行わない。既存の専用環境が破損した場合も `att update` で再準備する。

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
- `.updates/backup/runtime.json`: 直前の版が参照していた環境ID。ロールバック用に保持する。
- `.updates/transaction/runtime.json`: 切替前の環境。ソースの復元記録と一緒に保存する。
- `.updates/dependencies.log`: 導入・整合性確認・ブラウザ起動の段階と結果。

venvには絶対パスが含まれるため、候補環境は最終配置先で作成し、その後フォルダを移動しない。同じロックとPython・プラットフォームの環境は再利用する。直前の環境はロールバック用に保持し、準備に失敗した候補だけを削除する。タイムアウトや中断時は、Playwrightの子プロセスも停止してから未完成の環境を削除する。また、更新適用（`apply_pending`）やロールバック（`finish_update`）の完了時には、実行ロックに続いて `dependency.lock` を排他取得し、上記のいずれの記録からも参照されていない古い・孤立した環境を自動削除する。

`att`、`attendance`、直接の `python main.py`、既存LaunchAgentのいずれから起動しても、アプリ読込前に選択した環境のPythonへ再実行する。切替は実行ロックを排他的に取得して行い、待機後にロック情報を読み直す。準備時に実行中の別の授業処理やそのパッケージを変更しない。

実行環境の参照はUUID・ロックハッシュ・Python・プラットフォーム・実際の配置先・準備完了記録で検証する。任意のパスやシンボリックリンクを実行先として受け入れない。復元処理で必要な検証は標準ライブラリだけで実行でき、パッケージやアプリのモジュールが壊れていても復元記録を読み取れる。

## Chromium の取得元・バージョン検証

Playwright のブラウザ取得は環境変数の上書きによって配布元ホストやスキップ設定を差し替えられるため、`install.sh` と `src/dependencies.py`（`prepare_environment`）の双方で取得前に保護を行う。

1. **上書き環境変数の無害化**: `PLAYWRIGHT_DOWNLOAD_HOST`、`PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST`、`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD`、`PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT` を `install.sh` では `unset`、`prepare_environment`（`sanitized_environment`）ではプロセス環境から除外してから `playwright install chromium` および起動確認を実行する。
2. **配置先・シンボリックリンク・ピン留めリビジョン検証**: 導入後の起動確認ステップで、`p.chromium.executable_path` が `PLAYWRIGHT_BROWSERS_PATH` 配下に実在し（親ディレクトリを含めシンボリックリンクを経由しない実行可能な通常ファイル）、かつハッシュ検証済みで導入された `playwright` パッケージ内の `driver/package/browsers.json` に記録された Chromium リビジョン（`chromium-<revision>`）と一致することを検証してから `headless=True` で起動確認を行う。

## Issueとの対応

- #10: 403の利用制限を通常の拒否と区別し、解除時刻を表示・保存して、その時刻まで自動確認を抑止する。
- #11: API呼び出しゼロを維持したまま公開JSONのETagを保存し、304時は検証済みのキャッシュを使う。キャッシュ不正時は条件なしで取得し直す。
- #12: v0.2.2で実装したJSON方式に、取得開始・リダイレクト先のホストとHTTPSの検証を追加する。APIへのフォールバックは行わない。
- #22: 更新適用・ロールバック完了時に、現在・待機中・バックアップ・復元記録のいずれからも参照されていない `.updates/environments/<UUID>/` を排他ロック下で自動削除し、現在版以下の古い `pending` も更新確認前に破棄する。
- #17: 更新アーカイブ・マニフェストへの ECDSA P-256 署名検証、SLSA Build Provenance アテステーション、および Chromium 取得元環境変数の除外とピン留めリビジョン・配置検証を導入する。

## 検証

`test_updater_dependencies.py` は、各準備段階の失敗、環境参照の検証、ソースと環境の一括切替・復元、古い復元記録との互換性を確認する。ローカルwheelによる実pipテストでは、ハッシュ不一致による停止、パッケージ版の変更、アプリの再実行、ロールバックをネットワークなしで確認する。

実際の `requirements.lock` を使った隔離環境でもダウンロードとハッシュ検証を行い、Playwright 1.64.1・keyring 25.7.0などの導入、対応するChromiumの導入・起動を確認した。実行結果とログは `dist/dependency-live-20261116-153313/` に保存した。この検証は現在使用中の `.venv` と実際の個人設定を変更していない。

公開中のv0.2.2のJSONにも新しい取得処理で2回アクセスし、`github.com` → `release-assets.githubusercontent.com` のリダイレクト検証、ETag保存、2回目の304とキャッシュ再利用を確認した。GitHub APIは呼び出していない。今回の更新ZIPは、v0.2.2の検証処理でも受信可能であることを確認した。
