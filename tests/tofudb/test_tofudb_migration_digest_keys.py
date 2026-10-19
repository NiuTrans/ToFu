"""Binary receipt identities keep index order, snapshot isolation and bounds."""
import sqlite3
import tracemalloc

import pytest

from lib.storage_authority_sql.migration.tofudb_migration_source import MigrationSourceError, SQLiteMigrationSource

pytestmark = pytest.mark.unit
COLUMNS = ('command_key', 'response_json')


def _source(path, count=259):
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE receipts (command_key BLOB PRIMARY KEY, '
                           'response_json BLOB) WITHOUT ROWID')
        connection.executemany('INSERT INTO receipts VALUES (?, ?)', [
            (index.to_bytes(32, 'big'), b'\x00\xff' + str(index).encode())
            for index in reversed(range(count))
        ])


def test_binary_pages_resume_point_and_preserve_exact_payload(tmp_path):
    path = tmp_path / 'source.db'
    _source(path)
    with SQLiteMigrationSource(path) as source:
        rows = list(source.digest_key_rows('receipts', COLUMNS, 'command_key', batch_rows=7))
        assert [row['command_key'] for row in rows] == [f'{index:064x}' for index in range(259)]
        resumed = list(source.digest_key_rows('receipts', COLUMNS, 'command_key',
                                              after=rows[127]['command_key'], batch_rows=13))
        assert resumed == rows[128:]
        for row in (rows[0], rows[128], rows[-1]):
            assert source.digest_key_point('receipts', COLUMNS, 'command_key',
                                            row['command_key']) == row
        assert rows[-1]['response_json'] == b'\x00\xff258'
        assert source.digest_key_point('receipts', COLUMNS, 'command_key', 'f' * 64) is None
        iterator = source.digest_key_rows('receipts', COLUMNS, 'command_key', batch_rows=1)
        next(iterator)
        iterator.close()
        assert source.table_has_rows('receipts')
    with pytest.raises(MigrationSourceError, match='closed'):
        source.digest_key_point('receipts', COLUMNS, 'command_key', '0' * 64)


@pytest.mark.parametrize('cursor', ['', 'f' * 63, 'A' * 64, 'z' * 64, '00 ' * 32, 1, b'0' * 32])
def test_noncanonical_digest_cursor_is_rejected(tmp_path, cursor):
    path = tmp_path / 'source.db'
    _source(path, 1)
    with SQLiteMigrationSource(path) as source:
        with pytest.raises(MigrationSourceError, match='SHA-256'):
            list(source.digest_key_rows('receipts', COLUMNS, 'command_key', after=cursor))
        with pytest.raises(MigrationSourceError, match='SHA-256'):
            source.digest_key_point('receipts', COLUMNS, 'command_key', cursor)


@pytest.mark.parametrize('key', [b'short', '0' * 32])
def test_wrong_source_key_type_or_width_is_not_silently_skipped(tmp_path, key):
    path = tmp_path / 'source.db'
    _source(path, 0)
    with sqlite3.connect(path) as connection:
        connection.execute('INSERT INTO receipts VALUES (?, ?)', (key, b'body'))
    with SQLiteMigrationSource(path) as source:
        with pytest.raises(MigrationSourceError, match='SHA-256 BLOB'):
            list(source.digest_key_rows('receipts', COLUMNS, 'command_key'))


def test_digest_scan_requires_primary_key_and_enforces_row_memory_budget(tmp_path):
    path = tmp_path / 'source.db'
    _source(path, 1)
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE receipts SET response_json=zeroblob(1000000)')
    with SQLiteMigrationSource(path, maximum_row_bytes=1024) as source:
        with pytest.raises(MigrationSourceError, match='primary key'):
            list(source.digest_key_rows('receipts', COLUMNS, 'response_json'))
        tracemalloc.start()
        try:
            with pytest.raises(MigrationSourceError):
                list(source.digest_key_rows('receipts', COLUMNS, 'command_key'))
            assert tracemalloc.get_traced_memory()[1] < 512 * 1024
            with pytest.raises(MigrationSourceError):
                source.digest_key_point('receipts', COLUMNS, 'command_key', '0' * 64)
        finally:
            tracemalloc.stop()


def test_digest_pages_hold_one_snapshot_and_reject_committed_source_changes(tmp_path):
    path = tmp_path / 'source.db'
    _source(path, 3)
    writer = sqlite3.connect(path)
    writer.execute('PRAGMA journal_mode=WAL')
    source = SQLiteMigrationSource(path)
    try:
        pages = source.digest_key_rows('receipts', COLUMNS, 'command_key', batch_rows=1)
        first = next(pages)
        writer.execute('INSERT INTO receipts VALUES (?, ?)', (b'\xff' * 32, b'new'))
        writer.commit()
        assert len([first, *pages]) == 3
        with pytest.raises(MigrationSourceError, match='changed'):
            source.close()
    finally:
        source.close(verify_unchanged=False)
        writer.close()
