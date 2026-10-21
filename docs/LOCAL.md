# Article Harness — Windows 11 / WSL ローカル継続運転

既存Codex CLIログインで Manager → Worker → 独立Reviewer を繰り返す。追加のOpenAI APIキー不要。モデル実行には通信・利用枠が必要で、完全オフラインではない。PC/WSLが停止している間は動かない。

**安全境界：現在の無人運転はproposal-only。** 改稿候補と審査結果をstateに残すが、管理下サイトのmainや本番を自動更新しない。現行Site Operatorの共有claim・中央配送には未接続。

## WSLで起動

~~~bash
cd ~
git clone https://github.com/nomuonji/article-harness.git
git clone https://github.com/nomuonji/shikaku-wiki.git
cd ~/article-harness
python3 --version  # 3.11+
codex --version
codex login        # 既にログイン済みなら不要

# モデルを呼ばずに記事候補の選定だけ検証
bash scripts/run-local.sh --repo "$HOME/shikaku-wiki" --state "$HOME/.local/state/article-harness/shikaku-pilot" --dry-run

# 実際に一回だけ改稿・審査
bash scripts/run-local.sh --repo "$HOME/shikaku-wiki" --state "$HOME/.local/state/article-harness/shikaku-pilot" --once --workers 2 --max-jobs 3

# 継続実行（終わったら5分後に次サイクルを確認）
bash scripts/run-local.sh --repo "$HOME/shikaku-wiki" --state "$HOME/.local/state/article-harness/shikaku-pilot" --workers 2 --max-jobs 3 --interval-minutes 5
~~~

サイト・stateはWSL側のホーム配下を推奨する。/mnt/c 配下の小さなファイル大量処理は遅くなる場合がある。1サイクルの作業上限は3件・計画2回・最大45分。改稿に必要な一次資料はCodex CLIが確認する。根拠ある修正がなければ無理に作らずidleになる。

## Windows PowerShellから起動

リポジトリ内のPowerShellスクリプトからWSLを起動できる。次の3つはWindowsパスではなくWSL側の絶対パスに置き換える。

~~~powershell
.\scripts\start-local-wsl.ps1 -Distro Ubuntu -HarnessPath /home/USER/article-harness -SitePath /home/USER/shikaku-wiki -StatePath /home/USER/.local/state/article-harness/shikaku-pilot -Once
~~~

-Onceを外せば連続実行。wsl -l -q でディストリビューション名を確認する。Windowsログオン時の自動起動が必要なら、先に対話的起動を確認してからタスクスケジューラに同じスクリプトを登録する。Windowsをスリープ・終了すればローカル実行は止まる。常時稼働サーバーを新設する必要はない。

## 停止と再開

~~~bash
STATE="$HOME/.local/state/article-harness/shikaku-pilot"
python3 -m article_harness --state "$STATE" status
cat "$STATE/local-last-cycle.json"

# 新規割当停止。進行中のサブプロセスは既存の終了処理に従う
python3 -m article_harness --state "$STATE" stop

# 未完了の一件だけ状態・worktree・共有予約を確認した後
python3 -m article_harness --state "$STATE" resume TASK_ID

# STOP解除後、上のrun-local.shと同じコマンドを再実行
python3 -m article_harness --state "$STATE" clear-stop
~~~

Ctrl+C/SIGTERMでもSTOPを記録する。STOPがあれば自動再開は行わず、明示解除が必要。同じstateの二重起動はロックで拒否する。状態はWSLディスクに残るため、再起動後に同じstateで再開できる。ただし interrupted/refused の同一操作は勝手に再試行しない。

## 他のサイトも対象にする

資格図鑑以外は、別のproject IDでテンプレートを作る。article_globsは明示的な記事フォルダに限定する。

~~~json
{
  "id": "my-blog-pilot",
  "article_globs": ["src/content/blog/*.md", "src/content/blog/*.mdx"],
  "brief": "読者の判断材料を一次資料で検証し、具体的な不足だけ改稿",
  "sources": ["https://example.org/replace-with-real-official-source"],
  "policy_files": ["AGENTS.md"],
  "coordination": {"mode": "proposal-only"}
}
~~~

sources / policy_filesは実際のサイトに合わせる。ルート全体の無差別スキャンは拒否し、Markdown/MDXのみ対象とする。

~~~bash
python3 -m article_harness.local --template /absolute/path/blog-pilot.json --repo /absolute/path/site --state "$HOME/.local/state/article-harness/my-blog" --once
~~~

stateは別repoへ流用しない。モデル指定は任意（--model）。既存サイトへの本番反映はSite Operatorとの共有作業権と配送実装ができるまで別扱い。

## 失敗時の動作

AIが一件をblocked/refusedにしても別記事へ進む。CIや一般的なプロセスエラーを無限に再実行しない。制御基盤が3回連続で失敗したら停止してlocal-last-cycle.jsonを確認する。既存タスクで使用済みの試行回数をリセットしない。審査合格は公開成功・SEO効果を意味しない。
