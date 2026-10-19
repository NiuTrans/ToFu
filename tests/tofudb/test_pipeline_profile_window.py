"""Pipeline attribution requires complete samples from one quiescent incarnation."""
from copy import deepcopy

import pytest

from scripts.tofudb_profile_report import analyze_pipeline_window
from tests.tofudb.test_request_profile_window import matched_window_fixture

pytestmark = pytest.mark.unit


def pipeline_fixture():
    caller = matched_window_fixture()
    start, end = 'productive_start_storage_metrics', 'storage_metrics'
    document = {start: caller['metrics_before'], end: caller['metrics_after'],
                'storage_metrics_provenance': {}}
    for key, boundary in [(start, 'recovered-before-provider-release'),
                          (end, 'completed-before-terminal-restart')]:
        document['storage_metrics_provenance'][key] = {
            'process_id': 41, 'process_started_unix_seconds': 1234.5,
            'authority_incarnation': 1, 'boundary': boundary,
            'benchmark_calls_quiescent': True,
        }
        profile = document[key]['requestProfile']
        write = deepcopy(profile['operations'][0])
        write['operation'] = 'record.put'
        profile['operations'].append(write)
        profile['seen'] *= 2
        profile['selected'] *= 2
    return document


def test_complete_multi_operation_window_excludes_admission_and_preserves_input():
    document = pipeline_fixture()
    original = deepcopy(document)
    result = analyze_pipeline_window(document)
    assert document == original
    assert result['requests'] == result['selected'] == 12
    assert result['dropped'] == 0
    assert {row['operation']: row['requests'] for row in result['operations']} == {
        'record.get': 6, 'record.put': 6}
    assert result['summed_dispatch_ms'] == .006
    assert result['pipeline_window']['authority_incarnation'] == 1


@pytest.mark.parametrize(('field', 'value'), [
    ('process_id', 42), ('process_started_unix_seconds', 1235.0),
    ('authority_incarnation', 2), ('process_id', True),
    ('process_started_unix_seconds', float('nan')),
    ('process_started_unix_seconds', float('inf')),
    ('authority_incarnation', -1), ('authority_incarnation', False),
    ('benchmark_calls_quiescent', False), ('benchmark_calls_quiescent', 1),
    ('boundary', 'stream-submission-before-settlement'),
])
def test_changed_or_unproven_lifetime_is_refused_even_with_increasing_counters(field, value):
    document = pipeline_fixture()
    document['storage_metrics_provenance']['storage_metrics'][field] = value
    with pytest.raises(ValueError):
        analyze_pipeline_window(document)


@pytest.mark.parametrize('mutation', ['loss', 'inflight', 'sampling', 'reset', 'duplicate'])
def test_incomplete_native_population_cannot_become_exact_attribution(mutation):
    document = pipeline_fixture()
    after = document['storage_metrics']['requestProfile']
    if mutation == 'loss':
        after['dropped'] += 1
    elif mutation == 'inflight':
        after['seen'] += 1
        after['selected'] += 1
    elif mutation == 'sampling':
        after['sampleEvery'] = 2
    elif mutation == 'reset':
        after['selected'] = 1
    else:
        after['operations'].append(deepcopy(after['operations'][0]))
    with pytest.raises(ValueError):
        analyze_pipeline_window(document)


def test_errors_remain_counted_and_wire_queue_coverage_stays_unknown():
    document = pipeline_fixture()
    for key in ['productive_start_storage_metrics', 'storage_metrics']:
        for row in document[key]['requestProfile']['operations']:
            row['embeddedQueueNanos'] = None
            row['embeddedQueueSamples'] = 0
    document['storage_metrics']['requestProfile']['operations'][1]['errors'] += 1
    result = analyze_pipeline_window(document)
    assert sum(row['errors'] for row in result['operations']) == 1
    assert all(row['mean_embedded_queue_plus_dispatch_ms'] is None for row in result['operations'])


def test_legacy_report_without_process_proof_cannot_be_subtracted():
    document = pipeline_fixture()
    del document['storage_metrics_provenance']
    with pytest.raises(KeyError):
        analyze_pipeline_window(document)


@pytest.mark.parametrize('reader_kind', ['single', 'pipeline'])
def test_equal_inflight_populations_at_both_endpoints_cannot_cancel(reader_kind):
    from scripts.tofudb_profile_report import analyze_window

    if reader_kind == 'single':
        document, reader = matched_window_fixture(), analyze_window
        keys = ('metrics_before', 'metrics_after')
    else:
        document, reader = pipeline_fixture(), analyze_pipeline_window
        keys = ('productive_start_storage_metrics', 'storage_metrics')
    for key in keys:
        profile = document[key]['requestProfile']
        profile['selected'] += 1
        profile['seen'] += 1
    # Identical outstanding counts subtract to zero even though the request
    # populations may differ. Caller quiescence alone is not guard retirement.
    with pytest.raises(ValueError, match='endpoint'):
        reader(document)


@pytest.mark.parametrize('field', ['work', 'calls', 'dispatch'])
def test_counters_cannot_advance_for_an_operation_without_completed_calls(field):
    document = pipeline_fixture()
    before = document['productive_start_storage_metrics']['requestProfile']
    after = document['storage_metrics']['requestProfile']
    for profile in [before, after]:
        ghost = deepcopy(before['operations'][0])
        ghost['operation'] = 'record.old'
        profile['operations'].append(ghost)
        profile['selected'] += ghost['requests']
        profile['seen'] += ghost['requests']
    ghost = after['operations'][-1]
    if field == 'work':
        ghost['work']['inputBytes'] = ghost['work'].get('inputBytes', 0) + 1
    elif field == 'calls':
        ghost['stages'][0]['calls'] = ghost['stages'][0].get('calls', 0) + 1
    else:
        ghost['dispatchNanos'] += 1
        ghost['unattributedNanos'] += 1
    with pytest.raises(ValueError, match='without a completed request'):
        analyze_pipeline_window(document)
