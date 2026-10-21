"""Local supervisor behavior tests, without an LLM or network."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from article_harness.continuity import ContinuityError
from article_harness.core import Store, HarnessError, file_lock
from article_harness.local import cycle, supervise


class LocalContinuousTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'site'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        for key, value in [('user.name', 'Test'), ('user.email', 'test@example.invalid')]:
            subprocess.run(['git', '-C', str(self.repo), 'config', key, value], check=True)
        (self.repo / 'docs').mkdir()
        for name in ('one.md', 'two.md'):
            (self.repo / 'docs' / name).write_text('# Original\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-qm', 'fixture'], check=True)
        self.state = self.root / 'state'
        self.template = self.root / 'template.json'
        self.settings = {
            'id': 'local-fixture', 'article_globs': ['docs/*.md'],
            'brief': 'Find source-verified missing reader answers',
            'sources': ['https://example.org/source'],
            'policy_files': [], 'coordination': {'mode': 'proposal-only'},
        }
        self.template.write_text(json.dumps(self.settings), encoding='utf-8')

    def opts(self, **kwargs):
        return dict(template=self.template, repo=self.repo, state=self.state,
                    **kwargs)

    def test_dry_run_selects_real_git_files_without_codex(self):
        with patch('article_harness.local.shutil.which', side_effect=lambda v: '/usr/bin/git' if v == 'git' else None):
            result = supervise(**self.opts(dry_run=True))
        self.assertEqual(result, 0)
        inventory = json.loads((self.state / 'local-project.json').read_text())['inventory']
        self.assertEqual(set(inventory), {'docs/one.md', 'docs/two.md'})
        self.assertEqual((self.repo / 'docs/one.md').read_text(), '# Original\n')

    def test_three_cycles_continue_after_an_individual_task_block(self):
        events = []
        with patch('article_harness.local.shutil.which', return_value='/bin/true'):
            result = supervise(**self.opts(max_cycles=3, interval_minutes=0,
                run_cycle=lambda **kwargs: events.append(kwargs['project']) or {
                    'status': 'completed', 'exit_code': 2},
                sleep=lambda _: None))
        self.assertEqual(result, 0)
        self.assertEqual(events, ['local-fixture'] * 3)

    def test_repeated_infrastructure_failure_stops_for_review(self):
        events = []
        with patch('article_harness.local.shutil.which', return_value='/bin/true'):
            result = supervise(**self.opts(max_cycles=5, interval_minutes=0,
                run_cycle=lambda **kwargs: events.append(1) or {
                    'status': 'infrastructure_error', 'exit_code': 1},
                sleep=lambda _: None))
        self.assertEqual(result, 1)
        self.assertEqual(len(events), 3)

    def test_persistent_stop_flag_prevents_new_agent_runs(self):
        self.state.mkdir()
        (self.state / 'STOP').touch()
        with patch('article_harness.local.shutil.which', return_value='/bin/true'):
            result = supervise(**self.opts(run_cycle=lambda **kw: self.fail('unexpected run')))
        self.assertEqual(result, 0)

    def test_parallel_supervisor_for_one_state_is_rejected(self):
        self.state.mkdir()
        with file_lock(self.state / 'local-supervisor.lock'):
            with patch('article_harness.local.shutil.which', return_value='/bin/true'):
                with self.assertRaises(HarnessError):
                    supervise(**self.opts(dry_run=True))

    def test_previous_state_cannot_be_reused_for_another_site(self):
        self.state.mkdir()
        (self.state / 'local-owner.json').write_text(
            json.dumps({'project': 'different', 'repo': str(self.repo.resolve())}),
            encoding='utf-8')
        with patch('article_harness.local.shutil.which', return_value='/bin/true'):
            with self.assertRaises(ContinuityError):
                supervise(**self.opts(dry_run=True))

    def test_managed_repo_cannot_use_automatic_unmanaged_mode(self):
        self.settings['coordination'] = {'mode': 'unmanaged'}
        self.template.write_text(json.dumps(self.settings), encoding='utf-8')
        with self.assertRaises(ContinuityError):
            supervise(**self.opts(dry_run=True))

    def test_explicit_cycle_preserves_task_error_and_proceeds(self):
        args = []
        with patch('article_harness.local.shutil.which', return_value='/bin/true'):
            result = cycle(**self.opts(), project='local-fixture', runner='codex',
                           model=None, workers=2, max_jobs=3, max_plans=2,
                           max_seconds=2700, agent_timeout=650,
                           revisions=2, limit=24,
                           run_cli=lambda argv: args.append(argv) or 2)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['exit_code'], 2)
        self.assertEqual(len(args), 1)
        self.assertIn('--project', args[0])
        self.assertTrue((self.state / 'local-last-cycle.json').is_file())

    def test_idle_cycle_never_calls_codex(self):
        store = Store(self.state)
        repo = str(self.repo.resolve())
        for name in ('one.md', 'two.md'):
            store.add({'id': name.replace('.', '-'), 'repo': repo,
                       'files': ['docs/' + name], 'brief': 'Check source',
                       'reader_job': 'Choose relevant option',
                       'acceptance_criteria': ['Verified answer'],
                       'sources': ['https://example.org/'],
                       'coordination': {'mode': 'proposal-only'}})
            store.update(name.replace('.', '-'), 'reviewed_proposal')
        result = cycle(**self.opts(), project='local-fixture', runner='codex',
                       model=None, workers=2, max_jobs=3, max_plans=2,
                       max_seconds=2700, agent_timeout=650,
                       revisions=2, limit=24,
                       run_cli=lambda argv: self.fail('Codex must not run when idle'))
        self.assertEqual(result['status'], 'idle')


if __name__ == '__main__':
    unittest.main()
