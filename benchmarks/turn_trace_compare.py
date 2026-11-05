"""Frozen-source trace comparison: exact full/bounded outputs before clocks."""
from __future__ import annotations
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import statistics
import sys
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def make_rows(count, variant='parallel', seed=0):
    rng = random.Random(seed)
    rows = []

    def emit(kind, **payload):
        timestamp = 1000 + len(rows) * 20
        if variant == 'clock_regression' and len(rows) % 7 == 0:
            timestamp -= 100
        if variant == 'monotonic':
            payload.update(executionClockId='clock-1', executionOriginAtMs=900)
        rows.append({'event_id': len(rows), 'type': kind, 'ts_ms': timestamp,
                     'payload': {'type': kind, **payload}})

    emit('round_start', roundNum=1)
    emit('phase', phase='waiting_model')
    emit('phase', phase='waiting_model')
    emit('model_request_start', roundNum=1, spanId='logical', requestLevel='logical')
    emit('model_request_start', roundNum=1, spanId='attempt', parentSpanId='logical',
         requestLevel='provider_attempt', attempt=1, attemptScope='dispatcher')
    emit('delta')
    emit('model_request_complete', roundNum=1, spanId='attempt', status='failed')
    emit('phase', phase='retrying', attempt=2)
    emit('phase', phase='stream_stalled')
    emit('model_request_start', roundNum=1, spanId='attempt', parentSpanId='logical',
         requestLevel='provider_attempt', attempt=2, attemptScope='dispatcher')
    emit('model_request_complete', roundNum=1, spanId='attempt', status='succeeded')
    emit('model_request_complete', roundNum=1, spanId='logical', status='succeeded')
    emit('round_usage', roundNum=1, tokensIn=100, tokensOut=30,
         usage={'stream_elapsed_ms': 100})
    emit('round_end', roundNum=1)
    ids = [f'call-{i // 2 if variant == "duplicates" else i}' for i in range(count)]
    for index, call_id in enumerate(ids):
        if index % 3 == 0:
            emit('write_approval_request', roundNum=1, toolCallId=call_id)
        emit('tool_start', roundNum=1, toolCallId=call_id, toolName='read_files')
    for index in rng.sample(range(count), count):
        call_id = ids[index]
        emit('tool_progress', roundNum=1, toolCallId=call_id,
             execStartTs=1000 + len(rows) * 20)
        emit('workspace_admission_wait', roundNum=1, toolCallId=call_id,
             waitStartedAtMs=1000, waitEndedAtMs=1000 + len(rows) * 20,
             outcome='admitted', toolName='read_files')
        if variant == 'missing' and index % 4 == 0:
            continue
        emit('tool_complete', roundNum=1, toolCallId=call_id, isError=index % 5 == 0)
    emit('phase', phase='compacting')
    emit('compaction_done')
    emit('step_start', node_id='node-1', name='plan')
    emit('step_start', node_id='node-2', name='execute')
    emit('step_complete', node_id='node-2', status='done')
    emit('round_start', roundNum=2)
    emit('tool_start', roundNum=2, toolCallId='call-0', toolName='read_files')
    emit('tool_complete', roundNum=2, toolCallId='call-0')
    emit('round_end', roundNum=2)
    if variant != 'running':
        emit('done')
    if variant == 'orphan':
        rows = [row for row in rows if row['type'] not in
                ('round_start', 'tool_start', 'model_request_start', 'step_start')]
    return rows


def fold(module, rows, *, full=False):
    original_reader = module._read_trace_rows
    module._read_trace_rows = lambda _: rows
    original = module.compact_trace_document
    ledger = {}
    if full:
        module.compact_trace_document = lambda document, **kwargs: document
    try:
        result = module._fold_task_trace('comparison', now_ms=1000 + len(rows) * 25,
                                         ledger_out=ledger)
    finally:
        module.compact_trace_document = original
        module._read_trace_rows = original_reader
    return result, ledger


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def verify(baseline, candidate, rows):
    before = copy.deepcopy(rows)
    for full in (True, False):
        expected, actual = fold(baseline, rows, full=full), fold(candidate, rows, full=full)
        if expected != actual:
            raise ValueError(f'Trace output drift (full={full}, events={len(rows)})')
        if rows != before:
            raise ValueError('Trace fold mutated authority input rows')
    return fingerprint(actual)


def measure(baseline, candidate, count, *, samples=11):
    rows = make_rows(count)
    output_hash = verify(baseline, candidate, rows)
    arms = {'baseline': baseline, 'candidate': candidate}
    raw = {name: [] for name in arms}
    for module in arms.values():
        for _ in range(3):
            fold(module, rows)
    for sample in range(samples):
        for arm in (('baseline', 'candidate') if sample % 2 == 0 else ('candidate', 'baseline')):
            start_wall, start_cpu = time.perf_counter_ns(), time.process_time_ns()
            fold(arms[arm], rows)
            raw[arm].append({'wall_ns': time.perf_counter_ns() - start_wall,
                             'cpu_ns': time.process_time_ns() - start_cpu})
    result = {'tools': count, 'events': len(rows), 'samples': raw,
              'input_sha256': fingerprint(rows), 'output_sha256': output_hash}
    for arm, module in arms.items():
        result[arm] = {clock: statistics.median(row[clock] for row in raw[arm])
                       for clock in ('cpu_ns', 'wall_ns')}
        tracemalloc.start()
        try:
            fold(module, rows)
            result[arm]['peak_bytes'] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=11)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('samples must be positive')
    from lib.tasks_pkg import turn_trace as candidate
    spec = importlib.util.spec_from_file_location('lib.tasks_pkg._trace_comparison_baseline', args.baseline_source)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    variants = ('parallel', 'duplicates', 'missing', 'orphan', 'running', 'clock_regression', 'monotonic')
    checks = []
    for variant in variants:
        for seed in range(15):
            rows = make_rows(seed * 3, variant, seed)
            checks.append({'variant': variant, 'seed': seed,
                           'output_sha256': verify(baseline, candidate, rows)})
    checks.append({'variant': 'empty', 'output_sha256': verify(baseline, candidate, [])})
    result = {'scope': 'Pure read-free trace fold; no task/provider latency claim.',
              'parity': checks, 'cases': [measure(baseline, candidate, count, samples=args.samples) for count in (1, 32, 256, 1024)],
              'source_sha256': {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in {
                  'baseline': args.baseline_source, 'candidate': Path(candidate.__file__),
                  'span_owner': Path(candidate.__file__).with_name('turn_trace_spans.py'),
                  'benchmark': Path(__file__),
                  'ledger': ROOT / 'lib/tasks_pkg/turn_trace_ledger.py',
                  'contract': ROOT / 'lib/turn_trace_contract.py',
              }.items()}}
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print('Exact full/bounded trace and ledger parity:', len(checks), 'cases')
    for row in result['cases']:
        print(row['tools'], 'tools; CPU ms:', round(row['baseline']['cpu_ns']/1e6,3),
              '->', round(row['candidate']['cpu_ns']/1e6,3))


if __name__ == '__main__':
    main()
