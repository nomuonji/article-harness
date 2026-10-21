"""Behavioral integration tests. No models, network, or live repositories are used."""
import copy
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from article_harness.core import Controller, HarnessError, Reservation, Store, file_lock, git
from article_harness.runner import AgentError
from article_harness import __main__ as cli


class FakeRunner:
    def __init__(self, behaviors=None, barrier=None):
        self.behaviors = behaviors or {}
        self.barrier = barrier
        self.lock = threading.Lock()
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.by_repo = {}
        self.max_by_repo = {}
        self.counts = {}
        self.resume_saw_draft = False

    def run(self, role, task, workspace, run_dir, feedback, timeout):
        workspace = Path(workspace)
        with self.lock:
            self.calls.append((role, task['id'], copy.deepcopy(feedback)))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            repo = task['repo']
            self.by_repo[repo] = self.by_repo.get(repo, 0) + 1
            self.max_by_repo[repo] = max(self.max_by_repo.get(repo, 0), self.by_repo[repo])
            key = (role, task['id'])
            self.counts[key] = self.counts.get(key, 0) + 1
            count = self.counts[key]
        behavior = self.behaviors.get(task['id'], '')
        try:
            if role == 'worker':
                if self.barrier and count == 1:
                    self.barrier.wait(timeout=5)
                time.sleep(0.03)
                if behavior == 'blocked':
                    return {'status': 'blocked', 'summary': 'Primary source unavailable', 'evidence': []}
                if behavior == 'refused':
                    return {'status': 'refused', 'summary': 'Operation refused', 'evidence': []}
                article = workspace / task['files'][0]
                if behavior == 'interrupt-once':
                    if count == 1:
                        article.write_text('# Article\nDraft retained on interruption\n', encoding='utf-8')
                        raise AgentError('timeout', 'simulated interrupted model process')
                    self.resume_saw_draft = 'Draft retained' in article.read_text()
                article.write_text(f'# Article\nVerified useful answer for {task["id"]}, pass {count}.\n', encoding='utf-8')
                if behavior == 'outside':
                    (workspace / 'other.txt').write_text('unauthorized edit\n')
                if behavior == 'source-moved':
                    source = Path(task['repo'])
                    (source / 'other.txt').write_text('unrelated source update\n')
                    git(source, 'add', 'other.txt')
                    git(source, 'commit', '-m', 'concurrent source update')
                return {'status': 'done', 'summary': 'Added the missing reader answer',
                        'evidence': [{'claim': 'Reader answer is supported', 'source': 'https://example.org/source', 'support': 'Fixture source passage'}]}
            verdict = 'revise' if behavior in ('revise-once', 'always-revise') and (count == 1 or behavior == 'always-revise') else 'pass'
            status = 'fail' if verdict == 'revise' else 'unknown' if behavior == 'unknown-pass' else 'pass'
            return {'verdict': verdict, 'summary': 'Check the complete article',
                    'findings': [{'path': task['files'][0], 'detail': 'Missing a necessary condition'}] if verdict == 'revise' else [],
                    'criteria': [{'criterion': item, 'status': status, 'evidence': 'Fixture complete-body check'} for item in task['acceptance_criteria']]}
        finally:
            with self.lock:
                self.active -= 1
                self.by_repo[repo] -= 1


class HarnessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'ARTICLE_HARNESS_LOCK_DIR': str(self.root / 'locks')})
        self.env.start()
        self.store = Store(self.root / 'state')

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def repo(self, name, articles=('article.md',)):
        repo = self.root / name
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        git(repo, 'config', 'user.name', 'Harness Test')
        git(repo, 'config', 'user.email', 'test@example.invalid')
        for article in articles:
            (repo / article).write_text('# Article\nOriginal incomplete answer\n', encoding='utf-8')
        (repo / 'other.txt').write_text('original\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'fixture baseline')
        return repo

    def task(self, task_id, repo, article='article.md', **kwargs):
        return dict(id=task_id, repo=str(repo), files=[article],
                    brief='Replace the incomplete answer with a source-backed explanation',
                    reader_job='Understand the decision and applicable conditions',
                    acceptance_criteria=['Answer includes the necessary condition'],
                    sources=['https://example.org/source'], coordination={'mode': 'unmanaged'}, **kwargs)

    def run_controller(self, runner, **kwargs):
        controller = Controller(self.store, runner=runner, agent_timeout=10, max_seconds=30, **kwargs)
        return {row['id']: row for row in controller.run()}

    def test_refills_slots_and_runs_independent_repositories_in_parallel(self):
        for i in range(6):
            self.store.add(self.task(f'task-{i}', self.repo(f'repo-{i}')))
        runner = FakeRunner(barrier=threading.Barrier(3))
        # Only the first wave should synchronize; subsequent waves can proceed freely.
        original_run = runner.run
        def first_wave_only(role, task, *args):
            if int(task['id'].split('-')[-1]) >= 3:
                runner.barrier = None
            return original_run(role, task, *args)
        runner.run = first_wave_only
        rows = self.run_controller(runner, workers=3)
        self.assertEqual({r['status'] for r in rows.values()}, {'ready_for_handoff'}, rows)
        self.assertEqual(runner.max_active, 3)
        self.assertEqual(len([c for c in runner.calls if c[0] == 'worker']), 6)

    def test_same_repository_is_serialized(self):
        repo = self.repo('shared', ('one.md', 'two.md', 'three.md'))
        for i, path in enumerate(('one.md', 'two.md', 'three.md')):
            self.store.add(self.task(f'shared-{i}', repo, path))
        runner = FakeRunner()
        rows = self.run_controller(runner, workers=3)
        self.assertEqual({r['status'] for r in rows.values()}, {'ready_for_handoff'}, rows)
        self.assertEqual(runner.max_by_repo[str(repo)], 1)

    def test_proposal_only_preserves_review_without_authorizing_delivery(self):
        repo = self.repo('safe-proposal')
        task = self.task('safe-proposal', repo)
        task['coordination'] = {'mode': 'proposal-only'}
        self.store.add(task)
        rows = self.run_controller(FakeRunner())
        self.assertEqual(rows['safe-proposal']['status'], 'reviewed_proposal')
        receipt = json.loads((self.store.root / 'tasks/safe-proposal/receipt.json').read_text())
        self.assertEqual(receipt['status'], 'reviewed_proposal')
        self.assertEqual(receipt['publication'], 'not_run')
        self.assertEqual((repo / 'article.md').read_text(),
                         '# Article\nOriginal incomplete answer\n')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--state', str(self.store.root),
                                       'ack-applied', 'safe-proposal']), 1)

    def test_editorial_revise_is_returned_to_worker_then_rechecked(self):
        self.store.add(self.task('revise', self.repo('revise')))
        runner = FakeRunner({'revise': 'revise-once'})
        rows = self.run_controller(runner)
        self.assertEqual(rows['revise']['status'], 'ready_for_handoff', rows)
        worker_calls = [call for call in runner.calls if call[0] == 'worker']
        self.assertEqual(len(worker_calls), 2)
        self.assertEqual(worker_calls[0][2], [])
        self.assertIn('Missing a necessary condition', worker_calls[1][2][0]['findings'][0]['detail'])
        self.assertEqual(runner.counts[('reviewer', 'revise')], 2)

    def test_blocked_or_refused_task_does_not_stall_other_work(self):
        for task_id in ('blocked', 'refused', 'healthy'):
            self.store.add(self.task(task_id, self.repo(task_id)))
        runner = FakeRunner({'blocked': 'blocked', 'refused': 'refused'})
        rows = self.run_controller(runner, workers=1)
        self.assertEqual(rows['blocked']['status'], 'blocked')
        self.assertEqual(rows['refused']['status'], 'refused')
        self.assertEqual(rows['healthy']['status'], 'ready_for_handoff')
        self.assertEqual(runner.counts[('worker', 'refused')], 1)

    def test_out_of_scope_mutation_fails_before_review(self):
        repo = self.repo('outside')
        self.store.add(self.task('outside', repo))
        runner = FakeRunner({'outside': 'outside'})
        rows = self.run_controller(runner)
        self.assertEqual(rows['outside']['status'], 'failed')
        self.assertIn('Out-of-scope', rows['outside']['detail'])
        self.assertNotIn(('reviewer', 'outside'), runner.counts)
        self.assertEqual((repo / 'other.txt').read_text(), 'original\n')

    def test_interruption_keeps_worktree_and_resume_preserves_attempt_budget(self):
        repo = self.repo('resume')
        self.store.add(self.task('resume', repo))
        runner = FakeRunner({'resume': 'interrupt-once'})
        rows = self.run_controller(runner)
        self.assertEqual(rows['resume']['status'], 'interrupted')
        self.assertEqual(rows['resume']['attempts'], 1)
        draft = self.store.root / 'tasks/resume/worktree/article.md'
        self.assertIn('Draft retained', draft.read_text())
        self.store.update('resume', 'queued', 'User requested resume after inspecting worktree')
        rows = self.run_controller(runner)
        self.assertTrue(runner.resume_saw_draft)
        self.assertEqual(rows['resume']['attempts'], 2)
        self.assertEqual(rows['resume']['status'], 'ready_for_handoff', rows)

    def test_revision_limit_blocks_one_task_without_endless_retry(self):
        self.store.add(self.task('never', self.repo('never')))
        runner = FakeRunner({'never': 'always-revise'})
        rows = self.run_controller(runner, revisions=1)
        self.assertEqual(rows['never']['status'], 'blocked')
        self.assertEqual(runner.counts[('worker', 'never')], 2)
        self.assertIn('revision limit', rows['never']['detail'])

    def test_controller_recovers_orphaned_status_without_automatic_replay(self):
        self.store.add(self.task('orphan', self.repo('orphan')))
        self.store.update('orphan', 'working', attempts=1)
        runner = FakeRunner()
        rows = self.run_controller(runner)
        self.assertEqual(rows['orphan']['status'], 'interrupted')
        self.assertEqual(runner.calls, [])

    def test_foreign_repository_lock_prevents_agent_start(self):
        repo = self.repo('locked')
        self.store.add(self.task('locked', repo))
        key = hashlib.sha256(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip().encode()).hexdigest()
        runner = FakeRunner()
        with file_lock(self.root / 'locks' / (key + '.lock')):
            rows = self.run_controller(runner)
        self.assertEqual(rows['locked']['status'], 'failed')
        self.assertIn('Already locked', rows['locked']['detail'])
        self.assertEqual(runner.calls, [])

    def test_expired_external_lease_prevents_worktree_and_agent(self):
        repo = self.repo('expired')
        task = self.task('expired', repo)
        task['coordination'] = {'mode': 'external-reservation', 'command': [sys.executable, '-c', 'import json; print(json.dumps({"granted":True,"reservation_id":"r1","expires_at":"2000-01-01T00:00:00Z"}))']}
        self.store.add(task)
        runner = FakeRunner()
        rows = self.run_controller(runner)
        self.assertEqual(rows['expired']['status'], 'failed')
        self.assertIn('Invalid reservation', rows['expired']['detail'])
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.store.root / 'tasks/expired/worktree').exists())

    def test_pass_with_unknown_acceptance_is_not_handed_off(self):
        self.store.add(self.task('unknown', self.repo('unknown')))
        rows = self.run_controller(FakeRunner({'unknown': 'unknown-pass'}))
        self.assertNotEqual(rows['unknown']['status'], 'ready_for_handoff')
        self.assertFalse((self.store.root / 'tasks/unknown/receipt.json').exists())

    def test_changed_source_base_requires_reconciliation(self):
        self.store.add(self.task('moved', self.repo('moved')))
        rows = self.run_controller(FakeRunner({'moved': 'source-moved'}))
        self.assertEqual(rows['moved']['status'], 'failed')
        self.assertIn('Source HEAD changed', rows['moved']['detail'])

    def test_handoff_patch_is_applicable_and_source_checkout_is_untouched(self):
        repo = self.repo('patch')
        base = git(repo, 'rev-parse', 'HEAD')
        before = (repo / 'article.md').read_bytes()
        self.store.add(self.task('patch', repo))
        rows = self.run_controller(FakeRunner())
        self.assertEqual(rows['patch']['status'], 'ready_for_handoff', rows)
        patch_file = Path(rows['patch']['detail'])
        git(repo, 'apply', '--check', str(patch_file))
        self.assertEqual((repo / 'article.md').read_bytes(), before)
        self.assertEqual(git(repo, 'status', '--porcelain'), '')
        self.assertEqual(git(repo, 'rev-parse', 'HEAD'), base)
        receipt = json.loads(patch_file.with_name('receipt.json').read_text())
        self.assertEqual(receipt['build'], 'not_run')
        self.assertEqual(receipt['publication'], 'not_run')

    def test_task_addition_is_idempotent_but_changed_ids_and_overlap_are_rejected(self):
        repo = self.repo('dedupe')
        task = self.task('first', repo)
        self.assertTrue(self.store.add(task))
        self.assertFalse(self.store.add(task))
        with self.assertRaises(HarnessError):
            self.store.add(dict(task, brief='Changed immutable task'))
        with self.assertRaises(HarnessError):
            self.store.add(dict(task, id='second'))

    def test_max_jobs_leaves_remaining_tasks_queued_for_next_run(self):
        for i in range(3):
            self.store.add(self.task(f'limited-{i}', self.repo(f'limited-{i}')))
        rows = self.run_controller(FakeRunner(), workers=2, max_jobs=1)
        statuses = [row['status'] for row in rows.values()]
        self.assertEqual(statuses.count('ready_for_handoff'), 1)
        self.assertEqual(statuses.count('queued'), 2)

    def test_existing_stop_request_dispatches_nothing(self):
        self.store.add(self.task('stopped', self.repo('stopped')))
        (self.store.root / 'STOP').touch()
        runner = FakeRunner()
        rows = self.run_controller(runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(rows['stopped']['status'], 'queued')

    def test_granted_but_invalid_external_lease_is_saved_and_released(self):
        repo = self.repo('short-lease')
        task = self.task('short-lease', repo)
        task['coordination'] = {'mode': 'external-reservation', 'command': ['reservation-fixture']}
        self.store.add(task)
        operations = []
        real_run = subprocess.run
        def reservation_run(command, *args, **kwargs):
            if command[0] != 'reservation-fixture':
                return real_run(command, *args, **kwargs)
            payload = json.loads(kwargs['input'])
            operations.append(payload)
            receipt = {'granted': True, 'reservation_id': 'already-created-lease', 'expires_at': 'not-a-date'}
            return subprocess.CompletedProcess(command, 0, json.dumps(receipt), '')
        with patch('article_harness.core.subprocess.run', side_effect=reservation_run):
            rows = self.run_controller(FakeRunner())
        self.assertEqual([p['operation'] for p in operations], ['claim', 'release'])
        self.assertEqual(operations[1]['reservation']['reservation_id'], 'already-created-lease')
        saved = json.loads((self.store.root / 'tasks/short-lease/reservation.json').read_text())
        self.assertFalse(saved['active'])
        self.assertEqual(saved['receipt']['reservation_id'], 'already-created-lease')
        self.assertEqual(rows['short-lease']['status'], 'failed')

    def test_restarted_reservation_renews_existing_durable_identifier(self):
        task = self.task('durable', self.repo('durable'))
        task['coordination'] = {'mode': 'external-reservation', 'command': ['reservation-fixture']}
        checkpoint = self.root / 'reservation.json'
        operations = []
        def reservation_run(command, *args, **kwargs):
            payload = json.loads(kwargs['input'])
            operations.append(payload)
            expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
            return subprocess.CompletedProcess(command, 0, json.dumps({'granted': True, 'reservation_id': 'durable-r1', 'expires_at': expires}), '')
        with patch('article_harness.core.subprocess.run', side_effect=reservation_run):
            first = Reservation(task, 10, checkpoint)
            first.call('claim')
            restarted = Reservation(task, 10, checkpoint)
            restarted.call('claim')
        self.assertEqual([p['operation'] for p in operations], ['claim', 'renew'])
        self.assertEqual(operations[1]['reservation']['reservation_id'], 'durable-r1')

    def test_ack_applied_uses_reviewed_hashes_not_mutable_worktree(self):
        repo = self.repo('ack-content')
        self.store.add(self.task('ack-content', repo))
        rows = self.run_controller(FakeRunner())
        self.assertEqual(rows['ack-content']['status'], 'ready_for_handoff')
        folder = self.store.root / 'tasks/ack-content'
        unreviewed = '# Article\nUnreviewed replacement masquerading as the reviewed answer\n'
        (folder / 'worktree/article.md').write_text(unreviewed)
        (repo / 'article.md').write_text(unreviewed)
        git(repo, 'add', 'article.md')
        git(repo, 'commit', '-qm', 'unreviewed source bytes')
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = cli.main(['--state', str(self.store.root), 'ack-applied', 'ack-content'])
        self.assertEqual(code, 1)
        self.assertIn('Reviewed content is not in source HEAD', stderr.getvalue())
        self.assertEqual(self.store.rows()[0]['status'], 'ready_for_handoff')

    def test_ack_applied_rejects_tampered_patch_digest(self):
        repo = self.repo('ack-patch')
        self.store.add(self.task('ack-patch', repo))
        rows = self.run_controller(FakeRunner())
        patch_file = Path(rows['ack-patch']['detail'])
        git(repo, 'apply', str(patch_file))
        git(repo, 'add', 'article.md')
        git(repo, 'commit', '-qm', 'reviewed article')
        patch_file.write_text(patch_file.read_text() + '\n# tampered\n')
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = cli.main(['--state', str(self.store.root), 'ack-applied', 'ack-patch'])
        self.assertEqual(code, 1)
        self.assertIn('Reviewed patch digest mismatch', stderr.getvalue())

    def test_default_cli_state_is_outside_repository(self):
        repo = self.repo('cwd-source')
        fake_home = self.root / 'fake-home'
        old_cwd = Path.cwd()
        try:
            os.chdir(repo)
            with patch.object(Path, 'home', return_value=fake_home), contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(['status'])
        finally:
            os.chdir(old_cwd)
        self.assertEqual(code, 0)
        self.assertEqual(git(repo, 'status', '--porcelain'), '')
        self.assertEqual(len(list(fake_home.rglob('state.sqlite3'))), 1)

    def test_manager_replenishes_empty_queue_and_controller_executes_new_work(self):
        repo = self.repo('replenished')
        replenishes = []
        def replenish(timeout):
            replenishes.append(timeout)
            if len(replenishes) == 1:
                self.store.add(self.task('replenished', repo))
                return True
            return False
        rows = self.run_controller(FakeRunner(), replenish=replenish)
        self.assertEqual(rows['replenished']['status'], 'ready_for_handoff')
        self.assertEqual(len(replenishes), 2)
        self.assertTrue(all(0 < timeout <= 10 for timeout in replenishes))

    def test_readonly_planner_excludes_busy_files_but_reconsiders_applied_files(self):
        repo = self.repo('planning', ('busy.md', 'applied.md', 'fresh.md'))
        self.store.add(self.task('busy', repo, 'busy.md'))
        self.store.add(self.task('old-applied', repo, 'applied.md'))
        self.store.update('old-applied', 'applied', 'Previously delivered')
        config = {'repo': str(repo), 'inventory': ['busy.md', 'applied.md', 'fresh.md'],
                  'brief': 'Find missing decisive reader answers', 'sources': ['https://example.org/source'],
                  'coordination': {'mode': 'unmanaged'}}
        class PlanningRunner:
            def __init__(self):
                self.input = None
            def run(inner, role, task, workspace, run_dir, feedback, timeout):
                self.assertEqual(role, 'planner')
                inner.input = task
                return {'summary': 'Found sourced reader-facing omissions', 'tasks': [
                    {k: v for k, v in self.task('planned-applied', repo, 'applied.md').items()
                     if k in ('id', 'files', 'brief', 'reader_job', 'acceptance_criteria', 'sources')},
                    {k: v for k, v in self.task('planned-fresh', repo, 'fresh.md').items()
                     if k in ('id', 'files', 'brief', 'reader_job', 'acceptance_criteria', 'sources')}]}
        planner = PlanningRunner()
        result = cli.plan(self.store, config, planner, 2, 10)
        self.assertEqual(planner.input['inventory'], ['applied.md', 'fresh.md'])
        self.assertEqual(result['added'], ['planned-applied', 'planned-fresh'])
        self.assertEqual(result['rejected'], [])
        self.assertEqual(git(repo, 'status', '--porcelain'), '')
        tasks = {row['id']: json.loads(row['spec']) for row in self.store.rows()}
        self.assertEqual(tasks['planned-fresh']['planned_base_sha'], git(repo, 'rev-parse', 'HEAD').strip())


if __name__ == '__main__':
    unittest.main()
