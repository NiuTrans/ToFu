"""Retired data is covered only by a complete current mapped copy."""
import json
import sqlite3

import pytest

from lib.storage_sidecar.tofudb_migration_coverage import source_table_coverage
from lib.storage_sidecar.tofudb_migration_source import SQLiteMigrationSource

pytestmark = pytest.mark.unit

PLAN = {'id': 'test', 'retiredTradingMirrorPolicy': 'exact-current-plugin-rows',
        'domains': [{'tables': [{'name': 'storage_plugin_manifests'},
                                {'name': 'storage_plugin_rows'}]}]}


def _fixture(tmp_path):
    path = tmp_path / 'source.db'
    manifest = {'namespace': 'tofu.trading', 'operations': [{
        'action': 'legacy_scan', 'table': 'rows', 'legacy_table': 'trading_sample',
        'legacy_columns': ['id', 'body'], 'legacy_order_by': ['id'],
    }]}
    with sqlite3.connect(path) as connection:
        connection.executescript('''
            CREATE TABLE storage_plugin_manifests
                (namespace TEXT PRIMARY KEY, manifest_json TEXT);
            CREATE TABLE storage_plugin_rows
                (namespace TEXT, table_name TEXT, row_key TEXT, document_json TEXT,
                 PRIMARY KEY(namespace, table_name, row_key));
            CREATE TABLE trading_sample (id INTEGER PRIMARY KEY, body TEXT);
            INSERT INTO trading_sample VALUES (1, '完整历史');
        ''')
        connection.execute('INSERT INTO storage_plugin_manifests VALUES (?, ?)',
                           ('tofu.trading', json.dumps(manifest)))
        connection.execute('INSERT INTO storage_plugin_rows VALUES (?, ?, ?, ?)',
                           ('tofu.trading', 'rows', 'sample', json.dumps({
                               'logical_table': 'trading_sample',
                               'row': {'id': 1, 'body': '完整历史'},
                           })))
    return path


def _report(path, plan=PLAN):
    with SQLiteMigrationSource(path) as source:
        return source_table_coverage(source, plan)


def test_complete_current_copy_proves_coverage_without_mutating_source(tmp_path):
    path = _fixture(tmp_path)
    before = path.read_bytes()
    report = _report(path)
    assert report['sourceTableCoverageComplete'] is True
    assert report['verifiedMirrors']['trading_sample']['rows'] == 1
    assert report['verifiedMirrors']['trading_sample']['authorityTable'] == 'storage_plugin_rows'
    assert path.read_bytes() == before


@pytest.mark.parametrize('mutation', [
    "DELETE FROM storage_plugin_rows",
    "UPDATE storage_plugin_rows SET namespace='another.plugin'",
    "UPDATE trading_sample SET body='changed historical content'",
    "ALTER TABLE trading_sample ADD COLUMN forensic_data TEXT DEFAULT 'unique history'",
    "INSERT INTO storage_plugin_rows SELECT namespace, table_name, 'duplicate', document_json FROM storage_plugin_rows",
    "UPDATE storage_plugin_rows SET document_json='malformed'",
])
def test_missing_different_duplicated_or_unreadable_copy_stays_blocked(tmp_path, mutation):
    path = _fixture(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(mutation)
    report = _report(path)
    assert report['sourceTableCoverageComplete'] is False
    assert report['unmappedPopulatedTables'] == ['trading_sample']
    assert report['verifiedMirrors'] == {}


def test_mirror_does_not_count_when_current_authority_is_not_mapped(tmp_path):
    path = _fixture(tmp_path)
    report = _report(path, {**PLAN, 'domains': []})
    assert 'trading_sample' in report['unmappedPopulatedTables']
    assert report['verifiedMirrors'] == {}


def test_bounded_proof_failure_cannot_admit_partial_results(tmp_path, monkeypatch):
    from lib.storage_sidecar import tofudb_migration_mirrors
    path = _fixture(tmp_path)
    monkeypatch.setattr(tofudb_migration_mirrors, '_MAX_ROWS', 0)
    report = _report(path)
    assert report['mirrorVerificationFailed'] is True
    assert report['verifiedMirrors'] == {}
    assert 'trading_sample' in report['unmappedPopulatedTables']
