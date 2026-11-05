"""Compare identity admission with a frozen pipeline over identical occurrences.

Entry: --baseline-source <old _pipeline.py> --output <report.json>.
Dependencies: production identity/receipt owners. Fixtures, UUID sequences and
log sinks are controlled equally. Preparation, exact parity checks and allocation
measurement are outside timing. This measures admission, not execution or task
latency; settlement still computes the canonical receipt signature.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import statistics
import sys
import threading
import time
import tracemalloc
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class LogSink:
    def __init__(self, *, capture=False):
        self.events = [] if capture else None

    def info(self, *args):
        if self.events is not None:
            self.events.append(('info', args))

    def warning(self, *args):
        if self.events is not None:
            self.events.append(('warning', args))


CASES = (
    ('single_fresh', 1, 16), ('wide_fresh', 128, 64),
    ('large_fresh', 32, 16384), ('historical_same', 8, 64),
    ('historical_changed', 8, 64), ('historical_evicted', 8, 64),
    ('duplicate_fresh', 16, 64), ('invalid_arguments', 8, 64),
)


def fixture(case):
    from lib.tasks_pkg.tool_dispatch._flags import _call_id_signature

    kind, count, size = case
    calls, receipts, history = [], {}, set()
    for index in range(count):
        call_id = f'call-{index // 2 if kind == "duplicate_fresh" else index}'
        arguments = {'path': f'/synthetic/{index}', 'content': '豆腐α\n' * size}
        if kind == 'invalid_arguments':
            arguments = [None, [], 'invalid', 42][index % 4]
        row = {'toolCallId': call_id, 'roundNum': index + 1}
        call = {'id': call_id, 'type': 'function', 'function': {
            'name': 'write_file', 'arguments': json.dumps(arguments, ensure_ascii=False)}}
        calls.append((call, 'write_file', call_id, arguments, index + 1, row,
                      'malformed arguments' if kind == 'invalid_arguments' else None))
        if kind.startswith('historical'):
            history.add(call_id)
            if kind != 'historical_evicted':
                receipts[call_id] = {
                    'name': 'write_file', 'status': 'done',
                    'signature': _call_id_signature(
                        'write_file', {'content': 'old'}
                        if kind == 'historical_changed' else arguments)}
    task = {'id': 'identity-benchmark', '_tool_call_id_receipts': receipts,
            '_toolResultReceiptLock': threading.RLock(),
            '_tool_result_cache': {}, '_tool_result_cache_capacity': 1024}
    sequence = itertools.count(1)
    return {
        'task': task, 'messages': [], 'calls': calls, 'receipts': receipts,
        'historical_used_ids': history, 'used_ids': set(history), 'claimed_ids': set(),
        'settled_results': {key: 'old settled result' for key in history},
        'pending_envelopes': {key: {'old': True} for key in history},
        'uuid': SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=f'{next(sequence):032x}'[::-1])),
    }


def run(module, state):
    module.uuid = state['uuid']
    for index, item in enumerate(state['calls']):
        module._repair_current_tool_call_identity(
            state['task'], state['messages'], state['calls'], index, item,
            round_num=2, tid='identity-benchmark', receipts=state['receipts'],
            historical_used_ids=state['historical_used_ids'], used_ids=state['used_ids'],
            claimed_ids=state['claimed_ids'], settled_results=state['settled_results'],
            pending_envelopes=state['pending_envelopes'])


def snapshot(state):
    task = {key: value for key, value in state['task'].items()
            if key != '_toolResultReceiptLock'}
    return {key: (task if key == 'task' else sorted(value) if isinstance(value, set) else value)
            for key, value in state.items() if key != 'uuid'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def assert_parity(baseline, candidate, case):
    before, after = fixture(case), fixture(case)
    input_hash = digest(snapshot(before))
    baseline.logger, candidate.logger = LogSink(capture=True), LogSink(capture=True)
    run(baseline, before)
    run(candidate, after)
    expected, actual = snapshot(before), snapshot(after)
    if expected != actual or baseline.logger.events != candidate.logger.events:
        raise ValueError(f'Identity or diagnostic drift in {case[0]}')
    return {'input_sha256': input_hash, 'output_sha256': digest(actual),
            'diagnostics_sha256': digest(candidate.logger.events)}


def compare(baseline, candidate, case, *, samples, repeats):
    parity = assert_parity(baseline, candidate, case)
    modules = {'baseline': baseline, 'candidate': candidate}
    observations = {arm: [] for arm in modules}
    for module in modules.values():
        module.logger = LogSink()
        for _ in range(4):
            run(module, fixture(case))
    for sample in range(samples):
        order = ('baseline', 'candidate') if sample % 2 == 0 else ('candidate', 'baseline')
        for arm in order:
            states = [fixture(case) for _ in range(repeats)]
            start_wall, start_cpu = time.perf_counter_ns(), time.process_time_ns()
            for state in states:
                run(modules[arm], state)
            observations[arm].append({
                'cpu_ns': (time.process_time_ns() - start_cpu) / repeats,
                'wall_ns': (time.perf_counter_ns() - start_wall) / repeats})
    result = {'case': case, **parity, 'samples': observations, 'repeats': repeats}
    for arm, module in modules.items():
        result[arm] = {clock: statistics.median(row[clock] for row in observations[arm])
                       for clock in ('cpu_ns', 'wall_ns')}
        state = fixture(case)
        tracemalloc.start()
        try:
            run(module, state)
            result[arm]['peak_bytes'] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    result['cpu_speedup'] = result['baseline']['cpu_ns'] / result['candidate']['cpu_ns']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=21)
    parser.add_argument('--repeats', type=int, default=16)
    args = parser.parse_args()
    if min(args.samples, args.repeats) < 1:
        parser.error('samples and repeats must be positive')
    from lib.tasks_pkg.tool_dispatch import _pipeline as candidate
    spec = importlib.util.spec_from_file_location('identity_baseline', args.baseline_source)
    if spec is None or spec.loader is None:
        parser.error('baseline source cannot be loaded')
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    result = {
        'scope': 'Tool identity admission only; no execution or task latency claim.',
        'source_sha256': {label: hashlib.sha256(path.read_bytes()).hexdigest()
                          for label, path in {
                              'baseline': args.baseline_source,
                              'candidate': Path(candidate.__file__),
                              'receipt_owner': ROOT / 'lib/tasks_pkg/tool_dispatch/_flags.py',
                              'benchmark': Path(__file__),
                          }.items()},
        'cases': [compare(baseline, candidate, case, samples=args.samples, repeats=args.repeats)
                  for case in CASES],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    for row in result['cases']:
        print(f'{row["case"][0]}: CPU {row["baseline"]["cpu_ns"] / 1e6:.4f} -> '
              f'{row["candidate"]["cpu_ns"] / 1e6:.4f} ms; {row["cpu_speedup"]:.2f}x')


if __name__ == '__main__':
    main()
