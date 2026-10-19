"""Fresh-process first-use diagnostics for the owned native count encoder.

One selected encoding per process; no warm operations precede the clock.
Allocator instrumentation matches on both sides. This component fixture does
not establish HTTP, agent, provider or whole-system migration admission.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess

try:
    from .token_scratch import source_witness
except ImportError:
    from token_scratch import source_witness

EXAMPLE = 'packages/tofu-runtime/examples/token_ingress_measure.rs'
FIXTURE = 'packages/tofu-runtime/tests/fixtures/python_token_ingress.json'
ENCODINGS = ('cl100k_base', 'o200k_base')


def witness(root):
    sources = source_witness(root)
    for name in (EXAMPLE, FIXTURE):
        sources[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    return sources


def trial(binary, encoding, cpu):
    command = ['taskset', '-c', str(cpu), str(binary), '--cold-only', '--encoding', encoding]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as worker:
        try:
            output, errors = worker.communicate(timeout=180)
        except BaseException:
            worker.kill()
            worker.communicate()
            raise
        if worker.returncode:
            raise RuntimeError(errors.decode(errors='replace'))
        rows = [json.loads(line) for line in output.splitlines()]
        if len(rows) != 1 or rows[0]['pid'] != worker.pid:
            raise ValueError('first-use worker identity or receipt missing')
    return rows[0]


def summarize(raw):
    seen = set()
    summary, failures = {}, []
    for encoding in ENCODINGS:
        records = raw[encoding]
        if len(records) < 100:
            raise ValueError('each encoding requires at least 100 independent fresh workers per arm')
        for record in records:
            for arm in ('baseline', 'candidate'):
                row = record[arm]
                pid = row['pid']
                if (type(pid) is not int or pid <= 0 or pid in seen or
                        row['encoding'] != encoding or row['cold_only'] is not True or row['calls'] != 0):
                    raise ValueError('workers must be unique, fresh and encoding scoped')
                seen.add(pid)
                for metric in ('cold_wall_ns', 'cold_cpu_ns'):
                    if type(row[metric]) is not int or row[metric] <= 0:
                        raise ValueError('positive first-operation wall and CPU clocks required')
        summary[encoding] = {}
        for metric in ('cold_wall_ns', 'cold_cpu_ns'):
            values = {arm: sorted(record[arm][metric] for record in records)
                      for arm in ('baseline', 'candidate')}
            for label, quantile in (('p50', .5), ('p95', .95), ('p99', .99), ('mean', None)):
                clocks = {arm: statistics.mean(samples) if quantile is None else
                          statistics.median(samples) if quantile == .5 else
                          samples[int((len(samples) - 1) * quantile)]
                          for arm, samples in values.items()}
                result = dict(clocks, ratio=clocks['candidate'] / clocks['baseline'])
                key = metric + '_' + label
                summary[encoding][key] = result
                if result['ratio'] > 1.05:
                    failures.append(dict(encoding=encoding, metric=key, ratio=result['ratio']))
    return summary, failures


def measure(baseline, candidate, reference_root, candidate_root, samples, cpu):
    if samples < 100:
        raise ValueError('at least 100 fresh workers per encoding and variant required')
    roots = {'baseline': reference_root, 'candidate': candidate_root}
    sources = {arm: witness(root) for arm, root in roots.items()}
    for name in (EXAMPLE, FIXTURE):
        if sources['baseline'][name] != sources['candidate'][name]:
            raise ValueError('measurement code or Python fixture differs')
    paths = {'baseline': baseline, 'candidate': candidate}
    binaries = {arm: {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                for arm, path in paths.items()}
    if binaries['baseline']['sha256'] == binaries['candidate']['sha256']:
        raise ValueError('identical binaries cannot compare a change')
    raw = {encoding: [] for encoding in ENCODINGS}
    for encoding in ENCODINGS:
        for pair in range(samples):
            order = ('baseline', 'candidate') if pair % 2 == 0 else ('candidate', 'baseline')
            raw[encoding].append({arm: trial(paths[arm], encoding, cpu) for arm in order})
            if (pair + 1) % 20 == 0:
                print('completed cold', encoding, pair + 1, flush=True)
    summary, failures = summarize(raw)
    if sources != {arm: witness(root) for arm, root in roots.items()}:
        raise ValueError('source changed during measurement')
    for arm, path in paths.items():
        if binaries[arm]['sha256'] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError('binary changed during measurement')
    return dict(scope=__doc__, samples_per_encoding_per_arm=samples, cpu=cpu,
                sources=sources, binaries=binaries, raw=raw, summary=summary,
                guard_failures=failures, component_verdict='regression' if failures else 'pass')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--reference-root', type=Path, required=True)
    parser.add_argument('--candidate-root', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--cpu', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = measure(args.baseline, args.candidate, args.reference_root,
                     args.candidate_root, args.samples, args.cpu)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(dict(component_verdict=result['component_verdict'],
                         guard_failures=result['guard_failures'], summary=result['summary'])))
