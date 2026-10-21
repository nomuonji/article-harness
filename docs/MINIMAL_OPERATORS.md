# Operator連携の最小化

通常のNative実行はGitHub原文・同梱編集スキル・SQLiteで計画、改稿、独立審査、候補保存まで完結する。Firestoreの移行をハーネスの稼働前提にしない。

| 処理 | 保存先／取得先 | Operator呼出し |
| --- | --- | --- |
| 対象記事・編集方針 | 指定GitHub commit | 不要 |
| 記事品質スキル | `skills/editorial-quality.md` | 不要 |
| キュー・lease・審査・差し戻し | SQLiteと固定成果物 | 不要 |
| 新規の検索需要・計測分析 | 親が必要時にまとめて依頼 | 任意 |
| 管理下repoへの実配送 | 実共有claimと現行配送契約 | 必要時だけ |
| 最終結果の連携 | GitHub証跡の要約 | 必要な要約だけ |

## 候補作成と配送

管理下または管理状態不明のサイトは`coordination: {"mode":"proposal-only"}`で登録する。元repoへ書かずローカル候補を改稿して独立審査する。合格は`reviewed_proposal`。別記事へ枠を再利用でき、Operatorの429でもここまで継続できる。別実行者と調査や候補作成が重複する可能性はあるため、公開前に最新本文・重複PRを確認する。

管理下repoへ反映する時だけ親が正本の共有claimと現在HEAD・対象パス・期限・runIdをreadbackし、`native authorize-delivery --id TASK_ID --receipt actual-claim.json`へ渡す。成功すると`ready_for_handoff`となり、別配送とは排他されるが、同repoの別記事のproposal-only候補作成は継続できる。元Task IDとローカルTask IDが異なる場合は対応も記録する。helperは入力検査のみで遠隔権限を認証しない。架空receiptで代用しない。

延長が必要なら正本で延長/readback後に`renew-shared-claim`。配送前は`prepare-delivery`でGitHubのopen PR・原文を確認し、書込直前に`check-delivery`でHEADを確認する。配送後は`handoff`がGitHubのcommit・tree・blobを直接照合する。詳しくは[配送検証](DELIVERY.md)。期限切れ・Operator不通・元HEAD変化は配送だけ保留し、他の記事を進める。従来の`shared-claim`も維持する。

## スキルと境界

My Portalの確認済み`editorial-quality`をrepoへ同梱。由来とハッシュは`skills/sources.json`。通常はrepoから読み、更新時だけ正本revisionを確認して同期する。会話入口はユーザー指示に従うが、WorkerごとのMy Portal呼出しに増やさない。

Firestore全体のDB移行、既存スケジュール停止、管理下repoへの無予約の直接反映はこの変更に含めない。
