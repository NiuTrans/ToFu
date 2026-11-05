#!/usr/bin/env python3
"""Pre-request durability barrier: paid-call stall per round, before vs after.

Owner decision 2026-10-28: request diagnostics must not block a paid model
call. This replays agent rounds through the production event entry
(``append_event`` -> per-task FIFO -> ``turn_lifecycle.record_task_event``, or
``record_task_events`` for a queued run) with a deliberately slow storage stub
(one sleep per authority transaction),
the production Request Inspector snapshot emitter and the production
checkpoints, comparing two pre-request boundaries on identical work:

* ``awaited`` - the former behaviour: ``checkpoint_invocation(phase=
  'model_in_flight')`` drains every accepted event before dispatch;
* ``deferred`` - ``checkpoint_model_dispatch``: dispatch immediately, the same
  FIFO persists behind the request and the tool barrier still drains it.

Each round: ROUND_START, simulated preparation, the request snapshot, the
pre-request boundary, MODEL_REQUEST_START, a simulated provider call that
streams deltas, MODEL_REQUEST_COMPLETE, the awaited ``tools_in_flight`` and
``ready`` barriers. Per round it reports the pre-request stall (RoundPrepTiming
``stepsMs.barrier``, exactly as logged), the tool-barrier wait the deferral may
shift there, and the round wall time. Modes alternate per sample; committed
(sequence, type) logs must be identical and ordered before clocks count.
Models, providers and real storage engines are never contacted; this measures
the barrier's scheduling cost, not database or provider latency.

``--lane child`` runs the same rounds on a swarm child invocation, whose only
durable events are its Request Inspector snapshots (request, then post-tool
``state``) under the child's own Inspector stream, appended through
``event_log.append_persistent_event`` to a slow batcher stub. ``awaited``
emulates the former child port, which acknowledged each append inside
``deliver_invocation_event`` (charged to ``build_bodyMs.snapshot``);
``deferred`` is the production child path. It reports that snapshot step, the
pre-request barrier and their sum (the paid call's pre-call stall), the
post-tool snapshot emission and every barrier. Committed (sequence, kind) logs
must be identical. ``--sink-ms`` adds a domain checkpoint sink write (the
swarm write-ahead) to every child checkpoint.

    python3 benchmarks/model_dispatch_barrier.py --write-ms 0,100,350,1000
    python3 benchmarks/model_dispatch_barrier.py --lane child --write-ms 0,100,250,1000
"""
from __future__ import annotations

import argparse
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODES = ('awaited', 'deferred')
LANES = ('root', 'child')


class SlowAuthority:
    """One sleep per authority transaction, recording the committed order."""

    def __init__(self, write_seconds: float):
        self.write_seconds = write_seconds
        self.committed: list[tuple[int, str]] = []
        self.transactions = 0

    def record(self, task, wire, **kwargs):
        if self.write_seconds:
            time.sleep(self.write_seconds)
        carrier = kwargs['task_event']
        self.transactions += 1
        for row in [*(carrier.get('preceding_events') or []), carrier]:
            self.committed.append((row['sequence'], row['event'].get('type')))
        return 'carried'

    def record_batch(self, task, entries):
        """A queued FIFO run of events in one authority transaction."""
        if self.write_seconds:
            time.sleep(self.write_seconds)
        self.transactions += 1
        for entry in entries:
            carrier = entry.task_event
            for row in [*(carrier.get('preceding_events') or []), carrier]:
                self.committed.append((row['sequence'], row['event'].get('type')))
        return ['carried'] * len(entries)


class SlowEvidenceBatcher:
    """Child Inspector evidence authority: one sleep per acknowledged append."""

    def __init__(self, write_seconds: float):
        self.write_seconds = write_seconds
        self.committed: list[tuple[int, str]] = []
        self.transactions = 0
        self._lock = threading.Lock()

    def append(self, *, task_id, sequence, event, **kwargs):
        if self.write_seconds:
            time.sleep(self.write_seconds)
        with self._lock:
            self.transactions += 1
            self.committed.append((sequence, event.get('kind') or event.get('type')))
        return {'inserted': True}


def _history(round_index: int) -> list[dict]:
    messages = [{'role': 'system', 'content': 'You are a careful engineer. ' * 400},
                {'role': 'user', 'content': 'Fix the failing test.'}]
    for index in range(round_index):
        call_id = f'call_{index:04d}'
        messages.append({'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': call_id, 'type': 'function', 'function': {
                'name': 'read_files', 'arguments': json.dumps({'path': f'lib/m{index}.py'})}}]})
        messages.append({'role': 'tool', 'tool_call_id': call_id,
                         'content': f'def f{index}():\n    return {index}\n' * 120})
    return messages


def run_round(task, round_index: int, *, mode: str, prep_seconds: float,
              model_seconds: float, deltas: int) -> dict:
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg import round_prep_timing as rpt
    from lib.tasks_pkg.manager import append_event
    from lib.tasks_pkg.orchestrator._messages_snapshot import emit_messages_snapshot_event
    from lib.tasks_pkg.persistence_wait import checkpoint_invocation, checkpoint_model_dispatch

    started = time.perf_counter()
    rpt.begin_round_prep(task, round_index)
    append_event(task, build_event(EventType.ROUND_START, roundNum=round_index))
    time.sleep(prep_seconds)
    messages = _history(round_index)
    emit_messages_snapshot_event(
        task, messages, tid=task['id'][:8], round_num=round_index, model='fixture-model',
        thinking_enabled=False, thinking_depth=0, preset='', temperature=1.0,
        max_tokens=4096, response_format=None, tools=[], prepared_messages=messages)
    if mode == 'awaited':
        with rpt.round_prep_step(task, 'barrier'):
            checkpoint_invocation(task, messages, round_index, phase='model_in_flight')
    else:
        checkpoint_model_dispatch(task, messages, round_index)
    span = {'spanId': f'model:bench:{round_index}', 'model': 'fixture-model',
            'requestTag': f'R{round_index + 1}', 'roundNum': round_index + 1}
    append_event(task, build_event(EventType.MODEL_REQUEST_START, requestLevel='logical', **span))
    record = rpt.finish_round_prep(task, span_id=span['spanId'], request_tag=span['requestTag'],
                                   tail='request')
    for index in range(deltas):
        time.sleep(model_seconds / max(1, deltas))
        task['content'] += f'chunk {index} '
        append_event(task, {'type': 'delta', 'content': f'chunk {index} '})
    if not deltas:
        time.sleep(model_seconds)
    append_event(task, build_event(EventType.MODEL_REQUEST_COMPLETE, status='succeeded',
                                   durationMs=int(model_seconds * 1000), **span))
    tool_started = time.perf_counter()
    checkpoint_invocation(task, messages, round_index, phase='tools_in_flight')
    tool_barrier = time.perf_counter() - tool_started
    ready_started = time.perf_counter()
    checkpoint_invocation(task, messages, round_index + 1, phase='ready')
    ready_barrier = time.perf_counter() - ready_started
    return {'pre_request_ms': record['stepsMs']['barrier'],
            'pending_at_dispatch': record.get('barrierPendingEvents'),
            'tool_barrier_ms': round(tool_barrier * 1000, 1),
            'ready_barrier_ms': round(ready_barrier * 1000, 1),
            'round_ms': round((time.perf_counter() - started) * 1000, 1)}


def run_child_round(task, round_index: int, *, mode: str, prep_seconds: float,
                    model_seconds: float, deltas: int) -> dict:
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg import round_prep_timing as rpt
    from lib.tasks_pkg.event_delivery import flush_task_events
    from lib.tasks_pkg.manager import append_event
    from lib.tasks_pkg.orchestrator._messages_snapshot import emit_messages_snapshot_event
    from lib.tasks_pkg.persistence_wait import checkpoint_invocation, checkpoint_model_dispatch
    from lib.tasks_pkg.round_evidence import build_round_evidence

    started = time.perf_counter()
    rpt.begin_round_prep(task, round_index)
    append_event(task, build_event(EventType.ROUND_START, roundNum=round_index))
    time.sleep(prep_seconds)
    messages = _history(round_index)
    emit_messages_snapshot_event(
        task, messages, tid=task['id'][:8], round_num=round_index, model='fixture-model',
        thinking_enabled=False, thinking_depth=0, preset='', temperature=1.0,
        max_tokens=4096, response_format=None, tools=[], prepared_messages=messages)
    if mode == 'awaited':
        # The former child port acknowledged the append inside the emitter.
        with rpt.round_prep_step(task, 'build_body.snapshot'):
            flush_task_events(task)
    checkpoint_model_dispatch(task, messages, round_index)
    span = {'spanId': f'model:child-bench:{round_index}', 'model': 'fixture-model',
            'requestTag': f'R{round_index + 1}', 'roundNum': round_index + 1}
    append_event(task, build_event(EventType.MODEL_REQUEST_START, requestLevel='logical', **span))
    record = rpt.finish_round_prep(task, span_id=span['spanId'], request_tag=span['requestTag'],
                                   tail='request')
    for index in range(deltas):
        time.sleep(model_seconds / max(1, deltas))
        task['content'] += f'chunk {index} '
        append_event(task, {'type': 'delta', 'content': f'chunk {index} '})
    if not deltas:
        time.sleep(model_seconds)
    append_event(task, build_event(EventType.MODEL_REQUEST_COMPLETE, status='succeeded',
                                   durationMs=int(model_seconds * 1000), **span))
    tool_started = time.perf_counter()
    checkpoint_invocation(task, messages, round_index, phase='tools_in_flight')
    tool_barrier = time.perf_counter() - tool_started
    state_started = time.perf_counter()
    append_event(task, build_round_evidence(task, _history(round_index + 1),
                                            ref=task['_inspectorRef'], kind='state',
                                            model='fixture-model'))
    if mode == 'awaited':
        flush_task_events(task)
    state_snapshot = time.perf_counter() - state_started
    ready_started = time.perf_counter()
    checkpoint_invocation(task, messages, round_index + 1, phase='ready')
    ready_barrier = time.perf_counter() - ready_started
    snapshot_ms = (record.get('build_bodyMs') or {}).get('snapshot', 0.0)
    return {'snapshot_ms': snapshot_ms,
            'pre_request_ms': record['stepsMs']['barrier'],
            'pre_call_ms': round(snapshot_ms + record['stepsMs']['barrier'], 1),
            'pending_at_dispatch': record.get('barrierPendingEvents'),
            'state_snapshot_ms': round(state_snapshot * 1000, 1),
            'tool_barrier_ms': round(tool_barrier * 1000, 1),
            'ready_barrier_ms': round(ready_barrier * 1000, 1),
            'round_ms': round((time.perf_counter() - started) * 1000, 1)}


def run_child_case(*, mode: str, write_seconds: float, rounds: int, prep_seconds: float,
                   model_seconds: float, deltas: int, sink_seconds: float = 0.0) -> dict:
    from lib.tasks_pkg import event_delivery, event_log
    from lib.tasks_pkg.child_invocation import create_child_invocation
    from lib.tasks_pkg.manager import discard_task
    from lib.tasks_pkg.snapshot_delta import forget_projector_task

    owner = event_delivery.EventDeliveryCoordinator(workers=2, idle_timeout=.05)
    authority = SlowEvidenceBatcher(write_seconds)
    original_owner, original_batcher = event_delivery._COORDINATOR, event_log._ensure_sidecar_batcher
    event_delivery._COORDINATOR = owner
    event_log._ensure_sidecar_batcher = lambda: authority
    received = []
    task = create_child_invocation(
        {'id': 'child-bench-parent-' + uuid.uuid4().hex, 'convId': 'child-bench-conversation',
         '_userId': 1, '_transientRuntime': True, 'config': {'memoryEnabled': False}},
        model='fixture-model', tools=[], objective='Measure the child pre-request stall',
        event_sink=received.append, abort_check=lambda: False)
    # A storage-backed parent: the child keeps durable Inspector evidence.
    task['_invocationStorageFree'] = False
    task.update(content='', thinking='')

    def sink(carrier, messages, round_num):
        if sink_seconds:
            time.sleep(sink_seconds)

    task['_invocationCheckpointSink'] = sink
    try:
        samples = [run_child_round(task, index, mode=mode, prep_seconds=prep_seconds,
                                   model_seconds=model_seconds, deltas=deltas)
                   for index in range(rounds)]
        event_delivery.flush_task_events(task)
        assert len(authority.committed) == 2 * rounds, 'every snapshot must be durable'
        sequences = [sequence for sequence, _ in authority.committed]
        assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
        kinds = [kind for _, kind in authority.committed]
        delivered = [event['seq'] for event in received if event.get('type') == 'messages_snapshot']
        assert delivered == sequences, 'the required sink sees every snapshot in order'
        return {'samples': samples, 'transactions': authority.transactions,
                'committed_events': len(kinds),
                'committed_sha256': hashlib.sha256(json.dumps(authority.committed).encode()).hexdigest()}
    finally:
        assert owner.close(10)
        event_delivery._COORDINATOR = original_owner
        event_log._ensure_sidecar_batcher = original_batcher
        forget_projector_task(task['_inspectorTaskId'])
        task.pop('_invocationEvidenceLane', None)
        discard_task(task['id'])


def run_case(*, mode: str, write_seconds: float, rounds: int, prep_seconds: float,
             model_seconds: float, deltas: int) -> dict:
    from lib import turn_lifecycle
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    owner = event_delivery.EventDeliveryCoordinator(workers=2, idle_timeout=.05)
    authority = SlowAuthority(write_seconds)
    original_owner, original_record = event_delivery._COORDINATOR, turn_lifecycle.record_task_event
    original_record_batch = turn_lifecycle.record_task_events
    event_delivery._COORDINATOR = owner
    turn_lifecycle.record_task_event = authority.record
    turn_lifecycle.record_task_events = authority.record_batch
    task = chat_task_runtime.create(user_id=1, task_id='barrier-bench-' + uuid.uuid4().hex)
    task.update(_attemptId='bench-attempt', _turnId='bench-turn', convId='bench-conversation',
                _workerStartedAt=time.time(), status='running', config={}, content='',
                thinking='', toolRounds=[], model='fixture-model',
                content_lock=threading.Lock())
    try:
        samples = [run_round(task, index, mode=mode, prep_seconds=prep_seconds,
                             model_seconds=model_seconds, deltas=deltas)
                   for index in range(rounds)]
        lane = task['_eventDeliveryLane']
        assert lane.accepted == lane.completed, 'every accepted event must be durable'
        sequences = [sequence for sequence, _ in authority.committed]
        assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
        kinds = [kind for _, kind in authority.committed]
        return {'samples': samples, 'transactions': authority.transactions,
                'committed_events': len(kinds),
                'committed_sha256': hashlib.sha256(json.dumps(kinds).encode()).hexdigest()}
    finally:
        assert owner.close(10)
        event_delivery._COORDINATOR = original_owner
        turn_lifecycle.record_task_event = original_record
        turn_lifecycle.record_task_events = original_record_batch
        task['_executionSession'].settle(ExecutionPhase.CANCELLED)
        chat_task_runtime.discard(task['id'])


def _median(cases, key):
    return round(statistics.median(row[key] for case in cases for row in case['samples']), 1)


def _mean(cases, key):
    return round(statistics.fmean(row[key] for case in cases for row in case['samples']), 1)


_ROOT_KEYS = ('pre_request_ms', 'tool_barrier_ms', 'ready_barrier_ms', 'round_ms')
_CHILD_KEYS = ('snapshot_ms', 'pre_request_ms', 'pre_call_ms', 'state_snapshot_ms',
               'tool_barrier_ms', 'ready_barrier_ms', 'round_ms')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--write-ms', default='0,100,350,1000',
                        help='comma-separated stub delay per authority transaction')
    parser.add_argument('--rounds', type=int, default=6)
    parser.add_argument('--samples', type=int, default=2)
    parser.add_argument('--prep-ms', type=float, default=150.0)
    parser.add_argument('--model-ms', type=float, default=1500.0)
    parser.add_argument('--deltas', type=int, default=6)
    parser.add_argument('--lane', choices=LANES, default='root')
    parser.add_argument('--sink-ms', type=float, default=0.0,
                        help='child lane only: domain checkpoint sink write per checkpoint')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    delays = [float(value) for value in args.write_ms.split(',') if value.strip()]
    if (not delays or any(value < 0 or value > 10_000 for value in delays)
            or not 1 <= args.rounds <= 50 or not 1 <= args.samples <= 10
            or not 0 <= args.sink_ms <= 10_000):
        parser.error('write-ms and sink-ms must be 0..10000 ms, rounds 1..50 and samples 1..10')
    keys = _CHILD_KEYS if args.lane == 'child' else _ROOT_KEYS
    with tempfile.TemporaryDirectory(prefix='tofu-barrier-measure-') as runtime:
        os.environ['TOFU_DATA_DIR'] = runtime
        os.environ['TOFU_LOG_DIR'] = runtime
        scenarios = []
        for delay in delays:
            cases = {mode: [] for mode in MODES}
            for sample in range(args.samples):
                order = MODES if sample % 2 == 0 else tuple(reversed(MODES))
                for mode in order:
                    common = dict(mode=mode, write_seconds=delay / 1000, rounds=args.rounds,
                                  prep_seconds=args.prep_ms / 1000,
                                  model_seconds=args.model_ms / 1000, deltas=args.deltas)
                    cases[mode].append(
                        run_child_case(**common, sink_seconds=args.sink_ms / 1000)
                        if args.lane == 'child' else run_case(**common))
            digests = {case['committed_sha256'] for mode in MODES for case in cases[mode]}
            assert len(digests) == 1, 'both boundaries must commit the identical ordered log'
            summary = {mode: {key: _median(cases[mode], key) for key in keys} for mode in MODES}
            means = {mode: {key: _mean(cases[mode], key) for key in keys} for mode in MODES}
            scenarios.append({
                'write_ms': delay, 'medians': summary, 'means': means,
                'round_saving_ms': round(summary['awaited']['round_ms']
                                         - summary['deferred']['round_ms'], 1),
                'committed_sha256': digests.pop(),
                'committed_events_per_case': cases['deferred'][0]['committed_events'],
                'samples': cases})
        payload = {
            'contract': 'tofu.model-dispatch-barrier-measure/v1',
            'python': sys.version.split()[0], 'lane': args.lane, 'rounds': args.rounds,
            'samples': args.samples, 'prep_ms': args.prep_ms, 'model_ms': args.model_ms,
            'deltas': args.deltas, 'sink_ms': args.sink_ms,
            'source_sha256': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in (ROOT / 'lib/tasks_pkg/persistence_wait.py',
                                           ROOT / 'lib/tasks_pkg/event_delivery.py',
                                           ROOT / 'lib/tasks_pkg/invocation.py',
                                           ROOT / 'lib/tasks_pkg/manager/_events.py')},
            'scenarios': scenarios}
        if args.output:
            args.output.write_text(json.dumps(payload, indent=2) + '\n')
        print(json.dumps({**{key: value for key, value in payload.items() if key != 'scenarios'},
                          'scenarios': [{key: value for key, value in row.items() if key != 'samples'}
                                        for row in scenarios]}, indent=2))


if __name__ == '__main__':
    main()
