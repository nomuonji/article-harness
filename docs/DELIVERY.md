# Native delivery verification

通常の候補作成・審査はOperator不要。管理下repoの共有claim取得と現行配送契約は引き続き親が正本で確認する。helperは共有claimの実在を遠隔認証するものではない。

```bash
python3 -m article_harness.native --state STATE prepare-delivery --id TASK --branch main
python3 -m article_harness.native --state STATE check-delivery --id TASK
# 親が許可されたGitHub MCP / 既存Actionsでcommitを配送
python3 -m article_harness.native --state STATE handoff --id TASK --digest DIGEST --evidence commit.json
```

`commit.json`は`{"commit_sha":"配送した40桁SHA"}`。旧readback文字列のみの完了記録は廃止した。既存ready候補もprepareが必要。既存handed_offの履歴を遡って検証済と扱わない。

- prepare: GitHubのopen PR（rename前パスも含む）と対象記事の重複を検査。登録時base commitの実blobと固定originalの一致、現在HEADの対象blob/modeの一致を検査する。対象記事の変更は再診断・再審査。別記事だけの変更は新HEAD/treeを配送基底として保持する。
- 書込: prepareのbase_shaを唯一のparent、base_tree_shaを基底treeとして使い、固定candidateの許可ファイルのみ変更する。modeを維持する。MCP/現行Actionsの配送権限は拡張しない。
- check: 書込直前にHEADと共有claim期限を確認。遠隔読取後にもローカル状態とclaimを再検査する。HEAD変更ならprepareからやり直す。
- handoff: 実commitの唯一parent、完全treeの変更範囲、全対象ファイルの元本文・審査済本文・modeをGitHubから直接照合。成功時だけhanded_off。変更なしの許可ファイルは変更差分へ混ぜない。
- 429/取得不能/期限切れ/競合: 配送だけ保留。自動連打しない。別repoや、同repoの別記事のproposal-only改稿は継続可能。審査済candidateを保持する。

公開GitHubのread-only REST APIに追加キー不要。非公開repoは既存のGH_TOKEN/GITHUB_TOKENを必要に応じて利用する。API URLはapi.github.com固定、timeout30秒、immutable Git objectだけ呼出し内cache。GitHub MCPのログインはPythonへ自動転送されない。全文treeがtruncated、PR列挙が上限超過なら不完全な確認で成功にしない。

helperはGitHubへ書込まない。checkと外部書込の間を原子的にはできないため、強制pushを使わず、既存の共有claim・PR・CI・競合検査を保持する。配送commit検証は、PR作成・mainへのマージ・デプロイ・実サイト表示・SEO成果の証明ではない。サイトの公開検証は既存の中央Actions契約の担当である。
