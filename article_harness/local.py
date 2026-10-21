"""Local WSL/Linux bounded-loop supervisor for the existing Codex CLI harness.

One controller per state directory; any interrupted work remains quarantined
until manually inspected and resumed. This never publishes managed-site edits.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import shutil
import signal
import sys
import threading
import time

from . import __main__ as cli
from .continuity import ContinuityError, prepare_project
from .core import HarnessError, Store, atomic_json, file_lock


def _rows_summary(state: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in Store(state).rows():
        result[row['status']] = result.get(row['status'], 0) + 1
    return result


def cycle(*, template: Path, repo: Path, state: Path, project: str, runner: str,
          model: str | None, workers: int, max_jobs: int, max_plans: int,
          max_seconds: int, agent_timeout: int, revisions: int, limit: int,
          run_cli=cli.main) -> dict:
    """Execute one bounded cycle, with one durable per-cycle report."""
    state.mkdir(parents=True, exist_ok=True)
    if (state / 'STOP').exists():
        return {'status': 'stopped', 'reason': 'Persistent STOP flag'}
    config_path = state / 'local-project.json'
    selection = prepare_project(template, repo, state, config_path, project, limit)
    previous = _rows_summary(state)
    # Existing queued work must run even if every inventory file is reserved.
    if not selection['selected'] and not previous.get('queued', 0):
        outcome = {'status': 'idle', 'inventory': selection, 'tasks': previous}
    else:
        args = ['--state', str(state), '--codex', runner]
        if model:
            args += ['--model', model]
        args += ['run', '--workers', str(workers), '--max-jobs', str(max_jobs),
                 '--max-plans', str(max_plans), '--max-seconds', str(max_seconds),
                 '--agent-timeout', str(agent_timeout), '--revisions', str(revisions)]
        if selection['selected'] and max_plans:
            args += ['--project', str(config_path)]
        result = run_cli(args)
        # The CLI returns 2 if an individual task was blocked; other tasks
        # must still be picked up in the next cycle.
        outcome = {'status': 'completed' if result in (0, 2) else 'infrastructure_error',
                   'exit_code': result, 'inventory': selection,
                   'tasks': _rows_summary(state)}
    outcome['checked_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    atomic_json(state / 'local-last-cycle.json', outcome)
    return outcome


def supervise(*, template: Path, repo: Path, state: Path, runner: str = 'codex',
              model: str | None = None, workers: int = 2, max_jobs: int = 3,
              max_plans: int = 2, max_seconds: int = 2700, agent_timeout: int = 650,
              revisions: int = 2, limit: int = 24, interval_minutes: float = 5,
              max_cycles: int | None = None, dry_run: bool = False,
              stop_event: threading.Event | None = None, run_cycle=cycle,
              sleep=None) -> int:
    """Run bounded sessions sequentially, retaining state across all of them."""
    if not (1 <= workers <= 6 and max_jobs > 0 and 0 <= max_plans <= 20
            and max_seconds > 0 and agent_timeout > 0 and revisions >= 0
            and 1 <= limit <= 60 and interval_minutes >= 0
            and (max_cycles is None or max_cycles > 0)):
        raise ContinuityError('Invalid local supervisor bounds')
    template, repo, state = template.resolve(), repo.resolve(), state.resolve()
    if not template.is_file():
        raise ContinuityError('Project template is missing')
    settings = json.loads(template.read_text(encoding='utf-8'))
    project = settings.get('id')
    if not isinstance(project, str) or not project or settings.get('coordination') != {'mode': 'proposal-only'}:
        raise ContinuityError('Local unattended loop requires proposal-only template with stable project ID')
    if (not repo.is_dir() or not (repo / '.git').exists()
            or not Path(shutil.which('git') or '').is_file()):
        raise ContinuityError('Existing Git checkout and git executable required')
    if not dry_run and not shutil.which(runner):
        raise ContinuityError(f'Codex CLI executable unavailable: {runner}; log in to Codex in WSL')
    state.mkdir(parents=True, exist_ok=True)
    stopped = stop_event or threading.Event()
    sleep = sleep or stopped.wait
    with file_lock(state / 'local-supervisor.lock'):
        if (state / 'STOP').exists():
            print('STOP exists; use python -m article_harness --state STATE clear-stop before restarting')
            return 0
        if dry_run:
            result = prepare_project(template, repo, state, state / 'local-project.json', project, limit)
            print(json.dumps({'mode': 'dry-run', **result}, ensure_ascii=False), flush=True)
            return 0
        count = 0
        failures = 0
        while not stopped.is_set():
            if (state / 'STOP').exists():
                print('Persistent STOP flag detected; stopping supervisor.', flush=True)
                break
            try:
                result = run_cycle(template=template, repo=repo, state=state,
                                   project=project, runner=runner, model=model,
                                   workers=workers, max_jobs=max_jobs, max_plans=max_plans,
                                   max_seconds=max_seconds, agent_timeout=agent_timeout,
                                   revisions=revisions, limit=limit)
            except (ContinuityError, HarnessError, OSError, ValueError, KeyError) as error:
                result = {'status': 'infrastructure_error', 'error': str(error)}
                atomic_json(state / 'local-last-cycle.json', result)
            count += 1
            print(json.dumps({'cycle': count, **result}, ensure_ascii=False), flush=True)
            if result['status'] == 'stopped':
                break
            if result['status'] == 'infrastructure_error':
                failures += 1
                if failures >= 3:
                    print('Three consecutive infrastructure failures. Stopping; inspect local-last-cycle.json.',
                          file=sys.stderr, flush=True)
                    return 1
            else:
                failures = 0
            if max_cycles is not None and count >= max_cycles:
                break
            # An idle queue is normal. Do not hammer the model or re-run
            # refused/blocked/interrupted items to maintain artificial throughput.
            delay = max(0.1, interval_minutes * 60)
            if failures:
                delay = max(delay, min(1800, 30 * (2 ** (failures - 1))))
            sleep(delay)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Unattended local Article Harness with Codex CLI (WSL/Linux)')
    parser.add_argument('--template', type=Path, required=True, help='Proposal-only project template')
    parser.add_argument('--repo', type=Path, required=True, help='Local existing site checkout')
    parser.add_argument('--state', type=Path, required=True, help='Persistent state outside repository')
    parser.add_argument('--codex', default='codex')
    parser.add_argument('--model')
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--max-jobs', type=int, default=3)
    parser.add_argument('--max-plans', type=int, default=2)
    parser.add_argument('--max-seconds', type=int, default=2700)
    parser.add_argument('--agent-timeout', type=int, default=650)
    parser.add_argument('--revisions', type=int, default=2)
    parser.add_argument('--limit', type=int, default=24)
    parser.add_argument('--interval-minutes', type=float, default=5)
    parser.add_argument('--once', action='store_true', help='Run exactly one bounded cycle')
    parser.add_argument('--dry-run', action='store_true', help='No Codex calls or modifications to site checkout')
    args = parser.parse_args(argv)
    stop = threading.Event()
    # Only our explicit stop flag stops future cycles. The existing Controller
    # handles its own bounded child processes and STOP checkpoint.
    def handle_signal(signum, frame):
        stop.set()
        (args.state.expanduser().resolve() / 'STOP').touch()

    old = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        old[signum] = signal.signal(signum, handle_signal)
    try:
        return supervise(template=args.template.expanduser(), repo=args.repo.expanduser(),
                         state=args.state.expanduser(), runner=args.codex, model=args.model,
                         workers=args.workers, max_jobs=args.max_jobs, max_plans=args.max_plans,
                         max_seconds=args.max_seconds, agent_timeout=args.agent_timeout,
                         revisions=args.revisions, limit=args.limit,
                         interval_minutes=args.interval_minutes,
                         max_cycles=1 if args.once else None, dry_run=args.dry_run,
                         stop_event=stop)
    except (ContinuityError, HarnessError, OSError, ValueError) as error:
        print(f'Local supervisor blocked: {error}', file=sys.stderr)
        return 1
    finally:
        for signum, original in old.items():
            signal.signal(signum, original)


if __name__ == '__main__':
    raise SystemExit(main())
