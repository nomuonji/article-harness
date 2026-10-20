"""Offline by default; --live-codex uses real, authenticated Codex CLI sessions.

All shop policies here are fictional fixtures, never public factual material.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from article_harness.core import Controller, Store, atomic_json, git
from article_harness.runner import CodexRunner


class DemoRunner:
    def run(self, role, task, workspace, run_dir, feedback, timeout):
        run_dir.mkdir(parents=True, exist_ok=True)
        time.sleep(0.1)
        if role == 'worker':
            if task['id'] == 'demo-4':
                result = {'status': 'blocked', 'summary': '模擬: 必要な原典を確認できない', 'evidence': []}
            else:
                (workspace / 'article.md').write_text('# 返品できる条件\n\n受取後30日以内で、未使用かつセール品でない商品が対象です。\n返品前にサポートへ注文番号を伝えてください。\n\n出典: [架空店舗の公式方針](source.md)\n', encoding='utf-8')
                result = {'status': 'done', 'summary': '模擬: 架空の原典から期限・条件・連絡手順を訂正', 'evidence': [{'claim': '30日以内・未使用・セール品対象外', 'source': 'source.md', 'support': '架空店舗の方針全文'}]}
        else:
            revise = task['id'] == 'demo-2' and not (run_dir.parent / 'feedback.json').exists()
            result = {'verdict': 'revise' if revise else 'pass',
                      'summary': '模擬の差し戻し' if revise else '模擬の合格。実際の編集品質評価ではない',
                      'findings': [{'path': 'article.md', 'detail': '模擬: 返品前の連絡方法を再確認'}] if revise else [],
                      'criteria': [{'criterion': x, 'status': 'fail' if revise else 'pass', 'evidence': '模擬: ローカルの架空原典と比較'} for x in task['acceptance_criteria']]}
        atomic_json(run_dir / 'answer.json', result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--live-codex', action='store_true')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    store = Store(output / 'state')
    for number in range(1, 5):
        repo = output / f'site-{number}'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        git(repo, 'config', 'user.name', 'Harness Demo')
        git(repo, 'config', 'user.email', 'demo@example.invalid')
        (repo / 'source.md').write_text('# 架空店舗の公式方針（デモ専用）\n返品は受取後30日以内。未使用の商品に限る。セール品は返品不可。返品前にサポートへ注文番号を伝える。\n', encoding='utf-8')
        (repo / 'article.md').write_text('# 返品ガイド\n10日以内ならすべて返品できます。\n', encoding='utf-8')
        (repo / 'AGENTS.md').write_text('架空店舗のローカルデモ。source.mdを唯一の原典とする。記事の誤りを直し、原典や方針ファイルは変更しない。\n', encoding='utf-8')
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'fictional demo source')
        store.add({'id': f'demo-{number}', 'repo': str(repo), 'files': ['article.md'],
                   'brief': 'source.mdに反する期限と対象条件を訂正し、返品前の手順を読者が判断できるようにする。',
                   'reader_job': '返品対象か・いつまでか・最初に何をするか判断する',
                   'acceptance_criteria': ['30日という期限、未使用という条件、セール品の除外、事前の連絡方法が架空の原典と一致する'],
                   'sources': ['source.md'], 'policy_files': ['AGENTS.md'], 'coordination': {'mode': 'unmanaged'}})
    runner = CodexRunner() if args.live_codex else DemoRunner()
    rows = Controller(store, runner, workers=3, agent_timeout=300).run()
    result = {'mode': 'live-codex' if args.live_codex else 'offline-simulation',
              'tasks': [{k: row[k] for k in ('id', 'status', 'attempts', 'detail')} for row in rows]}
    atomic_json(output / 'summary.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
