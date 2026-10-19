"""Charter closure is a deterministic assessment, not a wall-clock CI test."""
from copy import deepcopy
import json
import subprocess
import sys

import pytest

from benchmarks.conversation_provenance import FORMAT, REQUIRED_SOURCES
from scripts import tofudb_charter_assess as charter


pytestmark = pytest.mark.unit

SOURCES = {'native_source_sha256': 'a' * 64,
           'files': {name: 'b' * 64 for name in REQUIRED_SOURCES}}
BUILD = {'format': 'tofu-db-build.v1', 'source_sha256': 'a' * 64, 'version': '0.2.0'}


def reports():
    """Complete synthetic operation cohorts; never a performance measurement."""
    workloads = sorted({name for dim in charter.POLICY['dimensions'] for name in dim.get('workloads', [])})
    artifact = {'binary_sha256': 'c' * 64, 'build_identity': BUILD,
                'shared_library_sha256': 'd' * 64, 'embedded_build_identity': BUILD}
    authority = {'backend': 'tofudb', 'build_identity': BUILD,
                 'checkout_source_sha256': 'a' * 64, 'source_matches_checkout': True,
                 'client_mode': 'embedded', 'shared_library_sha256': 'd' * 64,
                 'embedded_build_identity': BUILD}
    result = []
    for volume in ('xfs', 'fuse.bgfuse'):
        for stress in (False, True):
            for repeat in range(2):
                provenance = {}
                resources = {}
                for engine in charter.POLICY['required_engines']:
                    provenance[engine] = {
                        'format': FORMAT, 'sources_before': SOURCES, 'sources_after': SOURCES,
                        'declared_artifact': {'binary_sha256': 'c' * 64, 'shared_library_sha256': 'd' * 64} if engine == 'tofudb' else None,
                        'artifact_before': artifact if engine == 'tofudb' else None,
                        'artifact_after': artifact if engine == 'tofudb' else None,
                    }
                    resources[engine] = {'filesystem': volume, 'volume_device': 42, 'footprint': {
                        'schema_version': 1, 'timing': {'phase': 'post_workloads_before_shutdown', 'steady_state': False},
                        'total': {'complete': True, 'logical_bytes': 50 if engine == 'tofudb' else 100},
                    }}
                measurement = {
                    'native_client': 'embedded', 'profiling_requested': False,

                    'benchmark_source_sha256': 'b' * 64,
                    'runtime_policy': {
                        'tofudb': {phase: {'wal_durability': 'strict', 'profiling': False, 'profile_sample_every': 0}
                                   for phase in ('before', 'after')},
                        'sqlite': {phase: {'synchronous': 2, 'fastpath_active': False}
                                   for phase in ('before', 'after')},
                        'rawsqlite': {phase: {'synchronous': 2, 'journal_mode': 'wal'}
                                      for phase in ('before', 'after')},
                    },
                    'equivalent_durability': True, 'work_spec': {'scale': 'synthetic'},
                    'native_binary_sha256': 'c' * 64, 'host_identity': {'id': 'same-host', 'affinity': [0, 1]},
                    'collection_id': f'{volume}-{stress}-{repeat}',
                    'engine_order': ['tofudb', 'sqlite', 'rawsqlite'][::1 if repeat == 0 else -1],
                    'workload_order': workloads, 'provenance': provenance,
                    'authority': {'tofudb': authority},
                }
                if stress:
                    measurement['distracted_hardware'] = {'kind': 'fixture', 'observed_overlap': True}
                statistics = {}
                for workload in workloads:
                    statistics[workload] = {}
                    for engine, value in (('tofudb', 1), ('sqlite', 3), ('rawsqlite', 2)):
                        statistics[workload][engine] = {
                            'p50_ms': value, 'p99_ms': value, 'iterations': 1000,
                            'writer_p99_ms': value, 'writer_iterations': 1000,
                            'reader_p99_ms': value, 'reads_completed': 1000,
                            'ops_per_sec': 100 / value,

                            'mean_ms': value, 'shutdown': {'mean_ms': value},
                            'first_read': {'mean_ms': value}, 'stabilization_seconds': value,
                            'scope': 'fixture',
                        }
                result.append(deepcopy({
                    'schema': 'tofu.perf_compare.v2', 'scale': 'large', 'measurement': measurement,
                    'env': {'native': {'profile': False}, 'sqlite_synchronous': 2, 'sqlite_runtime': {'version': 'fixture'}},
                    'resources': resources, 'workloads': statistics,
                    'summary': {'failed_engines': {}}, 'skipped_engines': {},
                }))
    return result


def dimension(result, name):
    return next(item for item in result['dimensions'] if item['id'] == name)


def test_complete_operation_cells_pass_but_do_not_waive_missing_capacity():
    result = charter.assess(reports(), current_sources=SOURCES)
    gaps = {'history_read', 'degraded_fuse', 'horizontal_reads', 'distracted_hardware', 'conversation_capacity'}
    assert all(item['status'] == 'PASS' for item in result['dimensions'] if item['id'] not in gaps)
    assert all(dimension(result, name)['status'] == 'INSUFFICIENT' for name in gaps)
    assert dimension(result, 'history_read')['numeric_status'] == 'PASS'
    assert dimension(result, 'conversation_capacity')['status'] == 'INSUFFICIENT'
    assert result['comprehensive_victory'] is False
    assert result['status'] == 'INSUFFICIENT'


def test_search_visibility_is_not_waived_by_fast_queries():
    rows = reports()
    for row in rows:
        row['workloads']['conversation_search']['tofudb']['stabilization_seconds'] = 12
    assert dimension(charter.assess(rows, current_sources=SOURCES), 'search')['status'] == 'FAIL'


def test_zero_visibility_measurements_do_not_raise_or_hide_a_regression():
    rows = reports()
    for row in rows:
        for stats in row['workloads']['conversation_search'].values():
            stats['stabilization_seconds'] = 0
    assert dimension(charter.assess(rows, current_sources=SOURCES), 'search')['status'] == 'PASS'
    for row in rows:
        row['workloads']['conversation_search']['tofudb']['stabilization_seconds'] = 1
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'search')['status'] == 'FAIL'
    json.dumps(result, allow_nan=False)


def test_maintenance_includes_shutdown_and_first_read():
    rows = reports()
    for row in rows:
        row['workloads']['maintenance_reopen']['tofudb']['shutdown']['mean_ms'] = 15
    assert dimension(charter.assess(rows, current_sources=SOURCES), 'maintenance')['status'] == 'FAIL'


def test_empty_evidence_generates_actionable_missing_dimensions():
    result = charter.assess([], current_sources=SOURCES)
    assert len(result['dimensions']) == len(charter.POLICY['dimensions'])
    assert {item['status'] for item in result['dimensions']} == {'INSUFFICIENT'}
    text = charter.markdown(result)
    assert 'interactive_write' in text and 'conversation_capacity' in text
    assert 'no operation reports' in text


def test_repeated_receipt_is_not_an_independent_repeat():
    row = reports()[0]
    with pytest.raises(charter.CatalogError, match='duplicate'):
        charter.assess([row, deepcopy(row)], current_sources=SOURCES)


def test_duplicate_collection_id_is_incomparable_even_if_bytes_differ():
    rows = reports()
    rows[1]['measurement']['collection_id'] = rows[0]['measurement']['collection_id']
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_read')['status'] == 'INCOMPARABLE'


def test_small_sample_p99_does_not_inherit_claimed_sufficiency():
    rows = reports()
    for row in rows:
        for values in row['workloads']['turn_append_large'].values():
            values.update(iterations=20, p99_sample_count_sufficient=True)
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_write')['status'] == 'INSUFFICIENT'
    assert dimension(result, 'interactive_read')['status'] == 'PASS'


def test_known_current_regression_is_visible_despite_other_missing_evidence():
    rows = reports()
    for row in rows:
        row['workloads']['record_get_points']['tofudb']['p50_ms'] = 10
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_read')['status'] == 'FAIL'
    assert dimension(result, 'engine_floor')['status'] == 'FAIL'
    assert result['status'] == 'FAIL'
    assert result['comprehensive_victory'] is False


def test_opposite_repeat_outcomes_are_insufficient_not_averaged():
    rows = reports()
    rows[0]['workloads']['record_get_points']['tofudb']['p50_ms'] = 10
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_read')['status'] == 'INSUFFICIENT'


def test_profiling_and_missing_provenance_are_not_admitted():
    rows = reports()
    rows[0]['measurement']['profiling_requested'] = True
    rows[4]['measurement'].pop('provenance')
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_read')['status'] == 'INSUFFICIENT'
    assert not result['comprehensive_victory']


def test_frozen_source_is_historical_not_a_current_regression():
    current = deepcopy(SOURCES)
    current['files']['lib/storage/v2_client.py'] = 'f' * 64
    rows = reports()
    for row in rows:
        row['workloads']['record_get_points']['tofudb']['p50_ms'] = 10
    result = charter.assess(rows, current_sources=current)
    gate = dimension(result, 'interactive_read')
    assert gate['status'] == 'HISTORICAL'
    assert any(item.get('observed_status') == 'FAIL' for item in gate['cells'])


def test_changed_native_artifact_is_incomparable():
    rows = reports()
    rows[0]['measurement']['provenance']['tofudb']['artifact_after']['binary_sha256'] = 'f' * 64
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_read')['status'] == 'INCOMPARABLE'


def test_host_and_work_mismatch_cannot_hide_in_pooled_results():
    rows = reports()
    rows[1]['measurement']['host_identity'] = {'id': 'other-host'}
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_read')['status'] == 'INCOMPARABLE'


def test_reverse_order_is_required():
    rows = reports()
    rows[1]['measurement']['engine_order'] = rows[0]['measurement']['engine_order'][:]
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'interactive_read')['status'] == 'INSUFFICIENT'


def test_footprint_requires_total_scope_not_legacy_root_only():
    rows = reports()
    rows[0]['resources']['tofudb'].pop('footprint')
    rows[0]['resources']['tofudb']['data_size_bytes'] = 1
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'footprint')['status'] == 'INSUFFICIENT'


def test_footprint_phases_are_not_interchangeable():
    rows = reports()
    rows[0]['resources']['sqlite']['footprint']['timing']['phase'] = 'after_close'
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'footprint')['status'] == 'INCOMPARABLE'


def test_idle_hardware_does_not_satisfy_distracted_dimension():
    rows = [row for row in reports() if not row['measurement'].get('distracted_hardware')]
    result = charter.assess(rows, current_sources=SOURCES)
    assert dimension(result, 'distracted_hardware')['status'] == 'INSUFFICIENT'
    assert dimension(result, 'interactive_read')['status'] == 'PASS'


def test_unknown_volume_is_not_silently_called_local():
    rows = reports()
    for row in rows:
        row['resources']['tofudb']['filesystem'] = 'nfs4'
    result = charter.assess(rows, current_sources=SOURCES)
    assert result['unclassified']
    assert dimension(result, 'interactive_read')['status'] == 'INSUFFICIENT'


def test_summary_and_nonfinite_values_are_refused():
    with pytest.raises(charter.CatalogError, match='raw perf_compare'):
        charter.assess([{'decision': 'victory', 'passed': True}], current_sources=SOURCES)
    rows = reports()
    rows[0]['workloads']['record_get_points']['tofudb']['p50_ms'] = float('nan')
    with pytest.raises(ValueError):
        charter.assess(rows, current_sources=SOURCES)


def test_policy_has_all_charter_dimensions_and_no_aggregate_speedup():
    ids = [item['id'] for item in charter.POLICY['dimensions']]
    assert len(ids) == len(set(ids)) == 13
    assert {'engine_floor', 'footprint', 'distracted_hardware', 'swarm_write_tail', 'conversation_capacity'} <= set(ids)
    assert all((charter.ROOT / item['owner']).is_file() for item in charter.POLICY['dimensions'])
    assert dimension(charter.assess([], current_sources=SOURCES), 'engine_floor')['status'] == 'INSUFFICIENT'


def test_cli_refuses_curated_input_without_writing(tmp_path, monkeypatch, capsys):
    import benchmarks.conversation_provenance as provenance
    monkeypatch.setattr(provenance, 'capture_sources', lambda: SOURCES)
    source = tmp_path / 'source.json'
    source.write_text(json.dumps({'passed': True}))
    output = tmp_path / 'report.json'
    assert charter.main([str(source), '--json-output', str(output)]) == 2
    assert not output.exists()
    assert 'refused' in capsys.readouterr().err


def test_cli_writes_deterministic_outputs_and_cannot_overwrite(tmp_path, monkeypatch):
    import benchmarks.conversation_provenance as provenance
    monkeypatch.setattr(provenance, 'capture_sources', lambda: SOURCES)
    output, markdown = tmp_path / 'report.json', tmp_path / 'report.md'
    assert charter.main(['--json-output', str(output), '--markdown-output', str(markdown)]) == 1
    assert json.loads(output.read_text())['status'] == 'INSUFFICIENT'
    assert 'INSUFFICIENT' in markdown.read_text()
    assert charter.main(['--json-output', str(output), '--markdown-output', str(markdown)]) == 1
    output.write_text('user-owned conflicting content')
    assert charter.main(['--json-output', str(output)]) == 2
    assert output.read_text() == 'user-owned conflicting content'


def test_cli_help_never_loads_native_or_creates_authority():
    command = subprocess.run([sys.executable, str(charter.ROOT / 'scripts/tofudb_charter_assess.py'), '--help'],
                             capture_output=True, text=True, check=False)
    assert command.returncode == 0
    assert '--markdown-output' in command.stdout


@pytest.mark.parametrize('engine,field,value', [
    ('tofudb', 'profiling', True),
    ('tofudb', 'profile_sample_every', 1),
    ('tofudb', 'wal_durability', 'normal'),
    ('sqlite', 'synchronous', 1),
    ('sqlite', 'fastpath_active', True),
    ('rawsqlite', 'synchronous', 1),
    ('rawsqlite', 'journal_mode', 'delete'),
])
@pytest.mark.parametrize('phase', ['before', 'after'])
def test_runtime_observations_override_optimistic_summary(engine, field, value, phase):
    row = reports()[0]
    row['measurement']['runtime_policy'][engine][phase][field] = value
    assert charter.qualify(row, SOURCES)['status'] == 'INSUFFICIENT'


def test_missing_runtime_observation_is_not_proof_of_equivalent_durability():
    row = reports()[0]
    del row['measurement']['runtime_policy']['tofudb']['after']
    assert charter.qualify(row, SOURCES)['status'] == 'INSUFFICIENT'


def test_conflicting_benchmark_source_is_incomparable():
    row = reports()[0]
    row['measurement']['benchmark_source_sha256'] = 'f' * 64
    assert charter.qualify(row, SOURCES)['status'] == 'INCOMPARABLE'


def test_mixed_engine_sources_are_incomparable_not_merely_historical():
    row = reports()[0]
    # Break intentional fixture sharing before simulating independent legs.
    receipt = deepcopy(row['measurement']['provenance']['sqlite'])
    receipt['sources_before']['files']['lib/storage/v2_client.py'] = 'f' * 64
    receipt['sources_after'] = deepcopy(receipt['sources_before'])
    row['measurement']['provenance']['sqlite'] = receipt
    assert charter.qualify(row, SOURCES)['status'] == 'INCOMPARABLE'


def test_actual_collector_fields_are_accepted_by_charter(monkeypatch, tmp_path):
    """Exercise collector/assessor composition, not a hand-authored passed flag."""
    from types import SimpleNamespace
    import benchmarks.conversation_provenance as provenance
    from scripts import tofudb_perf_compare as bench

    row = reports()[0]
    receipt = row['measurement']['provenance']['tofudb']
    binary = tmp_path / 'tofu-db'
    binary.write_bytes(b'synthetic-artifact-never-executed')

    (binary.parent / 'libtofu_db.so').write_bytes(b'synthetic-library-never-loaded')
    engines = {'tofudb': SimpleNamespace(
        _binary=binary, _client_mode='embedded', _build_identity=BUILD,
        _embedded_build_identity=BUILD,
        _checkout_source_sha256=SOURCES['native_source_sha256']),
        'sqlite': object(), 'rawsqlite': object()}
    runtime = {name: values['before'] for name, values in row['measurement']['runtime_policy'].items()}
    meta = bench._measurement_metadata(engines)
    meta['benchmark_source_sha256'] = SOURCES['files']['scripts/tofudb_perf_compare.py']
    for name in ('collection_id', 'host_identity', 'workload_order', 'work_spec'):
        meta[name] = row['measurement'][name]
    row['measurement'] = meta
    monkeypatch.setattr(provenance, 'capture_sources', lambda: SOURCES)
    monkeypatch.setattr(provenance, 'capture_artifact', lambda *args, **kwargs: receipt['artifact_before'])
    monkeypatch.setattr(bench, '_operation_runtime_evidence', lambda name, engine: runtime[name])
    bench._finish_operation_evidence(
        row, engines, SOURCES, {'tofudb': receipt['declared_artifact']},
        {'tofudb': receipt['artifact_before']}, runtime)
    assert charter.qualify(row, SOURCES) == {'status': 'PASS', 'reasons': []}
