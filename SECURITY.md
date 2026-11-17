# セキュリティポリシー

## サポート対象バージョン

セキュリティ修正は原則として最新のリリース版および `main` ブランチを対象に行います。

| バージョン | サポート状況 |
| --- | --- |
| 最新版（`v0.2.7` / `main`） | サポート対象 |
| それ以前のバージョン | `att update` または手動更新でのアップデートを推奨 |

## 脆弱性の報告方法

認証情報（macOS Keychain 連携）、ブラウザ操作、または自己更新機能（`src/updater.py`）に関するセキュリティ上の問題を発見した場合は、**公開の Issue や Pull Request には詳細を投稿しないでください**。

GitHub の **Private Vulnerability Reporting** を利用して、非公開でご連絡ください。

- [脆弱性を非公開で報告する](https://github.com/HR0101/AttendanceSystem/security/advisories/new)

### 報告に含めていただきたい内容

- 問題の概要と想定される影響範囲
- 再現手順または PoC（可能な範囲で）
- 対象ファイルや該当箇所

### 報告に含めないでいただきたい情報

- 実在の学生番号、パスワード、認証済みセッション Cookie
- ご自身の `config.json`、`schedule.json`、実環境の実行ログ

## 設計上のセキュリティ方針

- パスワードは `config.json` やログに保存せず、macOS Keychain（`UniversityAttendanceTool`）へ保存します。
- ブラウザ遷移先および自己更新の取得先・リダイレクト先は HTTPS と許可されたホスト名のみに制限しています。
- 依存パッケージはハッシュ固定の `requirements.lock` で管理しています。
