"""Global receipt audit preserves the live dual-format refusal boundary."""
import sqlite3

import pytest

from lib.storage_sidecar.receipt_codec import command_receipt_key_v2, encode_receipt_response
from lib.storage_sidecar.tofudb_migration_receipt_source import audit_receipt_source
from lib.storage_sidecar.tofudb_migration_source import SQLiteMigrationSource, MigrationSourceError

pytestmark = pytest.mark.unit


def seed(path, *, duplicate=False):
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE storage_command_receipts(command_id TEXT PRIMARY KEY,
            operation TEXT, request_digest TEXT, response_json BLOB, committed_at_ms INTEGER);
            CREATE TABLE storage_command_receipts_v2(command_key BLOB PRIMARY KEY,
            operation TEXT, request_digest BLOB, response_json BLOB, committed_at_ms INTEGER) WITHOUT ROWID;''')
        db.execute('INSERT INTO storage_command_receipts VALUES (?,?,?,?,?)',
                   ('legacy', 'record.put', '12'*32, b'{"old":true}', 123))
        db.execute('INSERT INTO storage_command_receipts_v2 VALUES (?,?,?,?,?)',
                   (command_receipt_key_v2('legacy' if duplicate else 'new'), 'record.put',
                    bytes.fromhex('12'*32), encode_receipt_response({'content': 'x'*100000}), 124))


def test_both_formats_are_fingerprinted_without_owner_assignment(tmp_path):
    path = tmp_path/'source.db'; seed(path)
    with SQLiteMigrationSource(path) as source:
        result = audit_receipt_source(source)
        assert [v['rows'] for v in result['tables'].values()] == [1, 1]
        assert result['duplicateIdentities'] == 0
        assert 'not-native-owner' in result['scope']
        with pytest.raises(MigrationSourceError, match='budget exhausted'):
            audit_receipt_source(source, maximum_rows=1)
        assert audit_receipt_source(source) == result


def test_duplicate_identity_is_rejected_even_with_identical_receipt(tmp_path):
    path = tmp_path/'source.db'; seed(path, duplicate=True)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE storage_command_receipts_v2 SET response_json=?, committed_at_ms=123', (b'{"old":true}',))
    with SQLiteMigrationSource(path) as source:
        with pytest.raises(MigrationSourceError, match='multiple storage formats'):
            audit_receipt_source(source)


@pytest.mark.parametrize('column,value', [('request_digest', b'short'), ('committed_at_ms', 0)])
def test_invalid_source_metadata_fails_closed(tmp_path, column, value):
    path = tmp_path/'source.db'; seed(path)
    with sqlite3.connect(path) as db:
        db.execute(f'UPDATE storage_command_receipts_v2 SET {column}=?', (value,))
    with SQLiteMigrationSource(path) as source:
        with pytest.raises(MigrationSourceError, match='invalid'):
            audit_receipt_source(source)


def test_deadline_exhaustion_closes_iterator_without_a_partial_report(tmp_path, monkeypatch):
    import lib.storage_sidecar.tofudb_migration_receipt_source as audit_module
    path = tmp_path/'source.db'; seed(path)
    with SQLiteMigrationSource(path) as source:
        ticks = iter([0.0, 61.0])
        with monkeypatch.context() as patch:
            patch.setattr(audit_module.time, 'monotonic', lambda: next(ticks))
            with pytest.raises(MigrationSourceError, match='budget exhausted'):
                audit_receipt_source(source)
        assert len(audit_receipt_source(source)['tables']) == 2


def test_unknown_source_columns_are_not_silently_ignored(tmp_path):
    path = tmp_path/'source.db'; seed(path)
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE storage_command_receipts ADD COLUMN replay_scope TEXT')
    with SQLiteMigrationSource(path) as source:
        with pytest.raises(MigrationSourceError, match='unaccounted columns'):
            audit_receipt_source(source)
