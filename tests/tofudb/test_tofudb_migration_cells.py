"""Raw SQLite cell metadata and slices stay bounded and lossless."""
import sqlite3
import tracemalloc

import pytest

from lib.storage_authority_sql.migration.tofudb_migration_source import SQLiteMigrationSource, MigrationSourceError

pytestmark = pytest.mark.unit


def _seed(path):
    with sqlite3.connect(path) as connection:
        connection.executescript('''CREATE TABLE cells (id INTEGER PRIMARY KEY,
            text_value TEXT, blob_value BLOB, number INTEGER, fraction REAL, empty_value TEXT);
            INSERT INTO cells VALUES (1, CAST(zeroblob(1000000) AS TEXT), x'00ff',
                9223372036854775807, 1.25, NULL);''')


def test_cell_metadata_never_materializes_oversized_text_and_preserves_types(tmp_path):
    path = tmp_path / 'source.db'
    _seed(path)
    with SQLiteMigrationSource(path, maximum_row_bytes=1024) as source:
        tracemalloc.start()
        try:
            metadata = source.cell_metadata('cells', ['text_value', 'blob_value', 'number',
                                                       'fraction', 'empty_value'], ['id'], [1])
            assert tracemalloc.get_traced_memory()[1] < 512 * 1024
        finally:
            tracemalloc.stop()
        assert metadata == [{'type': 'text', 'bytes': 1000000}, {'type': 'blob', 'bytes': 2},
                            {'type': 'integer', 'value': 9223372036854775807},
                            {'type': 'real', 'value': 1.25}, {'type': 'null', 'value': None}]
        assert source.blob_slice('cells', 'text_value', ['id'], [1], offset=999997,
                                 size=3, maximum_bytes=1000000) == b'\0' * 3
        assert source.blob_slice('cells', 'blob_value', ['id'], [1], offset=0,
                                 size=2, maximum_bytes=2) == b'\x00\xff'


@pytest.mark.parametrize('offset,size,maximum', [(-1, 1, 1000000), (0, 1025, 1000000),
                                                (1000000, 1, 1000000), (0, 1, 999999)])
def test_cell_slice_refuses_invalid_or_oversized_range(tmp_path, offset, size, maximum):
    path = tmp_path / 'source.db'
    _seed(path)
    with SQLiteMigrationSource(path, maximum_row_bytes=1024) as source:
        with pytest.raises(MigrationSourceError):
            source.blob_slice('cells', 'text_value', ['id'], [1], offset=offset,
                              size=size, maximum_bytes=maximum)


def test_metadata_requires_exact_primary_key_and_open_source(tmp_path):
    path = tmp_path / 'source.db'
    _seed(path)
    with SQLiteMigrationSource(path) as source:
        with pytest.raises(MigrationSourceError, match='primary key'):
            source.cell_metadata('cells', ['text_value'], ['number'], [1])
        with pytest.raises(MigrationSourceError, match='missing'):
            source.cell_metadata('cells', ['text_value'], ['id'], [2])
    with pytest.raises(MigrationSourceError, match='closed'):
        source.cell_metadata('cells', ['text_value'], ['id'], [1])


def test_added_column_default_cells_survive_incremental_blob_gap(tmp_path):
    """ALTER TABLE ADD COLUMN serves schema DEFAULTs the blob handle cannot open.

    Rows that predate the alteration have no physical field for the added
    column: SQL reads materialize the DEFAULT, while sqlite3_blob_open sees
    NULL and refuses the handle. The migration source must still stream the
    exact default bytes through size/chunk/slice/metadata paths.
    """
    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as connection:
        connection.executescript('''CREATE TABLE evolved (id TEXT PRIMARY KEY, body TEXT);
            INSERT INTO evolved VALUES ('old', 'payload');
            ALTER TABLE evolved ADD COLUMN note TEXT NOT NULL DEFAULT 'seed';
            INSERT INTO evolved (id, body, note) VALUES ('new', 'payload', 'real');''')
    with SQLiteMigrationSource(path) as source:
        assert source.blob_size('evolved', 'note', ['id'], ['old']) == 4
        assert b''.join(source.blob_chunks('evolved', 'note', ['id'], ['old'],
                                           maximum_bytes=64)) == b'seed'
        assert source.blob_slice('evolved', 'note', ['id'], ['old'], offset=1,
                                 size=2, maximum_bytes=64) == b'ee'
        assert source.cell_metadata('evolved', ['note'], ['id'], ['old']) == [
            {'type': 'text', 'bytes': 4}]
        assert b''.join(source.blob_chunks('evolved', 'note', ['id'], ['new'],
                                           maximum_bytes=64)) == b'real'
