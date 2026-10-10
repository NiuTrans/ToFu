"""Offline migration identity, crash publication, cursor and memory boundaries."""

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tracemalloc
from uuid import uuid4

import pytest

from lib.storage_sidecar import durability
from lib.storage_sidecar.generated_tofudb_migration import MAX_CURSOR_BYTES, MAX_DOMAINS
from lib.storage_sidecar.tofudb_migration_checkpoint import (
    MigrationBinding, MigrationCheckpoint, MigrationCheckpointError,
)
from lib.storage_sidecar.tofudb_migration_source import MigrationSourceError, SQLiteMigrationSource


pytestmark = pytest.mark.unit
_EMPTY_SHA = hashlib.sha256(b'').hexdigest()
_ROW_SHA = hashlib.sha256(b'one verified row').hexdigest()


@pytest.fixture
def binding():
    return MigrationBinding('a' * 64, 58, str(uuid4()), 'b' * 64)


def _advance(checkpoint, *, rows=1, cursor=None, complete=False, **overrides):
    arguments = dict(cursor=[rows] if cursor is None else cursor, source_rows=rows,
                     source_sha256=_ROW_SHA, target_rows=rows, target_sha256=_ROW_SHA,
                     complete=complete)
    arguments.update(overrides)
    checkpoint.record_verified_prefix('records', **arguments)


def test_checkpoint_reopens_and_keeps_read_views_isolated(tmp_path, binding):
    path = tmp_path / 'migration.json'
    with MigrationCheckpoint(path, binding) as checkpoint:
        _advance(checkpoint)
        view = checkpoint.domain('records')
        view['cursor'].append('not owned')
        assert checkpoint.domain('records')['cursor'] == [1]
    with MigrationCheckpoint(path, binding) as checkpoint:
        assert checkpoint.domain('records')['rows'] == 1
        _advance(checkpoint, complete=True)
        assert checkpoint.domain('records')['cursor'] == []
        stable = path.read_bytes()
        _advance(checkpoint, complete=True)
        assert path.read_bytes() == stable
        with pytest.raises(MigrationCheckpointError):
            _advance(checkpoint, rows=2)


@pytest.mark.parametrize('field,value', [
    ('source_sha256', 'c' * 64), ('source_schema_version', 59),
    # Fixed UUID, not uuid4(): parametrize values must be identical across
    # pytest-xdist workers or collection diverges and the run errors out.
    ('target_authority_uuid', 'e3f1a2b4-5c6d-4e7f-8a9b-0c1d2e3f4a5b'),
    ('plan_sha256', 'd' * 64),
])
def test_checkpoint_rejects_replaced_source_target_or_plan(tmp_path, binding, field, value):
    path = tmp_path / 'migration.json'
    with MigrationCheckpoint(path, binding) as checkpoint:
        _advance(checkpoint)
    before = path.read_bytes()
    with pytest.raises(MigrationCheckpointError, match='binding'):
        MigrationCheckpoint(path, replace(binding, **{field: value}))
    assert path.read_bytes() == before


@pytest.mark.parametrize('overrides', [
    {'target_rows': 2}, {'target_sha256': 'e' * 64}, {'source_rows': True},
    {'cursor': ['x' * (MAX_CURSOR_BYTES + 1)]}, {'cursor': [None]},
    {'cursor': []},
])
def test_unverified_or_unbounded_progress_does_not_publish(tmp_path, binding, overrides):
    path = tmp_path / 'migration.json'
    with MigrationCheckpoint(path, binding) as checkpoint:
        before = path.read_bytes()
        with pytest.raises(MigrationCheckpointError):
            _advance(checkpoint, **overrides)
        assert path.read_bytes() == before
        assert checkpoint.domain('records') is None


def test_checkpoint_refuses_regressed_or_reused_cursor(tmp_path, binding):
    with MigrationCheckpoint(tmp_path / 'migration.json', binding) as checkpoint:
        _advance(checkpoint, rows=2)
        for arguments in ({'rows': 1}, {'rows': 3, 'cursor': [2]}, {'rows': 2, 'cursor': [3]}):
            with pytest.raises(MigrationCheckpointError):
                _advance(checkpoint, **arguments)


def test_checkpoint_is_exclusively_locked_and_rejects_linked_targets(tmp_path, binding):
    path = tmp_path / 'migration.json'
    with MigrationCheckpoint(path, binding):
        with pytest.raises(BlockingIOError):
            MigrationCheckpoint(path, binding)
    linked = tmp_path / 'linked.json'
    linked.symlink_to(path)
    with pytest.raises(MigrationCheckpointError):
        MigrationCheckpoint(linked, binding)


@pytest.mark.parametrize('failure', ['file-sync', 'replace', 'directory-sync'])
def test_publish_failure_reopens_one_complete_verified_state(tmp_path, binding, monkeypatch, failure):
    path = tmp_path / 'migration.json'
    with MigrationCheckpoint(path, binding) as checkpoint:
        original_sync, original_replace = durability.os.fsync, durability.os.replace
        calls = 0

        def injected_sync(descriptor):
            nonlocal calls
            calls += 1
            if (failure == 'file-sync' and calls == 1
                    or failure == 'directory-sync' and calls == 2):
                raise OSError('injected storage failure')
            return original_sync(descriptor)

        def injected_replace(source, target):
            if failure == 'replace':
                raise OSError('injected storage failure')
            return original_replace(source, target)

        with monkeypatch.context() as patch:
            patch.setattr(durability.os, 'fsync', injected_sync)
            patch.setattr(durability.os, 'replace', injected_replace)
            with pytest.raises(OSError):
                _advance(checkpoint)
            with pytest.raises(MigrationCheckpointError, match='reopen'):
                _advance(checkpoint, rows=2)
    with MigrationCheckpoint(path, binding) as reopened:
        entry = reopened.domain('records')
        assert (entry is not None) == (failure == 'directory-sync')
        _advance(reopened, rows=2)
        assert reopened.domain('records')['rows'] == 2
    assert not path.with_name(path.name + '.new').exists()


def test_checkpoint_rejects_corruption_and_bounds_all_domains(tmp_path, binding):
    path = tmp_path / 'migration.json'
    with MigrationCheckpoint(path, binding) as checkpoint:
        for index in range(MAX_DOMAINS):
            checkpoint.record_verified_prefix(
                f'domain_{index}', cursor=[], source_rows=0, source_sha256=_EMPTY_SHA,
                target_rows=0, target_sha256=_EMPTY_SHA, complete=True,
            )
        before = path.read_bytes()
        with pytest.raises(MigrationCheckpointError):
            _advance(checkpoint)
        assert path.read_bytes() == before
    document = json.loads(path.read_text())
    document['body']['domains']['domain_0']['rows'] = 100
    path.write_text(json.dumps(document))
    with pytest.raises(MigrationCheckpointError, match='checksum'):
        MigrationCheckpoint(path, binding)


def test_completed_domains_release_large_cursors_within_aggregate_budget(tmp_path, binding):
    path = tmp_path / 'migration.json'
    with MigrationCheckpoint(path, binding) as checkpoint:
        for index in range(MAX_DOMAINS):
            arguments = dict(cursor=['x' * 8000], source_rows=1, source_sha256=_ROW_SHA,
                             target_rows=1, target_sha256=_ROW_SHA)
            checkpoint.record_verified_prefix(f'domain_{index}', **arguments)
            checkpoint.record_verified_prefix(f'domain_{index}', **arguments, complete=True)
        assert path.stat().st_size < 20 * 1024
    with MigrationCheckpoint(path, binding) as checkpoint:
        for index in range(MAX_DOMAINS):
            assert checkpoint.domain(f'domain_{index}') == {
                'cursor': [], 'rows': 1, 'sha256': _ROW_SHA, 'complete': True,
            }


@pytest.mark.parametrize('phase', ['after-file-sync', 'after-replace', 'after-directory-sync'])
def test_process_exit_without_cleanup_resumes_verified_progress(tmp_path, binding, phase):
    path = tmp_path / 'migration.json'
    with MigrationCheckpoint(path, binding):
        pass
    script = '''
import json, os, sys
from pathlib import Path
from lib.storage_sidecar import durability
from lib.storage_sidecar.tofudb_migration_checkpoint import MigrationBinding, MigrationCheckpoint
checkpoint = MigrationCheckpoint(Path(sys.argv[1]), MigrationBinding(**json.loads(sys.argv[2])))
phase = sys.argv[3]
original_sync, original_replace = os.fsync, os.replace
calls = 0
def sync(descriptor):
    global calls
    original_sync(descriptor)
    calls += 1
    if (phase == 'after-file-sync' and calls == 1
            or phase == 'after-directory-sync' and calls == 2):
        os._exit(73)
def replace(source, target):
    original_replace(source, target)
    if phase == 'after-replace':
        os._exit(73)
durability.os.fsync, durability.os.replace = sync, replace
checkpoint.record_verified_prefix('records', cursor=[1], source_rows=1,
    source_sha256='a'*64, target_rows=1, target_sha256='a'*64)
raise AssertionError('crash boundary was not reached')
'''
    result = subprocess.run([sys.executable, '-c', script, str(path),
                             json.dumps(asdict(binding)), phase],
                            cwd=Path(__file__).resolve().parents[2], timeout=15,
                            capture_output=True, text=True)
    assert result.returncode == 73, result.stderr
    with MigrationCheckpoint(path, binding) as reopened:
        entry = reopened.domain('records')
        assert (entry is not None) == (phase != 'after-file-sync')
        _advance(reopened, rows=2)
    assert not path.with_name(path.name + '.new').exists()


def _source(path: Path, *, rows=7, payload_bytes=64):
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('CREATE TABLE docs (owner INTEGER, key TEXT, value BLOB, '
                       'PRIMARY KEY(owner,key))')
    for number in range(rows):
        connection.execute('INSERT INTO docs VALUES(?,?,?)',
                           (number // 3 + 1, f'key-{number:04}', b'x' * payload_bytes))
    connection.commit()
    return connection


def test_source_reads_pinned_snapshot_and_resumes_exact_keyset(tmp_path):
    path = tmp_path / 'source.db'
    connection = _source(path)
    connection.close()
    before = path.read_bytes()
    with SQLiteMigrationSource(path) as source:
        assert source.tables == ('docs',)
        rows = list(source.rows('docs', ('owner', 'key', 'value'), ('owner', 'key'), batch_rows=2))
        resume = list(source.rows('docs', ('owner', 'key', 'value'), ('owner', 'key'),
                                  after=(rows[2]['owner'], rows[2]['key']), batch_rows=1))
        assert resume == rows[3:]
        assert len(rows) == 7
    assert path.read_bytes() == before


def test_source_reports_a_concurrent_commit_without_mixing_snapshots(tmp_path):
    path = tmp_path / 'source.db'
    writer = _source(path)
    try:
        with pytest.raises(MigrationSourceError, match='changed'):
            with SQLiteMigrationSource(path) as source:
                writer.execute('INSERT INTO docs VALUES(99,?,?)', ('late', b'late'))
                writer.commit()
                rows = list(source.rows('docs', ('owner', 'key'), ('owner', 'key')))
                assert len(rows) == 7
    finally:
        writer.close()


def test_source_rejects_wrong_order_oversized_rows_and_driver_materialization(tmp_path):
    path = tmp_path / 'source.db'
    _source(path, rows=1, payload_bytes=2 * 1024 * 1024).close()
    with SQLiteMigrationSource(path, maximum_row_bytes=64 * 1024) as source:
        with pytest.raises(MigrationSourceError, match='primary key'):
            list(source.rows('docs', ('owner', 'key'), ('key', 'owner')))
        tracemalloc.start()
        try:
            with pytest.raises(MigrationSourceError, match='SQLite admission'):
                next(source.rows('docs', ('owner', 'key', 'value'), ('owner', 'key')))
            assert tracemalloc.get_traced_memory()[1] < 1024 * 1024
        finally:
            tracemalloc.stop()


def test_source_memory_does_not_grow_with_the_domain(tmp_path):
    path = tmp_path / 'source.db'
    _source(path, rows=96, payload_bytes=256 * 1024).close()
    tracemalloc.start()
    try:
        with SQLiteMigrationSource(path) as source:
            count = sum(1 for _ in source.rows('docs', ('owner', 'key', 'value'),
                                              ('owner', 'key'), batch_rows=7))
            assert count == 96
            assert tracemalloc.get_traced_memory()[1] < 4 * 1024 * 1024
    finally:
        tracemalloc.stop()


def test_source_keeps_user_tables_resembling_sqlite_internal_names(tmp_path):
    path = tmp_path / 'source.db'
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE sqliteXuser_state (id INTEGER PRIMARY KEY, value TEXT)')
    connection.execute("INSERT INTO sqliteXuser_state VALUES (1,'retained')")
    connection.commit()
    connection.close()
    with SQLiteMigrationSource(path) as source:
        assert source.tables == ('sqliteXuser_state',)
        assert list(source.rows('sqliteXuser_state', ('id', 'value'), ('id',))) == [
            {'id': 1, 'value': 'retained'},
        ]


@pytest.mark.parametrize('as_text', [False, True])
def test_source_blob_chunks_preserve_large_values_under_ordinary_row_limit(tmp_path, as_text):
    import hashlib
    path = tmp_path / 'blob.db'
    payload = ('保留🙂' * (1024 * 1024)) if as_text else b'large-event\x00' * (1024 * 1024)
    expected = hashlib.sha256(payload.encode('utf-8') if as_text else payload).hexdigest()
    size = len(payload.encode('utf-8') if as_text else payload)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE docs(owner INTEGER, key TEXT, value BLOB, PRIMARY KEY(owner,key))')
        db.execute('INSERT INTO docs VALUES(?,?,?)', (7, 'event', payload))
    del payload
    with SQLiteMigrationSource(path, maximum_row_bytes=64 * 1024) as source:
        with pytest.raises(MigrationSourceError, match='SQLite admission'):
            next(source.rows('docs', ('owner', 'key', 'value'), ('owner', 'key')))
        tracemalloc.start()
        try:
            assert source.blob_size('docs', 'value', ('owner', 'key'), (7, 'event')) == size
            digest = hashlib.sha256()
            observed = 0
            for chunk in source.blob_chunks('docs', 'value', ('owner', 'key'), (7, 'event'),
                                            maximum_bytes=size, chunk_bytes=32 * 1024):
                assert len(chunk) <= 32 * 1024
                observed += len(chunk)
                digest.update(chunk)
            assert observed == size
            assert digest.hexdigest() == expected
            assert tracemalloc.get_traced_memory()[1] < 1024 * 1024
        finally:
            tracemalloc.stop()
        with pytest.raises(MigrationSourceError, match='domain byte budget'):
            next(source.blob_chunks('docs', 'value', ('owner', 'key'), (7, 'event'),
                                    maximum_bytes=size-1, chunk_bytes=32768))
        with pytest.raises(MigrationSourceError, match='primary key'):
            next(source.blob_chunks('docs', 'value', ('key',), ('event',), maximum_bytes=size))
        with pytest.raises(MigrationSourceError, match='missing'):
            next(source.blob_chunks('docs', 'value', ('owner', 'key'), (8, 'event'),
                                    maximum_bytes=size, chunk_bytes=32768))
        iterator = source.blob_chunks('docs', 'value', ('owner', 'key'), (7, 'event'),
                                      maximum_bytes=size, chunk_bytes=32768)
        next(iterator)
        iterator.close()
        assert source.point('docs', ('owner', 'key'), ('owner', 'key'), (7, 'event')) == {'owner': 7, 'key': 'event'}


def test_primary_key_prefix_scan_stays_in_scope_and_rejects_cross_scope_resume(tmp_path):
    path = tmp_path / 'prefix.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE rows (conversation TEXT, owner INTEGER, identity TEXT, value TEXT, PRIMARY KEY(conversation,owner,identity))')
        db.executemany('INSERT INTO rows VALUES (?,?,?,?)', [('a', 11, str(i), 'owned') for i in range(4)] + [('a', 12, 'foreign', 'foreign'), ('b', 11, 'neighbor', 'neighbor')])
    with SQLiteMigrationSource(path) as source:
        columns = ('conversation', 'owner', 'identity', 'value')
        keys = columns[:3]
        rows = list(source.rows('rows', columns, keys, key_prefix=('a', 11), batch_rows=1))
        assert [row['identity'] for row in rows] == ['0', '1', '2', '3']
        assert [row['identity'] for row in source.rows('rows', columns, keys, key_prefix=('a', 11), after=('a', 11, '1'), batch_rows=1)] == ['2', '3']
        for prefix, after in [((), None), (('a', True), None), (('a', 2**64), None), (('a', 11), ('a', 12, '0'))]:
            with pytest.raises(MigrationSourceError, match='prefix'):
                list(source.rows('rows', columns, keys, key_prefix=prefix, after=after))
