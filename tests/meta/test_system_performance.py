"""Performance admission rejects coverage gaps and displaced costs."""
from __future__ import annotations

import copy
import statistics

import pytest

from benchmarks.system_performance import CONTRACT, compare

pytestmark = pytest.mark.unit


def reports():
    before = {'format': 'tofu.system-performance/v1', 'workloads': []}
    for domain, cases in CONTRACT['scope'].items():
        for case in cases:
            measurements = []
            for pair in range(3):
                measurements.append({
                    'process_identity': f'{domain}:{case}:{pair}',
                    CONTRACT['comparison']['warmup_operations_field']: 20,
                    'wall_samples_ms': [10.0] * 100,
                    'cpu_samples_ms': [5.0] * 100,
                    'p50_ms': 10.0, 'p95_ms': 10.0, 'p99_ms': 10.0,
                    'throughput_per_second': 100.0, 'cpu_ms_per_operation': 5.0,
                    'peak_rss_bytes': 10000, 'retained_bytes': 1000,
                    'profiler_enabled': False, 'tracemalloc_enabled': False,
                    'memory_separate_process': True})
            before['workloads'].append({
                'domain': domain, 'case': case, 'scope': 'domain_integration',
                'correctness_passed': True, 'failure_and_cleanup_passed': True,
                'identity': {'source_sha256': 'baseline', 'runtime': 'python',
                             'hardware': 'fixture-machine', 'fixture_sha256': 'fixture'},
                'measurements': measurements})
    after = copy.deepcopy(before)
    for row in after['workloads']:
        row['identity']['source_sha256'] = 'candidate'
    for measurement in after['workloads'][0]['measurements']:
        measurement['cpu_ms_per_operation'] = 4.0
        measurement['cpu_samples_ms'] = [4.0] * 100
    return before, after


def test_all_six_domains_required_for_acceptance():
    before, after = reports()
    assert compare(before, after)['verdict'] == 'accepted'
    after['workloads'].pop()
    result = compare(before, after)
    assert result['verdict'] == 'incomplete'
    assert result['coverage']['required'] == 30


@pytest.mark.parametrize('value,verdict', [(None,'incomplete'), (0,'incomplete'),
    (19,'incomplete'), (-1,'invalid'), ('20','invalid'), (True,'invalid'), (20,'accepted')])
def test_declared_warmup_requirement_is_enforced(value,verdict):
    before,after=reports()
    measurement=after['workloads'][0]['measurements'][0]
    field=CONTRACT['comparison']['warmup_operations_field']
    if value is None:measurement.pop(field)
    else:measurement[field]=value
    assert compare(before,after)['verdict']==verdict


def test_faster_median_cannot_hide_tail_or_memory_regression():
    before, after = reports()
    for measurement in after['workloads'][0]['measurements']:
        raw = [1.0] * 98 + [11.0] * 2
        measurement['wall_samples_ms'] = raw
        measurement['p50_ms'] = statistics.median(raw)
        measurement['p95_ms'] = 1.0
        measurement['p99_ms'] = 11.0
        measurement['throughput_per_second'] = len(raw) * 1000 / sum(raw)
        measurement['peak_rss_bytes'] = 11000
    result = compare(before, after)
    assert result['verdict'] == 'regression'
    assert {'p99_ms', 'peak_rss_bytes'} <= {issue.get('metric') for issue in result['issues']}


@pytest.mark.parametrize('field,value,verdict', [
    ('scope', 'microbenchmark', 'incomplete'),
    ('correctness_passed', False, 'incomplete'),
    ('failure_and_cleanup_passed', False, 'incomplete'),
])
def test_speed_requires_integration_and_correctness(field, value, verdict):
    before, after = reports()
    after['workloads'][0][field] = value
    assert compare(before, after)['verdict'] == verdict


@pytest.mark.parametrize('field,value,verdict', [
    ('p99_ms', float('nan'), 'invalid'),
    ('cpu_ms_per_operation', -1, 'invalid'),
    ('wall_samples_ms', [10.0] * 99, 'incomplete'),
    ('profiler_enabled', True, 'invalid'),
    ('memory_separate_process', False, 'invalid'),
])
def test_bad_measurements_cannot_be_admitted(field, value, verdict):
    before, after = reports()
    after['workloads'][0]['measurements'][0][field] = value
    if field == 'wall_samples_ms':
        after['workloads'][0]['measurements'][0]['cpu_samples_ms'] = [4.0] * len(value)
    assert compare(before, after)['verdict'] == verdict


def test_matching_hardware_and_independent_processes_are_required():
    before, after = reports()
    after['workloads'][0]['identity']['hardware'] = 'different-machine'
    assert compare(before, after)['verdict'] == 'invalid'
    before, after = reports()
    measurements = after['workloads'][0]['measurements']
    measurements[1]['process_identity'] = measurements[0]['process_identity']
    assert compare(before, after)['verdict'] == 'invalid'


def test_no_improvement_and_zero_retention_are_distinct_from_acceptance():
    before, _ = reports()
    for measurement in before['workloads'][0]['measurements']:
        measurement['retained_bytes'] = 0
    assert compare(before, copy.deepcopy(before))['verdict'] == 'no_gain'


def test_fabricated_summary_does_not_replace_raw_evidence():
    before, after = reports()
    after['workloads'][0]['measurements'][0]['p50_ms'] = 1.0
    assert compare(before, after)['verdict'] == 'invalid'


def test_new_retention_from_zero_is_serializable_and_a_regression():
    import json
    before, after = reports()
    for measurement in before['workloads'][0]['measurements']:
        measurement['retained_bytes'] = 0
    result = compare(before, after)
    assert result['verdict'] == 'regression'
    json.dumps(result, allow_nan=False)
