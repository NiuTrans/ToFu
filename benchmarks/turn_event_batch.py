#!/usr/bin/env python3
"""One batched Turn transaction versus K sequential event records.

A delivery barrier that waits on K queued structural task events used to pay K
``turn.event.record`` commits; ``turn_lifecycle.record_task_events`` commits
them as ordinary steps of one ``turn.event.record_batch`` transaction. This
replays the identical FIFO run of K structural frames (phase, tool start,
tool result and round end over bounded tool rounds, each with its detached
projection source and durable carrier prepared at acceptance) on a fresh
attempt of a disposable authority, alternating the two persistence modes:

* ``sequential`` - K ``record_task_event`` calls (the per-event path);
* ``batch`` - one ``record_task_events`` call.

Clocks count only after both modes committed identical durable state: the
projection, K revisions, K task-event rows and K conversation-sync patches
(wall-clock activity fields excluded) hash to the same digest. ``--writers``
background producers keep recording on their own conversations so every commit
queues behind theirs (storage congestion); ``--gil-threads`` CPU-bound Python
threads contend for the measuring process's interpreter lock. ``persist_ms`` is
the barrier-visible wall time; ``rpc_ms`` sums the measuring thread's storage
command round trips. Host observations of one component boundary, not
application latency.

    python3 benchmarks/turn_event_batch.py --backend sqlite --events 1,16,64,500
    TOFUDB_BIN=/path/to/tofu-db python3 benchmarks/turn_event_batch.py --backend tofudb
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import statistics
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_CLOCK_FIELDS = {'timingTrace', 'activityTimeline'}
_RECORD_OPERATIONS = {'turn.event.record', 'turn.event.record_batch'}


@contextlib.contextmanager
def authority(backend: str, parent: Path | None = None):
    """A disposable storage authority installed as the process runtime."""
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test
    if backend == 'tofudb':
        binary = Path(os.environ['TOFUDB_BIN']).resolve()
        os.environ.setdefault('TOFUDB_EXPECTED_SHA256',
                              hashlib.sha256(binary.read_bytes()).hexdigest())
        os.environ.setdefault('TOFUDB_TRANSPORT', 'daemon')
    with tempfile.TemporaryDirectory(prefix='turn-event-batch-', dir=parent) as root:
        supervisor = StorageSupervisor(project_root=Path(root), backend=backend,
                                       startup_timeout=60)
        runtime = StorageRuntime(supervisor, auto_restart=False)
        install_runtime_for_test(runtime)
        try:
            runtime.start()
            yield supervisor
        finally:
            runtime.stop()
            install_runtime_for_test(None)


def live_task(name: str, *, user_id: int = 1) -> dict[str, Any]:
    """A bound, started attempt whose producer caches its committed baseline."""
    from lib import turn_lifecycle
    from lib.identity import PrincipalContext
    created = turn_lifecycle.create_turn_pair(
        f'{name}-conv', command_id=f'{name}-pair', input_projection={'content': 'q'},
        config={'model': 'bench'}, user_id=user_id,
        conversation_defaults={'allowCreate': True, 'title': name, 'settings': {}})
    attempt_id = created['attempt']['attemptId']
    turn_lifecycle.bind_task(attempt_id, name, user_id=user_id)
    task = {
        '_attemptId': attempt_id, 'id': name, 'status': 'running', '_userId': user_id,
        '_principalContext': PrincipalContext.user(
            subject_id=f'bench-user:{user_id}', owner_user_id=user_id).to_payload(),
        'content': '', 'thinking': '', 'toolRounds': [], 'model': 'bench',
        'config': {'model': 'bench'}, 'convId': f'{name}-conv',
        '_turnId': created['turn']['turnId'],
    }
    assert turn_lifecycle.record_task_event(task, {'type': 'phase', 'phase': 'starting'})
    return task


def structural_run(task: dict[str, Any], events: int) -> list[Any]:
    """Freeze K accepted frames exactly like the delivery FIFO does."""
    from lib import turn_lifecycle
    from lib.tasks_pkg.event_log import project_persistent_event
    entries = []
    for sequence in range(events):
        kind = ('phase', 'tool_start', 'tool_result', 'round_end')[sequence % 4]
        round_num = sequence // 4 % 8 + 1
        task['content'] = f'{task["content"]}step {sequence}. '
        if kind == 'phase':
            task['phase'] = {'phase': 'calling', 'detail': f'round {round_num}'}
        elif kind in ('tool_start', 'tool_result'):
            row = {'roundNum': round_num, 'toolCallId': f'call-{round_num}',
                   'toolName': 'read_file', 'toolArgs': '{"path": "a.py"}',
                   'status': 'running' if kind == 'tool_start' else 'done'}
            if kind == 'tool_result':
                row['toolContent'] = f'result {sequence}'
            task['toolRounds'] = [item for item in task['toolRounds']
                                  if item['roundNum'] != round_num] + [row]
        wire = {'type': kind, 'roundNum': round_num, 'seq': sequence,
                'taskId': task['id'], 'attemptId': task['_attemptId']}
        entries.append(turn_lifecycle.TaskEventBatchEntry(
            raw_event=wire, sequence=sequence,
            projection_source=turn_lifecycle.capture_task_projection_source(task, wire),
            task_event={'task_id': task['id'], 'sequence': sequence,
                        'event': project_persistent_event(task['id'], wire)}))
    return entries


def durable_digest(client: Any, task: dict[str, Any], revision: int, sync: int,
                   events: int) -> str:
    """Hash the committed work; ids and wall-clock activity facts excluded."""
    turn = client.query('turn.get', {'conversation_id': task['convId'],
                                     'turn_id': task['_turnId'], 'user_id': 1})
    changes = client.query('turn.sync.changes', {
        'conversation_id': task['convId'], 'user_id': 1, 'after': sync})['events']
    rows = client.query('event.list', {'task_id': task['id'], 'after_sequence': -1,
                                       'limit': events + 1})
    assert turn['projectionRevision'] == revision + events
    assert [change['type'] for change in changes] == ['attempt.event'] * events
    assert [row['sequence'] for row in rows] == list(range(events))
    work = {
        'projection': {key: value for key, value in turn['projection'].items()
                       if key not in _CLOCK_FIELDS},
        'patches': [[operation for operation in
                     change['payload']['event']['payload']['projectionPatch']['operations']
                     if not (operation['path'] and operation['path'][0] in _CLOCK_FIELDS)]
                    for change in changes],
        'rows': [row['event'] for row in rows],
    }
    text = json.dumps(work, sort_keys=True)
    for key in ('_attemptId', 'convId', '_turnId', 'id'):
        text = text.replace(task[key], key)
    return hashlib.sha256(text.encode()).hexdigest()


class _RpcClock:
    """Sum the measuring thread's Turn event command round trips."""

    def __init__(self, client: Any) -> None:
        self.client, self.original = client, client.command
        self.thread = threading.get_ident()
        self.count, self.seconds = 0, 0.0
        client.command = self

    def __call__(self, operation, *args, **kwargs):
        if operation not in _RECORD_OPERATIONS or threading.get_ident() != self.thread:
            return self.original(operation, *args, **kwargs)
        started = time.perf_counter()
        try:
            return self.original(operation, *args, **kwargs)
        finally:
            self.seconds += time.perf_counter() - started
            self.count += 1

    def close(self) -> None:
        self.client.command = self.original


def run_case(client: Any, *, mode: str, events: int) -> dict[str, Any]:
    """Persist one prepared run; return clocks and the committed-work digest."""
    from lib import turn_lifecycle
    task = live_task(f'{mode}-{events}-{uuid.uuid4().hex[:8]}')
    revision = task['_turnProjectionState']['projectionRevision']
    sync = client.query('turn.sync.snapshot', {'conversation_id': task['convId'],
                                               'user_id': 1})['syncSequence']
    entries = structural_run(task, events)
    clock = _RpcClock(client)
    started = time.perf_counter()
    try:
        if mode == 'batch':
            outcomes = turn_lifecycle.record_task_events(task, entries)
        else:
            outcomes = [turn_lifecycle.record_task_event(
                task, entry.raw_event, task_event=entry.task_event,
                projection_source=entry.projection_source) for entry in entries]
    finally:
        elapsed = time.perf_counter() - started
        clock.close()
    assert outcomes == ['carried'] * events, outcomes
    return {'persist_ms': elapsed * 1000, 'rpc_ms': clock.seconds * 1000,
            'rpcs': clock.count,
            'digest': durable_digest(client, task, revision, sync, events)}


@contextlib.contextmanager
def contention(writers: int, gil_threads: int):
    """Background commit queue pressure and interpreter-lock pressure."""
    from lib import turn_lifecycle
    stop = threading.Event()
    failures: list[BaseException] = []

    def write(index: int) -> None:
        try:
            task = live_task(f'writer-{index}-{uuid.uuid4().hex[:8]}')
            step = 0
            while not stop.is_set():
                step += 1
                task['phase'] = {'phase': 'calling', 'detail': str(step)}
                task['content'] = f'writer step {step}'
                turn_lifecycle.record_task_event(task, {'type': 'phase', 'phase': 'calling'})
        except BaseException as exc:  # surfaced after the measurement
            failures.append(exc)

    def spin() -> None:
        value = 0
        while not stop.is_set():
            for _ in range(10_000):
                value += 1

    threads = ([threading.Thread(target=write, args=(index,), daemon=True)
                for index in range(writers)]
               + [threading.Thread(target=spin, daemon=True) for _ in range(gil_threads)])
    for thread in threads:
        thread.start()
    try:
        yield
    finally:
        stop.set()
        for thread in threads:
            thread.join(30)
    if failures:
        raise RuntimeError('a background writer failed') from failures[0]


def measure(backend: str, events: list[int], repetitions: int, writers: int,
            gil_threads: int, report: Callable[[dict[str, Any]], None],
            parent: Path | None = None) -> None:
    with authority(backend, parent) as supervisor:
        client = supervisor.client
        for mode in ('sequential', 'batch'):
            run_case(client, mode=mode, events=1)  # import and connection warm-up
        with contention(writers, gil_threads):
            for count in events:
                samples: dict[str, list[dict[str, Any]]] = {'sequential': [], 'batch': []}
                for _ in range(repetitions):
                    for mode in ('sequential', 'batch'):
                        samples[mode].append(run_case(client, mode=mode, events=count))
                digests = {sample['digest'] for mode in samples for sample in samples[mode]}
                if len(digests) != 1:
                    raise AssertionError(f'unequal committed work for K={count}: {digests}')
                row: dict[str, Any] = {'backend': backend, 'events': count, 'writers': writers,
                                       'gil_threads': gil_threads, 'repetitions': repetitions,
                                       'digest': digests.pop()}
                for mode, cases in samples.items():
                    row[mode] = {
                        'rpcs': cases[0]['rpcs'],
                        'persist_ms_median': statistics.median(c['persist_ms'] for c in cases),
                        'rpc_ms_median': statistics.median(c['rpc_ms'] for c in cases),
                        'persist_ms': [round(c['persist_ms'], 2) for c in cases],
                    }
                row['speedup'] = (row['sequential']['persist_ms_median']
                                  / max(row['batch']['persist_ms_median'], 1e-9))
                report(row)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--backend', choices=('sqlite', 'tofudb'), default='sqlite')
    parser.add_argument('--events', default='1,16,64,500')
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--writers', type=int, default=0)
    parser.add_argument('--gil-threads', type=int, default=0)
    parser.add_argument('--authority-parent', type=Path,
                        help='place the disposable authority on this volume')
    args = parser.parse_args()
    os.environ.setdefault('TOFU_TURN_DELTA_RECORD_MS', '0')
    measure(args.backend, [int(value) for value in args.events.split(',')],
            args.repetitions, args.writers, args.gil_threads,
            lambda row: print(json.dumps(row), flush=True), args.authority_parent)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
