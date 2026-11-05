"""Paired fingerprint comparison clocks over identical synthetic snapshots.

Run with --baseline-source pointing at a frozen wire_fingerprint_diff.py, the
self-contained comparison owner, from the baseline commit. A frozen
wire_fingerprint.py re-imports the live comparison module, so it is refused.
Compare complete outputs before timing, alternate arm order,
and measure allocations separately. These are component clocks, not task or
provider latency. Source hashes and raw samples accompany every result.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_baseline(path: Path):
    spec = importlib.util.spec_from_file_location('wire_comparison_baseline', path)
    if spec is None or spec.loader is None:
        raise ValueError(f'Cannot load baseline: {path}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def snapshots(size: int, change: str) -> tuple[list, list]:
    def rows():
        return [{'key': f'message-{i}', 'fields': {
            'role': i % 3, 'content': 104729 * i, 'thinking': 15485863 + i,
            'signature': i + 32452843, 'tool_calls': i + 49979687,
            'tool_result': i + 67867967,
        }} for i in range(size)]

    before, after = rows(), rows()
    if change == 'last':
        after[-1]['fields']['content'] += 1
    elif change == 'all':
        for row in after:
            row['fields']['content'] += 1
    elif change == 'shorter':
        after.pop()
    return before, after


def measure(function, inputs, repeats: int) -> dict:
    start_wall, start_cpu = time.perf_counter_ns(), time.process_time_ns()
    for _ in range(repeats):
        function(*inputs)
    return {
        'wall_ns': (time.perf_counter_ns() - start_wall) / repeats,
        'cpu_ns': (time.process_time_ns() - start_cpu) / repeats,
    }


def peak_bytes(function, inputs) -> int:
    tracemalloc.start()
    try:
        function(*inputs)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def compare_case(baseline, candidate, inputs, samples: int, repeats: int) -> dict:
    if baseline is candidate:
        raise ValueError('Baseline and candidate are the same implementation; '
                         'freeze lib/tasks_pkg/wire_fingerprint_diff.py instead')
    expected = baseline(*inputs)
    actual = candidate(*inputs)
    if expected != actual:
        raise ValueError(f'Comparison changed diagnostic output: {expected!r} != {actual!r}')
    for _ in range(3):
        baseline(*inputs)
        candidate(*inputs)
    functions = {'baseline': baseline, 'candidate': candidate}
    observations = {name: [] for name in functions}
    for index in range(samples):
        order = ('baseline', 'candidate') if index % 2 == 0 else ('candidate', 'baseline')
        for name in order:
            observations[name].append(measure(functions[name], inputs, repeats))
    result = {'output': expected, 'repeats': repeats, 'samples': observations}
    for name, function in functions.items():
        result[name] = {
            metric: statistics.median(sample[metric] for sample in observations[name])
            for metric in ('wall_ns', 'cpu_ns')
        }
        result[name]['peak_bytes'] = peak_bytes(function, inputs)
    result['cpu_speedup'] = result['baseline']['cpu_ns'] / result['candidate']['cpu_ns']
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-source', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=11)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 3:
        parser.error('--samples must be at least 3')
    baseline = load_baseline(args.baseline_source)
    from lib.tasks_pkg import wire_fingerprint as candidate

    sources = [args.baseline_source, Path(candidate.__file__),
               ROOT / 'lib/tasks_pkg/wire_fingerprint_diff.py', Path(__file__)]
    digests = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    results = {}
    for size, change in ((16, 'same'), (1000, 'same'), (10000, 'same'),
                         (10000, 'last'), (10000, 'all'), (10000, 'shorter')):
        inputs = snapshots(size, change)
        repeats = 100 if size == 16 or change == 'all' else 10
        for name in ('diff_canonical', 'diff_byte_field_prefix'):
            results[f'{name}/{size}/{change}'] = compare_case(
                getattr(baseline, name), getattr(candidate, name), inputs,
                args.samples, repeats,
            )
    if digests != {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}:
        raise RuntimeError('A measured source changed during the run')
    report = {'scope': 'fingerprint comparison only', 'python': sys.version,
              'source_sha256': digests, 'cases': results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(f'Wrote {len(results)} verified comparison cases to {args.output}')


if __name__ == '__main__':
    main()
