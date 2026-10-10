#!/usr/bin/env python3
"""Whole-system performance admission; gaps never turn into passing verdicts.

Entry point: compare(reference, candidate), or CLI with two report paths.
Dependencies: system_performance_contract.json only. Measurements and domain
integration tests are supplied by the owning executable benchmarks; this gate
does not infer correctness from speed or microbenchmark coverage.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics

CONTRACT = json.loads(Path(__file__).with_name('system_performance_contract.json').read_text())


def compare(reference: dict, candidate: dict) -> dict:
    required = {(domain, case) for domain, cases in CONTRACT['scope'].items() for case in cases}
    issues: list[dict] = []
    improvements: list[dict] = []
    measured = set()
    reports = []
    for name, report in [('reference', reference), ('candidate', candidate)]:
        by_case = {}
        if report.get('format') != 'tofu.system-performance/v1':
            issues.append({'kind': 'invalid', 'variant': name, 'reason': 'report format'})
        for row in report.get('workloads', []):
            key = (row.get('domain'), row.get('case'))
            if key in by_case or key not in required:
                issues.append({'kind': 'invalid', 'variant': name, 'case': list(key), 'reason': 'duplicate or undeclared case'})
            by_case[key] = row
        reports.append(by_case)
    for key in sorted(required):
        rows = [report.get(key) for report in reports]
        if any(row is None for row in rows):
            issues.append({'kind': 'incomplete', 'case': list(key), 'reason': 'missing comparison'})
            continue
        valid = True
        for name, row in zip(('reference', 'candidate'), rows):
            if row.get('scope') != 'domain_integration' or row.get('correctness_passed') is not True or row.get('failure_and_cleanup_passed') is not True:
                issues.append({'kind': 'incomplete', 'variant': name, 'case': list(key), 'reason': 'integration/correctness/failure evidence'})
                valid = False
            identity = row.get('identity', {})
            if not all(identity.get(field) for field in ('source_sha256', 'runtime', 'hardware', 'fixture_sha256')):
                issues.append({'kind': 'invalid', 'variant': name, 'case': list(key), 'reason': 'missing measurement identity'})
                valid = False
            pairs = row.get('measurements', [])
            if len(pairs) < CONTRACT['comparison']['minimum_independent_pairs']:
                issues.append({'kind': 'incomplete', 'variant': name, 'case': list(key), 'reason': 'too few independent processes'})
                valid = False
            process_ids = [p.get('process_identity') for p in pairs]
            if not all(process_ids) or len(set(process_ids)) != len(process_ids):
                issues.append({'kind': 'invalid', 'variant': name, 'case': list(key), 'reason': 'reused or missing process identity'})
                valid = False
            for measurement in pairs:
                warmup = measurement.get(CONTRACT['comparison']['warmup_operations_field'])
                if warmup is None:
                    issues.append({'kind': 'incomplete', 'variant': name, 'case': list(key), 'reason': 'missing warmup count'})
                    valid = False
                elif type(warmup) is not int or warmup < 0:
                    issues.append({'kind': 'invalid', 'variant': name, 'case': list(key), 'reason': 'invalid warmup count'})
                    valid = False
                elif warmup < CONTRACT['comparison']['steady_state_warmup_operations_per_variant']:
                    issues.append({'kind': 'incomplete', 'variant': name, 'case': list(key), 'reason': 'insufficient warmup'})
                    valid = False
                raw = measurement.get('wall_samples_ms', [])
                if len(raw) < CONTRACT['comparison']['minimum_samples_per_variant']:
                    issues.append({'kind': 'incomplete', 'variant': name, 'case': list(key), 'reason': 'too few raw samples'})
                    valid = False
                cpu_raw = measurement.get('cpu_samples_ms', [])
                numeric = list(raw) + list(cpu_raw) + [measurement.get(metric) for metric in CONTRACT['metrics']]
                if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in numeric):
                    issues.append({'kind': 'invalid', 'variant': name, 'case': list(key), 'reason': 'invalid metric'})
                    valid = False
                elif any(measurement[metric] <= 0 for metric in CONTRACT['metrics'] if metric != 'retained_bytes') or any(value <= 0 for value in raw):
                    issues.append({'kind': 'invalid', 'variant': name, 'case': list(key), 'reason': 'nonpositive metric'})
                    valid = False
                elif raw:
                    ordered = sorted(raw)
                    derived = {
                        'p50_ms': statistics.median(raw),
                        'p95_ms': ordered[int((len(raw) - 1) * .95)],
                        'p99_ms': ordered[int((len(raw) - 1) * .99)],
                        'throughput_per_second': len(raw) * 1000 / sum(raw),
                    }
                    if len(cpu_raw) != len(raw) or any(value <= 0 for value in cpu_raw):
                        issues.append({'kind': 'invalid', 'variant': name, 'case': list(key), 'reason': 'missing or invalid raw CPU samples'})
                        valid = False
                    else:
                        derived['cpu_ms_per_operation'] = statistics.mean(cpu_raw)
                    if any(not math.isclose(measurement[metric], value, rel_tol=.001, abs_tol=.000001) for metric, value in derived.items()):
                        issues.append({'kind': 'invalid', 'variant': name, 'case': list(key), 'reason': 'summary disagrees with raw samples'})
                        valid = False
                if measurement.get('profiler_enabled') is not False or measurement.get('tracemalloc_enabled') is not False or measurement.get('memory_separate_process') is not True:
                    issues.append({'kind': 'invalid', 'variant': name, 'case': list(key), 'reason': 'instrumented timing or inseparable memory'})
                    valid = False
        if not valid:
            continue
        if any(rows[0]['identity'][field] != rows[1]['identity'][field] for field in ('runtime', 'hardware', 'fixture_sha256')):
            issues.append({'kind': 'invalid', 'case': list(key), 'reason': 'unmatched environment or fixture'})
            continue
        if len(rows[0]['measurements']) != len(rows[1]['measurements']):
            issues.append({'kind': 'incomplete', 'case': list(key), 'reason': 'unmatched pair counts'})
            continue
        measured.add(key)
        limits = CONTRACT['admission']
        for metric in CONTRACT['metrics']:
            ratios = []
            for before, after in zip(rows[0]['measurements'], rows[1]['measurements']):
                baseline = before[metric]
                value = after[metric]
                ratios.append(value / baseline if baseline else (1.0 if value == 0 else math.inf))
            higher_better = metric == 'throughput_per_second'
            if higher_better:
                regression = any(ratio < 1 / limits['maximum_latency_ratio'] for ratio in ratios)
                improvement = all(ratio >= 1 / (1 - limits['minimum_primary_improvement_fraction']) for ratio in ratios)
            else:
                maximum = (limits['maximum_peak_rss_ratio'] if metric == 'peak_rss_bytes' else
                           limits['maximum_retained_bytes_ratio'] if metric == 'retained_bytes' else
                           limits['maximum_cpu_ratio'] if metric == 'cpu_ms_per_operation' else
                           limits['maximum_latency_ratio'])
                regression = any(ratio > maximum for ratio in ratios)
                improvement = all(ratio <= 1 - limits['minimum_primary_improvement_fraction'] for ratio in ratios)
            if regression:
                issues.append({'kind': 'regression', 'case': list(key), 'metric': metric,
                               'ratios': [ratio if math.isfinite(ratio) else 'unbounded' for ratio in ratios]})
            if improvement:
                improvements.append({'case': list(key), 'metric': metric, 'ratios': ratios})
    kinds = {issue['kind'] for issue in issues}
    verdict = ('invalid' if 'invalid' in kinds else 'regression' if 'regression' in kinds else
               'incomplete' if 'incomplete' in kinds else 'accepted' if improvements else 'no_gain')
    return {'format': 'tofu.system-performance-verdict/v1', 'verdict': verdict,
            'coverage': {'measured': len(measured), 'required': len(required)},
            'issues': issues, 'improvements': improvements}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference', type=Path)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = compare(json.loads(args.reference.read_text()), json.loads(args.candidate.read_text()))
    encoded = json.dumps(result, indent=2, allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end='')
    raise SystemExit(0 if result['verdict'] == 'accepted' else 1)


if __name__ == '__main__':
    main()
