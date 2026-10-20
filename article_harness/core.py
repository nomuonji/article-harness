from __future__ import annotations

import concurrent.futures as cf
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import threading
import time

from .runner import AgentError, CodexRunner


class HarnessError(Exception):
    pass


def git(repo, *args, timeout=60):
    p = subprocess.run(['git', '-C', str(repo), *args], capture_output=True,
                       timeout=timeout)
    if p.returncode:
        raise HarnessError(p.stderr.decode(errors='replace').strip())
    return p.stdout.decode('utf-8')


def relative_file(path):
    p = PurePosixPath(path)
    if not isinstance(path, str) or not path or p.is_absolute() or '..' in p.parts or '\\' in path or str(p) != path:
        raise HarnessError(f'Invalid relative file: {path!r}')
    if any(part.startswith('.git') for part in p.parts):
        raise HarnessError('Git metadata cannot be an article target')
    return path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


@contextlib.contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise HarnessError(f'Already locked: {path}') from e
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def validate_task(task):
    task = dict(task)
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', task.get('id', '')):
        raise HarnessError('Task id must be a short lowercase slug')
    repo = Path(task['repo']).expanduser().resolve()
    if Path(git(repo, 'rev-parse', '--show-toplevel').strip()).resolve() != repo:
        raise HarnessError('repo must be a Git repository root')
    task['repo'] = str(repo)
    for key in ('brief', 'reader_job'):
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise HarnessError(f'{key} must be nonempty')
    for key in ('files', 'acceptance_criteria', 'sources'):
        if not isinstance(task.get(key), list) or not task[key] or any(not isinstance(x, str) or not x.strip() for x in task[key]):
            raise HarnessError(f'{key} must contain nonempty strings')
        if len(task[key]) != len(set(task[key])):
            raise HarnessError(f'Duplicate {key}')
    for name in task['files'] + task.get('policy_files', []):
        relative_file(name)
        if not (repo / name).is_file() or (repo / name).is_symlink():
            raise HarnessError(f'Expected existing regular source: {name}')
        if (repo / name).resolve() != repo / name:
            raise HarnessError(f'Symlink component in source: {name}')
        git(repo, 'ls-files', '--error-unmatch', '--', name)
    coordination = task.get('coordination', {})
    if coordination.get('mode') not in ('unmanaged', 'external-reservation'):
        raise HarnessError('Explicit coordination mode required: unmanaged or external-reservation')
    if coordination['mode'] == 'external-reservation':
        command = coordination.get('command')
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise HarnessError('External reservation needs a command array, without shell interpolation')
    return task


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT \'\', updated REAL NOT NULL)')

    def connect(self):
        db = sqlite3.connect(self.root / 'state.sqlite3', timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def add(self, task):
        task = validate_task(task)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT spec FROM tasks WHERE id=?', (task['id'],)).fetchone()
            encoded = json.dumps(task, sort_keys=True, ensure_ascii=False)
            if existing:
                if existing['spec'] != encoded:
                    raise HarnessError('Existing id has different immutable task contents')
                return False
            # Pending patches also reserve overlapping files inside this queue.
            for row in db.execute("SELECT spec FROM tasks WHERE status != 'applied'"):
                other = json.loads(row['spec'])
                if other['repo'] == task['repo'] and set(other['files']) & set(task['files']):
                    raise HarnessError(f"Overlapping task: {other['id']}")
            db.execute('INSERT INTO tasks(id,spec,status,updated) VALUES(?,?,?,?)',
                       (task['id'], encoded, 'queued', time.time()))
        return True

    def rows(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM tasks ORDER BY updated,id')]

    def update(self, task_id, status, detail='', attempts=None):
        with self.connect() as db:
            db.execute('UPDATE tasks SET status=?, detail=?, updated=? WHERE id=?',
                       (status, detail, time.time(), task_id))
            if attempts is not None:
                db.execute('UPDATE tasks SET attempts=? WHERE id=?', (attempts, task_id))

    def recover(self):
        # No automatic replay after a killed process: re-inspect its private worktree first.
        with self.connect() as db:
            db.execute("UPDATE tasks SET status='interrupted', detail='Controller stopped; run resume after checking worktree and reservation', updated=? WHERE status IN ('preparing','working','reviewing','checking')", (time.time(),))


class Reservation:
    def __init__(self, task, timeout, checkpoint=None):
        self.task, self.timeout, self.receipt = task, timeout, None
        self.checkpoint = checkpoint
        if checkpoint and checkpoint.exists():
            saved = json.loads(checkpoint.read_text())
            if saved['task_id'] != task['id'] or saved['repo'] != task['repo']:
                raise HarnessError('Reservation checkpoint belongs to another task')
            if saved['active']:
                self.receipt = saved['receipt']

    def call(self, operation):
        cfg = self.task['coordination']
        if cfg['mode'] == 'unmanaged':
            return
        if operation == 'claim' and self.receipt:
            operation = 'renew'  # Reconcile an existing durable lease, never claim twice.
        payload = {'operation': operation, 'task_id': self.task['id'], 'repo': self.task['repo'],
                   'files': self.task['files'], 'reservation': self.receipt,
                   'required_seconds': self.timeout + 90}
        p = subprocess.run(cfg['command'], input=json.dumps(payload), text=True,
                           capture_output=True, timeout=30)
        if p.returncode:
            raise HarnessError(f'Reservation {operation} failed (exit {p.returncode})')
        try:
            receipt = json.loads(p.stdout)
            if receipt.get('granted') is not True or not receipt.get('reservation_id'):
                raise ValueError('not granted')
            # Keep the identifier even if TTL validation fails: the remote lease
            # may already exist and must still be released/reconciled.
            self.receipt = receipt
            if self.checkpoint:
                atomic_json(self.checkpoint, {'task_id': self.task['id'], 'repo': self.task['repo'],
                                             'receipt': receipt, 'active': operation != 'release'})
            if operation != 'release':
                expires = dt.datetime.fromisoformat(receipt['expires_at'].replace('Z', '+00:00'))
                if expires.tzinfo is None:
                    raise ValueError('timezone required')
                if expires.timestamp() < time.time() + self.timeout + 60:
                    raise ValueError('lease shorter than agent timeout')
        except (KeyError, TypeError, ValueError) as e:
            raise HarnessError(f'Invalid reservation {operation} receipt') from e
        self.receipt = receipt


class Controller:
    def __init__(self, store, runner=None, workers=3, max_jobs=20, max_seconds=7200,
                 agent_timeout=900, revisions=2, replenish=None):
        self.store = store
        self.runner = runner or CodexRunner()
        self.workers, self.max_jobs, self.max_seconds = workers, max_jobs, max_seconds
        self.agent_timeout, self.revisions = agent_timeout, revisions
        self.replenish = replenish
        if not 1 <= workers <= 6 or min(max_jobs, max_seconds, agent_timeout) <= 0 or revisions < 0:
            raise HarnessError('Invalid positive run limits (workers 1..6)')
        self.stop = threading.Event()
        self.deadline = float('inf')

    def task_dir(self, task):
        return self.store.root / 'tasks' / task['id']

    def timeout(self):
        remaining = self.deadline - time.monotonic()
        if self.stop.is_set() or remaining <= 0:
            raise AgentError('interrupted', 'Run stopped or time budget reached')
        return min(self.agent_timeout, remaining)

    def snapshot(self, task, worktree, base):
        if git(worktree, 'rev-parse', 'HEAD').strip() != base:
            raise HarnessError('Agent committed or changed HEAD')
        # Ignore no files: untracked/ignored agent output is an unexpected mutation too.
        untracked = git(worktree, 'ls-files', '--others', '-z').split('\0')
        if any(untracked):
            raise HarnessError(f'Unexpected untracked output: {[x for x in untracked if x]}')
        changed = [x for x in git(worktree, 'diff', '--name-only', '-z', base).split('\0') if x]
        outside = set(changed) - set(task['files'])
        if outside:
            raise HarnessError(f'Out-of-scope changes: {sorted(outside)}')
        for path in changed:
            p = worktree / path
            if not p.is_file() or p.is_symlink() or p.resolve() != worktree / path:
                raise HarnessError(f'Deletion or symlink rejected: {path}')
            p.read_text(encoding='utf-8')
        for item in git(worktree, 'diff', '--numstat', base).splitlines():
            if item.startswith('-\t-\t'):
                raise HarnessError('Binary article change rejected')
        git(worktree, 'diff', '--check', base)
        patch = git(worktree, 'diff', '--binary', base, '--')
        return changed, patch

    def execute(self, row):
        try:
            return self._execute(row)
        except Exception as error:
            # Bad preparation/checkpoint for one article must not kill the pool.
            self.store.update(row['id'], 'failed', f'{type(error).__name__}: {error}')

    def _execute(self, row):
        task = json.loads(row['spec'])
        folder = self.task_dir(task)
        folder.mkdir(parents=True, exist_ok=True)
        worktree = folder / 'worktree'
        reservation = Reservation(task, self.agent_timeout, folder / 'reservation.json')
        repo_key = hashlib.sha256(git(task['repo'], 'rev-parse', '--path-format=absolute', '--git-common-dir').strip().encode()).hexdigest()
        # All controllers on this host share locks, including different state directories.
        lock_root = Path(os.environ.get('ARTICLE_HARNESS_LOCK_DIR', str(Path.home() / '.cache' / 'article-harness-locks')))
        try:
            with file_lock(lock_root / (repo_key + '.lock')):
                self.store.update(task['id'], 'preparing')
                reservation.call('claim')
                meta_file = folder / 'workspace.json'
                if meta_file.exists():
                    meta = json.loads(meta_file.read_text())
                    base = meta['base_sha']
                    if not worktree.exists():
                        raise HarnessError('Saved worktree missing; manual reconciliation required')
                    if meta['task'] != task:
                        raise HarnessError('Task changed since initial preparation')
                else:
                    if worktree.exists():
                        raise HarnessError('Uncheckpointed worktree exists; inspect before resuming')
                    if git(task['repo'], 'status', '--porcelain'):
                        raise HarnessError('Source repository has uncommitted changes')
                    base = git(task['repo'], 'rev-parse', 'HEAD').strip()
                    if task.get('planned_base_sha', base) != base:
                        raise HarnessError('Source changed since planning; concrete defect needs fresh diagnosis')
                    git(task['repo'], 'worktree', 'add', '--detach', str(worktree), base)
                    atomic_json(meta_file, {'base_sha': base, 'task': task})
                self.snapshot(task, worktree, base)
                feedback_file = folder / 'feedback.json'
                feedback = json.loads(feedback_file.read_text()) if feedback_file.exists() else []
                attempts = row['attempts']
                while attempts < self.revisions + 1:
                    self.timeout()
                    attempts += 1
                    self.store.update(task['id'], 'working', attempts=attempts)
                    reservation.call('renew')
                    output = self.runner.run('worker', task, worktree, folder / f'worker-{attempts}', feedback, self.timeout())
                    from .runner import validate_result
                    validate_result('worker', output, task)
                    if output['status'] != 'done':
                        self.store.update(task['id'], 'refused' if output['status'] == 'refused' else 'blocked', output['summary'])
                        return
                    changed, patch = self.snapshot(task, worktree, base)
                    if not changed:
                        raise HarnessError('Worker reported done with no material diff')
                    self.store.update(task['id'], 'reviewing')
                    reservation.call('renew')
                    before = hashlib.sha256(patch.encode()).hexdigest()
                    review_task = dict(task, base_sha=base, worker_evidence=output['evidence'])
                    review = self.runner.run('reviewer', review_task, worktree, folder / f'review-{attempts}', [], self.timeout())
                    validate_result('reviewer', review, review_task)
                    _, after_patch = self.snapshot(task, worktree, base)
                    if hashlib.sha256(after_patch.encode()).hexdigest() != before:
                        raise HarnessError('Reviewer modified the artifact')
                    if review['verdict'] == 'blocked':
                        self.store.update(task['id'], 'blocked', review['summary'])
                        return
                    if review['verdict'] == 'pass':
                        # An actual reviewer report must cover every acceptance item.
                        self.store.update(task['id'], 'checking')
                        # A later source change invalidates applicability, never auto-rebase.
                        if git(task['repo'], 'rev-parse', 'HEAD').strip() != base:
                            raise HarnessError('Source HEAD changed; patch needs fresh review against new base')
                        patch_path = folder / 'change.patch'
                        patch_path.write_text(after_patch, encoding='utf-8')
                        git(task['repo'], 'apply', '--check', str(patch_path))
                        atomic_json(folder / 'receipt.json', {
                            'task_id': task['id'], 'base_sha': base, 'repo': task['repo'],
                            'changed_files': changed, 'patch_sha256': before, 'review': review,
                            'reviewed_sha256': {name: hashlib.sha256((worktree / name).read_bytes()).hexdigest() for name in changed},
                            'worker_evidence': output['evidence'], 'reservation': reservation.receipt,
                            'source_checks': ['allowlist', 'unchanged HEAD', 'regular UTF-8 files', 'git diff --check', 'git apply --check'],
                            'build': 'not_run', 'publication': 'not_run', 'status': 'ready_for_handoff'})
                        self.store.update(task['id'], 'ready_for_handoff', str(patch_path))
                        return
                    feedback.append(review)
                    atomic_json(feedback_file, feedback)
                self.store.update(task['id'], 'blocked', 'Editorial revision limit reached; see review reports')
        except AgentError as e:
            status = {'refused': 'refused', 'interrupted': 'interrupted', 'timeout': 'interrupted'}.get(e.kind, 'failed')
            self.store.update(task['id'], status, str(e))
        except Exception as e:
            self.store.update(task['id'], 'failed', f'{type(e).__name__}: {e}')
        finally:
            if reservation.receipt:
                try:
                    reservation.call('release')
                except Exception as e:
                    atomic_json(folder / 'reservation-release-error.json', {'error': str(e), 'reservation': reservation.receipt})
                    # Delivery is blocked until external reservation state is reconciled.
                    self.store.update(task['id'], 'blocked', 'External reservation release failed; inspect receipt')

    def run(self, watch=False):
        self.deadline = time.monotonic() + self.max_seconds
        count, running = 0, {}
        with file_lock(self.store.root / 'controller.lock'):
            self.store.recover()
            with cf.ThreadPoolExecutor(max_workers=self.workers) as pool:
                try:
                    while not self.stop.is_set() and time.monotonic() < self.deadline:
                        if (self.store.root / 'STOP').exists():
                            break
                        for future in list(running):
                            if future.done():
                                future.result()
                                del running[future]
                        busy = set(running.values())
                        for row in self.store.rows():
                            if len(running) >= self.workers or count >= self.max_jobs:
                                break
                            if row['status'] != 'queued':
                                continue
                            task = json.loads(row['spec'])
                            if task['repo'] in busy:
                                continue
                            # Change state before submission so queue scan cannot re-dispatch.
                            self.store.update(row['id'], 'preparing')
                            running[pool.submit(self.execute, row)] = task['repo']
                            busy.add(task['repo'])
                            count += 1
                        if (self.store.root / 'STOP').exists():
                            break
                        if not running:
                            if count < self.max_jobs and self.replenish:
                                if self.replenish(min(self.agent_timeout, max(0.1, self.deadline - time.monotonic()))):
                                    continue
                            if not watch or count >= self.max_jobs:
                                break
                        time.sleep(0.2 if running else 2)
                except KeyboardInterrupt:
                    self.stop.set()
                finally:
                    # Agents have their own timeout and are drained; never leave orphan writers.
                    self.stop.set()
                    for future in running:
                        future.result()
        return self.store.rows()
