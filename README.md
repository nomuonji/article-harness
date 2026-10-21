# Article Harness 0.2

## ChatGPT Work / Codex Cloudで使う

通常の計画・改稿・審査は**Site/Keywords Operator呼出しゼロ**。同梱スキルとSQLiteを使い、管理下サイトへの実配送時だけ共有claimを接続する。[Operator最小連携](docs/MINIMAL_OPERATORS.md)を参照。

クラウド用の**Nativeモード**を追加。親が環境内のサブエージェントを起動し、Python補助コードで予約・成果物固定・審査・再開を管理する。Codex CLIの入れ子起動は不要。

起動は[Native親エージェント手順](docs/native-parent-prompt.md)を読み、対象サイトと許可範囲を指定して「並列記事改善モードを開始」。My Portal接続済みならChat Skill `article-harness-native`を取得する。[構成と境界](docs/NATIVE_MODE.md)も参照。コード正本は独立repo `nomuonji/article-harness`。

```bash
python3 -m article_harness.native --state /absolute/path/to/run-state status
```

以下は、引き続き使えるローカルCLIモードの説明。

既存のSite Operatorスケジュールを変更せず、Codex CLIを計画・改稿・審査の役割ごとに起動する独立した記事改善ハーネス。親のPythonプロセスがキュー・並列数・時間・作業範囲・中断状態を管理する。Python 3.11以上、Git、認証済みCodex CLIがあるLinux/WSL向け。Pythonの追加依存は不要。

## すぐ試す

ZIPを展開したディレクトリで実行する。まず模擬実行（モデル呼び出し・外部通信・公開変更なし）：

```bash
python3 examples/demo.py --output /tmp/article-harness-demo
python3 -m article_harness --state /tmp/article-harness-demo/state status
```

架空の原典を使う4リポジトリを新設し、3並列・次の仕事の補充・1件の差し戻し・1件の保留を実行する。出力先は未作成のディレクトリにする。実Codexで同じ架空教材を試す場合：

```bash
codex --version
python3 examples/demo.py --live-codex --output /tmp/article-harness-live-demo
```

後者は認証済みCodexを実際に使い、利用枠を消費する。モデルは既存のCodex設定を使う。別途OpenAI APIキーを要求するコードはない。ここで提供した環境にはCodex CLIがなく、実モデルでの疎通・改稿品質は未検証。制御系は模擬エージェントと一時Gitリポジトリで検証した。

## 自分のサイトで使う

1. `examples/project.json`をコピーし、repo・実在する記事パス・原典URL・サイト方針ファイルを設定する。repoはGitルート。相対repoはJSONの所在を基準に解決する。記事パスはrepoからの相対パスを明示し、globを使わない。
2. Site Operator管理下なら、後述の外部予約を接続する。`unmanaged`は定期Worker等の別実行者がいないリポジトリ専用。これを設定しただけで定期側との排他は実現しない。
3. ソースをcleanにする。外部に状態ディレクトリを置いて起動する：

```bash
python3 -m article_harness --state "$HOME/article-work" run \
  --project project.json --workers 3 --max-jobs 12 \
  --max-plans 3 --max-seconds 7200 --agent-timeout 900 --revisions 2
```

Managerが記事全文と原典を読み、具体的な欠陥と受入条件を持つタスクを最大5件ずつ提案し、キューへ保存する。キューが空になると残りの対象記事から再計画する。すべて予約済み／根拠ある候補なしなら終了し、件数のために改稿を捏造しない。上限は1実行全体で共有する。

計画だけ確認する場合や、親Codexから具体的タスクを投入する場合：

```bash
python3 -m article_harness --state "$HOME/article-work" plan project.json --max-tasks 3
python3 -m article_harness --state "$HOME/article-work" add task.json
python3 -m article_harness --state "$HOME/article-work" run --workers 3
```

`examples/task.json`が明示タスクの形式。`--watch`は外から追加されたタスクを時間上限まで待つ。`--project`なしでは自動の計画補充をしない。`--model`と`--codex`はサブコマンドより前に置く。

## 実行と品質確認

- 同時3件が初期値、上限6。同一repoは1編集ずつ。各記事はdetached Git worktreeに分離し、元の作業ツリーを書き換えない。同じ共通Gitディレクトリへの別コントローラーもホスト共通のファイルロックで検知する。
- 改稿と審査は別々の新規Codexセッション。改稿だけworkspace-write、計画と審査はread-only。全役割に原典・全文・サイト方針を読む指示を渡す。審査は受入条件を全件照合し、unknown/failや欠陥があれば合格にできない。
- 不合格は具体的な指摘を渡して再改稿。初期値は初回＋修正2回。1件が保留・拒否・失敗しても無関係な次の仕事へ進む。件数は品質の証明ではない。
- allowlist外変更、無断コミット、未追跡出力、削除、symlink、バイナリ変更、元HEADの変化を検出したら引き渡さない。`git diff --check`と`git apply --check`も実行する。
- これは品質審査を実行する仕組みであり、AI審査の正しさを保証するものではない。ビルド・実画面・公開・検索成果は別の検証段階。
- Git worktreeとパス検査は誤操作を減らす仕組み。OSの強固な分離境界ではない。Codex本体のsandboxを維持し、権限回避オプションは使用しない。

## 成果物と引き渡し

合格すると `STATE/tasks/TASK_ID/` に保存する。

| ファイル | 内容 |
| --- | --- |
| `change.patch` | 元HEADに適用可能と確認した変更 |
| `receipt.json` | base SHA・変更ファイル・パッチ/本文のハッシュ・審査・検証範囲 |
| `worker-N/`、`review-N/` | 指示・構造化回答・Codexイベント・エラーログ |
| `workspace.json`、`worktree/` | 再開用の元SHAと作業実物 |
| `reservation.json` | 外部予約を使用する場合の識別子・期限・解放状態 |

`ready_for_handoff`は改稿とソース審査を終えた状態。自動push・PR・マージ・公開はしない。既存中央Actionsへの自動配送やSites Operatorへの結果書込みも初版では未接続。

引き渡し担当が最新HEAD・競合を確認し、パッチ適用、サイト固有CI、PR・公開確認を行う。新しいHEAD上で内容が変われば再審査する。変更が元repoにコミットされたら：

```bash
python3 -m article_harness --state "$HOME/article-work" ack-applied TASK_ID
```

保存したパッチのハッシュと、元repoにコミットされた本文のハッシュを照合してから`applied`へ進める。これは公開済みを意味しない。

## 中断・再開・停止

```bash
python3 -m article_harness --state "$HOME/article-work" status
python3 -m article_harness --state "$HOME/article-work" stop
python3 -m article_harness --state "$HOME/article-work" clear-stop
python3 -m article_harness --state "$HOME/article-work" resume TASK_ID
python3 -m article_harness --state "$HOME/article-work" run
```

停止は新規割当を止め、起動済みの処理を各timeout内で終了させる。SIGKILL／電源断後の作業中タスクは次の起動時に`interrupted`へ変わり、自動再実行しない。worktree・指摘・外部予約を確認してから`resume`する。使用済み試行回数をリセットしない。拒否された操作を別ID・別ツールに振り替えて再試行しない。

状態はSQLite WAL、コントローラーは1キュー1つ。既定の状態ディレクトリは作業ディレクトリ別に`~/.local/state/article-harness/`以下へ作る。コマンド間で同じ`--state`を明示すると確実。状態を記事repo内へ置くとclean判定を妨げるため避ける。作業ツリーは勝手に削除しないので、引き渡し後に必要に応じてGit worktreeの通常手順で整理する。

`--max-seconds`は新規実行とエージェントtimeoutの予算。終了時のGit・予約の後処理などで少し延びる場合がある。料金の厳密な上限ではない。プロセスを落としても永久に回り続けるサービスや自動再起動設定は含めない。

## Site Operatorとの最小接続

既存スケジュール・Manager・Worker・マクロ方針は変更しない。初版では、予約を担当する外部コマンドの契約だけを実装している。実際のSite Operator APIアダプターは未実装。接続前は管理下サイトへの並行実行をしない。

```json
{"coordination":{"mode":"external-reservation","command":["python3","/absolute/path/to/reservation_adapter.py"]}}
```

外部コマンドはJSONをstdinで受け取り、JSONだけをstdoutへ返す。shell経由では実行しない。

```json
{"operation":"claim","task_id":"example","repo":"/path/site","files":["article.md"],"reservation":null,"required_seconds":990}
```

```json
{"granted":true,"reservation_id":"actual-shared-reservation-id","expires_at":"2026-10-21T02:00:00+09:00"}
```

`claim / renew / release`を実装し、実際に定期Workerと共有できる予約を取得・更新・解放すること。単に上記JSONを返すだけのスタブでは排他にならない。各エージェント実行より長い期限が必要。予約はclaim/renew直後に保存し、中断後は同じIDをrenewして照合する。releaseも`granted:true`と識別子を返す。接続不能、取得失敗、短い期限なら編集を開始しない。解放失敗は保留にして識別子を保存する。現在のSites Operator claimがこの外部キューのまま利用できると仮定せず、対象Taskとの対応・所有権・実行期限をアダプター側で確認する。

## 検証と参考

```bash
python3 -m unittest discover -s tests -v
```

統合テストは一時repoと模擬実行者を使用する。実モデルの編集品質と、本番サイトの改善効果は別途検証が必要。

Codex接続方式は公式の[非対話実行](https://developers.openai.com/codex/noninteractive)と[CLIリファレンス](https://developers.openai.com/codex/cli/reference)に基づく。`exec`、JSON schema、最終回答ファイル、sandboxを利用する。
