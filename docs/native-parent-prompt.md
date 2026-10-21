---
name: article-harness-native
description: ChatGPT WorkまたはクラウドCodexで、親がネイティブサブエージェントに記事を割り当て、独立審査・差し戻し・継続補充・状態保存を行う。並列記事改善モード、記事改善ハーネス、複数Workerによる低品質記事の連続改稿で使用する。
---

# Article Harness Native — 親Managerの実行手順

コードの正本: https://github.com/nomuonji/article-harness 。My Portal接続時の本手順の正本はChat Skill `article-harness-native`。このファイルはGitHub/クラウドから読める保存版。実行状態と対象記事の正本とは分ける。

## 起動と権限

1. ユーザーの会話起動指示に従った後、通常の実行はGitHub/checkoutの対象repo・編集方針と、このrepoの`skills/editorial-quality.md`を読む。My Portalは案内/任意更新に限り、スキル/設定/キューを記事ごとに取得しない。My Portalが429でも取得済み正本と同梱スキルで計画・改稿・審査を続ける。
2. Python3、ネイティブsubagent起動/結果受取/追加指示、GitHub readbackまたはcheckoutの実在を確認する。`codex exec`や別のAPIキーを前提にしない。この会話のGitHub/MCP接続が別環境にもあると仮定しない。
3. repoをcheckoutできるならそのまま使用する。GitHub MCPしかない場合は、default branchの正確なHEADを読み、同じSHAから`article_harness/__init__.py`、`article_harness/native.py`、`article_harness/delivery.py`を取得して一つのruntimeディレクトリへ置く。取得したblob SHAとローカルbytesのGit blob SHAを照合してから実行する。利用不能ならAPI非公開エンドポイントなどを探さず、必要な能力を報告する。
4. 一つの実行に一つのSTATEディレクトリを用意し、予算を宣言する。既定は3Worker枠、対象候補最大12件、初回+修正2回。これは公開ノルマではない。ツール/時間の実上限に合わせ、品質を下げて数を達成しない。
5. 既定はOperator不要の候補作成モード。Site/Keywords Operatorのcontext、計測、Task一覧を起動前の必須条件にしない。GitHub本文と原典から独立して計画し、管理状況が不明または既存定期Workerの対象なら`coordination.mode=proposal-only`で候補だけ作る。既存スケジュールを変更せず、実サイト配送時だけ実共有claimと正本の現行配送契約を確認する。

## 計画と原文パケット

6. 現行本文と原典を読み、具体的な誤情報、欠けた判断材料、重複などを特定する。タイトルや字数だけで低品質と決めない。サイトの方向転換・大量削除・一括noindexは通常の改稿Taskに混ぜない。
7. Taskを作る。必須は`id, repository, base_sha, files, brief, reader_job, acceptance_criteria, sources, coordination`。`files`は変更可能な既存本文の正確な相対パス、`base_sha`は実際に読んだ40桁commit。canonical/URLや保持すべき内容もbriefに含める。条件を全記事共通の文字数/出典件数や自己採点へ置換しない。
8. 同じcommitから取得した原文をoriginal入力ディレクトリへ保存する。架空の本文やSHAで本番Taskを埋めない。GitHubの取得先・commit・blob SHAを親の取得記録に残す。`native add --task TASK.json --original-dir ORIGINAL`で固定する。
9. `proposal-only`は元repoへ書かない計画・改稿・独立審査用であり、共有claimは不要。合格は`reviewed_proposal`となり、同repoの別記事に進める。最初から正本共有claimを保持する実行は従来の`shared-claim`を使う。`unmanaged`は定期Workerなど別の実行者がいないことを確認した独立repo/教材用。Operator不通を理由に管理下repoをunmanagedに偽装しない。

## 実際のWorker起動

10. 本文を渡す前に、サブエージェントを「親から割当が届くまで編集せず待機」でspawnし、返された実agent IDを得る。`native claim --id TASK --actor ACTUAL_AGENT_ID --ttl 900`が成功してから、followup/sendで実Taskと編集許可を送る。claim失敗ならその子を停止/待機させ、編集させない。親子間ツールの名前は環境の実在APIに合わせる。
11. Workerへ`docs/native-worker-prompt.md`、Task、originalのパス、専用candidate出力先、原典、編集方針、具体的な前回指摘を渡す。過去の会話全体や期待する合格結論を渡さない。Workerは候補だけ編集し、DB・原文・snapshot・GitHub refsに触れない。
12. 親は実agentの進行を確認し、SQLite側のnative heartbeatでleaseを延長する。`proposal-only`の通常処理はOperator呼出しゼロ。実共有claimを取得した段階だけ正本側も延長・readbackし、`renew-shared-claim`で同じrunIdを反映する。429なら等価APIを連打せず、その配送だけ保留して他の候補を進める。
13. Workerの返答だけで完了としない。実candidateを`native submit --id TASK --token TOKEN --candidate-dir DIR`へ渡す。allowlist・UTF8・原文・本文・差分のハッシュ検査を通し、固定version_dirとdigestを得る。TOKENをWorkerやReviewerへ渡す必要はない。

## 独立審査と差し戻し

14. Workerとは別の新規Reviewerを待機状態でspawnし、実IDで`native begin-review --id TASK --actor REVIEWER_ID`を呼ぶ。成功後、`docs/native-reviewer-prompt.md`、Task、固定snapshot/diff、原文・原典を送る。Reviewerは読取り専用。実際に独立セッションを起動できない場合は未実施と報告し、自己審査を独立審査と呼ばない。
15. Reviewerは`digest, verdict, summary, findings, criteria`を返す。criteriaはTaskの条件を一字一句同じ文字列で各1回、`status=pass/fail/unknown`と具体的なevidenceを持つ。親は回答をJSONで保存し、`native review --id TASK --token TOKEN --report FILE`を実行する。本文やdiffが変わっていればhelperが拒否する。
16. `revise`は同Taskを再割当し、元の条件・予算を維持する。新しいIDで試行回数や拒否履歴をリセットしない。`blocked`/上限到来は具体的な不足を残し、別の候補へ移る。権限/安全上の拒否は`native refuse`で保存し、同じ操作を別agent・APIへ回して繰り返さない。

## 引き渡し・継続・終了

17. proposal-onlyのpassは`reviewed_proposal`で、外部配送はまだ許可されない。親が管理下repoへ実配送する時だけ、正本の共有claimと現在HEAD/本文を確認する。観測したTask/claimとローカルTaskの対応を記録し、`taskId, repository, files, runId, expiresAt`に正規化した実receiptを`native authorize-delivery --id TASK --receipt FILE`へ渡す。別Task IDの対応や元の正本IDを隠さず記録する。成功後はready_for_handoffとなり、現行配送契約に従う。元HEAD/条件が変われば再診断/再審査する。取得不能なら候補を保存し配送だけ保留する。
18. 配送前に`native prepare-delivery --id TASK --branch main`を実行する。helperがGitHubの公開REST APIをread-onlyで照会し、同じ記事を触るopen PR、元commit/現行原文の一致を検査する。別記事だけのHEAD更新は保持して準備する。返された`base_sha`を唯一のparent、`base_tree_sha`を基底treeとして、固定snapshotの許可ファイルだけを既存modeのまま配送する。書込直前に`native check-delivery --id TASK`を通す。HEAD更新ならprepareからやり直し、対象本文更新なら再診断・再審査する。GitHub書込は環境の既存MCP/現行Actions契約に従う。refの強制更新は禁止。管理下のmainを直接更新する経路を新設しない。
   配送commitを作成後、`native handoff --id TASK --digest DIGEST --evidence FILE`（`{"commit_sha":"実際の40桁SHA"}`）を実行する。helperがGitHubからcommit・parent・完全tree・全対象blobを取得し、変更ファイル・mode・本文が審査済snapshotと一致した時だけhanded_offにする。自由記述readbackは証明にならない。これは配送commitの照合であり、PR作成・マージ・CI・本番公開は別々に確認する。GitHub GET不能/429/権限不足でも完了を偽装せず配送だけ保留する。公開repoのGETにキーは不要。非公開repoは環境に既にあるGH_TOKEN/GITHUB_TOKENを使用できるが、MCP認証がPythonへ引継がれると仮定しない。
19. 空き枠へ次の候補を投入し、1件の保留で全体を止めない。reviewed_proposalは同repoの別記事を妨げない。共有claim取得後のready_for_handoffは別の配送を排他するが、同repoの別記事のproposal-only候補作成は継続できる。Keyword探索や計測は具体的に必要な時だけ親がまとめて依頼し、各Workerの定例処理へ入れない。結果連携も必要な最終要約だけで、全中間状態はSQLiteと固定成果物に保存する。
20. 中断Taskは勝手にclaimし直さず、実agentの停止・候補・共有claimを確認して`native resume --id TASK`。helperは期限切れをinterruptedにし、明示再開を要求する。拒否Taskはresume不可。
21. 重要checkpointと終了時に`native export --output OUTSIDE_STATE.zip`。機密を含めずユーザーの永続保存先へ保存し、既存checkpointなら同じidentityへ置換する。次回は空のSTATEへ`native restore --archive FILE.zip`。working/reviewing leaseは再利用されずinterruptedとなる。**このアーカイブは分散ロックではない**。複数チャット間は実共有claimで協調する。
22. 最終報告は実際の改稿・審査合格・配送・CI・公開を分け、保留理由と再開先を示す。1回の実行が終了した後の自動再起動や無制限常駐を保証しない。

## CLI構文

`python3 -m article_harness.native --state STATE COMMAND ...`。詳しい実在引数は`--help`で確認する。モデルをPythonから起動するローカルCLIモードとは別物。
