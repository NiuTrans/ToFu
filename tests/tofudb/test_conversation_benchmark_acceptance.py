"""Measurement verdicts fail closed on slow, absent or incomparable evidence."""
from copy import deepcopy

import pytest

from benchmarks.conversation_acceptance import POLICY, assess
from benchmarks.conversation_compare import compare

pytestmark = pytest.mark.unit


def evidence():
    return {'correctness_passed': True, 'conversations': 1000, 'rounds': 4, 'durability': 'strict',
            'chunk_bytes': 8192, 'restart_while_running': True, 'stream_interval_ms': 1000,
            'rpc_deadline_seconds': POLICY['rpc_deadline_seconds'],
            'running_restart_ms': 10, 'restart_ms': 20, 'recovered_running': 1000,
            'peak_simultaneous_running': 1000, 'integrity_returncode': 0,
            'recovered_completed': 1000, 'recovered_exact_content': 1000,
            'recovered_exact_replay': 1000,
            'authority': {'crash_scope': 'sigkill', 'filesystem': 'fuse.bgfuse', 'rpc_deadline_seconds': POLICY['rpc_deadline_seconds']},
            'latency_ms': {stage: {'p99': 100, 'count': 4000 if stage == 'stream_scheduled' else 1000}
                           for stage in POLICY['p99_budget_ms']}}


def test_complete_low_latency_evidence_passes_only_its_declared_scope():
    result = assess(evidence())
    assert result['passed']
    assert 'browser-send-to-visible-feedback' in result['uncovered']


@pytest.mark.parametrize('p99', [47000, 16534, float('nan'), float('inf'), None, -1])
def test_correct_content_does_not_make_slow_or_invalid_streaming_pass(p99):
    report = evidence()
    report['latency_ms']['stream_scheduled']['p99'] = p99
    result = assess(report)
    assert result['correctness_passed']
    assert not result['passed']
    assert not result['latency_passed']


@pytest.mark.parametrize('mutation', ['missing_metric', 'missing_sample', 'failed', 'smoke', 'no_restart', 'no_schedule'])
def test_incomplete_or_failed_evidence_cannot_pass(mutation):
    report = evidence()
    if mutation == 'missing_metric':
        del report['latency_ms']['stream_scheduled']
    elif mutation == 'missing_sample':
        report['latency_ms']['stream_scheduled']['count'] -= 1
    elif mutation == 'failed':
        report['correctness_passed'] = False
    elif mutation == 'smoke':
        report['conversations'] = 8
    elif mutation == 'no_restart':
        report['restart_while_running'] = False
    else:
        del report['stream_interval_ms']
    assert not assess(report)['passed']


def test_paired_repeats_retain_missing_failed_and_slower_baselines():
    legs = []
    for repeat in range(2):
        for backend in ('tofudb', 'sqlite'):
            report = evidence()
            report['acceptance'] = assess(report)
            legs.append({'repeat': repeat, 'backend': backend, 'report': report})
    assert compare(legs, 2, 'fuse.bgfuse')['passed']
    assert not compare(legs[:-1], 2, 'fuse.bgfuse')['passed']
    assert not compare(legs, 2, 'ext4')['passed']
    changed = deepcopy(legs)
    changed[0]['report']['latency_ms']['stream_scheduled']['p99'] = 101
    assert not compare(changed, 2, 'fuse.bgfuse')['passed']
    changed = deepcopy(legs)
    changed[1]['report']['correctness_passed'] = False
    changed[1]['report']['latency_ms']['stream_scheduled']['p99'] = 1
    result = compare(changed, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(not row['comparable'] and row['tofudb_over_sqlite'] is None
               for row in result['comparisons'] if row['repeat'] == 0)
    changed = deepcopy(legs)
    changed[0]['report']['rounds'] = 5
    assert not compare(changed, 2, 'fuse.bgfuse')['comparisons'][0]['comparable']
    changed = deepcopy(legs)
    changed[0]['report']['stream_event_kind'] = 'delta'
    changed[1]['report']['stream_event_kind'] = 'phase'
    assert not compare(changed, 2, 'fuse.bgfuse')['comparisons'][0]['comparable']
    changed = deepcopy(legs)
    changed[0]['report']['carry_stream_events'] = True
    changed[1]['report']['carry_stream_events'] = False
    assert not compare(changed, 2, 'fuse.bgfuse')['comparisons'][0]['comparable']
    changed = deepcopy(legs)
    changed[0]['report']['terminal_barrier'] = True
    changed[1]['report']['terminal_barrier'] = False
    assert not compare(changed, 2, 'fuse.bgfuse')['comparisons'][0]['comparable']
    changed = deepcopy(legs)
    changed[0]['report']['latency_ms']['stream_scheduled']['p99'] = float('nan')
    result = compare(changed, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert not next(row for row in result['comparisons']
                    if row['repeat'] == 0 and row['metric'] == 'stream_scheduled')['comparable']


def test_embedded_abandon_cannot_claim_process_crash_coverage():
    report = evidence()
    report['authority']['crash_scope'] = 'in-process-abandon'
    result = assess(report)
    assert result['correctness_passed'] and result['latency_passed']
    assert not result['coverage_passed'] and not result['passed']


@pytest.mark.parametrize('actual', [None, 5, 120])
def test_different_or_missing_rpc_deadlines_cannot_prove_comparison_coverage(actual):
    report = evidence()
    report['authority']['rpc_deadline_seconds'] = actual
    assert not assess(report)['coverage_passed']


def test_benchmark_deadline_is_scoped_and_reapplied_after_restart(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    from benchmarks.conversation_authority import ConversationAuthority
    monkeypatch.setenv('TOFU_STORAGE_RPC_DEADLINE_S', '5')
    seen = []
    def launch():
        seen.append(os.environ['TOFU_STORAGE_RPC_DEADLINE_S'])
        return 1.0
    authority = ConversationAuthority('sqlite', None, tmp_path)
    authority.engine = SimpleNamespace(_client=SimpleNamespace(_timeout=POLICY['rpc_deadline_seconds']),
                                      start=launch, crash_cycle=launch)
    authority.start()
    authority.crash_cycle()
    assert seen == [str(POLICY['rpc_deadline_seconds'])] * 2
    assert os.environ['TOFU_STORAGE_RPC_DEADLINE_S'] == '5'
    authority.engine._client._timeout = 5
    with pytest.raises(RuntimeError, match='RPC deadline differs'):
        authority.start()


@pytest.mark.parametrize('field', ['running_restart_ms', 'restart_ms'])
@pytest.mark.parametrize('value', [None, True, '10', float('nan'), float('inf'), -1])
def test_requested_restart_without_actual_measurement_leaves_coverage_gap(field, value):
    report = evidence()
    report[field] = value
    result = assess(report)
    assert result['correctness_passed'] and result['latency_passed']
    assert not result['coverage_passed'] and not result['passed']


@pytest.mark.parametrize('field', ['peak_simultaneous_running', 'recovered_running'])
@pytest.mark.parametrize('value', [None, 992])
def test_incomplete_running_recovery_cannot_claim_full_capacity_coverage(field, value):
    report = evidence()
    report[field] = value
    assert not assess(report)['coverage_passed']


@pytest.mark.parametrize('value', [None, 1, False])
def test_skipped_or_failed_integrity_verification_leaves_coverage_gap(value):
    report = evidence()
    report['integrity_returncode'] = value
    assert not assess(report)['coverage_passed']


def test_unspecified_crash_mechanism_cannot_prove_process_crash():
    report = evidence()
    del report['authority']['crash_scope']
    assert not assess(report)['coverage_passed']


def test_zero_duration_restart_is_valid_measured_evidence():
    report = evidence()
    report['running_restart_ms'] = report['restart_ms'] = 0
    assert assess(report)['passed']


@pytest.mark.parametrize('backend', ['sqlite', 'tofudb'])
def test_authority_declares_the_actual_process_crash_boundary(backend, tmp_path):
    from types import SimpleNamespace
    from benchmarks.conversation_authority import ConversationAuthority
    authority = ConversationAuthority(backend, tmp_path / 'tofu-db', tmp_path, native_client='application')
    authority.engine = SimpleNamespace(
        _data_dir=tmp_path, _project_root=tmp_path,
        _client=SimpleNamespace(_timeout=POLICY['rpc_deadline_seconds'],
                                metrics=lambda **kwargs: {'fastpath': {'active': False}}),
        _build_identity={}, _checkout_source_sha256='')
    assert authority.describe()['crash_scope'] == 'sigkill'


@pytest.mark.parametrize('field', ['recovered_completed', 'recovered_exact_content', 'recovered_exact_replay'])
def test_partial_post_crash_readback_cannot_claim_full_recovery_coverage(field):
    report = evidence()
    report['carry_stream_events'] = True
    report[field] = 992
    assert not assess(report)['coverage_passed']


def test_startup_readiness_failure_retains_components_and_remains_a_failure():
    from benchmarks.conversation_concurrency import record_readiness_probe
    health = {'probes': 0, 'unhealthy_probes': 0, 'max_consecutive_unhealthy': 0,
              'max_contended_milliseconds': 0, 'failure_examples': []}
    consecutive = record_readiness_probe(health, {'ready': False, 'components': {'search': False}},
        phase='startup', elapsed_seconds=0.25)
    assert consecutive == 1 and health['unhealthy_probes'] == 1
    assert health['failure_examples'] == [{'phase': 'startup', 'elapsed_seconds': 0.25,
        'components': {'search': False}, 'authorityContendedMilliseconds': None, 'probe_error': None}]
    assert record_readiness_probe(health, {'ready': True}, phase='productive', elapsed_seconds=1,
        consecutive_unhealthy=consecutive) == 0
    assert health['unhealthy_probes'] == 1
    report = evidence(); report['correctness_passed'] = False
    assert not assess(report)['passed']


def test_productive_readiness_failures_keep_bounded_examples_and_exact_counts():
    from benchmarks.conversation_concurrency import record_readiness_probe
    health = {'probes': 0, 'unhealthy_probes': 0, 'max_consecutive_unhealthy': 0,
              'max_contended_milliseconds': 0, 'failure_examples': []}
    consecutive = 0
    for index in range(12):
        consecutive = record_readiness_probe(health, {'ready': False, 'probe_error': 'timeout',
            'authorityContendedMilliseconds': index}, phase='productive', elapsed_seconds=index,
            consecutive_unhealthy=consecutive)
    assert health['probes'] == health['unhealthy_probes'] == health['max_consecutive_unhealthy'] == 12
    assert health['max_contended_milliseconds'] == 11
    assert len(health['failure_examples']) == 8
    assert all(row['phase'] == 'productive' and row['probe_error'] == 'timeout' for row in health['failure_examples'])
