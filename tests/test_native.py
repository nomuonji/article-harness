"""Native state integration tests. No models, network or live repositories."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from article_harness.native import NativeError, NativeStore


class NativeStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = NativeStore(self.root / 'state', max_parallel=2)
        clock = patch('article_harness.native.time.time', return_value=2_000_000_000.0)
        self.now = clock.start()
        self.addCleanup(clock.stop)

    def spec(self, task_id='article', repository='owner/repo', files=None, **changes):
        task = dict(id=task_id, repository=repository, base_sha='a' * 40,
                    brief='Improve the procedure using primary evidence.',
                    reader_job='Understand how to complete the procedure.',
                    files=files or ['docs/article.md'], sources=['https://example.invalid/source'],
                    acceptance_criteria=['Answers the reader question', 'Cites primary evidence'],
                    coordination={'mode': 'unmanaged'}, max_revisions=2)
        task.update(changes)
        return task

    def packet(self, name, files=None, content='Original article.\n'):
        folder = self.root / name
        for name in files or ['docs/article.md']:
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        return folder

    def add(self, task=None):
        task = task or self.spec()
        return self.store.add(task, self.packet('source-' + task['id'], task['files']))

    def submitted(self, task=None):
        task = task or self.spec()
        self.add(task)
        lease = self.store.claim(task['id'], 'native-worker')
        candidate = self.packet('candidate-' + task['id'], task['files'], 'Improved sourced procedure.\n')
        return self.store.submit(task['id'], lease['token'], candidate)

    def report(self, submission, verdict='pass', status='pass'):
        return dict(digest=submission['digest'], verdict=verdict,
                    summary='Checked candidate content against the source.',
                    criteria=[dict(criterion=c, status=status, evidence='docs/article.md: checked the prerequisite.')
                              for c in submission['manifest']['acceptance_criteria']],
                    findings=[] if verdict == 'pass' else [dict(path='docs/article.md', detail='Add a missing prerequisite.')])

    def test_repository_lock_and_parallel_limit(self):
        for task in (self.spec(), self.spec('same', 'OWNER/REPO', ['docs/other.md']),
                     self.spec('second', 'owner/second'), self.spec('third', 'owner/third')):
            self.add(task)
        self.store.claim('article', 'worker-one')
        with self.assertRaisesRegex(NativeError, 'Repository is owned'):
            self.store.claim('same', 'worker-two')
        self.store.claim('second', 'worker-two')
        with self.assertRaisesRegex(NativeError, 'slots'):
            self.store.claim('third', 'worker-three')
        self.assertEqual(2, sum(x['status'] == 'working' for x in self.store.list()))

    def test_expiry_cannot_submit_or_automatically_reclaim(self):
        self.add()
        lease = self.store.claim('article', 'worker', ttl=5)
        candidate = self.packet('candidate', content='Changed.\n')
        self.now.return_value += 6
        with self.assertRaisesRegex(NativeError, 'lease'):
            self.store.submit('article', lease['token'], candidate)
        with self.assertRaisesRegex(NativeError, 'queued'):
            self.store.claim('article', 'replacement')
        self.assertEqual('interrupted', self.store.list()[0]['status'])
        self.assertEqual('queued', self.store.resume('article')['status'])
        replacement = self.store.claim('article', 'replacement')
        self.assertNotEqual(lease['token'], replacement['token'])
        with self.assertRaises(NativeError):
            self.store.heartbeat('article', lease['token'])

    def test_heartbeat_extends_current_lease_and_rejects_stale_or_foreign_token(self):
        self.add()
        lease = self.store.claim('article', 'worker', ttl=5)
        self.now.return_value += 4
        renewed = self.store.heartbeat('article', lease['token'], ttl=10)
        self.assertGreater(renewed['expires_at'], lease['expires_at'])
        with self.assertRaises(NativeError):
            self.store.heartbeat('article', 'foreign-token')
        self.now.return_value += 11
        with self.assertRaises(NativeError):
            self.store.heartbeat('article', lease['token'])

    def test_candidate_exact_allowlist_symlink_deletion_and_non_utf8(self):
        self.add()
        lease = self.store.claim('article', 'worker')
        candidate = self.packet('candidate', content='Changed.\n')
        target, extra = candidate / 'docs/article.md', candidate / 'outside.txt'
        extra.write_text('Unauthorized.')
        with self.assertRaisesRegex(NativeError, 'exactly'):
            self.store.submit('article', lease['token'], candidate)
        extra.unlink()
        target.unlink()
        with self.assertRaisesRegex(NativeError, 'exactly'):
            self.store.submit('article', lease['token'], candidate)
        target.symlink_to(self.root / 'source-article/docs/article.md')
        with self.assertRaisesRegex(NativeError, 'symlink'):
            self.store.submit('article', lease['token'], candidate)
        target.unlink()
        target.write_bytes(b'\xff\xfe')
        with self.assertRaises((NativeError, UnicodeDecodeError)):
            self.store.submit('article', lease['token'], candidate)
        self.assertEqual('working', self.store.list()[0]['status'])
        target.write_text('Valid changed article.\n')
        self.assertEqual('article', self.store.submit('article', lease['token'], candidate)['task_id'])

    def test_unsafe_paths_and_symlinked_original_parent_rejected(self):
        source = self.packet('source', ['article.md'])
        folder = self.root / 'original'
        folder.mkdir()
        (folder / 'docs').symlink_to(source, target_is_directory=True)
        with self.assertRaisesRegex(NativeError, 'symlink'):
            self.store.add(self.spec(), folder)
        for name in ('../escape.md', '/absolute.md', '.git/config', 'docs/../article.md', 'docs\\article.md'):
            with self.subTest(path=name), self.assertRaises(NativeError):
                self.store.add(self.spec(files=[name]), source)

    def test_noop_submission_rejected(self):
        self.add()
        lease = self.store.claim('article', 'worker')
        with self.assertRaisesRegex(NativeError, 'No material'):
            self.store.submit('article', lease['token'], self.packet('candidate'))

    def test_pass_rejects_unknown_failed_or_defective_work(self):
        submission = self.submitted()
        lease = self.store.begin_review('article', 'reviewer')
        for status in ('unknown', 'fail'):
            with self.subTest(status=status), self.assertRaisesRegex(NativeError, 'cannot pass'):
                self.store.review('article', lease['token'], self.report(submission, status=status))
        report = self.report(submission)
        report['findings'] = [dict(path='docs/article.md', detail='Missing prerequisite.')]
        with self.assertRaisesRegex(NativeError, 'cannot pass'):
            self.store.review('article', lease['token'], report)
        self.assertEqual('ready_for_handoff', self.store.review('article', lease['token'], self.report(submission))['status'])

    def test_review_cannot_weaken_omit_or_duplicate_criteria(self):
        submission = self.submitted()
        lease = self.store.begin_review('article', 'reviewer')
        for mutation in ('weaken', 'omit', 'duplicate'):
            report = self.report(submission)
            if mutation == 'weaken':
                report['criteria'][0]['criterion'] = 'Looks acceptable'
            elif mutation == 'omit':
                report['criteria'].pop()
            else:
                report['criteria'].append(copy.deepcopy(report['criteria'][0]))
            with self.subTest(mutation=mutation), self.assertRaisesRegex(NativeError, 'each exact criterion'):
                self.store.review('article', lease['token'], report)

    def test_worker_cannot_review_own_submission(self):
        self.submitted()
        with self.assertRaisesRegex(NativeError, 'distinct reviewer'):
            self.store.begin_review('article', 'native-worker')
        self.assertEqual('awaiting_review', self.store.list()[0]['status'])

    def test_candidate_tampering_detected_before_and_during_review(self):
        submission = self.submitted()
        target = Path(submission['version_dir']) / 'candidate/docs/article.md'
        original = target.read_bytes()
        target.write_text('Tampered before review.\n')
        with self.assertRaisesRegex(NativeError, 'modified'):
            self.store.begin_review('article', 'reviewer')
        target.write_bytes(original)
        lease = self.store.begin_review('article', 'reviewer')
        target.write_text('Tampered during review.\n')
        with self.assertRaisesRegex(NativeError, 'modified'):
            self.store.review('article', lease['token'], self.report(submission))

    def test_original_packet_copied_and_immutable_hash_checked(self):
        added = self.add()
        self.assertEqual(hashlib.sha256(b'Original article.\n').hexdigest(),
                         added['task']['original_sha256']['docs/article.md'])
        (self.root / 'source-article/docs/article.md').write_text('Later source change.\n')
        stored = self.root / 'state/tasks/article/original/docs/article.md'
        self.assertEqual(b'Original article.\n', stored.read_bytes())
        stored.write_text('Tampered original.\n')
        lease = self.store.claim('article', 'worker')
        with self.assertRaisesRegex(NativeError, 'Original packet was modified'):
            self.store.submit('article', lease['token'], self.packet('candidate', content='Changed.\n'))

    def test_revision_scope_preserved_and_budget_exhausted(self):
        submission = self.submitted(self.spec(max_revisions=1))
        lease = self.store.begin_review('article', 'reviewer-one')
        revised = self.store.review('article', lease['token'], self.report(submission, 'revise', 'fail'))
        self.assertEqual('queued', revised['status'])
        self.assertEqual(['docs/article.md'], revised['task']['files'])
        self.assertEqual(submission['manifest']['acceptance_criteria'], revised['task']['acceptance_criteria'])
        worker = self.store.claim('article', 'worker-two')
        self.assertEqual(2, worker['attempt'])
        submission = self.store.submit('article', worker['token'], self.packet('revision', content='Second candidate.\n'))
        lease = self.store.begin_review('article', 'reviewer-two')
        result = self.store.review('article', lease['token'], self.report(submission, 'revise', 'fail'))
        self.assertEqual('blocked', result['status'])
        self.assertEqual(2, len(result['feedback']))
        with self.assertRaises(NativeError):
            self.store.claim('article', 'worker-three')
        with self.assertRaises(NativeError):
            self.store.resume('article')

    def test_add_idempotency_rejects_task_or_original_mutation(self):
        task = self.spec()
        first = self.add(task)
        source = self.root / 'source-article'
        self.assertEqual(first, self.store.add(task, source))
        for field, value in (('brief', 'Different assignment'), ('acceptance_criteria', ['Weakened']),
                             ('max_revisions', 9), ('base_sha', 'b' * 40)):
            changed = copy.deepcopy(task)
            changed[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(NativeError, 'immutable'):
                self.store.add(changed, source)
        (source / 'docs/article.md').write_text('Different original.\n')
        with self.assertRaisesRegex(NativeError, 'immutable'):
            self.store.add(task, source)

    def test_pending_handoff_reserves_repository_until_observed_commit(self):
        submission = self.submitted()
        lease = self.store.begin_review('article', 'reviewer')
        result = self.store.review('article', lease['token'], self.report(submission))
        self.assertEqual('ready_for_handoff', result['status'])
        self.add(self.spec('next', files=['docs/next.md']))
        with self.assertRaisesRegex(NativeError, 'Repository is owned'):
            self.store.claim('next', 'next-worker')
        with self.assertRaises(NativeError):
            self.store.handoff('article', submission['digest'], {'commit_sha': 'b' * 40})
        self.store.handoff('article', submission['digest'], dict(commit_sha='b' * 40,
                           readback='Parent read back the committed exact candidate bytes.'))
        self.assertEqual('working', self.store.claim('next', 'next-worker')['status'])

    def test_handoff_rejects_wrong_digest_or_post_review_tampering(self):
        submission = self.submitted()
        lease = self.store.begin_review('article', 'reviewer')
        self.store.review('article', lease['token'], self.report(submission))
        evidence = dict(commit_sha='b' * 40, readback='Parent observed exact committed bytes.')
        with self.assertRaisesRegex(NativeError, 'exact independently reviewed'):
            self.store.handoff('article', '0' * 64, evidence)
        target = Path(submission['version_dir']) / 'candidate/docs/article.md'
        target.write_text('Changed after independent review.\n')
        with self.assertRaisesRegex(NativeError, 'modified'):
            self.store.handoff('article', submission['digest'], evidence)
        self.assertEqual('ready_for_handoff', self.store.list()[0]['status'])

    def test_refusal_cannot_resume_or_reissue_overlapping_work(self):
        self.add()
        lease = self.store.claim('article', 'worker')
        result = self.store.refuse('article', lease['token'], 'The native agent declined this task.')
        self.assertEqual('refused', result['status'])
        for action in (lambda: self.store.resume('article'),
                       lambda: self.store.claim('article', 'replacement'),
                       lambda: self.store.heartbeat('article', lease['token'])):
            with self.assertRaises(NativeError):
                action()
        with self.assertRaisesRegex(NativeError, 'Overlapping task'):
            self.add(self.spec('reissued'))

    def test_export_restore_interrupts_live_lease_without_replaying_token(self):
        self.add()
        lease = self.store.claim('article', 'worker')
        archive = self.root / 'portable.zip'
        exported = self.store.export(archive)
        self.assertTrue(exported['portable'])
        self.assertNotIn(lease['token'], json.dumps(exported))
        restored = NativeStore.restore(archive, self.root / 'restored')
        self.assertEqual('interrupted', restored.list()[0]['status'])
        self.assertNotIn('token', restored.list()[0])
        with sqlite3.connect(restored.db_path) as db:
            self.assertEqual((None, None), db.execute('SELECT token,expires FROM tasks').fetchone())
        with self.assertRaises(NativeError):
            restored.heartbeat('article', lease['token'])
        with self.assertRaises(NativeError):
            restored.claim('article', 'replacement')
        restored.resume('article')
        self.assertNotEqual(lease['token'], restored.claim('article', 'replacement')['token'])

    def test_interrupted_review_restores_exact_snapshot(self):
        submission = self.submitted()
        lease = self.store.begin_review('article', 'reviewer')
        archive = self.root / 'review.zip'
        self.store.export(archive)
        restored = NativeStore.restore(archive, self.root / 'restored')
        self.assertEqual('interrupted', restored.list()[0]['status'])
        self.assertEqual('awaiting_review', restored.resume('article')['status'])
        new = restored.begin_review('article', 'new-reviewer')
        with self.assertRaises(NativeError):
            restored.review('article', lease['token'], self.report(submission))
        self.assertEqual('ready_for_handoff', restored.review('article', new['token'], self.report(submission))['status'])

    def test_restore_validates_snapshots_and_cleans_failed_restoration(self):
        self.submitted()
        archive = self.root / 'good.zip'
        self.store.export(archive)
        for name in ('tasks/article/original/docs/article.md',
                     'tasks/article/versions/1/candidate/docs/article.md',
                     'tasks/article/versions/1/manifest.json'):
            with self.subTest(path=name):
                damaged = self.root / 'damaged.zip'
                with zipfile.ZipFile(archive) as old, zipfile.ZipFile(damaged, 'w') as new:
                    for item in old.infolist():
                        replacement = b'{}' if name.endswith('.json') else b'Corrupt content.\n'
                        new.writestr(item, replacement if item.filename == name else old.read(item))
                destination = self.root / 'rejected'
                with self.assertRaises(NativeError):
                    NativeStore.restore(damaged, destination)
                self.assertEqual([], list(destination.iterdir()))

    def test_restore_rejects_traversal_and_preserves_nonempty_destination(self):
        archive = self.root / 'unsafe.zip'
        with zipfile.ZipFile(archive, 'w') as packet:
            packet.writestr('../escaped.txt', 'escape')
        with self.assertRaises(NativeError):
            NativeStore.restore(archive, self.root / 'restored')
        self.assertFalse((self.root / 'escaped.txt').exists())
        occupied = self.root / 'occupied'
        occupied.mkdir()
        marker = occupied / 'keep.txt'
        marker.write_text('Keep existing data.')
        with self.assertRaises(NativeError):
            NativeStore.restore(archive, occupied)
        self.assertEqual('Keep existing data.', marker.read_text())


if __name__ == '__main__':
    unittest.main()
