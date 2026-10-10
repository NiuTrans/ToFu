"""Source coverage is a bounded pre-target gate, never a cutover certificate."""
import json
import sqlite3

import pytest

from lib.storage_sidecar.tofudb_migration_coverage import source_table_coverage
from lib.storage_sidecar.tofudb_migration_source import MigrationSourceError, SQLiteMigrationSource

pytestmark = pytest.mark.unit


def test_presence_does_not_load_large_body_or_ignore_internal_looking_names(tmp_path):
    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as connection:
        connection.executescript('''CREATE TABLE mapped (body BLOB);
            CREATE TABLE empty (body BLOB);
            CREATE TABLE sqliteXuser (body BLOB);
            INSERT INTO mapped VALUES (zeroblob(200000));
            INSERT INTO sqliteXuser VALUES (zeroblob(200000));''')
    plan = {'id': 'test', 'domains': [{'tables': [{'name': 'mapped'}]}]}
    with SQLiteMigrationSource(path, maximum_row_bytes=1024) as source:
        report = source_table_coverage(source, plan)
        assert report['unmappedPopulatedTables'] == ['sqliteXuser']
        assert report['sourceTableCoverageComplete'] is False
        with pytest.raises(MigrationSourceError, match='unavailable'):
            source.table_has_rows('mapped; DROP TABLE mapped')
    with pytest.raises(MigrationSourceError, match='closed'):
        source.table_has_rows('mapped')


def test_cli_refuses_unmapped_data_before_starting_native_target(tmp_path, monkeypatch, capsys):
    from lib.storage_sidecar import tofudb_migration_import as importer

    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as connection:
        connection.executescript('CREATE TABLE durable_unknown (id INTEGER); '
                                 'INSERT INTO durable_unknown VALUES (1);')
    def forbidden(*args, **kwargs):
        pytest.fail('coverage failure must precede native target startup')
    monkeypatch.setattr(importer, 'OfflineMigrationSession', forbidden)
    code = importer.main([
        '--source', str(path), '--checkpoint', str(tmp_path / 'checkpoint'),
        '--binary', str(tmp_path / 'binary'), '--target', str(tmp_path / 'target'),
        '--owner-id', '1', '--tenant-id', '1', '--authority-uuid', 'test',
    ])
    assert code == 2
    assert 'durable_unknown' in json.loads(capsys.readouterr().err)['message']
    assert not (tmp_path / 'target').exists()
    assert not (tmp_path / 'checkpoint').exists()


def test_empty_unknown_table_is_reported_without_claiming_cutover(tmp_path):
    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE unknown (id INTEGER)')
    with SQLiteMigrationSource(path) as source:
        report = source_table_coverage(source, {'id': 'test', 'domains': []})
    assert report['sourceTableCoverageComplete'] is True
    assert report['scope'] == 'source-table-coverage-only'
    assert report['tables'] == [{'table': 'unknown', 'mapped': False, 'populated': False}]


def test_import_rechecks_coverage_inside_its_own_source_snapshot(tmp_path):
    from lib.storage_sidecar.tofudb_migration_import import import_sqlite_authority, MigrationImportError

    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as connection:
        connection.executescript('CREATE TABLE newly_populated (id INTEGER); '
                                 'INSERT INTO newly_populated VALUES (1);')
    with pytest.raises(MigrationImportError, match='newly_populated'):
        import_sqlite_authority(path, None, tmp_path / 'checkpoint', 'test',
                                require_complete_source_coverage=True)
    assert not (tmp_path / 'checkpoint').exists()
