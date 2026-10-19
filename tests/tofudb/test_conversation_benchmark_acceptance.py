"""Measurement verdicts fail closed on slow, absent or incomparable evidence."""
from copy import deepcopy

import pytest

from benchmarks.conversation_acceptance import POLICY, assess
from benchmarks.conversation_compare import compare

from benchmarks.conversation_provenance import FORMAT, REQUIRED_SOURCES
pytestmark = pytest.mark.unit


def source_snapshot():
    return {'native_source_sha256': 'a' * 64,
            'files': {name: 'a' * 64 for name in (*REQUIRED_SOURCES, 'tofu_db/wire.py')}}


@pytest.fixture(autouse=True)
def synthetic_checkout(monkeypatch):
    # Verdict tests are pure/offline; no repository scan or native executable.
    monkeypatch.setattr('benchmarks.conversation_acceptance.capture_sources', source_snapshot)
    monkeypatch.setattr('benchmarks.conversation_compare.capture_sources', source_snapshot)


def evidence(backend='tofudb'):
    report = {'backend': backend, 'correctness_passed': True,
            'conversations': 1000, 'rounds': 4, 'durability': 'strict',
            'benchmark_source_sha256': 'a' * 64,
            'chunk_bytes': 8192, 'restart_while_running': True, 'stream_interval_ms': 1000,
            'rpc_deadline_seconds': POLICY['rpc_deadline_seconds'],
            'running_restart_ms': 10, 'restart_ms': 20, 'recovered_running': 1000,
            'peak_simultaneous_running': 1000, 'integrity_returncode': 0,
            'recovered_completed': 1000, 'recovered_exact_content': 1000,
            'recovered_exact_replay': 1000,
            'authority': {'backend': backend, 'crash_scope': 'sigkill', 'filesystem': 'fuse.bgfuse', 'volume_device': 42, 'rpc_deadline_seconds': POLICY['rpc_deadline_seconds']},
            'latency_ms': {stage: {'p99': 100, 'count': 4000 if stage == 'stream_scheduled' else 1000}
                           for stage in POLICY['p99_budget_ms']}}

    artifact = None
    declared = None
    if backend == 'tofudb':
        identity = {'format': 'tofu-db-build.v1', 'source_sha256': 'a' * 64}
        report['binary_sha256'] = 'b' * 64
        report['authority'].update(client_mode='application', build_identity=identity,
                                   checkout_source_sha256='a' * 64, source_matches_checkout=True)
        declared = {'binary_sha256': report['binary_sha256']}
        artifact = {**declared, 'build_identity': deepcopy(identity)}
    report['provenance'] = {
        'format': FORMAT, 'sources_before': source_snapshot(), 'sources_after': source_snapshot(),
        'declared_artifact': declared, 'artifact_before': deepcopy(artifact),
        'artifact_after': deepcopy(artifact),
    }
    return report

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
            report = evidence(backend)
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


def test_profiled_timings_cannot_become_capacity_admission():
    report = evidence()
    report['latency_comparison_eligible'] = False
    result = assess(report)
    assert result['correctness_passed'] and result['coverage_passed']
    assert not result['latency_passed'] and not result['passed']
    assert any('diagnostic profiling' in failure for failure in result['failures'])


@pytest.mark.parametrize('diagnostic', [
    {'requested_native_profile_every': '1'},
    {'requested_native_profile_every': 10},
    {'requested_native_profile_every': 'unknown'},
    {'requested_client_profile': True},
    {'client_stages_ms': {}},
    {'storage_metrics': {'requestProfile': {'enabled': True}}},
    {'admission_storage_metrics': {'requestProfile': {'enabled': True, 'available': False}}},

    {'productive_start_storage_metrics': {'requestProfile': {'enabled': True}}},
    {'productive_start_storage_metrics': {'requestProfile': {'sampleEvery': 1}}},
    {'terminal_boundary_storage_metrics': {'requestProfile': {'sampleEvery': 2}}},
])
def test_observed_profiling_cannot_be_overridden_by_missing_or_positive_wrapper_verdict(diagnostic):
    report = evidence()
    report.update(diagnostic)
    report['latency_comparison_eligible'] = True
    result = assess(report)
    assert result['correctness_passed'] and result['coverage_passed']
    assert not result['passed'] and not result['latency_comparison_eligible']
    legs = paired_evidence()
    for leg in legs:
        leg['report'].update(diagnostic, latency_comparison_eligible=True)
    assert all(assess(leg['report'])['current_source_qualified'] for leg in legs)
    paired = compare(legs, 2, 'fuse.bgfuse')
    assert not paired['passed']
    assert all(not row['comparable'] and row['tofudb_over_sqlite'] is None
               for row in paired['comparisons'])


@pytest.mark.parametrize('source', [None, '', 'unknown', 'a' * 63])
def test_equal_missing_source_identities_do_not_prove_same_harness(source):
    legs = paired_evidence()
    for leg in legs:
        leg['report']['benchmark_source_sha256'] = source
    result = compare(legs, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(not row['comparable'] for row in result['comparisons'])


@pytest.mark.parametrize('device', [None, True, '42', -1, 99])
def test_same_filesystem_name_does_not_prove_same_authority_volume(device):
    legs = [{'repeat': repeat, 'backend': backend, 'report': evidence(backend)}
            for repeat in range(2) for backend in POLICY['required_backends']]
    for leg in legs:
        if leg['backend'] == 'sqlite':
            leg['report']['authority']['volume_device'] = device
    result = compare(legs, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(not row['comparable'] for row in result['comparisons'])


def test_authority_volume_witness_comes_from_the_actual_directory(tmp_path):
    from types import SimpleNamespace
    from benchmarks.conversation_authority import ConversationAuthority
    authority = ConversationAuthority('tofudb', tmp_path / 'unused-binary', tmp_path)
    authority.engine = SimpleNamespace(_data_dir=tmp_path,
        _client=SimpleNamespace(_timeout=POLICY['rpc_deadline_seconds']),
        _build_identity={'source_sha256': 'a' * 64}, _checkout_source_sha256='a' * 64)
    assert authority.describe()['volume_device'] == tmp_path.stat().st_dev


def paired_evidence():
    return [{'repeat': repeat, 'backend': backend, 'report': evidence(backend)}
            for repeat in range(2) for backend in POLICY['required_backends']]


def test_stable_frozen_native_build_is_not_current_source_qualification():
    report = evidence()
    report['authority']['build_identity']['source_sha256'] = 'c' * 64
    report['authority']['source_matches_checkout'] = False
    for name in ('artifact_before', 'artifact_after'):
        report['provenance'][name]['build_identity']['source_sha256'] = 'c' * 64
    result = assess(report)
    assert result['frozen_build_comparable']
    assert result['correctness_passed'] and result['coverage_passed']
    assert not result['current_source_qualified'] and not result['passed']
    assert any('native build source differs' in reason for reason in result['failures'])
    legs = paired_evidence()
    legs[0]['report'] = report
    paired = compare(legs, 2, 'fuse.bgfuse')
    assert not paired['passed']
    assert all(not row['comparable'] and row['tofudb_over_sqlite'] is None
               for row in paired['comparisons'] if row['repeat'] == 0)


@pytest.mark.parametrize(('path', 'value'), [
    (('provenance',), None),
    (('provenance',), []),
    (('provenance',), {}),
    (('provenance', 'format'), 'unknown'),
    (('provenance', 'sources_before'), None),
    (('provenance', 'sources_before', 'files'), []),
    (('provenance', 'sources_after'), None),
    (('provenance', 'sources_after', 'native_source_sha256'), 'c' * 64),
    (('provenance', 'declared_artifact'), None),
    (('provenance', 'declared_artifact', 'binary_sha256'), 'c' * 64),
    (('provenance', 'artifact_before'), None),
    (('provenance', 'artifact_after'), None),
    (('provenance', 'artifact_after', 'binary_sha256'), 'c' * 64),
    (('provenance', 'artifact_after', 'build_identity'), {}),
    (('authority',), None),
    (('authority', 'backend'), 'sqlite'),
    (('authority', 'client_mode'), None),
    (('authority', 'build_identity'), None),
    (('authority', 'build_identity', 'format'), 'unknown'),
    (('authority', 'build_identity', 'source_sha256'), 'c' * 64),
    (('authority', 'checkout_source_sha256'), None),
    (('authority', 'checkout_source_sha256'), 'unknown'),
    (('authority', 'checkout_source_sha256'), 'c' * 64),
    (('authority', 'source_matches_checkout'), None),
    (('authority', 'source_matches_checkout'), False),
    (('authority', 'source_matches_checkout'), 1),
    (('authority', 'source_matches_checkout'), 'true'),
    (('binary_sha256',), None),
    (('binary_sha256',), 'b' * 63),
    (('binary_sha256',), 'c' * 64),
    (('benchmark_source_sha256',), 'c' * 64),
])
def test_bad_provenance_cannot_qualify_or_publish_comparison_ratios(path, value):
    legs = paired_evidence()
    report = legs[0]['report']
    target = report
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    # A previously saved optimistic verdict is not an authority.
    report['acceptance'] = {'passed': True, 'current_source_qualified': True}
    result = assess(report)
    assert not result['passed'] and not result['current_source_qualified']
    paired = compare(legs, 2, 'fuse.bgfuse')
    assert not paired['passed']
    assert all(not row['comparable'] and row['tofudb_over_sqlite'] is None
               for row in paired['comparisons'] if row['repeat'] == 0)


@pytest.mark.parametrize('name', [
    'benchmarks/conversation_authority.py', 'benchmarks/conversation_acceptance.py',
    'benchmarks/conversation_concurrency_contract.json', 'tofu_db/wire.py',
    'tofu_db/embedded.py', 'tofu_db/generated_contract.py', 'lib/storage/v2_client.py',
    'contracts/storage_v2.json',

    'tofu_workers/__init__.py', 'tofu_workers/executor.py', 'runtime_guards.py',
    'bootstrap_pkg/resource_policy_generated.py',
])
@pytest.mark.parametrize('mutation', ['missing', 'changed-during-run', 'old-checkout'])
def test_entire_relevant_inventory_is_required_not_only_entrypoint_hash(name, mutation):
    report = evidence()
    if mutation == 'missing':
        for phase in ('sources_before', 'sources_after'):
            del report['provenance'][phase]['files'][name]
    else:
        phases = ('sources_after',) if mutation == 'changed-during-run' else ('sources_before', 'sources_after')
        for phase in phases:
            report['provenance'][phase]['files'][name] = 'c' * 64
    assert report['benchmark_source_sha256'] == source_snapshot()['files']['benchmarks/conversation_concurrency.py']
    result = assess(report)
    assert not result['passed'] and not result['current_source_qualified']
    if mutation == 'changed-during-run':
        assert not result['frozen_build_comparable']


def test_missing_provenance_is_rejected_even_for_two_equal_old_reports():
    legs = paired_evidence()
    for leg in legs:
        del leg['report']['provenance']
    result = compare(legs, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(row['tofudb_over_sqlite'] is None for row in result['comparisons'])


def test_a_complete_report_cannot_be_relabelled_as_the_other_backend():
    legs = paired_evidence()
    legs[1]['report'] = deepcopy(legs[0]['report'])
    assert assess(legs[1]['report'])['passed']
    result = compare(legs, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(not row['comparable'] for row in result['comparisons'] if row['repeat'] == 0)


def test_failed_current_source_capture_never_falls_back_to_report_claims(monkeypatch):
    def unavailable():
        raise OSError('synthetic unreadable source')
    monkeypatch.setattr('benchmarks.conversation_acceptance.capture_sources', unavailable)
    monkeypatch.setattr('benchmarks.conversation_compare.capture_sources', unavailable)
    assert not assess(evidence())['current_source_qualified']
    result = compare(paired_evidence(), 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(row['tofudb_over_sqlite'] is None for row in result['comparisons'])


def pipeline_evidence(backend='tofudb'):
    report = evidence(backend)
    files = source_snapshot()['files']
    manifest = {'coordinator': files['lib/tasks_pkg/event_delivery.py'],
                'benchmark': files['benchmarks/storage_write_pipeline.py']}
    for name in ('lib/tasks_pkg/event_delivery_profile.py',
                 'lib/tasks_pkg/event_delivery_profile_metrics.py',
                 'lib/storage/generated_task_event_delivery_profile.py',
                 'contracts/task_event_delivery_v1.json'):
        manifest[name] = files[name]
    report['delivery_pipeline'] = {
        'contract': 'tofu.storage-write-pipeline/v1', 'submission_mode': 'barrier',
        'delivery_profile_every': 0, 'sources_unchanged': True,
        'source_manifest_sha256': manifest, 'delivery_source_sha256': manifest['coordinator'],
        'benchmark_source_sha256': manifest['benchmark'],
        'coordinator': {'performance_profile': {'enabled': False, 'sampleEvery': 0}},
    }
    return report


def test_current_unprofiled_delivery_pipeline_can_qualify():
    legs = [{'repeat': repeat, 'backend': backend, 'report': pipeline_evidence(backend)}
            for repeat in range(2) for backend in POLICY['required_backends']]
    assert all(assess(leg['report'])['passed'] for leg in legs)
    assert compare(legs, 2, 'fuse.bgfuse')['passed']


@pytest.mark.parametrize('observed', ['requested', 'enabled', 'sampleEvery'])
@pytest.mark.parametrize('verdict', [None, True])
def test_delivery_profiles_are_ineligible_even_when_wrapper_verdict_is_missing_or_optimistic(observed, verdict):
    legs = [{'repeat': repeat, 'backend': backend, 'report': pipeline_evidence(backend)}
            for repeat in range(2) for backend in POLICY['required_backends']]
    for leg in legs:
        report = leg['report']
        if verdict is not None:
            report['latency_comparison_eligible'] = verdict
        if observed == 'requested':
            report['delivery_pipeline']['delivery_profile_every'] = 1
        else:
            report['delivery_pipeline']['coordinator']['performance_profile'][observed] = 1
        result = assess(report)
        assert result['current_source_qualified']
        assert not result['latency_comparison_eligible'] and not result['passed']
    compared = compare(legs, 2, 'fuse.bgfuse')
    assert not compared['passed']
    assert all(row['tofudb_over_sqlite'] is None for row in compared['comparisons'])


def test_foreign_frozen_delivery_source_is_comparable_evidence_not_current_qualification():
    legs = [{'repeat': repeat, 'backend': backend, 'report': pipeline_evidence(backend)}
            for repeat in range(2) for backend in POLICY['required_backends']]
    for leg in legs:
        pipeline = leg['report']['delivery_pipeline']
        # Existing frozen coordinator manifests use these two aliases. They
        # must not be laundered by unchanged/current disk sources around them.
        pipeline['source_manifest_sha256'] = {'coordinator': 'f' * 64, 'benchmark': 'a' * 64}
        pipeline['delivery_source_sha256'] = 'f' * 64
        result = assess(leg['report'])
        assert result['frozen_build_comparable']
        assert not result['current_source_qualified'] and not result['passed']
    compared = compare(legs, 2, 'fuse.bgfuse')
    assert not compared['passed']
    assert all(row['tofudb_over_sqlite'] is None for row in compared['comparisons'])


@pytest.mark.parametrize(('field', 'value'), [
    ('contract', None), ('sources_unchanged', None), ('sources_unchanged', False),
    ('source_manifest_sha256', None), ('source_manifest_sha256', {}),
    ('source_manifest_sha256', {'coordinator': 'not-a-hash', 'benchmark': 'a' * 64}),
    ('delivery_source_sha256', None), ('delivery_source_sha256', 'f' * 64),
    ('benchmark_source_sha256', 'f' * 64),
])
def test_malformed_or_changed_delivery_source_receipts_cannot_qualify(field, value):
    report = pipeline_evidence()
    report['delivery_pipeline'][field] = value
    result = assess(report)
    assert not result['passed']
    assert not result['current_source_qualified'] and not result['frozen_build_comparable']


@pytest.mark.parametrize('other_work', ['direct', 'queued'])
def test_current_source_identity_does_not_make_different_delivery_work_comparable(other_work):
    legs = [{'repeat': repeat, 'backend': backend, 'report': pipeline_evidence(backend)}
            for repeat in range(2) for backend in POLICY['required_backends']]
    for leg in legs:
        if leg['backend'] == 'sqlite':
            if other_work == 'direct':
                del leg['report']['delivery_pipeline']
            else:
                leg['report']['delivery_pipeline']['submission_mode'] = other_work
    assert all(assess(leg['report'])['passed'] for leg in legs)
    result = compare(legs, 2, 'fuse.bgfuse')
    assert not result['passed']
    assert all(row['tofudb_over_sqlite'] is None for row in result['comparisons'])
