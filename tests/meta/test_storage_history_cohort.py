"""A history-cohort comparison requires identical, recovered durable work."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from benchmarks.storage_history_cohort import compare_reports, retain_failure

pytestmark = pytest.mark.unit


def report():
    return {
        'integrity': {'integrity_returncode': 0}, 'engine_pids': [1, 2],
        'authority': {'backend': 'tofudb', 'filesystem': 'fuse', 'durability': 'strict',
                      'client_mode': 'application'},
        'workload': {'corpus_sha256': 'corpus', 'submission': 'barrier', 'cycles': 2,
                     'conversations': ['c']},
        'cases': [{'conversation': 'c', 'fixture_sha256': 'fixture',
                   'events': 2, 'digest': 'work', 'recovered': True,
                   'push_ordered': True, 'event_ms': [10., 20.],
                   'event_stages': ['metadata', 'mixed']}],
    }


def test_pair_reports_the_same_work_and_full_latency_population():
    before, after = report(), report()
    after['cases'][0]['event_ms'] = [5., 10.]
    result = compare_reports(before, after)
    assert result['conversations'] == 1
    assert result['speedup'] == 2.
    assert result['observations'] == 2


@pytest.mark.parametrize('field,value', [
    ('fixture_sha256', 'different'), ('events', 3), ('digest', 'lost result'),
    ('recovered', False), ('push_ordered', False),
    ('event_stages', ['metadata']), ('event_ms', [1.]),
])
def test_pair_rejects_missing_or_different_evidence(field, value):
    before = report()
    after = deepcopy(before)
    after['cases'][0][field] = value
    with pytest.raises(ValueError):
        compare_reports(before, after)


def test_pair_rejects_missing_conversations_or_changed_workload():
    before = report()
    after = deepcopy(before)
    after['cases'] = []
    with pytest.raises(ValueError):
        compare_reports(before, after)
    after = deepcopy(before)
    after['workload']['submission'] = 'queued'
    with pytest.raises(ValueError):
        compare_reports(before, after)


@pytest.mark.parametrize('clock', [float('nan'), float('inf'), -1., 0.])
def test_pair_rejects_invalid_latency(clock):
    before, after = report(), report()
    after['cases'][0]['event_ms'][0] = clock
    with pytest.raises(ValueError):
        compare_reports(before, after)


@pytest.mark.parametrize('field,value', [
    ('integrity', {'integrity_returncode': 1}), ('engine_pids', [1, 1]),
])
def test_pair_requires_successful_integrity_and_a_different_reopened_process(field, value):
    before, after = report(), report()
    after[field] = value
    with pytest.raises(ValueError):
        compare_reports(before, after)


def test_concurrent_comparison_requires_overlapping_case_clocks():
    before = report()
    before['workload'].update(concurrent=True, conversations=['c', 'd'])
    before['cases'][0]['clock_window_seconds'] = [1., 3.]
    other = deepcopy(before['cases'][0])
    other.update(conversation='d', clock_window_seconds=[2., 4.])
    before['cases'].append(other)
    after = deepcopy(before)
    assert compare_reports(before, after)['conversations'] == 2
    after['cases'][1]['clock_window_seconds'] = [3., 4.]
    with pytest.raises(ValueError, match='overlap'):
        compare_reports(before, after)


def test_failed_run_retains_authority_logs_and_partial_evidence(tmp_path):
    before = report()
    engine = SimpleNamespace(retain_failure=lambda: ['/private/authority'],
                             log_path='/private/daemon.log')
    path = tmp_path / 'failure.json'
    retain_failure(before, engine, path, RuntimeError('terminal rejected'))
    saved = json.loads(path.read_text())
    assert saved['cases'] == before['cases']
    assert saved['failure']['retained_paths'] == ['/private/authority']
    assert saved['failure']['daemon_log'] == '/private/daemon.log'
    with pytest.raises(ValueError, match='Failed run'):
        compare_reports(saved, saved)


def test_startup_failure_survives_an_uninitialized_retention_owner(tmp_path):
    def unavailable():
        raise RuntimeError('authority not initialized')
    value = report()
    path = tmp_path / 'failure.json'
    retain_failure(value, SimpleNamespace(retain_failure=unavailable, log_path=None),
                   path, RuntimeError('original startup failure'))
    failure = json.loads(path.read_text())['failure']
    assert failure['message'] == 'original startup failure'
    assert failure['retention_error'] == 'authority not initialized'
