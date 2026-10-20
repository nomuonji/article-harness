# Actual native-subagent fixture run

2026-10-21 JST、この会話のChatGPT Workネイティブサブエージェントを実際に起動し、架空店舗の原典を使って改稿と独立審査を行った記録。CLIや模擬モデルの結果ではない。

- Worker: `/root/native_fixture_worker`、Reviewer: `/root/native_fixture_reviewer`（別セッション）。
- 元記事: `original/article.md`、唯一の架空原典: `source.md`。
- 改稿結果: `candidate/article.md`、固定差分: `change.diff`。
- 実際に返された審査: `review.json`、固定時のハッシュ: `manifest.json`。
- Native helperでclaim→submit→begin-review→reviewを実行し、`ready_for_handoff`を確認。その状態をZIPへexportし、別の空ディレクトリへのrestore後も同じ状態とdigestを確認した。

review.json内の出典パスは実行時のローカルfixtureパス。ここには同じ内容を`source.md`として同梱した。manifestのrepository/base_shaは一時Git教材の識別子であり、実在するGitHubサイトへの配送記録ではない。実店舗の方針でも本番記事でもなく、GitHubへの記事公開・Codex Cloud実行・SEO改善効果は検証していない。
