---
name: article-harness-native
description: ChatGPT WorkまたはクラウドCodexで、親がネイティブサブエージェントに記事を割り当て、独立審査・差し戻し・継続補充・状態保存を行う。並列記事改善モード、記事改善ハーネス、複数Workerによる低品質記事の連続改稿で使用する。
---

# Article Harness Native — 親Managerの実行手順

コードの正本: https://github.com/nomuonji/article-harness 。My Portal接続時の本手順の正本はChat Skill `article-harness-native`。このファイルはGitHub/クラウドから読める保存版。実行状態と対象記事の正本とは分ける。

## 起動と権限

1. My Portalが使える会話では最初にWorld HomeとProject `article-harness`、関連Contextを確認する。対象サイトの正本repo、編集方針、依頼された範囲を特定する。記事審査にはMy Portal `editorial-quality`を併用する。My PortalがないCodex環境はこのrepoのAGENTSと対象repoの方針を読む。
2. Python3、ネイティブsubagent起動/結果受取/追加指示、GitHub readbackまたはcheckoutの実在を確認する。`codex exec`や別のAPIキーを前提にしない。この会話のGitHub/MCP接続が別環境にもあると仮定しない。
3. repoをcheckoutできるならそのまま使用する。GitHub MCPしかない場合は、default branchの正確なHEADを読み、同じSHAから`article_harness/__init__.py`と`article_harness/native.py`を取得して一つのruntimeディレクトリへ置く。取得したblob SHAとローカルbytesのGit blob SHAを照合してから実行する。利用不能ならAPI非公開エンドポイントなどを探さず、必要な能力を報告する。
4. 一つの実行に一つのSTATEディレクトリを用意し、予算を宣言する。既定は3Worker枠、対象候補最大12件、初回+修正2回。これは公開ノルマではない。ツール/時間の実上限に合わせ、品質を下げて数を達成しない。
5. 既存のSite Operatorスケジュール・マクロ方針を変更しない。管理下サイトでは現行`seo_agent_context`と正本Task/Worker契約を確認し、実際に定期側が尊重する共有claimを取得する。対応Taskがないままローカルleaseを「共有予約済み」と扱わない。共有claimを取得できないサイトは読取り診断までに留め、無関係な実行可能候補を進める。

## 計画と原文パケット

6. 現行本文と原典を読み、具体的な誤情報、欠けた判断材料、重複などを特定する。タイトルや字数だけで低品質と決めない。サイトの方向転換・大量削除・一括noindexは通常の改稿Taskに混ぜない。
7. Taskを作る。必須は`id, repository, base_sha, files, brief, reader_job, acceptance_criteria, sources, coordination`。`files`は変更可能な既存本文の正確な相対パス、`base_sha`は実際に読んだ40桁commit。canonical/URLや保持すべき内容もbriefに含める。条件を全記事共通の文字数/出典件数や自己採点へ置換しない。
8. 同じcommitから取得した原文をoriginal入力ディレクトリへ保存する。架空の本文やSHAで本番Taskを埋めない。GitHubの取得先・commit・blob SHAを親の取得記録に残す。`native add --task TASK.json --original-dir ORIGINAL`で固定する。
9. 管理下Taskでは`coordination.mode=shared-claim`。実ツールでreadbackしたclaimを、`taskId, repository, files, runId, expiresAt`に正規化して`receipt`へ渡す。Task IDとの対応を確認し、整形を根拠の捏造にしない。現物のTask IDがhelperのslug条件に適合しなければ勝手に別Taskを作らず対応を調整する。管理されていない独立repoや架空教材だけ`unmanaged`を使う。

## 実際のWorker起動

10. 本文を渡す前に、サブエージェントを「親から割当が届くまで編集せず待機」でspawnし、返された実agent IDを得る。`native claim --id TASK --actor ACTUAL_AGENT_ID --ttl 900`が成功してから、followup/sendで実Taskと編集許可を送る。claim失敗ならその子を停止/待機させ、編集させない。親子間ツールの名前は環境の実在APIに合わせる。
11. Workerへ`docs/native-worker-prompt.md`、Task、originalのパス、専用candidate出力先、原典、編集方針、具体的な前回指摘を渡す。過去の会話全体や期待する合格結論を渡さない。Workerは候補だけ編集し、DB・原文・snapshot・GitHub refsに触れない。
12. 親は進行中の実agentを確認し、必要ならnative heartbeatでleaseを延長する。managedでは先に正本claimも延長・readbackし、`renew-shared-claim --receipt FILE`で同じrunIdを反映する。ローカルheartbeatだけで共有予約を延命したと報告しない。
13. Workerの返答だけで完了としない。実candidateを`native submit --id TASK --token TOKEN --candidate-dir DIR`へ渡す。allowlist・UTF8・原文・本文・差分のハッシュ検査を通し、固定version_dirとdigestを得る。TOKENをWorkerやReviewerへ渡す必要はない。

## 独立審査と差し戻し

14. Workerとは別の新規Reviewerを待機状態でspawnし、実IDで`native begin-review --id TASK --actor REVIEWER_ID`を呼ぶ。成功後、`docs/native-reviewer-prompt.md`、Task、固定snapshot/diff、原文・原典を送る。Reviewerは読取り専用。実際に独立セッションを起動できない場合は未実施と報告し、自己審査を独立審査と呼ばない。
15. Reviewerは`digest, verdict, summary, findings, criteria`を返す。criteriaはTaskの条件を一字一句同じ文字列で各1回、`status=pass/fail/unknown`と具体的なevidenceを持つ。親は回答をJSONで保存し、`native review --id TASK --token TOKEN --report FILE`を実行する。本文やdiffが変わっていればhelperが拒否する。
16. `revise`は同Taskを再割当し、元の条件・予算を維持する。新しいIDで試行回数や拒否履歴をリセットしない。`blocked`/上限到来は具体的な不足を残し、別の候補へ移る。権限/安全上の拒否は`native refuse`で保存し、同じ操作を別agent・APIへ回して繰り返さない。

## 引き渡し・継続・終了

17. passは`ready_for_handoff`。公開済みとは違う。親が現在のHEAD・差分・claimを再照合し、許可された既存配送経路を使う。Sites Operator TaskならgitOperationCheckpoint/claimRunId/transport/readback/中央Actions所有権を現行契約どおり守る。汎用の新規repo作業では、依頼されたbranch/PR操作の範囲で配送する。元HEAD変更・条件変化は再診断/再審査する。
18. 実際に配送したcommit SHAとreadback根拠を`native handoff --id TASK --digest DIGEST --evidence FILE`へ記録する。入力形式は`{"commit_sha":"40桁の観測済SHA","readback":"確認したtree/body/commit等の根拠"}`。helperはGitHubを自動照会しないので、架空の証跡で埋めない。CI・公開・検索成果は別に報告する。
19. 空き枠へ次の実行可能Taskを投入し、1件の保留だけで全体を止めない。同repoはready_for_handoffまで含めて排他されるので、引渡しを進めるか別repoへ移る。候補不足なら未調査の全文を調べて補充し、低品質な仕事を捏造しない。親はユーザーへ約60秒ごとに確認済成果と残る課題を伝える。
20. 中断Taskは勝手にclaimし直さず、実agentの停止・候補・共有claimを確認して`native resume --id TASK`。helperは期限切れをinterruptedにし、明示再開を要求する。拒否Taskはresume不可。
21. 重要checkpointと終了時に`native export --output OUTSIDE_STATE.zip`。機密を含めずユーザーの永続保存先へ保存し、既存checkpointなら同じidentityへ置換する。次回は空のSTATEへ`native restore --archive FILE.zip`。working/reviewing leaseは再利用されずinterruptedとなる。**このアーカイブは分散ロックではない**。複数チャット間は実共有claimで協調する。
22. 最終報告は実際の改稿・審査合格・配送・CI・公開を分け、保留理由と再開先を示す。1回の実行が終了した後の自動再起動や無制限常駐を保証しない。

## CLI構文

`python3 -m article_harness.native --state STATE COMMAND ...`。詳しい実在引数は`--help`で確認する。モデルをPythonから起動するローカルCLIモードとは別物。
