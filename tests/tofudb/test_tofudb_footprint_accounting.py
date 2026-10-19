"""Hermetic operation-report footprint accounting; no native runtime or live DB."""

from contextlib import contextmanager
import errno
import itertools
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import tofudb_perf_compare as bench


pytestmark = pytest.mark.unit

@pytest.fixture(autouse=True)
def no_process_io(monkeypatch):
    monkeypatch.setattr(bench, '_pid_write_bytes', lambda _pid: None)


def _native(tmp_path, *, metrics=None):
    authority = tmp_path / 'authority'
    projection = tmp_path / 'projection'
    authority.mkdir()
    projection.mkdir()
    engine = bench.TofuDbEngine(tmp_path / 'unused-binary')
    engine._data_dir = authority
    engine._projection_dir = projection
    calls = []
    engine.prepare_workload = lambda: calls.append('prepare')

    def execute(operation, payload, command):
        calls.append(operation)
        assert (operation, payload, command) == ('system.metrics', {}, False)
        return metrics if metrics is not None else {
            'resourceBudget': {'blockCacheBytes': 1, 'entityPageCacheBytes': 2}}

    engine.execute = execute
    return engine, calls


def _bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _allocated(*paths):
    values = [getattr(path.stat(), 'st_blocks', None) for path in paths]
    return sum(value * 512 for value in values) if None not in values else None


def test_native_report_splits_exact_roots_without_changing_legacy_size(tmp_path):
    engine, calls = _native(tmp_path)
    authority_files = [_bytes(engine._data_dir / 'CONTROL', b'abc'),
                       _bytes(engine._data_dir / 'unknown' / 'old.wal', b'12345')]
    projection_file = _bytes(engine._projection_dir / 'index', b'1234567')
    report = bench._engine_resource_report(engine)
    footprint = report['footprint']
    assert report['data_size_bytes'] == 8
    assert report['data_size_bytes_complete'] is True
    assert footprint['schema_version'] == 1
    assert footprint['total']['complete'] is True
    assert footprint['total']['logical_bytes'] == 15
    assert footprint['total']['allocated_bytes'] == _allocated(*authority_files, projection_file)
    assert footprint['total']['file_count'] == 3
    assert footprint['components']['authority']['logical_bytes'] == 8
    assert footprint['components']['authority']['file_count'] == 2
    assert footprint['components']['projection']['logical_bytes'] == 7
    assert footprint['components']['projection']['file_count'] == 1
    assert footprint['timing']['phase'] == 'post_workloads_before_shutdown'
    assert footprint['timing']['steady_state'] is False
    assert footprint['timing']['consistent_snapshot'] is False
    assert footprint['timing']['finished_at_unix_ms'] >= footprint['timing']['started_at_unix_ms']
    assert footprint['accounting']['liveness'].startswith('not_assessed')
    assert 'reclaimable_bytes' not in footprint['components']['authority']
    assert calls == ['prepare', 'system.metrics']


def test_sparse_file_reports_stat_allocation_not_logical_length(tmp_path):
    sparse = tmp_path / 'sparse'
    with sparse.open('wb') as stream:
        stream.truncate(1 << 20)
    footprint = bench._directory_footprints({'authority': tmp_path})
    assert footprint['total']['logical_bytes'] == 1 << 20
    assert footprint['total']['allocated_bytes'] == _allocated(sparse)
    assert footprint['total']['file_count'] == 1


def test_empty_directory_is_measured_zero_not_missing(tmp_path):
    footprint = bench._directory_footprints({'authority': tmp_path})
    component = footprint['components']['authority']
    assert component['status'] == 'complete'
    assert component['logical_bytes'] == 0
    assert component['file_count'] == 0
    assert component['allocated_bytes'] == (0 if hasattr(tmp_path.stat(), 'st_blocks') else None)
    assert footprint['legacy']['authority'] == {'bytes': 0, 'complete': True}


@pytest.mark.parametrize('state', ['absent', 'not_configured', 'not_directory'])
def test_unknown_roots_do_not_claim_zero_bytes(tmp_path, state):
    root = tmp_path / 'root'
    if state == 'not_directory':
        root.write_bytes(b'not a directory')
    elif state == 'not_configured':
        root = None
    footprint = bench._directory_footprints({'authority': root})
    component = footprint['components']['authority']
    assert component['status'] == state
    assert component['complete'] is False
    assert component['logical_bytes'] is None
    assert component['allocated_bytes'] is None
    assert component['file_count'] is None
    assert footprint['total']['complete'] is False
    assert footprint['total']['logical_bytes'] is None


def test_missing_projection_keeps_legacy_authority_scalar_but_not_combined_total(tmp_path):
    engine, _ = _native(tmp_path)
    _bytes(engine._data_dir / 'file', b'abc')
    engine._projection_dir.rmdir()
    report = bench._engine_resource_report(engine)
    assert report['data_size_bytes'] == 3
    assert report['footprint']['components']['projection']['status'] == 'absent'
    assert report['footprint']['total']['logical_bytes'] is None
    assert report['footprint']['total']['observed']['logical_bytes'] == 3


def test_hardlinks_are_deduplicated_within_and_between_components(tmp_path):
    engine, _ = _native(tmp_path)
    original = _bytes(engine._data_dir / 'a', b'abcdef')
    os.link(original, engine._data_dir / 'b')
    os.link(original, engine._projection_dir / 'linked')
    projection_file = _bytes(engine._projection_dir / 'own', b'xy')
    report = bench._engine_resource_report(engine)
    footprint = report['footprint']
    assert report['data_size_bytes'] == 12  # Legacy path multiplicity is unchanged.
    assert footprint['components']['authority']['logical_bytes'] == 6
    assert footprint['components']['projection']['logical_bytes'] == 2
    assert footprint['components']['authority']['duplicate_file_count'] == 1
    assert footprint['components']['projection']['duplicate_file_count'] == 1
    assert footprint['total']['logical_bytes'] == 8
    assert footprint['total']['allocated_bytes'] == _allocated(original, projection_file)
    assert footprint['total']['file_count'] == 2


def test_nested_roots_have_exclusive_components_and_original_legacy_scope(tmp_path):
    _bytes(tmp_path / 'authority.dat', b'abcd')
    projection = tmp_path / 'projection'
    _bytes(projection / 'index', b'123')
    footprint = bench._directory_footprints({'authority': tmp_path, 'projection': projection})
    assert footprint['components']['authority']['logical_bytes'] == 4
    assert footprint['components']['projection']['logical_bytes'] == 3
    assert footprint['total']['logical_bytes'] == 7
    assert footprint['total']['file_count'] == 2
    assert footprint['legacy']['authority'] == {'bytes': 7, 'complete': True}
    assert footprint['legacy']['projection'] == {'bytes': 3, 'complete': True}


def test_identical_roots_do_not_double_count_or_claim_proven_ownership(tmp_path):
    _bytes(tmp_path / 'file', b'abc')
    footprint = bench._directory_footprints({'authority': tmp_path, 'projection': tmp_path})
    assert footprint['components']['authority']['logical_bytes'] == 3
    assert footprint['components']['projection']['status'] == 'overlapping_root'
    assert footprint['components']['projection']['logical_bytes'] is None
    assert footprint['total']['complete'] is False
    assert footprint['total']['observed']['logical_bytes'] == 3
    assert footprint['total']['observed']['file_count'] == 1


def test_symlink_targets_never_scanned_and_legacy_path_size_is_retained(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'outside'
    _bytes(outside / 'big', b'x' * 10000)
    _bytes(root / 'own', b'abc')
    (root / 'dir-link').symlink_to(outside, target_is_directory=True)
    (root / 'file-link').symlink_to(outside / 'big')
    (root / 'dangling').symlink_to(tmp_path / 'missing')
    (root / 'cycle').symlink_to(root, target_is_directory=True)
    real_scandir = os.scandir
    visited = []

    def scandir(path):
        visited.append(Path(path))
        assert Path(path).is_relative_to(root)
        return real_scandir(path)

    monkeypatch.setattr(bench.os, 'scandir', scandir)
    footprint = bench._directory_footprints({'authority': root})
    assert footprint['components']['authority']['symlink_count'] == 4
    assert footprint['total']['logical_bytes'] == 3
    assert footprint['total']['file_count'] == 1
    expected_legacy = 3 + (root / 'file-link').lstat().st_size + (root / 'dangling').lstat().st_size
    assert footprint['legacy']['authority']['bytes'] == expected_legacy
    assert bench._directory_bytes(root) == expected_legacy
    assert visited == [root, root]


@pytest.mark.parametrize('ancestor', [False, True])
def test_symlink_root_or_ancestor_is_refused_without_walking(tmp_path, monkeypatch, ancestor):
    outside = tmp_path / 'outside'
    _bytes(outside / 'nested' / 'file', b'abcdef')
    link = tmp_path / 'linked'
    link.symlink_to(outside, target_is_directory=True)

    def forbidden(_path):
        pytest.fail('symlink roots must not be traversed')

    monkeypatch.setattr(bench.os, 'scandir', forbidden)
    footprint = bench._directory_footprints({'authority': link / 'nested' if ancestor else link})
    assert footprint['components']['authority']['status'] == 'symlink'
    assert footprint['total']['logical_bytes'] is None


def test_special_files_are_not_regular_payload_or_allocation(tmp_path):
    os.mkfifo(tmp_path / 'pipe')
    footprint = bench._directory_footprints({'authority': tmp_path})
    assert footprint['components']['authority']['special_file_count'] == 1
    assert footprint['total']['logical_bytes'] == 0
    assert footprint['total']['file_count'] == 0


def test_inaccessible_root_is_distinct_from_absent(tmp_path, monkeypatch):
    original = bench.os.lstat

    def denied(path, *args, **kwargs):
        if Path(path) == tmp_path:
            raise PermissionError(errno.EACCES, 'fixture denial')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(bench.os, 'lstat', denied)
    footprint = bench._directory_footprints({'authority': tmp_path})
    component = footprint['components']['authority']
    assert component['status'] == 'inaccessible'
    assert component['error_examples'] == [{'reason': 'root_stat_failed', 'errno': errno.EACCES}]
    assert footprint['total']['logical_bytes'] is None


def test_inaccessible_children_preserve_partial_evidence_with_bounded_errors(tmp_path, monkeypatch):
    _bytes(tmp_path / 'known', b'abc')
    for index in range(12):
        (tmp_path / f'denied-{index}').mkdir()
    original = bench.os.scandir

    def denied(path):
        if Path(path).name.startswith('denied-'):
            raise PermissionError(errno.EACCES, 'fixture denial')
        return original(path)

    monkeypatch.setattr(bench.os, 'scandir', denied)
    footprint = bench._directory_footprints({'authority': tmp_path})
    component = footprint['components']['authority']
    assert component['status'] == 'partial'
    assert component['error_count'] == 12
    assert len(component['error_examples']) == 8
    assert component['logical_bytes'] is None
    assert component['observed']['logical_bytes'] == 3
    assert footprint['legacy']['authority'] == {'bytes': 3, 'complete': False}


@contextmanager
def _altered_entries(real_scandir, path, transform):
    with real_scandir(path) as entries:
        yield (SimpleNamespace(name=entry.name,
                    stat=lambda *, follow_symlinks, entry=entry: transform(entry.stat(follow_symlinks=follow_symlinks)))
               for entry in entries)


def test_allocation_is_null_if_any_file_lacks_st_blocks(tmp_path, monkeypatch):
    _bytes(tmp_path / 'file', b'abcd')
    original = bench.os.scandir

    def without_blocks(info):
        return SimpleNamespace(**{key: getattr(info, key)
            for key in ('st_mode', 'st_dev', 'st_ino', 'st_size')})

    monkeypatch.setattr(bench.os, 'scandir', lambda path: _altered_entries(original, path, without_blocks))
    footprint = bench._directory_footprints({'authority': tmp_path})
    assert footprint['total']['complete'] is True
    assert footprint['total']['logical_bytes'] == 4
    assert footprint['total']['allocated_bytes'] is None
    assert footprint['components']['authority']['allocated_bytes_supported'] is False


def test_disappearing_file_does_not_become_a_complete_zero(tmp_path, monkeypatch):
    _bytes(tmp_path / 'file', b'abcd')
    original = bench.os.scandir

    def vanished(_info):
        raise FileNotFoundError(errno.ENOENT, 'fixture unlink race')

    monkeypatch.setattr(bench.os, 'scandir', lambda path: _altered_entries(original, path, vanished))
    footprint = bench._directory_footprints({'authority': tmp_path})
    component = footprint['components']['authority']
    assert component['status'] == 'partial'
    assert component['logical_bytes'] is None
    assert component['error_examples'] == [{'reason': 'entry_stat_failed', 'errno': errno.ENOENT}]


def test_entry_limit_is_global_across_roots_and_not_silently_complete(tmp_path):
    authority, projection = tmp_path / 'a', tmp_path / 'p'
    _bytes(authority / 'file', b'x')
    for index in range(3):
        _bytes(projection / str(index), b'x')
    footprint = bench._directory_footprints({'authority': authority, 'projection': projection}, max_entries=2)
    assert footprint['collection']['entries_visited'] == 2
    assert footprint['collection']['budget_exhausted'] == 'entry_limit'
    assert footprint['components']['authority']['complete'] is True
    assert footprint['components']['projection']['complete'] is False
    assert footprint['total']['logical_bytes'] is None
    assert footprint['total']['observed']['logical_bytes'] == 2


def test_time_limit_is_explicit_without_wall_clock_assertions(tmp_path, monkeypatch):
    _bytes(tmp_path / 'file', b'abcd')
    clock = itertools.count()
    monkeypatch.setattr(bench.time, 'monotonic', lambda: next(clock) * 0.1)
    footprint = bench._directory_footprints({'authority': tmp_path}, max_seconds=0.15)
    assert footprint['collection']['budget_exhausted'] == 'time_limit'
    assert footprint['collection']['entries_visited'] == 0
    assert footprint['total']['logical_bytes'] is None


def test_depth_limit_is_partial_not_an_unbounded_recursive_walk(tmp_path):
    _bytes(tmp_path / 'one' / 'two' / 'deep', b'abcd')
    footprint = bench._directory_footprints({'authority': tmp_path}, max_depth=1)
    component = footprint['components']['authority']
    assert component['complete'] is False
    assert component['error_examples'] == [{'reason': 'depth_limit', 'errno': None}]
    assert footprint['total']['logical_bytes'] is None


@pytest.mark.parametrize('budget', [{'max_entries': 0}, {'max_seconds': 0}, {'max_depth': 0}])
def test_invalid_budgets_are_rejected(tmp_path, budget):
    with pytest.raises(ValueError, match='positive'):
        bench._directory_footprints({'authority': tmp_path}, **budget)


def test_maintenance_uses_existing_metrics_once_without_claiming_settled(tmp_path):
    engine, calls = _native(tmp_path, metrics={
        'resourceBudget': {'blockCacheBytes': 1, 'entityPageCacheBytes': 2},
        'maintenance': {'rounds': 4, 'searchFailedRounds': 2, 'historyCompactions': 1,
                        'searchDirtyConversations': -1, 'private': 'must not be copied'},
        'onlineGarbageCollector': {'epoch_in_progress': True, 'total_reclaimed_bytes': 99},
        'backgroundPacker': {'packedBytes': 42},
        'searchGarbageCollector': {'epochs_completed': 3},
    })
    report = bench._engine_resource_report(engine, {'maintenance_reopen': {'rounds': 2}})
    evidence = report['footprint']['evidence']
    assert calls == ['prepare', 'system.metrics']
    assert evidence['maintenance']['available'] is True
    assert evidence['maintenance']['settled'] == 'not_assessed'
    assert evidence['maintenance']['metrics']['maintenance'] == {
        'rounds': 4, 'searchFailedRounds': 2, 'historyCompactions': 1}
    assert evidence['maintenance']['metrics']['onlineGarbageCollector'] == {
        'epoch_in_progress': True, 'total_reclaimed_bytes': 99}
    assert evidence['maintenance_reopen']['status'] == 'completed'
    assert evidence['maintenance_reopen']['rounds'] == 2
    assert evidence['readiness']['available'] is False
    assert evidence['readiness']['post_run_readiness_proven'] is False


def test_missing_metrics_preserve_file_evidence_and_do_not_invent_health(tmp_path):
    engine, calls = _native(tmp_path, metrics={})
    _bytes(engine._data_dir / 'file', b'abc')
    report = bench._engine_resource_report(engine)
    assert report['footprint']['total']['logical_bytes'] == 3
    assert report['native_resource_budget']['available'] is False
    assert report['footprint']['evidence']['maintenance']['available'] is False
    assert report['footprint']['evidence']['maintenance_reopen']['status'] == 'not_run'
    assert calls == ['prepare', 'system.metrics']


def test_metrics_failure_preserves_original_error_and_file_counts(tmp_path):
    engine, _ = _native(tmp_path)

    def failed(*_args):
        raise RuntimeError('fixture unavailable')

    engine.execute = failed
    report = bench._engine_resource_report(engine, {'maintenance_reopen': {'error': 'fixture'}})
    assert report['footprint']['total']['file_count'] == 0
    assert report['footprint']['evidence']['maintenance']['error'] == 'RuntimeError: fixture unavailable'
    assert report['footprint']['evidence']['maintenance_reopen']['status'] == 'error'


def test_search_setup_readiness_is_preserved_but_not_promoted_to_post_run(tmp_path):
    engine, _ = _native(tmp_path)
    metrics_execute = engine.execute

    def execute(operation, payload, command):
        if command:
            return {}
        if payload['query']:
            return {'items': [{'id': 'seed'}], 'next_cursor': None, 'incomplete': False}
        return {'coverage': 'ready', 'coverage_reasons': []}

    engine.execute = execute
    setup = next(row[1] for row in bench._workloads({**bench._SCALES['tiny'], 'search_docs': 1}, 11)
                 if row[0] == 'conversation_search')
    setup(engine)
    engine.execute = metrics_execute
    readiness = bench._engine_resource_report(engine)['footprint']['evidence']['readiness']
    assert readiness['available'] is True
    assert readiness['coverage'] == 'ready'
    assert readiness['phase'] == 'conversation_search_setup'
    assert readiness['seed_documents_visible'] == 1
    assert readiness['post_run_readiness_proven'] is False


@pytest.mark.parametrize('backend', ['sqlite', 'postgres'])
def test_legacy_local_artifacts_are_not_claimed_as_complete_database_size(tmp_path, backend):
    _bytes(tmp_path / 'local-file', b'abc')
    engine = bench.LegacyEngine(backend)
    engine._project_root = tmp_path
    report = bench._engine_resource_report(engine)
    footprint = report['footprint']
    assert report['data_size_bytes'] == 3
    assert footprint['components']['local_artifacts']['logical_bytes'] == 3
    assert footprint['components']['authority']['status'] == ('not_local' if backend == 'postgres' else 'not_collected')
    assert footprint['components']['projection']['status'] == 'not_collected'
    assert footprint['total']['complete'] is False
    assert footprint['total']['logical_bytes'] is None
    assert footprint['total']['observed']['logical_bytes'] == 3


def test_rawsqlite_directory_can_be_measured_but_indexes_are_not_separable(tmp_path):
    _bytes(tmp_path / 'database', b'abcd')
    engine = bench.RawSqliteEngine()
    engine._dir = tmp_path
    report = bench._engine_resource_report(engine)
    footprint = report['footprint']
    assert report['data_size_bytes'] == 4
    assert footprint['total']['logical_bytes'] == 4
    assert footprint['components']['authority_and_projection']['logical_bytes'] == 4
    assert footprint['components']['authority']['status'] == 'not_separable'
    assert footprint['components']['projection']['status'] == 'not_separable'
