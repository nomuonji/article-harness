"""Delivery integrity at the GitHub object boundary; no remote writes."""
import base64
import copy
import hashlib
import json
import unittest
from unittest.mock import patch
import urllib.error

from article_harness.delivery import DeliveryError, GitHubReader, prepare, verify


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


class FixtureReader(GitHubReader):
    def __init__(self, paths=None):
        super().__init__('owner/repo')
        paths = paths or ['docs/article.md']
        self.objects = {}
        original = {p: self.entry(b'Original article.\n') for p in paths}
        candidate = {p: self.entry(b'Improved sourced procedure.\n') for p in paths}
        self.trees = {'1' * 40: original, '2' * 40: candidate}
        self.commits = {'a' * 40: {'sha': 'a' * 40, 'tree': {'sha': '1' * 40}, 'parents': []},
                        'b' * 40: {'sha': 'b' * 40, 'tree': {'sha': '2' * 40}, 'parents': [{'sha': 'a' * 40}]}}
        self.current = 'a' * 40
        self.failure = False

    def entry(self, data):
        sha = blob(data)
        self.objects[sha] = {'encoding': 'base64', 'content': base64.b64encode(data).decode()}
        return {'type': 'blob', 'mode': '100644', 'sha': sha}

    def get(self, path):
        if self.failure:
            raise DeliveryError('GitHub HTTP 429; delivery pending')
        if path.startswith('/pulls?'):
            return []
        if path.startswith('/git/ref/'):
            return {'object': {'sha': self.current}}
        if path.startswith('/git/commits/'):
            return self.commits[path.split('/')[-1]]
        if path.startswith('/git/trees/'):
            sha = path.split('/')[-1].split('?')[0]
            return {'sha': sha, 'truncated': False, 'tree': [dict(x, path=p) for p, x in self.trees[sha].items()]}
        return self.objects[path.split('/')[-1]]


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.reader = FixtureReader()
        self.task = {'id': 'article', 'repository': 'owner/repo', 'base_sha': 'a' * 40}
        self.manifest = {'files': {'docs/article.md': {
            'original_sha256': hashlib.sha256(b'Original article.\n').hexdigest(),
            'candidate_sha256': hashlib.sha256(b'Improved sourced procedure.\n').hexdigest()}}}
        self.plan = prepare(self.reader, self.task, self.manifest, 'main')

    def test_admitted_uppercase_base_sha_is_supported(self):
        plan = prepare(self.reader, dict(self.task, base_sha='A' * 40), self.manifest, 'main')
        self.assertEqual('a' * 40, plan['base_sha'])

    def test_existing_pr_rename_blocks_duplicate_delivery(self):
        original = self.reader.get
        def get(path):
            if path.startswith('/pulls?'):
                return [{'number': 12}]
            if path.startswith('/pulls/12/files?'):
                return [{'filename': 'renamed.md', 'previous_filename': 'docs/article.md'}]
            return original(path)
        with patch.object(self.reader, 'get', side_effect=get):
            with self.assertRaisesRegex(DeliveryError, 'Open PRs'):
                prepare(self.reader, self.task, self.manifest, 'main')

    def test_exact_commit_verified_even_before_merge(self):
        result = verify(self.reader, self.task, self.manifest, self.plan, 'b' * 40)
        self.assertEqual(['docs/article.md'], result['verified_files'])
        self.assertEqual('unverified', result['publication'])
        self.assertEqual('unverified', result['merge'])

    def test_stale_article_or_fabricated_original_cannot_prepare(self):
        self.reader.trees['1' * 40]['docs/article.md'] = self.reader.entry(b'Actual remote original.\n')
        with self.assertRaisesRegex(DeliveryError, 'Original packet differs'):
            prepare(self.reader, self.task, self.manifest, 'main')
        self.reader.current = 'b' * 40
        with self.assertRaisesRegex(DeliveryError, 'Article changed'):
            prepare(self.reader, self.task, self.manifest, 'main')

    def test_unrelated_head_change_preserved(self):
        tree = copy.deepcopy(self.reader.trees['1' * 40])
        tree['other.md'] = self.reader.entry(b'Someone else changed another article.\n')
        self.reader.trees['3' * 40] = tree
        self.reader.commits['c' * 40] = {'sha': 'c' * 40, 'tree': {'sha': '3' * 40}, 'parents': [{'sha': 'a' * 40}]}
        self.reader.current = 'c' * 40
        plan = prepare(self.reader, self.task, self.manifest, 'main')
        self.assertEqual('c' * 40, plan['base_sha'])
        self.assertEqual('3' * 40, plan['base_tree_sha'])

    def test_unexpected_file_addition_deletion_or_mode_change_rejected(self):
        for change in ('extra', 'delete', 'mode'):
            with self.subTest(change=change):
                reader = FixtureReader()
                if change == 'extra': reader.trees['2' * 40]['other.md'] = reader.entry(b'extra')
                if change == 'delete': del reader.trees['2' * 40]['docs/article.md']
                if change == 'mode': reader.trees['2' * 40]['docs/article.md']['mode'] = '100755'
                with self.assertRaises(DeliveryError):
                    verify(reader, self.task, self.manifest, self.plan, 'b' * 40)

    def test_wrong_body_wrong_parent_or_merge_commit_rejected(self):
        for change in ('body', 'parent', 'merge'):
            with self.subTest(change=change):
                reader = FixtureReader()
                if change == 'body': reader.trees['2' * 40]['docs/article.md'] = reader.entry(b'Not approved.\n')
                if change == 'parent': reader.commits['b' * 40]['parents'] = [{'sha': 'c' * 40}]
                if change == 'merge': reader.commits['b' * 40]['parents'].append({'sha': 'c' * 40})
                with self.assertRaises(DeliveryError):
                    verify(reader, self.task, self.manifest, self.plan, 'b' * 40)

    def test_blob_integrity_and_truncated_tree_rejected(self):
        self.reader.objects[next(iter(self.reader.objects))]['content'] = base64.b64encode(b'wrong').decode()
        with self.assertRaisesRegex(DeliveryError, 'integrity'):
            prepare(self.reader, self.task, self.manifest, 'main')
        with patch.object(self.reader, 'get', return_value={'sha': '1' * 40, 'truncated': True}):
            with self.assertRaisesRegex(DeliveryError, 'Incomplete'):
                self.reader.tree('1' * 40)

    def test_http_429_is_one_request_and_does_not_expose_token(self):
        error = urllib.error.HTTPError('https://api.github.com', 429, 'limited', {'Retry-After': '60'}, None)
        with patch('urllib.request.urlopen', side_effect=error) as request, patch.dict('os.environ', {'GH_TOKEN': 'secret'}):
            with self.assertRaisesRegex(DeliveryError, 'HTTP 429; delivery pending; Retry-After=60') as caught:
                GitHubReader('owner/repo').head('main')
            self.assertNotIn('secret', str(caught.exception))
            request.assert_called_once()
