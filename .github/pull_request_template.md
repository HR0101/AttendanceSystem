## 変更内容
<!-- 何を・なぜ変更したかを簡潔に記載してください -->

## バージョン変更
- 現在の最新リリースタグ: <!-- 例: v0.3.3 -->
- この PR の `src/version.py` バージョン: <!-- 例: 0.3.4 -->
- 変更区分: <!-- patch / minor / major のいずれか -->

## 関連 Issue
<!-- 例: Closes #123 -->

## 確認チェックリスト
- [ ] `src/version.py` の `VERSION` を最新リリースタグより上げ（または未タグの次期版へ揃え）、`python3 scripts/check_version_bump.py` が通ることを確認した
- [ ] `python3 scripts/generate_homebrew_formula.py --check` を実行し、`Formula/attendance.rb` が `VERSION` と同期していることを確認した
- [ ] `python3 scripts/check_update_notes.py` を実行し、`updates/X.Y.Z.md` の更新漏れがないことを確認した
- [ ] `.venv/bin/python -m unittest discover -s tests` を実行し、テストがすべて通ることを確認した
- [ ] `config.json`、`schedule.json`、学生番号、パスワード、実ログ、匿名化されていない HTML 等の機微情報を含んでいない
- [ ] 提出するコード・ドキュメントは自身が権利を持つものであり、[MIT License](LICENSE) に基づき公開・利用されることに同意する（[CONTRIBUTING.md](CONTRIBUTING.md)）
