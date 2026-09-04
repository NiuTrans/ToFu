"""Snapshot-log v2 contracts for bounded FileHistory persistence.

The public FileHistory API continues to expose full ``files`` maps.  Only the
append-only representation may use deltas, and every cache remains disposable:
deleting or corrupting it must fall back to the authoritative JSONL log.
"""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from lib.file_history import api
from lib.file_history import store

pytestmark = pytest.mark.unit


def _record(snapshot_id: str, files: dict[str, int]) -> dict:
    return {
        'id': snapshot_id,
        'taskId': 'task-1',
        'convId': 'conv-1',
        'messageId': None,
        'tools': ['write_file'],
        'summary': None,
        'when': 1.0,
        'files': files,
        'external': False,
        'redoOf': None,
    }


def _raw_lines(base_path: str) -> list[bytes]:
    return Path(store.snapshots_path(base_path)).read_bytes().splitlines()


def _append_in_subprocess(
    base_path: str,
    snapshot_id: str,
    files: dict[str, int],
    start_event,
) -> None:
    start_event.wait(timeout=10)
    store.append_snapshot_record(base_path, _record(snapshot_id, files))


def test_delta_row_round_trips_full_public_snapshot_and_is_small(tmp_path):
    base = str(tmp_path)
    files = {f'src/module_{index:04d}.py': 1 for index in range(750)}
    first = _record('snapshot-a', files)
    second = _record('snapshot-b', {**files, 'src/module_0007.py': 2})

    assert store.append_snapshot_record(base, first) == 1
    assert store.append_snapshot_record(base, second) == 2

    lines = _raw_lines(base)
    stored_second = json.loads(lines[1])
    full_second = json.dumps(
        second, ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8')
    assert stored_second['storageVersion'] == 2
    assert 'files' not in stored_second
    assert len(lines[1]) < len(full_second) * 0.10
    assert list(store.iter_snapshots(base)) == [first, second]


def test_legacy_full_rows_remain_readable(tmp_path):
    base = str(tmp_path)
    records = [
        _record('legacy-a', {'a.py': 1}),
        _record('legacy-b', {'a.py': 2, 'b.py': 1}),
    ]
    path = Path(store.snapshots_path(base))
    path.parent.mkdir(parents=True)
    path.write_text(
        ''.join(json.dumps(record) + '\n' for record in records),
        encoding='utf-8',
    )

    assert list(store.iter_snapshots(base)) == records
    assert store.get_snapshot_file_maps(
        base, 'legacy-a', 'legacy-b',
    ) == (records[0]['files'], records[1]['files'])


def test_broken_delta_chain_is_skipped_until_next_full_anchor(tmp_path):
    base = str(tmp_path)
    first = _record('anchor-a', {'a.py': 1})
    broken = {
        'id': 'broken-b',
        'storageVersion': 2,
        'filesDelta': {
            'baseId': 'not-anchor-a',
            'set': {'b.py': 1},
            'remove': [],
        },
    }
    downstream = {
        'id': 'downstream-c',
        'storageVersion': 2,
        'filesDelta': {
            'baseId': 'broken-b',
            'set': {'c.py': 1},
            'remove': [],
        },
    }
    recovered = _record('anchor-d', {'d.py': 1})
    path = Path(store.snapshots_path(base))
    path.parent.mkdir(parents=True)
    path.write_text(
        ''.join(json.dumps(row) + '\n'
                for row in (first, broken, downstream, recovered)),
        encoding='utf-8',
    )

    assert [row['id'] for row in store.iter_snapshots(base)] == [
        'anchor-a', 'anchor-d',
    ]


def test_torn_final_line_cannot_poison_the_next_append(tmp_path):
    base = str(tmp_path)
    first = _record('snapshot-a', {'a.py': 1})
    second = _record('snapshot-b', {'a.py': 2})
    store.append_snapshot_record(base, first)
    with open(store.snapshots_path(base), 'ab') as handle:
        handle.write(b'{"id":"torn"')

    assert store.append_snapshot_record(base, second) == 2

    assert [row['id'] for row in store.iter_snapshots(base)] == [
        'snapshot-a', 'snapshot-b',
    ]


def test_cache_publish_failure_keeps_fsynced_snapshot_authoritative(
        tmp_path, monkeypatch):
    base = str(tmp_path)

    def fail_cache(*_args, **_kwargs):
        raise OSError('fault-injected cache failure')

    monkeypatch.setattr(store, '_atomic_write_compact_json', fail_cache)

    assert store.append_snapshot_record(
        base, _record('snapshot-a', {'a.py': 1}),
    ) == 1
    assert not Path(store.snapshot_tail_path(base)).exists()
    assert [row['id'] for row in store.iter_snapshots(base)] == ['snapshot-a']


def test_advisory_lock_serializes_cross_process_delta_appends(tmp_path):
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('requires POSIX fork + advisory file locking')
    base = str(tmp_path)
    store.append_snapshot_record(base, _record('snapshot-a', {'a.py': 1}))
    context = multiprocessing.get_context('fork')
    start_event = context.Event()
    processes = [
        context.Process(
            target=_append_in_subprocess,
            args=(base, 'snapshot-b', {'a.py': 1, 'b.py': 1}, start_event),
        ),
        context.Process(
            target=_append_in_subprocess,
            args=(base, 'snapshot-c', {'a.py': 1, 'c.py': 1}, start_event),
        ),
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        assert process.exitcode == 0

    snapshots = list(store.iter_snapshots(base))
    assert len(snapshots) == 3
    assert {snapshot['id'] for snapshot in snapshots} == {
        'snapshot-a', 'snapshot-b', 'snapshot-c',
    }


def test_deleted_tail_index_rebuilds_from_delta_log(tmp_path):
    base = str(tmp_path)
    store.append_snapshot_record(base, _record('snapshot-a', {'a.py': 1}))
    store.append_snapshot_record(base, _record('snapshot-b', {'a.py': 2}))
    os.unlink(store.snapshot_tail_path(base))

    assert api.get_last_snapshot_id(base) == 'snapshot-b'
    assert store.append_snapshot_record(
        base, _record('snapshot-c', {'a.py': 3}),
    ) == 3
    assert [row['id'] for row in store.iter_snapshots(base)] == [
        'snapshot-a', 'snapshot-b', 'snapshot-c',
    ]


def test_tail_index_serves_adjacent_diff_without_log_scan(
        tmp_path, monkeypatch):
    base = str(tmp_path)
    store.append_snapshot_record(
        base, _record('snapshot-a', {'a.py': 1, 'gone.py': 1}),
    )
    store.append_snapshot_record(
        base, _record('snapshot-b', {'a.py': 2, 'gone.py': 0, 'new.py': 1}),
    )

    def fail_scan(*_args, **_kwargs):
        raise AssertionError('validated adjacent tail lookup must not scan JSONL')

    monkeypatch.setattr(store, '_iter_materialized_snapshot_entries', fail_scan)
    assert api.diff_name_status(base, 'snapshot-a', 'snapshot-b') == [
        {'path': 'a.py', 'action': 'modified'},
        {'path': 'gone.py', 'action': 'deleted'},
        {'path': 'new.py', 'action': 'created'},
    ]


def test_latest_metadata_lookup_returns_only_the_matching_id(tmp_path):
    base = str(tmp_path)
    first = _record('snapshot-a', {'a.py': 1})
    second = {**_record('snapshot-b', {'a.py': 2}), 'taskId': 'task-2'}
    third = {**_record('snapshot-c', {'a.py': 3}), 'convId': 'conv-2'}
    for record in (first, second, third):
        store.append_snapshot_record(base, record)

    assert api.find_latest_snapshot_id(base, task_id='task-1') == 'snapshot-c'
    assert api.find_latest_snapshot_id(base, task_id='task-2') == 'snapshot-b'
    assert api.find_latest_snapshot_id(base, conv_id='conv-1') == 'snapshot-b'
    assert api.find_latest_snapshot_id(base) is None


def test_version_reference_scan_is_reused_for_other_files(
        tmp_path, monkeypatch):
    base = str(tmp_path)
    records = [
        _record(
            f'snapshot-{index}',
            {'a.py': index + 1, 'b.py': (index // 2) + 1},
        )
        for index in range(8)
    ]
    path = Path(store.snapshots_path(base))
    path.parent.mkdir(parents=True)
    path.write_text(
        ''.join(json.dumps(record) + '\n' for record in records),
        encoding='utf-8',
    )
    store._REFERENCE_CACHE.clear()
    original_iterator = store._iter_materialized_snapshot_entries
    scans = 0

    def counted_iterator(*args, **kwargs):
        nonlocal scans
        scans += 1
        yield from original_iterator(*args, **kwargs)

    monkeypatch.setattr(
        store, '_iter_materialized_snapshot_entries', counted_iterator,
    )

    assert store._versions_referenced_by_snapshots(base, 'a.py') == set(
        range(1, 9)
    )
    assert store._versions_referenced_by_snapshots(base, 'b.py') == set(
        range(1, 5)
    )
    assert scans == 1


def test_oversized_jsonl_row_is_discarded_with_bounded_reader(
        tmp_path, monkeypatch):
    base = str(tmp_path)
    path = Path(store.snapshots_path(base))
    path.parent.mkdir(parents=True)
    valid = {'id': 'anchor', 'files': {}}
    path.write_bytes(b'x' * 1024 + b'\n' + json.dumps(valid).encode() + b'\n')
    monkeypatch.setattr(store, 'MAX_SNAPSHOT_RECORD_BYTES', 64)

    assert list(store.iter_snapshots(base)) == [valid]


def test_tail_cache_size_cap_never_rejects_authoritative_append(
        tmp_path, monkeypatch):
    base = str(tmp_path)
    monkeypatch.setattr(store, 'MAX_SNAPSHOT_TAIL_BYTES', 100)

    assert store.append_snapshot_record(
        base, _record('snapshot-a', {'long/path/name.py': 1}),
    ) == 1
    assert not Path(store.snapshot_tail_path(base)).exists()
    assert [row['id'] for row in store.iter_snapshots(base)] == ['snapshot-a']


def test_reference_cache_pair_budget_is_global_across_projects(
        tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'MAX_REFERENCE_CACHE_PAIRS', 3)
    store._REFERENCE_CACHE.clear()
    bases = []
    for index in range(2):
        base = tmp_path / f'project-{index}'
        path = Path(store.snapshots_path(str(base)))
        path.parent.mkdir(parents=True)
        path.write_text('{}\n', encoding='utf-8')
        bases.append(str(base))
    store._reference_cache_put(
        bases[0], store._snapshot_log_fingerprint(bases[0]),
        {'a.py': {1, 2}},
    )
    store._reference_cache_put(
        bases[1], store._snapshot_log_fingerprint(bases[1]),
        {'b.py': {1, 2}},
    )

    assert len(store._REFERENCE_CACHE) == 1
    assert sum(
        entry['pairCount'] for entry in store._REFERENCE_CACHE.values()
    ) <= 3


def test_compaction_reencodes_legacy_rows_without_losing_history(
        tmp_path, monkeypatch):
    base = str(tmp_path)
    records = []
    files = {f'src/module_{index:04d}.py': 1 for index in range(400)}
    for index in range(12):
        files = {**files, f'src/module_{index:04d}.py': index + 2}
        records.append(_record(f'snapshot-{index:02d}', files))
    path = Path(store.snapshots_path(base))
    path.parent.mkdir(parents=True)
    path.write_text(
        ''.join(json.dumps(record) + '\n' for record in records),
        encoding='utf-8',
    )
    bytes_before = path.stat().st_size
    monkeypatch.setattr(store, 'MAX_SNAPSHOTS', 20)
    monkeypatch.setattr(store, 'SNAPSHOT_LOG_REWRITE_BYTES', 1)

    result = store.compact_store(base)

    assert result['snapshots_before'] == result['snapshots_after'] == 12
    assert result['bytes_before'] == bytes_before
    assert result['bytes_after'] < bytes_before * 0.25
    assert list(store.iter_snapshots(base)) == records
    assert any('filesDelta' in json.loads(line) for line in _raw_lines(base))


def test_compaction_refuses_to_erase_a_broken_delta_chain(
        tmp_path, monkeypatch):
    base = str(tmp_path)
    rows = [
        _record('anchor-a', {'a.py': 1}),
        {
            'id': 'broken-b',
            'storageVersion': 2,
            'filesDelta': {
                'baseId': 'missing',
                'set': {'b.py': 1},
                'remove': [],
            },
        },
    ]
    path = Path(store.snapshots_path(base))
    path.parent.mkdir(parents=True)
    original = ''.join(json.dumps(row) + '\n' for row in rows).encode()
    path.write_bytes(original)
    monkeypatch.setattr(store, 'SNAPSHOT_LOG_REWRITE_BYTES', 1)

    result = store.compact_store(base)

    assert path.read_bytes() == original
    assert result['bytes_before'] == result['bytes_after'] == len(original)


def test_compaction_replace_failure_leaves_original_log_untouched(
        tmp_path, monkeypatch):
    base = str(tmp_path)
    rows = [
        _record('snapshot-a', {'a.py': 1}),
        _record('snapshot-b', {'a.py': 2}),
    ]
    path = Path(store.snapshots_path(base))
    path.parent.mkdir(parents=True)
    original = ''.join(json.dumps(row) + '\n' for row in rows).encode()
    path.write_bytes(original)
    monkeypatch.setattr(store, 'SNAPSHOT_LOG_REWRITE_BYTES', 1)

    def fail_replace(*_args, **_kwargs):
        raise OSError('fault-injected replace failure')

    monkeypatch.setattr(store.os, 'replace', fail_replace)

    result = store.compact_store(base)

    assert result['snapshots_before'] == 2
    assert path.read_bytes() == original


def test_make_snapshot_normalizes_and_deduplicates_declared_paths(
        tmp_path, monkeypatch):
    base = str(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        api, 'stage_backup',
        lambda _base, rel, **_kwargs: calls.append(rel),
    )
    monkeypatch.setattr(
        api, 'load_tracked',
        lambda _base: {'a.py': {'latest_version': 1, 'deleted': False}},
    )
    monkeypatch.setattr(api, 'append_snapshot_record', lambda *_args: 1)
    monkeypatch.setattr(api, 'maybe_compact_store', lambda *_args: None)

    snapshot_id = api.make_snapshot(
        base,
        task_id='task-1',
        rel_paths=['a.py', './a.py', str(tmp_path / 'a.py'), 'a.py'],
    )

    assert snapshot_id
    assert calls == ['a.py']
