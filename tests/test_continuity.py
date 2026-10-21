"""Offline continuity tests: no model calls or external services."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from article_harness.continuity import ContinuityError, backup, restore
from article_harness.core import Store


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        Store(self.state)
        (self.state / 'tasks' / 'example').mkdir(parents=True)
        (self.state / 'tasks/example/receipt.json').write_text(
            '{"status":"reviewed_proposal"}', encoding='utf-8')
        self.archive = self.root / 'checkpoint.tar.gz'

    def test_restores_sqlite_and_immutable_receipt(self):
        prior = (self.state / 'tasks/example/receipt.json').read_bytes()
        receipt = backup(self.state, self.archive, 'qualification-pilot')
        self.assertGreaterEqual(receipt['files'], 2)
        import shutil
        shutil.rmtree(self.state)
        result = restore(self.archive, self.state, 'qualification-pilot')
        self.assertEqual(result['files'], receipt['files'])
        self.assertEqual((self.state / 'tasks/example/receipt.json').read_bytes(), prior)
        self.assertEqual(Store(self.state).rows(), [])

    def test_project_identity_mismatch_fails_closed(self):
        backup(self.state, self.archive, 'qualification-pilot')
        dest = self.root / 'wrong-target'
        with self.assertRaises(ContinuityError):
            restore(self.archive, dest, 'another-project')
        self.assertFalse(dest.exists())

    def test_does_not_overwrite_existing_state(self):
        backup(self.state, self.archive, 'qualification-pilot')
        with self.assertRaises(ContinuityError):
            restore(self.archive, self.state, 'qualification-pilot')

    def test_rejects_archive_path_traversal(self):
        malicious = b'not allowed'
        files = {'../../escape': hashlib.sha256(malicious).hexdigest()}
        meta = json.dumps({'schema': 1, 'project': 'qualification-pilot',
                           'files': files}).encode()
        with tarfile.open(self.archive, 'w:gz') as tf:
            bad = tarfile.TarInfo('state/../../escape')
            bad.size = len(malicious)
            tf.addfile(bad, io.BytesIO(malicious))
            manifest = tarfile.TarInfo('manifest.json')
            manifest.size = len(meta)
            tf.addfile(manifest, io.BytesIO(meta))
        with self.assertRaises(ContinuityError):
            restore(self.archive, self.root / 'restored', 'qualification-pilot')
        self.assertFalse((self.root / 'escape').exists())

    def test_prepares_only_unreserved_tracked_articles(self):
        import subprocess
        from article_harness.continuity import prepare_project
        repo = self.root / 'site'
        repo.mkdir()
        subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True)
        subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'test@example.invalid'], check=True)
        subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Test'], check=True)
        (repo / 'docs').mkdir()
        for name in ('a.md', 'b.md', 'c.mdx'):
            (repo / 'docs' / name).write_text('# Article', encoding='utf-8')
        (repo / 'ignored.md').write_text('# Not an article', encoding='utf-8')
        subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'fixture'], check=True)
        task = {
            'id': 'reserved-one', 'repo': str(repo.resolve()), 'files': ['docs/a.md'],
            'brief': 'Reader decision', 'reader_job': 'Decide what to choose',
            'acceptance_criteria': ['Verified comparison'],
            'sources': ['https://example.org/source'],
            'coordination': {'mode': 'proposal-only'},
        }
        Store(self.state).add(task)
        cfg = {'id': 'qualification-pilot', 'article_globs': ['docs/*.md', 'docs/*.mdx'],
               'brief': 'Check actual reader benefits', 'sources': ['https://example.org/source'],
               'policy_files': [], 'coordination': {'mode': 'proposal-only'}}
        template = self.root / 'template.json'
        template.write_text(json.dumps(cfg), encoding='utf-8')
        output = self.root / 'project.json'
        result = prepare_project(template, repo, self.state, output, 'qualification-pilot', 10)
        selected = json.loads(output.read_text())['inventory']
        self.assertEqual(result['eligible'], 2)
        self.assertEqual(set(selected), {'docs/b.md', 'docs/c.mdx'})
        self.assertEqual(result['previously_reserved'], 1)

    def test_rejects_symlink_in_state(self):
        (self.state / 'tasks/link').symlink_to(self.root)
        with self.assertRaises(ContinuityError):
            backup(self.state, self.archive, 'qualification-pilot')


if __name__ == '__main__':
    unittest.main()
