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


def _cold_metrics(row, variant, case, issues):
    """Validate first-use receipts; warm clocks cannot certify activation cost."""
    cold = row.get(CONTRACT['comparison']['cold_start_field'])
    context = {'variant': variant, 'case': list(case)}
    if cold is None:
        issues.append(dict(context, kind='incomplete', reason='missing cold-start evidence'))
        return None
    if not isinstance(cold, dict):
        issues.append(dict(context, kind='invalid', reason='invalid cold-start evidence'))
        return None
    if cold.get('scope') != CONTRACT['comparison']['cold_start_scope']:
        issues.append(dict(context, kind='incomplete', reason='cold-start fresh-process scope'))
        return None
    wall, cpu, identities = (cold.get(field) for field in
                            ('wall_samples_ms', 'cpu_samples_ms', 'process_identities'))
    if not all(isinstance(value, list) for value in (wall, cpu, identities)):
        issues.append(dict(context, kind='invalid', reason='invalid cold-start sample arrays'))
        return None
    if len(wall) < CONTRACT['comparison']['minimum_cold_samples_per_variant']:
        issues.append(dict(context, kind='incomplete', reason='too few cold-start samples'))
        return None
    if (len(cpu) != len(wall) or len(identities) != len(wall)
            or not all(type(value) is str and value for value in identities)
            or len(set(identities)) != len(identities)):
        issues.append(dict(context, kind='invalid', reason='reused or missing cold-start process identity'))
        return None
    if (any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
            for value in wall + cpu)
            or cold.get('profiler_enabled') is not False
            or cold.get('tracemalloc_enabled') is not False):
        issues.append(dict(context, kind='invalid', reason='invalid or instrumented cold-start clock'))
        return None
    ordered = sorted(wall)
    return {'p50_ms': statistics.median(wall), 'p95_ms': ordered[int((len(wall)-1)*.95)],
            'p99_ms': ordered[int((len(wall)-1)*.99)], 'cpu_ms_per_operation': statistics.mean(cpu)}


def compare(reference: dict, candidate: dict) -> dict:
    required = {(domain, case) for domain, cases in CONTRACT['scope'].items() for case in cases}
    issues: list[dict] = []
    improvements: list[dict] = []
    measured = set()
    reports = []
    cold_processes = set()
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
    warm_processes = {measurement.get('process_identity')
                      for report in reports for row in report.values()
                      for measurement in row.get('measurements', [])
                      if isinstance(measurement.get('process_identity'), str)}
    for key in sorted(required):
        rows = [report.get(key) for report in reports]
        if any(row is None for row in rows):
            issues.append({'kind': 'incomplete', 'case': list(key), 'reason': 'missing comparison'})
            continue
        valid = True
        cold_metrics = []
        for name, row in zip(('reference', 'candidate'), rows):
            cold = _cold_metrics(row, name, key, issues)
            cold_metrics.append(cold)
            if cold is None:
                valid = False
            else:
                identities = set(row[CONTRACT['comparison']['cold_start_field']]['process_identities'])
                if identities & (cold_processes | warm_processes):
                    issues.append({'kind': 'invalid', 'variant': name, 'case': list(key),
                                   'reason': 'cold-start process reused across workloads, variants or warm workers'})
                    valid = False
                cold_processes.update(identities)
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
        field = CONTRACT['comparison']['cold_start_field']
        cold_rows = [row[field] for row in rows]
        if (len(cold_rows[0]['wall_samples_ms']) != len(cold_rows[1]['wall_samples_ms'])
                or set(cold_rows[0]['process_identities']) & set(cold_rows[1]['process_identities'])):
            issues.append({'kind': 'invalid', 'case': list(key), 'reason': 'unmatched or shared cold-start processes'})
            continue
        measured.add(key)
        limits = CONTRACT['admission']
        for metric in cold_metrics[0]:
            ratio = cold_metrics[1][metric] / cold_metrics[0][metric]
            maximum = limits['maximum_cpu_ratio'] if metric == 'cpu_ms_per_operation' else limits['maximum_latency_ratio']
            if ratio > maximum:
                issues.append({'kind': 'regression', 'case': list(key), 'metric': 'cold_' + metric, 'ratios': [ratio]})
            if ratio <= 1 - limits['minimum_primary_improvement_fraction']:
                improvements.append({'case': list(key), 'metric': 'cold_' + metric, 'ratios': [ratio]})
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
