"""Bounded, integrity-checked GitHub Actions state checkpoint.

This is a *single-runner* continuation archive, not a distributed lock or a
permission to retry interrupted/refused work. A caller must serialize runs.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_FILES = 20000
SCHEMA = 1


class ContinuityError(Exception):
    pass


def _safe_member(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or name.startswith('/') or '\\' in name
            or path.is_absolute() or any(part in ('', '.', '..') for part in path.parts)
            or len(path.parts) < 2 or path.parts[0] != 'state'):
        raise ContinuityError('Unsafe archive member')
    return str(PurePosixPath(*path.parts[1:]))


def backup(state: Path, archive: Path, project: str) -> dict:
    if not project.strip() or not state.is_dir():
        raise ContinuityError('Project identity and existing state directory are required')
    state = state.resolve()
    archive = archive.resolve()
    if archive.is_relative_to(state):
        raise ContinuityError('Archive must be outside state directory')
    db_path = state / 'state.sqlite3'
    if db_path.is_file():
        with sqlite3.connect(db_path, timeout=30) as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
    files: dict[str, str] = {}
    total = 0
    for base, dirs, names in os.walk(state, followlinks=False):
        for entry in list(dirs) + names:
            path = Path(base) / entry
            if path.is_symlink():
                raise ContinuityError('State contains a symlink; checkpoint refused')
        for name in names:
            path = Path(base) / name
            if not path.is_file():
                raise ContinuityError('State contains nonregular data')
            rel = path.relative_to(state).as_posix()
            size = path.stat().st_size
            total += size
            if size > MAX_FILE_BYTES or total > MAX_ARCHIVE_BYTES or len(files) >= MAX_FILES:
                raise ContinuityError('Checkpoint size/file limit exceeded')
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {'schema': SCHEMA, 'project': project, 'files': files}
    archive.parent.mkdir(parents=True, exist_ok=True)
    temp = archive.with_name(archive.name + '.tmp')
    try:
        with tarfile.open(temp, 'w:gz') as tf:
            for rel in sorted(files):
                tf.add(state / rel, arcname='state/' + rel, recursive=False)
            blob = json.dumps(manifest, sort_keys=True).encode('utf-8')
            info = tarfile.TarInfo('manifest.json')
            info.size = len(blob)
            info.mode = 0o600
            tf.addfile(info, io.BytesIO(blob))
        os.replace(temp, archive)
    finally:
        temp.unlink(missing_ok=True)
    return {'files': len(files), 'bytes': total, 'archive': str(archive)}


def restore(archive: Path, state: Path, project: str) -> dict:
    if not project.strip() or not archive.is_file():
        raise ContinuityError('Existing archive and project identity are required')
    if state.exists() and any(state.iterdir()):
        raise ContinuityError('Refusing to overwrite nonempty state')
    state = state.resolve()
    with tarfile.open(archive, 'r:gz') as tf:
        members = tf.getmembers()
        if len(members) > MAX_FILES + 1:
            raise ContinuityError('Excessive number of archive members')
        meta = [m for m in members if m.name == 'manifest.json']
        if len(meta) != 1 or not meta[0].isfile() or meta[0].size > 1048576:
            raise ContinuityError('Missing or oversized manifest')
        info_stream = tf.extractfile(meta[0])
        if info_stream is None:
            raise ContinuityError('Missing manifest content')
        manifest = json.loads(info_stream.read())
        if (manifest.get('schema') != SCHEMA or manifest.get('project') != project
                or not isinstance(manifest.get('files'), dict)):
            raise ContinuityError('Checkpoint identity/schema mismatch')
        items = {}
        total = 0
        for member in members:
            if member.name == 'manifest.json':
                continue
            name = _safe_member(member.name)
            if not member.isfile() or name in items or name not in manifest['files']:
                raise ContinuityError('Unsafe, duplicate or unexpected member')
            total += member.size
            if member.size > MAX_FILE_BYTES or total > MAX_ARCHIVE_BYTES:
                raise ContinuityError('Checkpoint extraction limit exceeded')
            items[name] = member
        if set(items) != set(manifest['files']):
            raise ContinuityError('Archive file inventory mismatch')
        state.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=state.parent) as temp:
            output = Path(temp) / 'state'
            output.mkdir()
            for name, member in items.items():
                dest = output / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                stream = tf.extractfile(member)
                if stream is None:
                    raise ContinuityError('Invalid archive data')
                digest = hashlib.sha256()
                with dest.open('wb') as file:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                        file.write(chunk)
                if digest.hexdigest() != manifest['files'][name]:
                    raise ContinuityError('Checkpoint hash mismatch')
            if state.exists():
                state.rmdir()
            os.replace(output, state)
    return {'files': len(items), 'bytes': total, 'state': str(state)}


def prepare_project(template: Path, repo: Path, state: Path, output: Path,
                    identity: str, limit: int = 24) -> dict:
    """Select a bounded, reproducible frontier of untouched tracked article files."""
    import fnmatch
    import subprocess
    from .core import Store

    data = json.loads(template.read_text(encoding='utf-8'))
    if (not identity or identity != data.get('id')
            or data.get('coordination') != {'mode': 'proposal-only'}):
        raise ContinuityError('Project identity or proposal-only coordination mismatch')
    if not isinstance(limit, int) or not 1 <= limit <= 60:
        raise ContinuityError('Inventory limit must be between 1 and 60')
    repo = repo.resolve()
    if not repo.is_dir() or not (repo / '.git').exists():
        raise ContinuityError('Target must be an existing Git checkout')
    paths = subprocess.check_output(['git', '-C', str(repo), 'ls-files', '-z'],
                                    timeout=30).decode('utf-8').split('\0')
    patterns = data.get('article_globs')
    # The user explicitly selects a content subtree. Do not sweep repository
    # roots (e.g. "*") or GitHub/workflow/policy files by accident.
    if (not isinstance(patterns, list) or not patterns or any(
            not isinstance(p, str) or '/' not in p or p.startswith(('/', '.'))
            or '\\\\' in p or '..' in p.split('/')
            or p.split('/')[0].startswith(('*', '?', '['))
            for p in patterns)):
        raise ContinuityError('article_globs must specify an explicit content directory')
    busy = set()
    for row in Store(state).rows():
        task = json.loads(row['spec'])
        if task.get('repo') == str(repo) and row['status'] != 'applied':
            busy.update(task.get('files', []))
    eligible = []
    for path in paths:
        if (path in busy or not path.endswith(('.md', '.mdx'))
                or not any(fnmatch.fnmatchcase(path, p) for p in patterns)):
            continue
        original = repo / path
        if (not original.is_file() or original.is_symlink()
                or original.resolve() != original):
            continue
        eligible.append(path)
    # Stable spread across topics instead of repeatedly concentrating on
    # alphabetically early directories.
    eligible.sort(key=lambda path: hashlib.sha256(
        (identity + ':' + path).encode('utf-8')).hexdigest())
    inventory = eligible[:limit]
    project = {k: data[k] for k in ('brief', 'sources', 'policy_files', 'coordination')}
    project['repo'] = str(repo)
    project['inventory'] = inventory
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'eligible': len(eligible), 'selected': len(inventory),
            'previously_reserved': len(busy), 'project': identity}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Checkpoint/restore the one-runner Article Harness queue')
    parser.add_argument('command', choices=['backup', 'restore', 'prepare'])
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--project', required=True)
    parser.add_argument('--template', type=Path)
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--limit', type=int, default=24)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            if not args.template or not args.repo or not args.output:
                raise ContinuityError('prepare requires --template, --repo, --output')
            result = prepare_project(args.template, args.repo, args.state,
                                     args.output, args.project, args.limit)
        else:
            if not args.archive:
                raise ContinuityError('backup/restore require --archive')
            fn = backup if args.command == 'backup' else restore
            result = fn(args.state if args.command == 'backup' else args.archive,
                        args.archive if args.command == 'backup' else args.state,
                        args.project)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ContinuityError, OSError, ValueError, tarfile.TarError, sqlite3.Error,
            subprocess.CalledProcessError) as error:
        parser.exit(status=1, message=f'Checkpoint blocked: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
