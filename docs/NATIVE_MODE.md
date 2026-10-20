# Native mode: ChatGPT Work / Codex Cloud

## Placement

**独立プロジェクト Article Harness**、repoは`nomuonji/article-harness`に置く。`keywords`には既存のSite Operator/Keywords Operatorを残し、本ハーネスの親子実行ループを移植しない。対象記事の正本は各サイトのrepoのまま。

この版は`codex exec`の代わりに、親エージェントが実行環境のネイティブサブエージェントを起動する。Pythonを通じてChatGPTの内部起動APIを呼ぶものではない。

| 担当 | 実行主体 |
| --- | --- |
| 全体計画・新しい仕事の割当 | 起動スキルに従う親チャット |
| 記事の改稿 | 環境内のWorkerサブエージェント |
| 独立した全文審査 | 別のReviewerサブエージェント |
| ローカル予約・状態遷移・成果物ハッシュ | `article_harness.native`補助コード |
| GitHubの取得・配送 | その環境に実際に公開されているGitHub MCP/接続またはGit checkout |
| クラウド間の引継ぎ | exportした状態アーカイブを永続保存し、次回にrestore |

## 起動

My Portal Chat Skill **`article-harness-native`**がチャットでの起動手順の正本。My Portalへの保存は、ChatGPTのSkills一覧へインストールする操作ではない。新しい会話ではWorld Homeから同Skillを取得し、「並列記事改善モードを開始」と明示する。

コードはPython 3.11以上の標準ライブラリのみ。クラウド実行でもCLI本体、APIキー、ローカルPCの常時稼働は不要。ただしPython実行とサブエージェント起動がその環境に存在する必要がある。起動時に確認し、存在しなければ実行済みと報告しない。

```bash
python3 -m article_harness.native --help
```

ローカル用`python3 -m article_harness run`は引き続き別のCLIモード。Workではそれを起動してCodex CLIを入れ子で呼ばない。

## 作業ループ

1. 親が対象サイトの編集方針と現行本文を読む。GitHubなら**正確なcommit SHA**で対象ファイルを取得し、originalディレクトリへ保存する。MCPの有無・読み取り/書き込み・commit readback・スクリプト実行・サブエージェント起動を確認する。
2. 管理下サイトは、既存SEO Taskのclaimなど実際に定期側が尊重する共有予約を取得する。native helperのローカルロックは定期Workerとの共有ロックではない。共有予約が取れなければ、元repoへ書き込まず、独立した調査/提案/fixture試験を行う。
3. 具体的な欠陥、読者の問い、原典、変更可能ファイル、合格条件、base SHAを持つタスクだけ登録する。
4. 親がclaimし、実際のWorkerをspawnして、Taskの最小情報・original・候補出力先を渡す。初期値3枠。同じrepoの作業を重ねない。
5. Workerはcandidateディレクトリにだけ改稿を書く。元repo、Taskの合格条件、snapshot、他のWorkerのファイルを変更しない。
6. 親がsubmitして候補を固定する。補助コードがallowlist、原文、差分、ハッシュを記録する。
7. Workerとは別の新規Reviewerをspawnする。独立した原典確認・改稿前後・全文・要約等を審査し、固定した合格条件すべてを支持/不支持/不明で返す。自分の前の出力を正当化する指示や期待する結論を渡さない。
8. reviseなら理由を同Taskへ戻す。初回+最大2回の改稿を上限とし、保留になっても他の仕事へ進む。passでも機械的なハッシュ確認が通るまで引き渡さない。
9. `ready_for_handoff`の実物を親が確認し、現在の環境で許可された配送だけを行う。既存SEO Taskならその正本のcheckpoint/transport/中央Actions所有権を守る。未管理の独立作業はユーザーが許可したbranch/PR操作の範囲で配送する。ツール不足なら成果物を保存して未配送と報告する。
10. 空き枠に次を投入する。起動しただけ、差分を作っただけ、mainに入っただけ、公開済み、検索成果をそれぞれ区別する。セッション終了前や重要なcheckpointでexportを保存する。

## GitHub MCPの扱い

この版は親や子が環境のGitHub MCPを**直接使う手順**に対応する。別のPython用GitHubトークンは必要ない。接続済みであること、必要なツールが実在すること、子に利用可能なことを各実行で確認する。ChatGPTの接続が別のCodex Cloud環境へ自動的に引き継がれると仮定しない。

GitHubの取得失敗・403・権限拒否を、ローカルoriginalに架空の本文を置くことで隠さない。GitHubに原文を保存する操作と、ローカルで改稿候補を作る操作を区別する。

## 保存と同時実行の限界

状態の正本はそのrunのSQLiteと固定した成果物。別チャットから同じアーカイブを同時にrestoreすると、SQLiteのロックは共有されない。**同じ対象を複数チャットで走らせるには、別途実共有claimが必要**。Libraryの保存はcheckpointの永続化であり、分散ロックではない。

終了したセッションを勝手に自動再起動するサービスは含めない。クラウドで1回の実行を長く続けることと、上限なく常駐することは別。予算/実行環境の限界で止まる場合は、完了数・保留理由・再開アーカイブを保存して終える。

## 検証の区別

- ローカルCLI版: 31テスト（模擬モデル/一時Git repo）。
- Native helper: 別途状態遷移・lease・審査・改変検知・export/restoreを試験する。
- ChatGPT Workの実サブエージェント: 架空教材で改稿→独立審査→ハッシュ検証を試す。
- Codex Cloud上の実運用、GitHubの本番配送、SEO結果は、その環境で成功の実物を確認するまで未検証。
