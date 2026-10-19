"""Paired native request-count clocks with separate memory processes.

This is a count-kernel comparison, not whole-agent or system admission.
Build both frozen variants with the identical token_scratch_measure example.
Independent warmed workers alternate 20-operation batches on the same CPU.
Each case has separate RSS workers. Cold clocks are diagnostics, not a
fresh-process first-use certification cohort.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import selectors
import statistics
import subprocess
import tempfile

SOURCES = ('Cargo.toml', 'Cargo.lock', 'packages/tofu-runtime/Cargo.toml',
           'packages/tofu-runtime/src/lib.rs',
           'packages/tofu-runtime/src/token_ingress.rs',
           'packages/tofu-runtime/src/token_rank_counter.rs',
           'packages/tofu-runtime/src/token_merge_heap.rs',
           'packages/tofu-runtime/src/token_ranks/cl100k_base.rs',
           'packages/tofu-runtime/src/token_ranks/o200k_base.rs',
           'packages/tofu-runtime/src/execution_contract.rs',
           'packages/tofu-runtime/examples/token_scratch_measure.rs')
OPTIONAL_SOURCES = ('packages/tofu-runtime/src/token_pair_ranks.rs',
                    'packages/tofu-runtime/src/token_merge_linear.rs')


def source_witness(root):
    witness = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCES}
    for name in OPTIONAL_SOURCES:
        path = root / name
        witness[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return witness


def trial(binary, cpu, samples, memory=False, encoding=None, shape=None):
    command = ['taskset', '-c', str(cpu), str(binary)] + (['--memory'] if memory else [])
    if encoding is not None:
        command += ['--encoding', encoding, '--case', shape]
    environment = dict(os.environ, TOFU_TOKEN_SCRATCH_SAMPLES=str(samples))
    with subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            output, errors = process.communicate(timeout=180)
        except BaseException:
            process.kill()
            process.communicate()
            raise
    if process.returncode:
        raise RuntimeError(errors.decode(errors='replace'))
    rows = [json.loads(line) for line in output.splitlines()]
    expected_rows = 14 if encoding is None else 1
    assert len(rows) == expected_rows
    assert len({(row['encoding'], row['shape']) for row in rows}) == expected_rows
    for row in rows:
        assert row['memory_worker'] is memory and row['samples'] == samples
        assert row['warmup_operations'] == 20
        assert len(row['wall_ns']) == len(row['cpu_ns']) == (0 if memory else samples)
    return {'pid': process.pid, 'rows': rows}


def read_frame(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout=30):
            raise TimeoutError('native benchmark worker did not respond')
        line = process.stdout.readline()
    if not line:
        raise RuntimeError('native benchmark worker closed before its receipt')
    return json.loads(line)


def controlled_pair(binaries, cpu, samples, encoding, shape, pair, block):
    children, logs, rows = {}, {}, {}
    try:
        for arm, binary in binaries.items():
            logs[arm] = tempfile.TemporaryFile()
            command = ['taskset', '-c', str(cpu), str(binary), '--worker',
                       '--encoding', encoding, '--case', shape]
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=logs[arm], text=True)
            children[arm] = process
            ready = read_frame(process)
            assert ready['ready'] and ready['pid'] == process.pid
            assert ready['encoding'] == encoding and ready['shape'] == shape
            rows[arm] = dict(ready, wall_ns=[], cpu_ns=[], samples=samples, memory_worker=False)
        for batch, offset in enumerate(range(0, samples, block)):
            operations = min(block, samples - offset)
            order = ('baseline', 'candidate') if (pair + batch) % 2 == 0 else ('candidate', 'baseline')
            for arm in order:
                process = children[arm]
                process.stdin.write(str(operations) + '\n')
                process.stdin.flush()
                result = read_frame(process)
                assert len(result['wall_ns']) == len(result['cpu_ns']) == operations
                for metric in ('wall_ns', 'cpu_ns'):
                    rows[arm][metric].extend(result[metric])
        for arm, process in children.items():
            process.stdin.write('0\n')
            process.stdin.flush()
            assert process.wait(timeout=30) == 0
        return rows
    finally:
        for process in children.values():
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()
        for log in logs.values():
            log.close()


def measure(baseline, candidate, pairs, cpu, samples, reference_root, candidate_root):
    sources = {'baseline': source_witness(reference_root), 'candidate': source_witness(candidate_root)}
    example = 'packages/tofu-runtime/examples/token_scratch_measure.rs'
    if sources['baseline'][example] != sources['candidate'][example]:
        raise ValueError('benchmark fixtures and instrumentation differ')
    binaries = {name: {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                for name, path in [('baseline', baseline), ('candidate', candidate)]}
    if binaries['baseline']['sha256'] == binaries['candidate']['sha256']:
        raise ValueError('identical benchmark binaries cannot compare a native implementation change')
    raw = []
    cases = [(encoding, shape) for encoding in ('cl100k_base', 'o200k_base')
             for shape in ('small', 'prose', 'code', 'unicode', 'short_unmerged', 'mixed', 'long_piece')]
    for pair in range(pairs):
        order = ['baseline', 'candidate'] if pair % 2 == 0 else ['candidate', 'baseline']
        record = {'pair': pair, 'order': order}
        for arm in order:
            record[arm] = {'rows': [], 'memory': {'rows': []}}
        for encoding, shape in cases:
            rows = controlled_pair({'baseline': baseline, 'candidate': candidate}, cpu,
                                   samples, encoding, shape, pair, block=20)
            for arm in order:
                binary = baseline if arm == 'baseline' else candidate
                memory = trial(binary, cpu, samples, memory=True, encoding=encoding, shape=shape)
                rows[arm]['peak_rss_bytes'] = memory['rows'][0]['peak_rss_bytes']
                record[arm]['rows'].append(rows[arm])
                record[arm]['memory']['rows'].append(dict(memory['rows'][0], pid=memory['pid']))
            print('completed', pair + 1, encoding, shape, flush=True)
        raw.append(record)
    summary = []
    failures = []
    improvements = []
    for index, first in enumerate(raw[0]['baseline']['rows']):
        ratios = []
        for record in raw:
            metrics = {}
            for arm in ('baseline', 'candidate'):
                row = record[arm]['rows'][index]
                memory = record[arm]['memory']['rows'][index]
                for matched in (row, memory):
                    assert (matched['encoding'], matched['shape'], matched['tokens']) == (
                        first['encoding'], first['shape'], first['tokens'])
                wall = sorted(row['wall_ns'])
                metrics[arm] = {'p50_ns': statistics.median(wall), 'p95_ns': wall[int((samples-1)*.95)],
                                'p99_ns': wall[int((samples-1)*.99)], 'cpu_ns': statistics.mean(row['cpu_ns']),
                                'peak_rss_bytes': memory['peak_rss_bytes']}
            ratios.append({key: metrics['candidate'][key] / metrics['baseline'][key]
                           for key in metrics['baseline']})
        key = {'encoding': first['encoding'], 'shape': first['shape']}
        summary.append(dict(key, ratios=ratios))
        for pair, metrics in enumerate(ratios):
            for metric, ratio in metrics.items():
                if ratio > 1.05:
                    failures.append(dict(key, pair=pair, metric=metric, ratio=ratio))
        for metric in ratios[0]:
            if all(pair[metric] <= .9 for pair in ratios):
                improvements.append(dict(key, metric=metric))
    for name, path in [('baseline', baseline), ('candidate', candidate)]:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == binaries[name]['sha256']
    assert sources == {'baseline': source_witness(reference_root), 'candidate': source_witness(candidate_root)}
    return {'scope': __doc__, 'pairs': pairs, 'samples': samples, 'cpu': cpu, 'block_operations': 20,
            'platform': platform.platform(),
            'component_verdict': 'regression' if failures else 'improved' if improvements else 'no_gain',
            'guard_failures': failures, 'improvements': improvements,
            'sources': sources, 'binaries': binaries, 'summary': summary, 'raw': raw}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--samples', type=int, default=1000)
    parser.add_argument('--cpu', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference-root', type=Path, required=True)
    parser.add_argument('--candidate-root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    if args.pairs < 3 or not 100 <= args.samples <= 10000 or args.cpu not in os.sched_getaffinity(0):
        parser.error('at least three pairs, 100..10000 samples and one allowed CPU are required')
    report = measure(args.baseline.resolve(strict=True), args.candidate.resolve(strict=True),
                     args.pairs, args.cpu, args.samples, args.reference_root, args.candidate_root)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    for row in report['summary']:
        print(row['encoding'], row['shape'],
              [round(pair['p50_ns'], 3) for pair in row['ratios']])
