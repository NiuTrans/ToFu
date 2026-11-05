#!/usr/bin/env python3
"""Standalone task-event appends under authority congestion, before vs after.

The native authority admits foreground work through one FIFO queue in front of
one exclusive mutex and charges the queue wait against the caller's deadline
(``lib/storage/deadline_policy.py``). This replays durable task events through
the production entry -- ``wait_for_persistence`` around
``event_log.append_persistent_event`` -> ``StorageEventBatcher`` -> the real
``StorageV2Client`` command retry loop -- against an in-process model of that
authority. Foreign writers (other tasks' records, sent through the same client
and therefore observed by the same deadline policy) keep the queue congested,
and CPU-bound threads add GIL pressure.

Reported per run: authority attempts and ``deadline_elapsed`` refusals per
operation, row sends per durable row, concurrent duplicate sends (a row carried
by one RPC while another RPC carrying the same row is still queued or in
service) and per-event durable latency. Every lane must commit its exact rows
before a run counts. The authority's service times are inputs, not
measurements; models, the real engine and the event-delivery coordinator are
excluded.

    python3 benchmarks/event_append_congestion.py
    python3 benchmarks/event_append_congestion.py --compare /tmp/base-tree --runs 3

``--compare`` alternates fresh worker processes between an exported pre-change
tree and this tree with identical parameters.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVENT_OPERATIONS = frozenset({'event.append', 'event.append_batch'})
FOREIGN_OPERATION = 'record.put'


class FifoAuthority:
    """One FIFO admission queue in front of one exclusive mutex.

    A request that is still queued, or still executing, when its wire deadline
    passes is answered ``deadline_elapsed`` without committing. Event rows use
    natural-key deduplication: identical replay is a no-op.
    """

    def __init__(self, *, transaction_seconds: float, row_seconds: float,
                 foreign_seconds: float):
        self.transaction_seconds = transaction_seconds
        self.row_seconds = row_seconds
        self.foreign_seconds = foreign_seconds
        self._condition = threading.Condition()
        self._queue: deque[object] = deque()
        self._busy = False
        self._carried: Counter = Counter()
        self.committed: dict[tuple[str, int], str] = {}
        self.attempts: Counter = Counter()
        self.refusals: Counter = Counter()
        self.deadlines: dict[str, list[float]] = {}
        self.row_sends = 0
        self.concurrent_duplicate_sends = 0
        self.foreign_commits = 0

    def attempt(self, operation, payload, *, timeout, command_id, deadline_unix_ms):
        from lib.storage.errors import StorageError

        if operation == 'event.append_batch':
            rows = list(payload['events'])
        elif operation == 'event.append':
            rows = [payload]
        else:
            rows = []
        keys = [(str(row['task_id']), int(row['sequence'])) for row in rows]
        deadline = deadline_unix_ms / 1000.0
        ticket = object()
        with self._condition:
            self.attempts[operation] += 1
            self.deadlines.setdefault(operation, []).append(round(float(timeout), 3))
            self.row_sends += len(keys)
            for key in keys:
                self._carried[key] += 1
                if self._carried[key] > 1:
                    self.concurrent_duplicate_sends += 1
            self._queue.append(ticket)
        try:
            with self._condition:
                while self._busy or self._queue[0] is not ticket:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        self._queue.remove(ticket)
                        self.refusals[operation] += 1
                        self._condition.notify_all()
                        raise StorageError('database_timeout', 'deadline_elapsed')
                    self._condition.wait(remaining)
                self._queue.popleft()
                self._busy = True
            try:
                service = (self.transaction_seconds + self.row_seconds * len(keys)
                           if operation in EVENT_OPERATIONS else self.foreign_seconds)
                remaining = deadline - time.time()
                if remaining < service:
                    time.sleep(max(0.0, remaining))
                    with self._condition:
                        self.refusals[operation] += 1
                    raise StorageError('database_timeout', 'deadline_elapsed')
                time.sleep(service)
                return self._commit(operation, rows, keys)
            finally:
                with self._condition:
                    self._busy = False
                    self._condition.notify_all()
        finally:
            with self._condition:
                for key in keys:
                    self._carried[key] -= 1

    def _commit(self, operation, rows, keys):
        from lib.storage.errors import StorageError

        results = []
        with self._condition:
            if operation not in EVENT_OPERATIONS:
                self.foreign_commits += 1
                return {'ok': True}
            for row, key in zip(rows, keys):
                body = json.dumps(row['event'], sort_keys=True)
                prior = self.committed.get(key)
                if prior is not None and prior != body:
                    raise StorageError('database_conflict', 'conflicting event payload')
                self.committed[key] = body
                results.append({'inserted': prior is None, 'task_id': key[0],
                                'sequence': key[1]})
        if operation == 'event.append':
            return results[0]
        inserted = sum(1 for result in results if result['inserted'])
        return {'results': results, 'inserted': inserted,
                'deduplicated': len(results) - inserted}


def _quantile(samples: list[float], quantile: float) -> float:
    ordered = sorted(samples)
    rank = max(1, -(-len(ordered) * quantile // 1))
    return ordered[min(len(ordered), int(rank)) - 1]


def run_case(options: argparse.Namespace) -> dict:
    import lib.storage as storage
    from lib.storage import event_batcher, v2_client
    from lib.storage.errors import StorageError
    from lib.tasks_pkg import event_log
    from lib.tasks_pkg.persistence_wait import wait_for_persistence
    # Lazy imports on the append path load before the clock and the foreign
    # writers start, so a cold run has no service observations at its first
    # batch and source-tree location never shows up as append latency.
    import lib.llm.llm_errors  # noqa: F401
    import lib.tasks_pkg.invocation  # noqa: F401
    import lib.tasks_pkg.request_inspector  # noqa: F401
    import lib.tasks_pkg.snapshot_delta  # noqa: F401
    import lib.tasks_pkg.turn_trace  # noqa: F401

    authority = FifoAuthority(
        transaction_seconds=options.transaction_ms / 1000,
        row_seconds=options.row_ms / 1000,
        foreign_seconds=options.foreign_ms / 1000)
    client = v2_client.StorageV2Client('127.0.0.1', 1, b's' * 48, owner_id=1, tenant_id=1)
    client._attempt = authority.attempt
    storage.get_storage_client = lambda *_args, **_kwargs: client

    stop = threading.Event()
    threads = []

    def foreign_writer(index: int) -> None:
        sequence = 0
        while not stop.is_set():
            sequence += 1
            try:
                client.command(FOREIGN_OPERATION,
                               {'namespace': 'bench', 'key': f'w{index}-{sequence}'},
                               None, deadline=options.foreign_deadline_s)
            except StorageError:
                pass  # a refused foreign write is load; the authority counts it

    def gil_pressure() -> None:
        while not stop.is_set():
            total = 0
            for value in range(2_000):
                total += value * value

    for index in range(options.foreign_writers):
        threads.append(threading.Thread(target=foreign_writer, args=(index,), daemon=True))
    for _ in range(options.gil_threads):
        threads.append(threading.Thread(target=gil_pressure, daemon=True))
    for thread in threads:
        thread.start()
    warm_until = time.monotonic() + options.budget_s
    while authority.foreign_commits < options.warmup_commands and time.monotonic() < warm_until:
        time.sleep(0.01)

    latencies: list[float] = []
    retries = Counter()
    failures: list[str] = []
    lock = threading.Lock()
    deadline = time.monotonic() + options.budget_s

    def lane(index: int) -> None:
        task = {'id': f'bench-task-{index}'}

        def stopping() -> bool:
            return time.monotonic() >= deadline

        for sequence in range(options.events):
            event = {'type': 'delta', 'content': f'lane {index} chunk {sequence} ' * 8}
            started = time.monotonic()
            try:
                wait_for_persistence(
                    task,
                    lambda: event_log.append_persistent_event(task['id'], sequence, event),
                    label='task event', cancellable=False, stopping=stopping,
                    on_retry=lambda: retries.update([task['id']]))
            except Exception as error:
                with lock:
                    failures.append(f'{task["id"]}:{sequence}:{type(error).__name__}')
                return
            with lock:
                latencies.append(time.monotonic() - started)

    started = time.monotonic()
    lanes = [threading.Thread(target=lane, args=(index,), daemon=True)
             for index in range(options.lanes)]
    for thread in lanes:
        thread.start()
    for thread in lanes:
        thread.join()
    wall = time.monotonic() - started
    stop.set()

    expected = {(f'bench-task-{index}', sequence)
                for index in range(options.lanes) for sequence in range(options.events)}
    with authority._condition:
        committed = set(authority.committed)
        attempts = dict(authority.attempts)
        refusals = dict(authority.refusals)
        deadlines = {operation: sorted(set(values))
                     for operation, values in authority.deadlines.items()
                     if operation in EVENT_OPERATIONS}
        row_sends = authority.row_sends
        duplicates = authority.concurrent_duplicate_sends
    rows = len(expected)
    return {
        'event_log': event_log.__file__,
        'event_batcher': event_batcher.__file__,
        'rows': rows,
        'committed': len(committed & expected),
        'complete': committed == expected and not failures,
        'failures': failures,
        'wall_s': round(wall, 2),
        'latency_s': ({'p50': round(_quantile(latencies, 0.5), 2),
                       'p95': round(_quantile(latencies, 0.95), 2),
                       'max': round(max(latencies), 2)} if latencies else {}),
        'attempts': attempts,
        'refusals': refusals,
        'event_rpc_attempts': sum(attempts.get(op, 0) for op in EVENT_OPERATIONS),
        'row_sends': row_sends,
        'sends_per_row': round(row_sends / rows, 2),
        'concurrent_duplicate_sends': duplicates,
        'durability_retries': sum(retries.values()),
        'event_deadlines_s': {op: [values[0], values[-1]] for op, values in deadlines.items()},
    }


def _worker_command(source_root: Path, options: argparse.Namespace) -> list[str]:
    command = [sys.executable, str(Path(__file__).resolve()), '--worker',
               '--source-root', str(source_root)]
    for name in ('lanes', 'events', 'foreign_writers', 'foreign_ms', 'foreign_deadline_s',
                 'transaction_ms', 'row_ms', 'gil_threads', 'warmup_commands', 'budget_s'):
        command += [f'--{name.replace("_", "-")}', str(getattr(options, name))]
    return command


def _run_worker(source_root: Path, options: argparse.Namespace) -> dict:
    with tempfile.TemporaryDirectory(prefix='event-append-congestion-') as data_dir:
        environment = dict(os.environ, PYTHONPATH=str(source_root),
                           TOFU_DATA_DIR=os.path.join(data_dir, 'data'))
        completed = subprocess.run(_worker_command(source_root, options), cwd=data_dir,
                                   env=environment, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(f'worker failed ({source_root}):\n{completed.stderr[-4000:]}')
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _summarize(label: str, results: list[dict]) -> dict:
    def median(*path):
        values = []
        for result in results:
            value = result
            for key in path:
                value = value.get(key, 0) if isinstance(value, dict) else 0
            values.append(value)
        return statistics.median(values)

    return {
        'tree': label,
        'runs': len(results),
        'complete': all(result['complete'] for result in results),
        'median_wall_s': median('wall_s'),
        'median_latency_p50_s': median('latency_s', 'p50'),
        'median_latency_p95_s': median('latency_s', 'p95'),
        'median_event_rpc_attempts': median('event_rpc_attempts'),
        'median_event_refusals': statistics.median(
            sum(result['refusals'].get(op, 0) for op in EVENT_OPERATIONS) for result in results),
        'median_sends_per_row': median('sends_per_row'),
        'median_concurrent_duplicate_sends': median('concurrent_duplicate_sends'),
        'median_durability_retries': median('durability_retries'),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--compare', type=Path, help='exported pre-change tree')
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--lanes', type=int, default=8)
    parser.add_argument('--events', type=int, default=5)
    parser.add_argument('--foreign-writers', type=int, default=4)
    parser.add_argument('--foreign-ms', type=float, default=750.0)
    parser.add_argument('--foreign-deadline-s', type=float, default=30.0)
    parser.add_argument('--transaction-ms', type=float, default=25.0)
    parser.add_argument('--row-ms', type=float, default=1.0)
    parser.add_argument('--gil-threads', type=int, default=4)
    parser.add_argument('--warmup-commands', type=int, default=8)
    parser.add_argument('--budget-s', type=float, default=240.0)
    options = parser.parse_args()
    if options.worker:
        sys.path.insert(0, str(options.source_root.resolve()))
        result = run_case(options)
        print(json.dumps(result, sort_keys=True))
        sys.stdout.flush()
        # Doomed pre-change batches may still be retrying on daemon threads.
        os._exit(0)
    trees = [('current', ROOT)]
    if options.compare is not None:
        trees.insert(0, ('before', options.compare.resolve()))
    results: dict[str, list[dict]] = {label: [] for label, _ in trees}
    for run in range(options.runs):
        order = trees if run % 2 == 0 else list(reversed(trees))
        for label, source_root in order:
            result = _run_worker(source_root, options)
            results[label].append(result)
            print(json.dumps({'run': run, 'tree': label, **result}, sort_keys=True), flush=True)
    for label, _ in trees:
        print(json.dumps(_summarize(label, results[label]), sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
