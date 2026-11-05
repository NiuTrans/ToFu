#!/usr/bin/env python3
"""Real retained histories through capture -> delivery FIFO -> native commit -> push.

Entry: CLI below. Dependencies: the production capture/delivery/Turn owners and
conversation_authority's disposable engine. Private exports are inputs, never repo
fixtures. The controlled continuation includes metadata, text, tools, usage and
terminal settlement; it is not replay of the original model/tool execution.
Every accepted event is carried in a Turn transaction. Clocks include capture,
queue and commit, but exclude seeding, validation, restart, HTTP and browser work.
Run fresh serial processes with alternating treatment order; compare_reports
refuses unequal durable work. No live store is written.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import statistics
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def summary(values):
    ordered = sorted(values)
    if not ordered:
        raise ValueError('Empty clock population')
    return {'count': len(values), 'total': sum(values),
            'median': statistics.median(values),
            'p95': ordered[math.ceil(len(ordered) * .95) - 1],
            'max': ordered[-1]}


def compare_reports(before, after):
    """Keep all cases and clocks; missing parity/recovery evidence fails closed."""
    if before['workload'] != after['workload']:
        raise ValueError('Different workload')
    for report in (before, after):
        if report.get('failure'):
            raise ValueError('Failed run cannot qualify a comparison')
        if (report['integrity']['integrity_returncode'] != 0
                or len(set(report['engine_pids'])) != 2):
            raise ValueError('Missing process crash/integrity evidence')
        if report['workload'].get('concurrent'):
            windows = [case.get('clock_window_seconds', []) for case in report['cases']]
            if (not windows or any(len(window) != 2 or
                    not all(math.isfinite(value) for value in window) or
                    window[0] >= window[1] for window in windows) or
                    max(window[0] for window in windows) >= min(window[1] for window in windows)):
                raise ValueError('Concurrent case clocks do not overlap')
    for key in ('backend', 'filesystem', 'durability', 'client_mode'):
        if before['authority'].get(key) != after['authority'].get(key):
            raise ValueError(f'Different authority {key}')
    left, right = before['cases'], after['cases']
    if not left or len(left) != len(right):
        raise ValueError('Missing conversations')
    if [c['conversation'] for c in left] != before['workload']['conversations']:
        raise ValueError('Incomplete selected population')
    old, new, ratios = [], [], []
    for a, b in zip(left, right):
        for key in ('conversation', 'fixture_sha256', 'events', 'digest', 'event_stages'):
            if a[key] != b[key]:
                raise ValueError(f'Different {key}: {a["conversation"]}')
        for case in (a, b):
            if not case['recovered'] or not case['push_ordered']:
                raise ValueError('Missing recovery/ordered publication')
            if len(case['event_ms']) != len(case['event_stages']) or len(case['event_ms']) != case['events']:
                raise ValueError('Missing clocks')
            if not all(math.isfinite(v) and v > 0 for v in case['event_ms']):
                raise ValueError('Invalid clocks')
        old.extend(a['event_ms'])
        new.extend(b['event_ms'])
        ratios.append({'conversation': a['conversation'],
                       'speedup': sum(a['event_ms']) / sum(b['event_ms'])})
    return {'conversations': len(left), 'observations': len(old),
            'before_ms': summary(old), 'after_ms': summary(new),
            'speedup': sum(old) / sum(new), 'per_conversation': ratios,
            'stages': {stage: {
                'before_ms': summary([v for c in left for v, s in
                                      zip(c['event_ms'], c['event_stages']) if s == stage]),
                'after_ms': summary([v for c in right for v, s in
                                     zip(c['event_ms'], c['event_stages']) if s == stage]),
            } for stage in sorted(set(s for c in left for s in c['event_stages']))}}


def install_reference(path):
    """An explicit frozen normalizer is the only application-code treatment."""
    from lib import turn_lifecycle
    spec = importlib.util.spec_from_file_location('_cohort_reference_segments', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def reference(*args, memo=None, **kwargs):
        return module.projection_with_stable_segments(*args, **kwargs)

    turn_lifecycle.projection_with_stable_segments = reference


def retain_failure(report, engine, output, error):
    """Preserve the disposable authority and partial evidence before cleanup."""
    report['failure'] = {'type': type(error).__name__, 'message': str(error),
                         'daemon_log': engine.log_path}
    try:
        report['failure']['retained_paths'] = engine.retain_failure()
    except Exception as retention_error:
        report['failure']['retention_error'] = str(retention_error)
    output.write_text(json.dumps(report, indent=2) + '\n')


def continuation(task, cycles):
    """Same controlled work on every complete history, with no paid execution."""
    from lib.conversation_sync.settled_rows import settle
    from lib.tasks_pkg.segments import tool_use_segment_from_round
    last_round = max((r.get('roundNum', 0) for r in task['toolRounds']
                      if type(r.get('roundNum', 0)) is int), default=0)
    for index in range(8):
        task['phase'] = {'phase': 'calling', 'detail': f'metadata {index}'}
        yield 'metadata', {'type': 'phase', **task['phase']}
    for cycle in range(cycles):
        round_num = last_round + cycle + 1
        yield 'mixed', {'type': 'model_request_start', 'roundNum': round_num}
        for part in range(2):
            text = f' cohort continuation {cycle}/{part}.'
            task['content'] = (task.get('content') or '') + text
            task['segments'].append(settle({'type': 'text', 'text': text,
                                           'blockId': f'cohort-text:{cycle}:{part}'}))
            yield 'mixed', {'type': 'delta', 'text': text}
        row = {'toolCallId': f'cohort-call-{cycle}', 'toolName': 'read_file',
               'toolArgs': '{"path":"cohort-fixture"}', 'roundNum': round_num,
               'attemptId': task['_attemptId'], 'taskId': task['id'],
               'status': 'running'}
        task['toolRounds'].append(settle(row))
        task['segments'].append(settle(tool_use_segment_from_round(row, len(task['segments']))))
        yield 'mixed', {'type': 'tool_start', 'toolCallId': row['toolCallId']}
        yield 'mixed', {'type': 'phase', 'phase': 'tool_running'}
        row = {**row, 'status': 'done', 'toolContent': f'complete cohort result {cycle}'}
        task['toolRounds'][-1] = settle(row)
        task['segments'][-1] = settle(tool_use_segment_from_round(row, len(task['segments']) - 1))
        yield 'mixed', {'type': 'tool_result', 'toolCallId': row['toolCallId'],
                        'content': row['toolContent']}
        task['apiRounds'].append(settle({'roundNum': round_num, 'model': 'bench',
                                        'usage': {'input_tokens': 7, 'output_tokens': 3}}))
        yield 'mixed', {'type': 'round_usage', 'roundNum': round_num}
        yield 'mixed', {'type': 'round_end', 'roundNum': round_num}
    task['status'] = 'done'
    task['finishReason'] = 'stop'
    yield 'terminal', {'type': 'done', 'finishReason': 'stop'}


def read_work(client, task, events, revision):
    turn = client.query('turn.get', {'conversation_id': task['convId'],
                                   'turn_id': task['_turnId'], 'user_id': task['_userId']})
    rows = client.query('event.list', {'task_id': task['id'], 'after_sequence': -1,
                                     'limit': events + 1})
    if (turn['status'] != 'completed' or turn['projectionRevision'] != revision
            or [r['sequence'] for r in rows] != list(range(events))):
        raise ValueError(f'Durable boundary differs: status={turn["status"]}, '
                         f'revision={turn["projectionRevision"]}/{revision}, '
                         f'sequences={[r["sequence"] for r in rows]}/{events}')
    raw_digest = digest({'turn': turn, 'rows': rows})
    work = {'status': turn['status'], 'projection': {
        k: v for k, v in turn['projection'].items()
        if k not in {'activityTimeline', 'timingTrace'}},
        'events': [r['event'] for r in rows]}
    encoded = json.dumps(work, sort_keys=True, ensure_ascii=False)
    for key in ('_attemptId', 'convId', '_turnId', 'id'):
        encoded = encoded.replace(task[key], key)
    return hashlib.sha256(encoded.encode()).hexdigest(), raw_digest


def run_case(client, owner, fixture, row, *, cycles, submission, ready=None):
    from benchmarks.turn_event_batch import live_task
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.settled_rows import settle
    from lib.tasks_pkg.event_delivery import DeliveryLane, retained_snapshot_bytes
    from lib.tasks_pkg.event_log import project_persistent_event
    from scripts.tofudb_perf_compare import _OWNER_ID
    task = live_task(f'cohort-{row["conversation"]}', user_id=_OWNER_ID)
    projection = fixture['turn']['projection']
    task.update(projection)
    task['status'] = 'running'
    for key in ('toolRounds', 'segments', 'apiRounds'):
        task[key] = [settle(value) for value in projection.get(key, [])]
    seed_started = time.perf_counter()
    assert turns.record_task_event(task, {'type': 'phase', 'phase': 'cohort_seed'})
    seed_ms = (time.perf_counter() - seed_started) * 1000
    revision = task['_turnProjectionState']['projectionRevision']
    lane = DeliveryLane(task)
    previous, previous_bytes = None, 0
    started, pushed, stages, capture_ms = {}, {}, [], []
    acknowledged, observed = {}, []

    def persist_many(entries):
        outcomes = turns.record_task_events(task, entries)
        if outcomes != ['carried'] * len(entries):
            raise ValueError(f'Rejected continuation: {outcomes}')
        at = time.perf_counter()
        for entry in entries:
            acknowledged[entry.sequence] = at

    if ready is not None:
        ready.wait()
    wall = time.perf_counter()
    for sequence, (stage, raw) in enumerate(continuation(task, cycles)):
        if stage == 'terminal':
            owner.flush(lane, timeout=None)
        started[sequence] = time.perf_counter()
        event = {**raw, 'seq': sequence, 'taskId': task['id'],
                 'attemptId': task['_attemptId']}
        source, source_bytes = turns.capture_task_projection_snapshot(
            task, event, previous_source=previous, previous_bytes=previous_bytes)
        previous, previous_bytes = source, source_bytes
        capture_ms.append((time.perf_counter() - started[sequence]) * 1000)
        entry = turns.TaskEventBatchEntry(raw_event=event, sequence=sequence,
            projection_source=source, task_event={'task_id': task['id'],
                'sequence': sequence, 'event': project_persistent_event(task['id'], event)})

        def persist(entry=entry, stage=stage):
            if stage == 'terminal':
                result = turns.record_task_event(task, entry.raw_event,
                    task_event=entry.task_event, projection_source=entry.projection_source)
                if result != 'carried':
                    raise ValueError(f'Rejected terminal: {result}')
                acknowledged[entry.sequence] = time.perf_counter()
            else:
                persist_many([entry])

        def publish(sequence=sequence):
            at = time.perf_counter()
            if sequence not in acknowledged or at < acknowledged[sequence]:
                raise ValueError('Publication preceded durable acknowledgement')
            pushed[sequence] = at
            observed.append(sequence)

        batch = {} if stage == 'terminal' else {
            'batch_scope': task['id'], 'persist_batch': persist_many,
            'batch_entry': lambda frames, entry=entry: entry}
        owner.submit(lane, sequence, retained_snapshot_bytes(event), persist, publish,
                     projection_source=source, projection_bytes=source_bytes, **batch)
        stages.append(stage)
        if submission == 'barrier' or stage == 'terminal':
            owner.flush(lane, timeout=None)
    elapsed_ms = (time.perf_counter() - wall) * 1000
    events = len(stages)
    work_digest, raw_digest = read_work(client, task, events, revision + events)
    locator = {key: task[key] for key in ('id', 'convId', '_turnId', '_attemptId', '_userId')}
    result = {'conversation': row['conversation'], 'fixture_sha256': row['sha256'],
              'projection_bytes': row['projection_bytes'], 'rounds': row['rounds'],
              'events': events, 'digest': work_digest, 'seed_ms': seed_ms,
              'elapsed_ms': elapsed_ms, 'capture_ms': capture_ms, 'event_stages': stages,
              'clock_window_seconds': [wall, wall + elapsed_ms / 1000],
              'event_ms': [(pushed[i] - started[i]) * 1000 for i in range(events)],
              'push_ordered': observed == list(range(events)), 'recovered': False}
    return result, (locator, events, revision + events, raw_digest)


def measure(args):
    from benchmarks.conversation_authority import ConversationAuthority
    from benchmarks.conversation_concurrency import capture_storage_metrics
    from lib.tasks_pkg.event_delivery import EventDeliveryCoordinator
    if args.reference_normalizer:
        install_reference(args.reference_normalizer)
    manifest_bytes = (args.corpus / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    if args.conversations:
        selected = set(args.conversations.split(','))
        manifest = [row for row in manifest if row['conversation'] in selected]
        if len(manifest) != len(selected):
            raise ValueError('Requested conversation missing from corpus')
    if not manifest:
        raise ValueError('Empty corpus')
    concurrent = getattr(args, 'concurrent', False)
    args.directory.mkdir(parents=True, exist_ok=True)
    report = {'workload': {'corpus_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                           'submission': args.submission, 'cycles': args.cycles,
                           'concurrent': concurrent,
                           'conversations': [row['conversation'] for row in manifest]},
              'backend': args.backend, 'recovery': 'process_sigkill_reopen',
              'reference_normalizer': None if not args.reference_normalizer else
                  hashlib.sha256(args.reference_normalizer.read_bytes()).hexdigest(),
              'binary_sha256': hashlib.sha256(Path(os.environ['TOFUDB_BIN']).read_bytes()).hexdigest()
                  if args.backend == 'tofudb' else None,
              'sources': {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
                  'benchmarks/storage_history_cohort.py', 'benchmarks/turn_event_batch.py',
                  'lib/turn_lifecycle.py', 'lib/turn_projection_segments.py',
                  'lib/tasks_pkg/event_delivery.py', 'lib/conversation_sync/settled_rows.py')},
              'cases': []}
    recoveries = []
    binary = Path(os.environ['TOFUDB_BIN']) if args.backend == 'tofudb' else None
    engine = ConversationAuthority(args.backend, binary, args.directory)
    try:
        engine.start()
        report['authority'] = engine.describe()
        report['artifact_before'] = engine.artifact_before
        report['storage_metrics_before'] = capture_storage_metrics(
            engine, incarnation=0, boundary='before_seeding', quiescent=True)
        owner = EventDeliveryCoordinator(profile_every=0)
        try:
            with patch('lib.storage.get_storage_client', lambda **kwargs: engine._client):
                def load_fixture(row):
                    data = (args.corpus / f'{row["conversation"]}.json').read_bytes()
                    if hashlib.sha256(data).hexdigest() != row['sha256']:
                        raise ValueError('Changed corpus fixture')
                    return json.loads(data)

                def retain(result):
                    case, recovery = result
                    report['cases'].append(case)
                    recoveries.append(recovery)
                    print(json.dumps({'conversation': case['conversation'],
                                      'elapsed_ms': round(case['elapsed_ms'], 1)}), flush=True)

                if concurrent:
                    fixtures = [load_fixture(row) for row in manifest]
                    ready = threading.Barrier(len(manifest))

                    def run(fixture, row):
                        try:
                            return run_case(engine._client, owner, fixture, row,
                                cycles=args.cycles, submission=args.submission, ready=ready)
                        except BaseException:
                            ready.abort()
                            raise

                    # The explicit workload starts every participant; no host
                    # capacity probe sizes or queues this cohort.
                    with ThreadPoolExecutor(max_workers=len(manifest)) as executor:
                        try:
                            futures = [executor.submit(run, fixture, row)
                                       for fixture, row in zip(fixtures, manifest)]
                        except BaseException:
                            ready.abort()
                            raise
                        for future in futures:
                            retain(future.result())
                else:
                    for row in manifest:
                        retain(run_case(engine._client, owner, load_fixture(row), row,
                                        cycles=args.cycles, submission=args.submission))
        finally:
            owner.close()
        report['storage_metrics_after'] = capture_storage_metrics(
            engine, incarnation=0, boundary='after_cases', quiescent=True)
        old_pid = engine.pid
        report['reopen_ms'] = engine.crash_cycle()
        report['engine_pids'] = [old_pid, engine.pid]
        if report['engine_pids'][0] == report['engine_pids'][1]:
            raise ValueError('No engine restart')
        for case, (locator, events, revision, raw_digest) in zip(report['cases'], recoveries):
            _, reopened = read_work(engine._client, locator, events, revision)
            if reopened != raw_digest:
                raise ValueError('Crash readback changed durable work')
            case['recovered'] = True
        report['integrity'] = engine.verify(timeout=180)
        if report['integrity']['integrity_returncode'] != 0:
            raise ValueError('Offline integrity check failed')
        report['artifact_after'] = engine.artifact_snapshot()
        if report['artifact_after'] != report['artifact_before']:
            raise ValueError('Pinned engine artifact changed during collection')
        if any(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != value
               for path, value in report['sources'].items()):
            raise ValueError('Measurement sources changed during collection')
    except BaseException as error:
        retain_failure(report, engine, args.output, error)
        raise
    finally:
        engine.stop()
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--backend', choices=('tofudb', 'sqlite'), default='tofudb')
    parser.add_argument('--reference-normalizer', type=Path)
    parser.add_argument('--conversations', help='Explicit pilot subset; default is the complete corpus')
    parser.add_argument('--submission', choices=('barrier', 'queued'), default='barrier')
    parser.add_argument('--concurrent', action='store_true',
                        help='Start all continuations together after every history is seeded')
    parser.add_argument('--cycles', type=int, default=2)
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error('--cycles must be positive')
    if os.environ.get('TOFU_DB_REQUEST_PROFILE_EVERY', '0') != '0':
        parser.error('Latency runs require native request profiling off')
    measure(args)


if __name__ == '__main__':
    main()
