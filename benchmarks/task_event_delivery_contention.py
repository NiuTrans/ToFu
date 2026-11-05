#!/usr/bin/env python3
"""Pump churn and coordinator-lock traffic of task event delivery under GIL pressure.

Drives ``EventDeliveryCoordinator`` from a selected source tree with bursty
producers on independent lanes, a fixed simulated persist time and optional
CPU-bound background threads that contend for the GIL. Instrumentation is
tree-agnostic, so an exported pre-change tree (``--source-root``) runs the same
workload: the coordinator's state lock is a counting RLock installed while the
coordinator is constructed, pump jobs are counted where they are submitted, and
condition notifications are counted on that lock's conditions. Every sample
checks exact per-lane commit-then-publish order before a number is reported.

``--backlog 16,64,136`` measures instead the durability barrier over N
structural events queued behind one in-flight head: the production
``append_event`` producer, the coordinator and ``turn_lifecycle`` record code
of the selected tree run against a slow Turn authority stub (one
``--authority-ms`` sleep per transaction) installed at the storage client
both trees call. The head's transaction waits until the barrier has fixed its
target; the sample reports the barrier wait, its ``BarrierCost`` and the
authority transactions, after proving every row committed once in order and
every frame published after its commit. One unreported sample per process
absorbs first-use imports.

This measures delivery scheduling work. Storage, push and whole-application
throughput are out of scope; clocks are host observations.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


class CountingRLock:
    """An RLock that counts acquisitions and contended waits for a Condition."""

    def __init__(self):
        self._lock = _REAL_RLOCK()
        self.acquisitions = 0
        self.contended = 0
        self.wait_ns = 0
        self.max_wait_ns = 0

    def _waited(self, started):
        waited = time.perf_counter_ns() - started
        self.wait_ns += waited
        self.max_wait_ns = max(self.max_wait_ns, waited)

    def acquire(self, blocking=True, timeout=-1):
        if self._lock.acquire(False):
            self.acquisitions += 1
            return True
        if not blocking:
            return False
        started = time.perf_counter_ns()
        acquired = self._lock.acquire(True, timeout)
        if acquired:
            self._waited(started)
            self.acquisitions += 1
            self.contended += 1
        return acquired

    def release(self):
        self._lock.release()

    __enter__ = acquire

    def __exit__(self, *exc):
        self._lock.release()

    def _is_owned(self):
        return self._lock._is_owned()

    def _release_save(self):
        return self._lock._release_save()

    def _acquire_restore(self, state):
        # Re-entry after Condition.wait: an acquisition the woken thread pays.
        started = time.perf_counter_ns()
        self._lock._acquire_restore(state)
        self._waited(started)
        self.acquisitions += 1


_REAL_RLOCK = threading.RLock


def _burn(stop):
    value = 0
    while not stop.is_set():
        for index in range(2000):
            value += index * index


def _quantile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))] if ordered else 0.0


def run_sample(module, *, lanes, events_per_lane, persist_ms, think_ms, burst_every,
               burst_pause_ms, cpu_threads, idle_reuse):
    counting = None

    def counting_factory():
        nonlocal counting
        counting = CountingRLock()
        return counting

    threading.RLock = counting_factory
    try:
        owner = module.EventDeliveryCoordinator(workers=idle_reuse)
    finally:
        threading.RLock = _REAL_RLOCK
    if counting is None or owner._state_lock is not counting:
        raise RuntimeError('coordinator state lock was not instrumented')

    pump_jobs, pump_exits = [], []
    run = owner._run

    def counted_run():
        pump_jobs.append(time.monotonic())
        try:
            return run()
        finally:
            pump_exits.append(time.monotonic())

    owner._run = counted_run
    notices = collections.Counter()
    in_notify_all = threading.local()
    original_notify, original_notify_all = threading.Condition.notify, threading.Condition.notify_all

    def notify(condition, n=1):
        if condition._lock is counting and not getattr(in_notify_all, 'active', False):
            notices['notify'] += 1
        return original_notify(condition, n)

    def notify_all(condition):
        if condition._lock is counting:
            notices['notify_all'] += 1
        in_notify_all.active = True
        try:
            return original_notify_all(condition)
        finally:
            in_notify_all.active = False

    stop = threading.Event()
    burners = [threading.Thread(target=_burn, args=(stop,), daemon=True) for _ in range(cpu_threads)]
    committed = [[] for _ in range(lanes)]
    published = [[] for _ in range(lanes)]
    submit_ms, first_submit_ms = [], []
    submit_lock = threading.Lock()
    lane_objects = [module.DeliveryLane({'id': f'contention-{index}'}) for index in range(lanes)]

    def produce(index):
        lane, samples = lane_objects[index], []
        for sequence in range(events_per_lane):
            def persist(sequence=sequence):
                time.sleep(persist_ms / 1000)
                committed[index].append(sequence)

            def publish(sequence=sequence):
                if committed[index][-1] != sequence:
                    raise AssertionError('published before its own commit')
                published[index].append(sequence)

            started = time.perf_counter()
            owner.submit(lane, sequence, 128, persist, publish)
            samples.append((time.perf_counter() - started) * 1000)
            pause = burst_pause_ms if burst_every and (sequence + 1) % burst_every == 0 else think_ms
            time.sleep(pause / 1000)
        with submit_lock:
            # A lane's first submission also starts its pump; report it apart.
            first_submit_ms.append(samples[0])
            submit_ms.extend(samples[1:])

    threading.Condition.notify, threading.Condition.notify_all = notify, notify_all
    for burner in burners:
        burner.start()
    try:
        started = time.perf_counter()
        producers = [threading.Thread(target=produce, args=(index,)) for index in range(lanes)]
        for producer in producers:
            producer.start()
        for producer in producers:
            producer.join()
        produced = time.perf_counter()
        for lane in lane_objects:
            owner.flush(lane, timeout=60)
        finished = time.perf_counter()
        exits_during_workload = len(pump_exits)
        acquisitions, contended = counting.acquisitions, counting.contended
        lock_wait_ms, lock_max_wait_ms = counting.wait_ns / 1e6, counting.max_wait_ns / 1e6
        notify_counts = dict(notices)
    finally:
        threading.Condition.notify, threading.Condition.notify_all = original_notify, original_notify_all
        stop.set()
        for burner in burners:
            burner.join()
        closed = owner.close(10)
    expected = list(range(events_per_lane))
    if not closed or any(rows != expected for rows in committed) or published != committed:
        raise AssertionError('delivery lost, reordered or published before commit')
    events = lanes * events_per_lane
    return {
        'events': events,
        'pump_jobs': len(pump_jobs),
        'pump_exits_during_workload': exits_during_workload,
        'lock_acquisitions_per_event': acquisitions / events,
        'contended_acquisitions_per_event': contended / events,
        'lock_wait_ms_total': lock_wait_ms,
        'lock_wait_ms_max': lock_max_wait_ms,
        'notify_per_event': notify_counts.get('notify', 0) / events,
        'notify_all_per_event': notify_counts.get('notify_all', 0) / events,
        'first_submit_ms_max': max(first_submit_ms),
        'submit_ms_p50': _quantile(submit_ms, .5),
        'submit_ms_p99': _quantile(submit_ms, .99),
        'submit_ms_max': max(submit_ms) if submit_ms else 0.0,
        'wall_ms': (finished - started) * 1000,
        'drain_ms': (finished - produced) * 1000,
    }


class SlowTurnAuthority:
    """Turn event authority stub: one sleep per transaction, the first gated.

    It answers the reads of one live attempt and commits ``turn.event.record``
    and ``turn.event.record_batch`` like both authorities: one projection
    revision and one result per entry, every carried row in order.
    """

    def __init__(self, task, write_seconds):
        self.attempt = {'attemptId': task['_attemptId'], 'status': 'running',
                        'conversationId': task['convId'], 'turnId': task['_turnId'],
                        'taskId': task['id']}
        self.write_seconds = write_seconds
        self.gate = threading.Event()
        self.revision = 0
        self.rows = []
        self.transactions = collections.Counter()
        self.entries = []
        self._lock = threading.Lock()
        self._gated = False

    def query(self, operation, payload, *args, **kwargs):
        if operation == 'turn.attempt.get':
            return dict(self.attempt)
        if operation == 'turn.get':
            return {'conversationId': self.attempt['conversationId'],
                    'turnId': self.attempt['turnId'], 'actor': 'assistant',
                    'status': 'running', 'projectionRevision': self.revision, 'projection': {}}
        raise AssertionError(f'unexpected storage read {operation}')

    def command(self, operation, payload, *args, **kwargs):
        if operation not in ('turn.event.record', 'turn.event.record_batch'):
            raise AssertionError(f'unexpected storage command {operation}')
        if not self._gated:
            self._gated = True
            if not self.gate.wait(120):
                raise AssertionError('the barrier never released the head transaction')
        time.sleep(self.write_seconds)
        entries = payload['entries'] if operation == 'turn.event.record_batch' else [payload]
        results = []
        with self._lock:
            for entry in entries:
                carrier = entry['task_event']
                rows = [*(carrier.get('preceding_events') or ()), carrier]
                self.revision += 1
                self.rows.extend(row['sequence'] for row in rows)
                results.append({'applied': True, 'projection_revision': self.revision,
                                'task_event': carrier, 'task_event_count': len(rows)})
            self.transactions[operation] += 1
            self.entries.append(len(entries))
        return results if operation == 'turn.event.record_batch' else results[0]


def _structural_event(task, index):
    """One structural frame; tool frames grow the tool rows like real rounds."""
    round_num, step = index // 3 + 1, index % 3
    if step == 0:
        return {'type': 'phase', 'phase': 'tool', 'detail': f'round {round_num}'}
    status = 'running' if step == 1 else 'done'
    rows = [row for row in task['toolRounds'] if row['roundNum'] != round_num]
    task['toolRounds'] = rows + [{
        'roundNum': round_num, 'toolCallId': f'call-{round_num}', 'toolName': 'read_file',
        'toolArgs': '{"path": "lib/module.py"}', 'status': status,
        **({'toolContent': f'def f{round_num}():\n    return {round_num}\n' * 40}
           if status == 'done' else {})}]
    return {'type': 'tool_start' if step == 1 else 'tool_result', 'roundNum': round_num,
            'toolName': 'read_file', 'toolCallId': f'call-{round_num}', 'query': 'lib/module.py',
            **({'results': [], 'status': 'done'} if step == 2 else {})}


def run_backlog_sample(*, events, authority_ms, cpu_threads):
    """Barrier over ``events`` structural frames queued behind one in-flight head."""
    from lib import turn_lifecycle
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager import append_event
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    owner = event_delivery.EventDeliveryCoordinator(workers=2, idle_timeout=.05)
    task = chat_task_runtime.create(user_id=1, task_id='backlog-bench-' + uuid.uuid4().hex)
    task.update(_attemptId='backlog-attempt-' + uuid.uuid4().hex, _turnId='backlog-turn',
                convId='backlog-conversation', _workerStartedAt=time.time(), status='running',
                config={}, content='', thinking='', toolRounds=[], model='fixture-model',
                content_lock=threading.Lock())
    authority = SlowTurnAuthority(task, authority_ms / 1000)
    published, early = [], []
    original_owner, original_client = event_delivery._COORDINATOR, turn_lifecycle._turn_client
    original_visibility = event_delivery.advance_event_visibility

    def advance_event_visibility(target, sequence):
        if target is task:
            with authority._lock:
                if sequence not in authority.rows:
                    early.append(sequence)
            published.append(sequence)
        return original_visibility(target, sequence)

    stop = threading.Event()
    burners = [threading.Thread(target=_burn, args=(stop,), daemon=True) for _ in range(cpu_threads)]
    costs = []
    event_delivery._COORDINATOR = owner
    turn_lifecycle._turn_client = lambda *, write=False: authority
    event_delivery.advance_event_visibility = advance_event_visibility
    for burner in burners:
        burner.start()
    try:
        for index in range(events):
            append_event(task, _structural_event(task, index))
        started = time.perf_counter()
        # The barrier fixes its target and timing records before the head
        # transaction proceeds: it awaits exactly ``events`` queued frames.
        awaited = event_delivery.flush_task_events(
            task, slow_after=0, on_slow=lambda pending: authority.gate.set(),
            on_release=costs.append)
        barrier_ms = (time.perf_counter() - started) * 1000
    finally:
        authority.gate.set()
        stop.set()
        for burner in burners:
            burner.join()
        closed = owner.close(30)
        event_delivery._COORDINATOR = original_owner
        turn_lifecycle._turn_client = original_client
        event_delivery.advance_event_visibility = original_visibility
        task['_executionSession'].settle(ExecutionPhase.CANCELLED)
        chat_task_runtime.discard(task['id'])
    expected = list(range(events))
    if (not closed or awaited != events or authority.rows != expected
            or published != expected or early):
        raise AssertionError('backlog lost, reordered or published before commit')
    [cost] = costs
    return {
        'events': events,
        'barrier_ms': barrier_ms,
        'persist_calls': cost.persist_calls,
        'persist_ms': cost.persist_seconds * 1000,
        'queued_ms': cost.queued_seconds * 1000,
        'record_transactions': authority.transactions['turn.event.record'],
        'batch_transactions': authority.transactions['turn.event.record_batch'],
        'max_entries_per_transaction': max(authority.entries),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--source-root', type=Path, default=ROOT,
                        help='tree whose lib/ is measured (an exported pre-change tree for before)')
    parser.add_argument('--lanes', type=int, default=8)
    parser.add_argument('--events-per-lane', type=int, default=300)
    parser.add_argument('--persist-ms', type=float, default=3.0)
    parser.add_argument('--think-ms', type=float, default=4.0)
    parser.add_argument('--burst-every', type=int, default=50)
    parser.add_argument('--burst-pause-ms', type=float, default=300.0)
    parser.add_argument('--cpu-threads', type=int, default=8)
    parser.add_argument('--idle-reuse', type=int, default=16)
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--backlog', default='',
                        help='comma-separated queued structural event counts: measure the '
                             'durability barrier over each backlog instead')
    parser.add_argument('--authority-ms', type=float, default=50.0,
                        help='backlog mode: slow authority sleep per Turn event transaction')
    parser.add_argument('--json', action='store_true', help='print the full JSON report')
    args = parser.parse_args(argv)
    source_root = args.source_root.resolve()
    sys.path.insert(0, str(source_root))
    os.environ.setdefault('TOFU_DATA_DIR', tempfile.mkdtemp(prefix='tofu-delivery-contention-'))
    backlog = [int(value) for value in args.backlog.split(',') if value.strip()]
    from lib.tasks_pkg import event_delivery
    modules = [event_delivery]
    if backlog:
        from lib import turn_lifecycle
        from lib.tasks_pkg.manager import _events
        modules += [_events, turn_lifecycle]
    sources = [Path(module.__file__).resolve() for module in modules]
    if not all(source.is_relative_to(source_root) for source in sources):
        raise SystemExit(f'imported {sources}, not the requested source root {source_root}')
    if backlog:
        # One unreported sample absorbs the first-use imports of the persist path.
        run_backlog_sample(events=4, authority_ms=0, cpu_threads=0)
        cases = {str(events): [run_backlog_sample(events=events, authority_ms=args.authority_ms,
                                                  cpu_threads=args.cpu_threads)
                               for _ in range(args.samples)]
                 for events in backlog}
    else:
        cases = {'contention': [run_sample(
            event_delivery, lanes=args.lanes, events_per_lane=args.events_per_lane,
            persist_ms=args.persist_ms, think_ms=args.think_ms, burst_every=args.burst_every,
            burst_pause_ms=args.burst_pause_ms, cpu_threads=args.cpu_threads,
            idle_reuse=args.idle_reuse) for _ in range(args.samples)]}
    report = {
        'source': str(sources[0]),
        'source_sha256': {str(source.relative_to(source_root)): hashlib.sha256(
            source.read_bytes()).hexdigest() for source in sources},
        'python': sys.version.split()[0],
        'parameters': {key: value for key, value in vars(args).items()
                       if key not in ('source_root', 'json')},
        'samples': cases,
        'median': {case: {key: statistics.median(sample[key] for sample in samples)
                          for key in samples[0]} for case, samples in cases.items()},
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"source={report['source']} cpu_threads={args.cpu_threads} "
              f"samples={args.samples}")
        for case, median in report['median'].items():
            print(f'{case}:')
            for key, value in median.items():
                print(f'  {key:34s} {value:10.3f}' if isinstance(value, float)
                      else f'  {key:34s} {value:10d}')
    return report


if __name__ == '__main__':
    main()
