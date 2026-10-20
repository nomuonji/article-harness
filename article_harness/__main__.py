from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

from .core import Controller, HarnessError, Store, atomic_json, file_lock, git, validate_task
from .runner import AgentError, CodexRunner


def read_config(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8'))
    if 'repo' in data:
        data['repo'] = str((path.parent / data['repo']).resolve())
    return data


def plan(store, config, runner, maximum, timeout):
    repo = Path(config['repo']).resolve()
    inventory = config.get('inventory', [])
    if not isinstance(inventory, list) or not inventory:
        raise HarnessError('Project inventory must explicitly list article paths')
    busy = set()
    for row in store.rows():
        if row['status'] == 'applied':
            continue
        spec = json.loads(row['spec'])
        if spec['repo'] == str(repo):
            busy.update(spec['files'])
    inventory = [name for name in inventory if name not in busy]
    if not inventory:
        return {'summary': 'All listed files already reserved by this queue', 'added': []}
    # Validate project ownership and inputs with the same rules as worker tasks.
    validate_task(dict(config, id='plan-preflight', files=inventory,
                       reader_job='Find concrete reader-facing defects',
                       acceptance_criteria=['Propose only sourced, bounded changes']))
    before = git(repo, 'rev-parse', 'HEAD').strip()
    if git(repo, 'status', '--porcelain'):
        raise HarnessError('Planner source repository must be clean')
    task = dict(config, inventory=inventory, max_tasks=maximum)
    folder = store.root / 'plans' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    result = runner.run('planner', task, repo, folder, [], timeout)
    from .runner import validate_result
    validate_result('planner', result, task)
    if git(repo, 'rev-parse', 'HEAD').strip() != before or git(repo, 'status', '--porcelain'):
        raise HarnessError('Repository changed during planning; do not queue stale tasks')
    added, rejected = [], []
    for item in result['tasks']:
        candidate = dict(item, repo=str(repo), coordination=config['coordination'],
                         policy_files=config.get('policy_files', []), planned_base_sha=before)
        try:
            if store.add(candidate):
                added.append(item['id'])
        except HarnessError as error:
            rejected.append({'id': item['id'], 'reason': str(error)})
    receipt = {'summary': result['summary'], 'added': added, 'rejected': rejected,
               'base_sha': before, 'plan_directory': str(folder)}
    atomic_json(folder / 'queue-receipt.json', receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description='Resumable local Codex article manager + workers')
    cwd_key = hashlib.sha256(str(Path.cwd().resolve()).encode()).hexdigest()[:12]
    default_state = Path.home() / '.local' / 'state' / 'article-harness' / cwd_key
    parser.add_argument('--state', default=str(default_state), help='Durable state directory outside source repos; use one per queue')
    parser.add_argument('--codex', default='codex', help='Codex executable path')
    parser.add_argument('--model', help='Optional model; default is your Codex configuration')
    sub = parser.add_subparsers(dest='command', required=True)
    add = sub.add_parser('add', help='Queue one explicit task JSON')
    add.add_argument('task')
    planner = sub.add_parser('plan', help='Read-only Codex manager audits an explicit article inventory')
    planner.add_argument('project')
    planner.add_argument('--max-tasks', type=int, default=5)
    planner.add_argument('--timeout', type=float, default=900)
    run = sub.add_parser('run', help='Continuously dispatch queued tasks to workers and reviewers')
    run.add_argument('--workers', type=int, default=3)
    run.add_argument('--max-jobs', type=int, default=20)
    run.add_argument('--max-seconds', type=float, default=7200)
    run.add_argument('--agent-timeout', type=float, default=900)
    run.add_argument('--revisions', type=int, default=2)
    run.add_argument('--watch', action='store_true', help='Wait for added tasks until time limit; no auto planning')
    run.add_argument('--project', help='Optional project JSON for automatic planning when the queue drains')
    run.add_argument('--max-plans', type=int, default=3, help='Bounded manager invocations with --project')
    sub.add_parser('status', help='Show persisted task states')
    sub.add_parser('stop', help='Stop dispatching and drain active bounded invocations')
    sub.add_parser('clear-stop', help='Remove the persistent stop request')
    resume = sub.add_parser('resume', help='Requeue interrupted work after inspecting its worktree and external reservation')
    resume.add_argument('id')
    ack = sub.add_parser('ack-applied', help='Verify reviewed file contents are committed in source HEAD')
    ack.add_argument('id')
    args = parser.parse_args(argv)
    try:
        store = Store(args.state)
        runner = CodexRunner(args.codex, args.model)
        if args.command == 'add':
            print(json.dumps({'added': store.add(read_config(args.task))}))
        elif args.command == 'plan':
            if not 1 <= args.max_tasks <= 20 or args.timeout <= 0:
                raise HarnessError('max-tasks must be 1..20 and timeout positive')
            with file_lock(store.root / 'controller.lock'):
                print(json.dumps(plan(store, read_config(args.project), runner, args.max_tasks, args.timeout), ensure_ascii=False))
        elif args.command == 'run':
            if args.max_plans < 0:
                raise HarnessError('max-plans cannot be negative')
            config = read_config(args.project) if args.project else None
            plans_left = args.max_plans
            def replenish(timeout):
                nonlocal plans_left
                if not config or plans_left <= 0:
                    return False
                plans_left -= 1
                receipt = plan(store, config, runner, min(5, args.max_jobs), timeout)
                print(json.dumps({'manager': receipt}, ensure_ascii=False), flush=True)
                if not receipt['added']:
                    plans_left = 0
                return bool(receipt['added'])
            controller = Controller(store, runner, args.workers, args.max_jobs, args.max_seconds,
                                    args.agent_timeout, args.revisions, replenish=replenish if config else None)
            rows = controller.run(args.watch)
            print(json.dumps([{k: row[k] for k in ('id', 'status', 'attempts', 'detail')} for row in rows], ensure_ascii=False, indent=2))
            if any(row['status'] in ('blocked', 'failed', 'refused', 'interrupted') for row in rows):
                return 2
        elif args.command == 'status':
            print(json.dumps([{k: row[k] for k in ('id', 'status', 'attempts', 'detail')} for row in store.rows()], ensure_ascii=False, indent=2))
        elif args.command == 'stop':
            (store.root / 'STOP').touch()
        elif args.command == 'clear-stop':
            (store.root / 'STOP').unlink(missing_ok=True)
        else:
            with file_lock(store.root / 'controller.lock'):
                store.recover()
                row = next((row for row in store.rows() if row['id'] == args.id), None)
                if not row:
                    raise HarnessError('Unknown task id')
                if args.command == 'resume':
                    if row['status'] != 'interrupted':
                        raise HarnessError('Only interrupted tasks may resume; refused work is never replayed')
                    store.update(args.id, 'queued', 'Explicitly resumed; existing attempt budget preserved')
                elif args.command == 'ack-applied':
                    if row['status'] != 'ready_for_handoff':
                        raise HarnessError('Task has no reviewed patch to acknowledge')
                    task = json.loads(row['spec'])
                    folder = store.root / 'tasks' / args.id
                    receipt = json.loads((folder / 'receipt.json').read_text())
                    if hashlib.sha256((folder / 'change.patch').read_bytes()).hexdigest() != receipt['patch_sha256']:
                        raise HarnessError('Reviewed patch digest mismatch')
                    repo = Path(task['repo'])
                    if git(repo, 'status', '--porcelain'):
                        raise HarnessError('Source repository must be clean and changes committed')
                    git(repo, 'merge-base', '--is-ancestor', receipt['base_sha'], 'HEAD')
                    for name in receipt['changed_files']:
                        if hashlib.sha256((repo / name).read_bytes()).hexdigest() != receipt['reviewed_sha256'][name]:
                            raise HarnessError(f'Reviewed content is not in source HEAD: {name}')
                    store.update(args.id, 'applied', git(repo, 'rev-parse', 'HEAD').strip())
        return 0
    except (HarnessError, AgentError, OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
