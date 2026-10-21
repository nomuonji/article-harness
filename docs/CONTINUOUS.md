# Article Harness Continuous — unattended pilot (2026-10-21)

## この実装で自動化する範囲

独立 repo `nomuonji/article-harness` に、既存の `Manager → Worker → Reviewer` を
**GitHub Actionsの繰り返し起動**で継続するオプションを追加した。恒久的なエージェントプロセスではない。

- `.github/workflows/continuous.yml`: 6時間ごとの再起動、手動起動、ジョブ直列化。
- `article_harness.continuity prepare`: 実repoのGit管理下にある記事だけを毎回選び、既存キューに登録したファイルを除外する。
- `python -m article_harness run`: 既存の最大3件／3 Worker／独立Reviewer／差し戻し。
- `article_harness.continuity backup / restore`: SQLite・審査結果・worktreeをSHA-256検査付きアーカイブで次回へ引き継ぐ。
- `proposal-only`: **既存Sites Operator管理下の実ソースを編集・配信しない**候補作成モード。レビュー合格は `reviewed_proposal`。本番に入った事実は意味しない。

実験対象: `nomuonji/shikaku-wiki` の既存資格解説Markdown/MDX。
入力定義: `config/qualification-pilot.json`。ただし現在はデフォルトで**自動起動OFF**。

## 起動方法・費用

1. GitHubリポジトリ `nomuonji/article-harness` に Actions secret `OPENAI_API_KEY` が必要。別途従量課金のAPI利用が必要であり、ChatGPT Plus/Codex CloudへのログインがそのままActionsに引き継がれるわけではない。
2. Actionsで `article-harness-continuous` を手動実行して1回の挙動を確認する。秘密情報をログやIssueに貼らない。
3. 予算・品質・チェックポイント復元を確認できてから、Actions repository variable `ARTICLE_HARNESS_CONTINUOUS_ENABLED` を `1` にすると、6時間ごとの実行を許可する。
4. モデル名を変える場合は `ARTICLE_HARNESS_MODEL` を既存Codexで利用できるモデル識別子に設定する。未設定ならCodexデフォルト。
5. 止める場合は repository variable を `0` にする。既存Site Operator側の定期Manager/Worker設定は無変更。

`workflow_dispatch` はキー未設定なら通知のみで何も改稿しない。
上限は1実行あたり3改稿タスク・計画2回・最大45分、最長60分のActions job。
Actions cronは正確な時刻で発火する保証はなく、実行枠不足や課金上限で無人実行を開始できない場合がある。

## チェックポイント仕様と障害動作

- 同じGitHub Actions workflowは `concurrency` で1本ずつ処理する。
- 前回の `article-harness-continuous-state` artifactを復元し、SHA-256照合・スキーマ・project ID一致を検査する。
- 状態に競合やファイル破損があった場合、**新しい空のキューに黙って切り替えず失敗する**。
- 許可されないファイルやsymlink、パストラバーサル、異なるプロジェクトのアーカイブを拒否する。
- ブロック／拒否／中断タスクは、別の名前や実行経路を使った自動再試行をしない。別記事の処理だけ進める。
- 実行結果とcheckpointはGitHub Actionsのrun summary / artifactに保存。artifact保持期間は30日。30日を超えて実行が止まるとcheckpointを利用できなくなる可能性がある。再開時は状態の有無を必ず確認する。
- `proposal-only` の合格は「原文との差分が審査済み」のみ。site main更新・CI・公開・GSC改善は**未達**。

## Site Operatorへの本番配送は別段階

このPRでは **管理下サイトの自動配送はまだ接続していない**。
既存の `seo_task_claim / heartbeat / gitOperationCheckpoint / deliveryHandoff`、
`nomuonji/keywords` の中央配送Actionsを置き換えない。

本番化に必要な接続:

1. `reviewed_proposal` の固定パッチについて、Site Operator正本の対象Task ID、対象ファイル、現在HEAD、現在記事本文・既存PRとの一致を確認する。
2. 配送直前にのみ、実際のSite Operator共有claimと現行Worker権限・契約を満たす。
3. `native prepare-delivery / check-delivery / handoff` と同等のGitHub commit/tree/blob検査を経て、現行 `push_pending` の契約で中央配送へ渡す。
4. 中央ActionsがPR/CI/merge/本番確認を行う。受け取れなかった候補は公開済みと扱わない。
5. 本番記事の効果判定は十分な観測期間を取ってGSC指標で行い、改稿件数だけを最適化しない。

本番配送が動作確認されるまで**旧Site OperatorのChatGPT定期Workerを停止しない**。

## ローカルでの補助コマンド

```bash
python -m unittest discover -s tests -v
python -m article_harness.continuity prepare \
  --state /tmp/harness-state --project qualification-pilot \
  --template config/qualification-pilot.json --repo /absolute/path/to/shikaku-wiki \
  --output /tmp/project.json
python -m article_harness.continuity backup \
  --state /tmp/harness-state --project qualification-pilot --archive /tmp/checkpoint.tar.gz
python -m article_harness.continuity restore \
  --state /tmp/restored-state --project qualification-pilot --archive /tmp/checkpoint.tar.gz
```

