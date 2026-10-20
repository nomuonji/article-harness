"""State and artifact helper for native ChatGPT Work/Codex subagents.

This module never starts a model or performs a GitHub/MCP write. The parent
agent owns native delegation and must acquire any real external shared claim.
Locks here coordinate only callers using the same SQLite state directory.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import difflib
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import sys
import tempfile
import time
import uuid
import zipfile


class NativeError(Exception):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _path(name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise NativeError('Expected a relative POSIX path')
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or str(p) != name or any(x.startswith('.git') for x in p.parts):
        raise NativeError(f'Unsafe artifact path: {name}')
    return name


def _strings(items, name):
    if not isinstance(items, list) or not items or any(not isinstance(x, str) or not x.strip() for x in items) or len(set(items)) != len(items):
        raise NativeError(f'{name} must be a unique nonempty string list')
    return items


def _bytes(root, name):
    root = Path(root).resolve()
    p = root / _path(name)
    if not p.is_file() or p.is_symlink() or p.resolve() != p:
        raise NativeError(f'Missing, nonregular or symlink artifact: {name}')
    data = p.read_bytes()
    data.decode('utf-8')
    return data


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_bytes(data)
    temp.replace(path)


def _stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _expires(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise NativeError('Shared claim expiry must contain a timezone')
    return parsed.timestamp()


class NativeStore:
    """Transactional session queue. Tokens are capabilities; keep them private."""

    def __init__(self, root, max_parallel=3):
        if type(max_parallel) is not int or not 1 <= max_parallel <= 6:
            raise NativeError('max_parallel must be 1..6')
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / 'native.sqlite3'
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, max_parallel INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO settings VALUES (1, ?)', (max_parallel,))
            db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL, actor TEXT, worker TEXT, token TEXT, expires REAL, attempt INTEGER NOT NULL DEFAULT 0, digest TEXT, feedback TEXT NOT NULL DEFAULT \'[]\', detail TEXT NOT NULL DEFAULT \'\', shared_receipt TEXT, updated REAL NOT NULL)')

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _tx(self):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE tasks SET status='interrupted', token=NULL, detail='Lease expired; parent must verify checkpoint before resume', updated=? WHERE status IN ('working','reviewing') AND expires<=?", (time.time(), time.time()))
            yield db

    def _row(self, db, task_id):
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise NativeError('Unknown task id')
        return dict(row)

    def _task_dir(self, task_id):
        return self.root / 'tasks' / task_id

    def _shared(self, task, receipt, seconds=0):
        if task['coordination']['mode'] == 'unmanaged':
            return
        if not isinstance(receipt, dict) or receipt.get('taskId') != task['id'] or receipt.get('repository') != task['repository']:
            raise NativeError('Shared claim must match this task and repository')
        if not isinstance(receipt.get('runId'), str) or not receipt['runId'].strip() or not isinstance(receipt.get('files'), list) or set(receipt['files']) != set(task['files']):
            raise NativeError('Shared claim needs actual runId and exact file scope')
        try:
            expiry = _expires(receipt['expiresAt'])
        except (KeyError, TypeError, ValueError) as error:
            raise NativeError('Shared claim expiry is invalid') from error
        if expiry <= time.time() + seconds:
            raise NativeError('Shared claim expires before the requested execution lease')

    def add(self, task, original_dir):
        task = json.loads(_json(task))
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', task.get('id', '')):
            raise NativeError('Task id must be a lowercase slug, at most 64 characters')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', task.get('repository', '')) or not re.fullmatch(r'[a-fA-F0-9]{40}', task.get('base_sha', '')):
            raise NativeError('repository and exact 40-character base_sha are required')
        for key in ('brief', 'reader_job'):
            if not isinstance(task.get(key), str) or not task[key].strip():
                raise NativeError(f'{key} is required')
        for key in ('files', 'acceptance_criteria', 'sources'):
            _strings(task.get(key), key)
        for name in task['files']:
            _path(name)
        maximum = task.setdefault('max_revisions', 2)
        if type(maximum) is not int or not 0 <= maximum <= 10:
            raise NativeError('max_revisions must be 0..10')
        mode = task.get('coordination', {}).get('mode')
        if mode not in ('unmanaged', 'shared-claim'):
            raise NativeError('Explicit unmanaged or shared-claim coordination required')
        receipt = task['coordination'].get('receipt')
        self._shared(task, receipt)
        original = {name: _bytes(original_dir, name) for name in task['files']}
        task['original_sha256'] = {name: _hash(data) for name, data in original.items()}
        encoded = _json(task)
        with self._tx() as db:
            found = db.execute('SELECT spec FROM tasks WHERE id=?', (task['id'],)).fetchone()
            if found:
                if found['spec'] != encoded:
                    raise NativeError('Task id already exists with different immutable scope or originals')
                return self._public(self._row(db, task['id']))
            for row in db.execute("SELECT spec,status FROM tasks WHERE status!='handed_off'"):
                other = json.loads(row['spec'])
                if other['repository'].lower() == task['repository'].lower() and set(other['files']) & set(task['files']):
                    raise NativeError(f"Overlapping task already reserved: {other['id']} ({row['status']})")
            folder = self._task_dir(task['id'])
            if folder.exists():
                raise NativeError('Uncheckpointed task folder exists; reconcile before adding')
            for name, data in original.items():
                _write(folder / 'original' / name, data)
            _write(folder / 'task.json', encoded.encode())
            db.execute('INSERT INTO tasks(id,spec,status,shared_receipt,updated) VALUES (?,?,?,?,?)',
                       (task['id'], encoded, 'queued', _json(receipt) if receipt else None, time.time()))
            return self._public(self._row(db, task['id']))

    def _public(self, row):
        result = dict(row)
        result.pop('token', None)
        result['task'] = json.loads(result.pop('spec'))
        result['feedback'] = json.loads(result['feedback'])
        if result['shared_receipt']:
            result['shared_receipt'] = json.loads(result['shared_receipt'])
        return result

    def list(self):
        with self._tx() as db:
            return [self._public(dict(row)) for row in db.execute('SELECT * FROM tasks ORDER BY updated,id')]

    def _ttl(self, ttl):
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or not 1 <= ttl <= 7200:
            raise NativeError('ttl must be 1..7200 seconds')

    def _lease(self, db, row, actor, ttl, status):
        self._ttl(ttl)
        if not isinstance(actor, str) or not actor.strip():
            raise NativeError('Actual native agent identity is required')
        slots = db.execute('SELECT max_parallel FROM settings WHERE id=1').fetchone()[0]
        if db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('working','reviewing')").fetchone()[0] >= slots:
            raise NativeError('All session agent slots are occupied')
        task = json.loads(row['spec'])
        self._shared(task, json.loads(row['shared_receipt']) if row['shared_receipt'] else None, ttl)
        token = secrets.token_urlsafe(32)
        expiry = time.time() + ttl
        db.execute('UPDATE tasks SET status=?,actor=?,token=?,expires=?,updated=? WHERE id=?', (status, actor, token, expiry, time.time(), row['id']))
        result = self._public(self._row(db, row['id']))
        result.update(token=token, expires_at=expiry)
        return result

    def claim(self, task_id, actor, ttl=900):
        with self._tx() as db:
            row = self._row(db, task_id)
            if row['status'] != 'queued':
                raise NativeError('Only queued tasks may be claimed')
            task = json.loads(row['spec'])
            for other in db.execute("SELECT id,spec,status FROM tasks WHERE id!=? AND status IN ('working','awaiting_review','reviewing','ready_for_handoff','interrupted')", (task_id,)):
                if json.loads(other['spec'])['repository'].lower() == task['repository'].lower():
                    raise NativeError(f"Repository is owned by {other['id']} ({other['status']})")
            if row['attempt'] >= task['max_revisions'] + 1:
                raise NativeError('Attempt budget is exhausted')
            result = self._lease(db, row, actor, ttl, 'working')
            db.execute('UPDATE tasks SET worker=?,attempt=attempt+1 WHERE id=?', (actor, task_id))
            result['attempt'] = row['attempt'] + 1
            result['worker'] = actor
            return result

    def _owned(self, db, task_id, token, status=None):
        row = self._row(db, task_id)
        if not isinstance(token, str) or not row['token'] or not secrets.compare_digest(row['token'], token) or row['expires'] <= time.time():
            raise NativeError('Missing, expired or foreign execution lease')
        if status and row['status'] != status:
            raise NativeError(f'Task is not {status}')
        return row

    def heartbeat(self, task_id, token, ttl=900):
        self._ttl(ttl)
        with self._tx() as db:
            row = self._owned(db, task_id, token)
            if row['status'] not in ('working', 'reviewing'):
                raise NativeError('Only active execution leases may heartbeat')
            self._shared(json.loads(row['spec']), json.loads(row['shared_receipt']) if row['shared_receipt'] else None, ttl)
            db.execute('UPDATE tasks SET expires=?,updated=? WHERE id=?', (time.time() + ttl, time.time(), task_id))
            return {'task_id': task_id, 'expires_at': time.time() + ttl}

    def renew_shared_claim(self, task_id, receipt):
        with self._tx() as db:
            row = self._row(db, task_id)
            task = json.loads(row['spec'])
            if task['coordination']['mode'] != 'shared-claim':
                raise NativeError('Task does not use external shared coordination')
            self._shared(task, receipt)
            previous = json.loads(row['shared_receipt'])
            if receipt['runId'] != previous['runId']:
                raise NativeError('Cannot replace an unresolved shared claim with another run')
            db.execute('UPDATE tasks SET shared_receipt=?,updated=? WHERE id=?', (_json(receipt), time.time(), task_id))
            return {'task_id': task_id, 'shared_receipt': receipt}

    def submit(self, task_id, token, candidate_dir):
        with self._tx() as db:
            row = self._owned(db, task_id, token, 'working')
            task = json.loads(row['spec'])
            self._shared(task, json.loads(row['shared_receipt']) if row['shared_receipt'] else None)
            root = Path(candidate_dir).resolve()
            if not root.is_dir():
                raise NativeError('candidate_dir must exist')
            entries = list(root.rglob('*'))
            if any(p.is_symlink() for p in entries):
                raise NativeError('Candidate packet cannot contain symlinks')
            actual = {str(p.relative_to(root)) for p in entries if p.is_file()}
            if actual != set(task['files']):
                raise NativeError('Candidate packet must contain exactly the allowed files')
            original = self._task_dir(task_id) / 'original'
            version = self._task_dir(task_id) / 'versions' / str(row['attempt'])
            if version.exists():
                raise NativeError('Attempt snapshot already exists; reconcile before retry')
            records, patches = {}, []
            candidate = {name: _bytes(root, name) for name in task['files']}
            for name, data in candidate.items():
                old = _bytes(original, name)
                if _hash(old) != task['original_sha256'][name]:
                    raise NativeError('Original packet was modified')
                records[name] = {'original_sha256': _hash(old), 'candidate_sha256': _hash(data)}
                patches.extend(difflib.unified_diff(old.decode().splitlines(keepends=True), data.decode().splitlines(keepends=True), fromfile='a/' + name, tofile='b/' + name))
            if all(value['original_sha256'] == value['candidate_sha256'] for value in records.values()):
                raise NativeError('No material artifact change')
            diff_bytes = ''.join(patches).encode()
            manifest = {'task_id': task_id, 'repository': task['repository'], 'base_sha': task['base_sha'], 'attempt': row['attempt'], 'worker': row['worker'], 'files': records, 'diff_sha256': _hash(diff_bytes), 'acceptance_criteria': task['acceptance_criteria']}
            digest = _hash(_json(manifest).encode())
            for name, data in candidate.items():
                _write(version / 'candidate' / name, data)
            _write(version / 'manifest.json', _json(manifest).encode())
            _write(version / 'change.diff', diff_bytes)
            db.execute("UPDATE tasks SET status='awaiting_review',token=NULL,expires=NULL,digest=?,updated=? WHERE id=?", (digest, time.time(), task_id))
            return {'task_id': task_id, 'digest': digest, 'version_dir': str(version), 'manifest': manifest}

    def _verify_snapshot(self, row):
        task = json.loads(row['spec'])
        folder = self._task_dir(row['id'])
        version = folder / 'versions' / str(row['attempt'])
        manifest = json.loads((version / 'manifest.json').read_text())
        if _hash(_json(manifest).encode()) != row['digest']:
            raise NativeError('Snapshot manifest was modified')
        if _hash(_bytes(version, 'change.diff')) != manifest['diff_sha256']:
            raise NativeError('Reviewed diff was modified')
        for name, hashes in manifest['files'].items():
            if _hash(_bytes(version / 'candidate', name)) != hashes['candidate_sha256'] or _hash(_bytes(folder / 'original', name)) != hashes['original_sha256']:
                raise NativeError('Reviewed artifact or original packet was modified')
        if manifest['acceptance_criteria'] != task['acceptance_criteria']:
            raise NativeError('Acceptance criteria changed')
        return manifest

    def begin_review(self, task_id, actor, ttl=900):
        with self._tx() as db:
            row = self._row(db, task_id)
            if row['status'] != 'awaiting_review' or actor == row['worker']:
                raise NativeError('A distinct reviewer may claim only awaiting_review work')
            self._verify_snapshot(row)
            return self._lease(db, row, actor, ttl, 'reviewing')

    def review(self, task_id, token, report):
        with self._tx() as db:
            row = self._owned(db, task_id, token, 'reviewing')
            task = json.loads(row['spec'])
            self._shared(task, json.loads(row['shared_receipt']) if row['shared_receipt'] else None)
            self._verify_snapshot(row)
            if not isinstance(report, dict) or report.get('digest') != row['digest'] or report.get('verdict') not in ('pass', 'revise', 'blocked') or not isinstance(report.get('summary'), str) or not report['summary'].strip():
                raise NativeError('Review needs exact artifact digest, verdict and summary')
            criteria, findings = report.get('criteria'), report.get('findings')
            if not isinstance(criteria, list) or not isinstance(findings, list):
                raise NativeError('Review needs criteria and findings arrays')
            names = []
            for item in criteria:
                if not isinstance(item, dict) or item.get('status') not in ('pass', 'fail', 'unknown') or not isinstance(item.get('evidence'), str) or not item['evidence'].strip():
                    raise NativeError('Every criterion needs explicit status and concrete evidence')
                names.append(item.get('criterion'))
            if len(names) != len(set(names)) or set(names) != set(task['acceptance_criteria']):
                raise NativeError('Review must cover each exact criterion once')
            if any(not isinstance(x, dict) or not isinstance(x.get('detail'), str) or not x['detail'].strip() or not isinstance(x.get('path'), str) for x in findings):
                raise NativeError('Each finding needs path and detail')
            verdict = report['verdict']
            if verdict == 'pass' and (findings or any(x['status'] != 'pass' for x in criteria)):
                raise NativeError('Unknown, failed or defective work cannot pass')
            if verdict == 'revise' and not findings:
                raise NativeError('Revision requires concrete findings')
            status = 'ready_for_handoff' if verdict == 'pass' else 'queued' if verdict == 'revise' and row['attempt'] <= task['max_revisions'] else 'blocked'
            feedback = json.loads(row['feedback']) + [dict(report, reviewer=row['actor'], checked_at=_stamp())]
            _write(self._task_dir(task_id) / 'versions' / str(row['attempt']) / 'review.json', _json(feedback[-1]).encode())
            db.execute('UPDATE tasks SET status=?,token=NULL,expires=NULL,feedback=?,detail=?,updated=? WHERE id=?', (status, _json(feedback), report['summary'], time.time(), task_id))
            return self._public(self._row(db, task_id))

    def stop_task(self, task_id, token, reason, refused=False):
        with self._tx() as db:
            self._owned(db, task_id, token)
            if not isinstance(reason, str) or not reason.strip():
                raise NativeError('Concrete stop reason is required')
            db.execute('UPDATE tasks SET status=?,token=NULL,expires=NULL,detail=?,updated=? WHERE id=?', ('refused' if refused else 'blocked', reason, time.time(), task_id))
            return self._public(self._row(db, task_id))

    def refuse(self, task_id, token, reason):
        return self.stop_task(task_id, token, reason, refused=True)

    def resume(self, task_id):
        with self._tx() as db:
            row = self._row(db, task_id)
            if row['status'] != 'interrupted':
                raise NativeError('Only interrupted work may resume; refused work is not replayed')
            # If there is a completed current candidate, resume its independent review.
            version = self._task_dir(task_id) / 'versions' / str(row['attempt'])
            status = 'awaiting_review' if row['digest'] and version.exists() else 'queued'
            if status == 'awaiting_review':
                self._verify_snapshot(row)
            elif row['attempt'] >= json.loads(row['spec'])['max_revisions'] + 1:
                raise NativeError('Attempt budget is exhausted')
            db.execute('UPDATE tasks SET status=?,actor=NULL,token=NULL,expires=NULL,detail=?,updated=? WHERE id=?', (status, 'Parent explicitly reconciled checkpoint and resumed', time.time(), task_id))
            return self._public(self._row(db, task_id))

    def handoff(self, task_id, digest, evidence):
        with self._tx() as db:
            row = self._row(db, task_id)
            if row['status'] != 'ready_for_handoff' or digest != row['digest']:
                raise NativeError('Only the exact independently reviewed artifact may be handed off')
            self._verify_snapshot(row)
            if not isinstance(evidence, dict) or not re.fullmatch(r'[a-fA-F0-9]{40}', evidence.get('commit_sha', '')) or not isinstance(evidence.get('readback'), str) or not evidence['readback'].strip():
                raise NativeError('Observed commit SHA and actual readback evidence are required')
            _write(self._task_dir(task_id) / 'handoff.json', _json(dict(evidence, digest=digest, recorded_at=_stamp(), publication='unverified')).encode())
            db.execute("UPDATE tasks SET status='handed_off',detail=?,updated=? WHERE id=?", (_json(evidence), time.time(), task_id))
            return self._public(self._row(db, task_id))

    def export(self, path=None):
        value = {'schema_version': 1, 'exported_at': _stamp(), 'scope': 'this state directory only; not a global repository lock', 'tasks': self.list()}
        if path and str(path).lower().endswith('.zip'):
            destination = Path(path).expanduser().resolve()
            if destination == self.db_path or self.root == destination.parent or self.root in destination.parents:
                raise NativeError('Export archive must be outside the state directory')
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Hold the state write transaction while backing up a separate reader
            # and copying artifact bytes, so a submitted snapshot cannot race export.
            with self._tx() as db, tempfile.TemporaryDirectory() as temporary:
                backup_path = Path(temporary) / 'native.sqlite3'
                with self._connect() as source, sqlite3.connect(backup_path) as backup:
                    source.backup(backup)
                    backup.execute("UPDATE tasks SET token=NULL, expires=NULL, status=CASE WHEN status IN ('working','reviewing') THEN 'interrupted' ELSE status END")
                with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
                    archive.write(backup_path, 'native.sqlite3')
                    archive.writestr('export.json', _json(value))
                    folder = self.root / 'tasks'
                    for item in sorted(folder.rglob('*')) if folder.exists() else []:
                        if item.is_symlink():
                            raise NativeError('Cannot export symlink artifacts')
                        if item.is_file():
                            archive.write(item, item.relative_to(self.root).as_posix())
            value = dict(value, archive=str(destination), portable=True)
        elif path:
            raise NativeError('Portable export requires a .zip path; no path prints a summary only')
        return value

    @classmethod
    def restore(cls, archive, root):
        root = Path(root).expanduser().resolve()
        if root.exists() and any(root.iterdir()):
            raise NativeError('Restore requires an empty destination directory')
        root.mkdir(parents=True, exist_ok=True)
        extracted = []
        try:
            with zipfile.ZipFile(archive) as packet:
                if sum(item.file_size for item in packet.infolist()) > 500 * 1024 * 1024:
                    raise NativeError('Restore archive is too large')
                seen = set()
                for item in packet.infolist():
                    name = item.filename
                    _path(name)
                    if name in seen or item.is_dir() or (item.external_attr >> 16) & 0o170000 == 0o120000:
                        raise NativeError('Duplicate, directory or symlink archive entry')
                    seen.add(name)
                    if name not in ('native.sqlite3', 'export.json') and not name.startswith('tasks/'):
                        raise NativeError('Unexpected archive entry')
                    destination = root / name
                    _write(destination, packet.read(item))
                    extracted.append(destination)
                if 'native.sqlite3' not in seen or 'export.json' not in seen:
                    raise NativeError('Incomplete portable state archive')
            store = cls(root)
            with store._tx() as db:
                db.execute("UPDATE tasks SET token=NULL, expires=NULL, status=CASE WHEN status IN ('working','reviewing') THEN 'interrupted' ELSE status END")
                for source in db.execute('SELECT * FROM tasks'):
                    row = dict(source)
                    task = json.loads(row['spec'])
                    if task.get('id') != row['id'] or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', row['id']):
                        raise NativeError('Invalid restored task identity')
                    for name in _strings(task.get('files'), 'files'):
                        _path(name)
                        if _hash(_bytes(store._task_dir(row['id']) / 'original', name)) != task['original_sha256'][name]:
                            raise NativeError('Restored original does not match immutable task')
                    if row['digest'] and (store._task_dir(row['id']) / 'versions' / str(row['attempt'])).exists():
                        store._verify_snapshot(row)
                    elif row['status'] in ('awaiting_review', 'reviewing', 'ready_for_handoff', 'handed_off'):
                        raise NativeError('Restored reviewed snapshot is missing')
                    versions = store._task_dir(row['id']) / 'versions'
                    for version in versions.iterdir() if versions.exists() else []:
                        if not version.is_dir() or not version.name.isdigit():
                            raise NativeError('Invalid restored version directory')
                        attempt = int(version.name)
                        if attempt == row['attempt']:
                            continue  # Already checked above.
                        if not (version / 'review.json').is_file():
                            raise NativeError('Historical snapshot has no independent review checkpoint')
                        historic = json.loads((version / 'review.json').read_text())
                        expected = next((x for x in json.loads(row['feedback']) if x.get('digest') == historic.get('digest')), None)
                        if expected is None or historic != expected:
                            raise NativeError('Historical review differs from saved feedback')
                        store._verify_snapshot(dict(row, attempt=attempt, digest=historic['digest']))
            return store
        except Exception:
            # Do not leave partial state looking usable after a failed restoration.
            for path in sorted(root.rglob('*'), reverse=True):
                if path.is_file() or path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, help='Shared state directory for this parent/subagent session')
    parser.add_argument('--max-parallel', type=int, default=3, help='Set once when creating state (1..6)')
    commands = parser.add_subparsers(dest='command', required=True)
    add = commands.add_parser('add'); add.add_argument('--task', required=True); add.add_argument('--original-dir', required=True)
    for command in ('claim', 'begin-review'):
        p = commands.add_parser(command); p.add_argument('--id', required=True); p.add_argument('--actor', required=True); p.add_argument('--ttl', type=float, default=900)
    p = commands.add_parser('heartbeat'); p.add_argument('--id', required=True); p.add_argument('--token', required=True); p.add_argument('--ttl', type=float, default=900)
    p = commands.add_parser('submit'); p.add_argument('--id', required=True); p.add_argument('--token', required=True); p.add_argument('--candidate-dir', required=True)
    p = commands.add_parser('review'); p.add_argument('--id', required=True); p.add_argument('--token', required=True); p.add_argument('--report', required=True)
    p = commands.add_parser('stop-task'); p.add_argument('--id', required=True); p.add_argument('--token', required=True); p.add_argument('--reason', required=True); p.add_argument('--refused', action='store_true')
    p = commands.add_parser('refuse'); p.add_argument('--id', required=True); p.add_argument('--token', required=True); p.add_argument('--reason', required=True)
    p = commands.add_parser('resume'); p.add_argument('--id', required=True)
    p = commands.add_parser('renew-shared-claim'); p.add_argument('--id', required=True); p.add_argument('--receipt', required=True)
    p = commands.add_parser('handoff'); p.add_argument('--id', required=True); p.add_argument('--digest', required=True); p.add_argument('--evidence', required=True)
    commands.add_parser('status')
    p = commands.add_parser('export'); p.add_argument('--output')
    p = commands.add_parser('restore'); p.add_argument('--archive', required=True)
    args = parser.parse_args(argv)
    def load(path):
        return json.loads(Path(path).read_text(encoding='utf-8'))
    try:
        cmd = args.command
        if cmd == 'restore':
            result = NativeStore.restore(args.archive, args.state).export()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        store = NativeStore(args.state, args.max_parallel)
        if cmd == 'add': result = store.add(load(args.task), args.original_dir)
        elif cmd == 'status': result = store.list()
        elif cmd == 'claim': result = store.claim(args.id, args.actor, args.ttl)
        elif cmd == 'heartbeat': result = store.heartbeat(args.id, args.token, args.ttl)
        elif cmd == 'submit': result = store.submit(args.id, args.token, args.candidate_dir)
        elif cmd == 'begin-review': result = store.begin_review(args.id, args.actor, args.ttl)
        elif cmd == 'review': result = store.review(args.id, args.token, load(args.report))
        elif cmd == 'resume': result = store.resume(args.id)
        elif cmd == 'renew-shared-claim': result = store.renew_shared_claim(args.id, load(args.receipt))
        elif cmd == 'handoff': result = store.handoff(args.id, args.digest, load(args.evidence))
        elif cmd == 'stop-task': result = store.stop_task(args.id, args.token, args.reason, args.refused)
        elif cmd == 'refuse': result = store.refuse(args.id, args.token, args.reason)
        else: result = store.export(args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (NativeError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
