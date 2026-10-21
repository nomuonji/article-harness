"""Read-only GitHub delivery verification; writes remain with the authorized parent."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


class DeliveryError(Exception):
    pass


class GitHubReader:
    """Official REST GETs only. No retries, ref updates, or Operator calls."""

    def __init__(self, repository):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise DeliveryError('Invalid repository')
        self.repository = repository
        self.cache = {}

    def get(self, path):
        # Cache immutable Git objects, never refs.
        if path in self.cache:
            return self.cache[path]
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'article-harness',
                   'X-GitHub-Api-Version': '2022-11-28'}
        token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
        if token:
            headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request('https://api.github.com/repos/' + self.repository + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            wait = error.headers.get('Retry-After')
            raise DeliveryError(f'GitHub HTTP {error.code}; delivery pending' + (f'; Retry-After={wait}' if wait else '')) from None
        except (OSError, ValueError) as error:
            raise DeliveryError('GitHub read unavailable; delivery pending') from error
        if path.startswith(('/git/commits/', '/git/trees/', '/git/blobs/')):
            self.cache[path] = result
        return result

    def head(self, branch):
        return self.get('/git/ref/heads/' + urllib.parse.quote(branch, safe=''))['object']['sha']

    def commit(self, sha):
        sha = sha.lower()
        if not re.fullmatch(r'[a-f0-9]{40}', sha):
            raise DeliveryError('Exact commit SHA required')
        result = self.get('/git/commits/' + sha)
        if result.get('sha') != sha:
            raise DeliveryError('GitHub returned a different commit')
        return result

    def tree(self, sha):
        result = self.get('/git/trees/' + sha + '?recursive=1')
        if result.get('truncated') or result.get('sha') != sha:
            raise DeliveryError('Incomplete GitHub tree; delivery pending')
        return {x['path']: {'sha': x['sha'], 'mode': x['mode'], 'type': x['type']}
                for x in result['tree'] if x['type'] != 'tree'}

    def blob(self, entry):
        if entry['type'] != 'blob' or entry['mode'] not in ('100644', '100755'):
            raise DeliveryError('Article must be a regular Git blob')
        result = self.get('/git/blobs/' + entry['sha'])
        if result.get('encoding') != 'base64':
            raise DeliveryError('Unsupported blob encoding')
        data = base64.b64decode(result['content'])
        actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if actual != entry['sha']:
            raise DeliveryError('Git blob integrity mismatch')
        return data

    def open_conflicts(self, branch, files):
        conflicts = []
        for page in range(1, 11):
            query = urllib.parse.urlencode({'state': 'open', 'base': branch, 'per_page': 100, 'page': page})
            pulls = self.get('/pulls?' + query)
            for pull in pulls:
                for part in range(1, 11):
                    changed = self.get(f"/pulls/{pull['number']}/files?per_page=100&page={part}")
                    names = {p for x in changed for p in (x['filename'], x.get('previous_filename')) if p}
                    if names & set(files):
                        conflicts.append(pull['number'])
                        break
                    if len(changed) < 100:
                        break
                else:
                    raise DeliveryError('PR file listing incomplete; delivery pending')
            if len(pulls) < 100:
                return conflicts
        raise DeliveryError('PR listing incomplete; delivery pending')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def prepare(reader, task, manifest, branch):
    """Accept unrelated HEAD changes only when all scoped originals are identical."""
    conflicts = reader.open_conflicts(branch, manifest['files'])
    if conflicts:
        raise DeliveryError(f'Open PRs already touch these articles: {conflicts}; reconcile before delivery')
    head = reader.head(branch)
    commit = reader.commit(head)
    tree = reader.tree(commit['tree']['sha'])
    original_commit = reader.commit(task['base_sha'])
    originals = reader.tree(original_commit['tree']['sha'])
    for path, hashes in manifest['files'].items():
        before, current = originals.get(path), tree.get(path)
        if before is None or current != before:
            raise DeliveryError(f'Article changed since review: {path}; re-diagnose and review before delivery')
        if _sha(reader.blob(current)) != hashes['original_sha256']:
            raise DeliveryError(f'Original packet differs from GitHub: {path}')
    return {'repository': task['repository'], 'task_id': task['id'], 'branch': branch,
            'base_sha': head, 'base_tree_sha': commit['tree']['sha'], 'files': manifest['files']}


def verify(reader, task, manifest, plan, commit_sha):
    if plan['repository'] != task['repository'] or plan['task_id'] != task['id'] or plan['files'] != manifest['files']:
        raise DeliveryError('Delivery plan does not match reviewed task')
    commit = reader.commit(commit_sha)
    if [x['sha'] for x in commit['parents']] != [plan['base_sha']]:
        raise DeliveryError('Delivered commit must have the prepared HEAD as its sole parent')
    parent = reader.commit(plan['base_sha'])
    if parent['tree']['sha'] != plan['base_tree_sha']:
        raise DeliveryError('Delivery base tree mismatch')
    before, after = reader.tree(plan['base_tree_sha']), reader.tree(commit['tree']['sha'])
    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    expected = {p for p, h in manifest['files'].items() if h['candidate_sha256'] != h['original_sha256']}
    if changed != expected:
        raise DeliveryError('Delivered commit changed unexpected files or missed approved changes')
    for path, hashes in manifest['files'].items():
        if path not in before or path not in after or before[path]['mode'] != after[path]['mode']:
            raise DeliveryError('Delivered file mode or presence changed')
        if _sha(reader.blob(before[path])) != hashes['original_sha256'] or _sha(reader.blob(after[path])) != hashes['candidate_sha256']:
            raise DeliveryError(f'Delivered body differs from approved snapshot: {path}')
    return {'commit_sha': commit_sha, 'base_sha': plan['base_sha'], 'tree_sha': commit['tree']['sha'],
            'verified_files': sorted(manifest['files']), 'verification': 'github-git-objects',
            'publication': 'unverified', 'merge': 'unverified'}
