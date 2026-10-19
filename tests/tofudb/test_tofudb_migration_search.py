"""Retired FTS provenance and archived-input requirements at source admission."""
import copy
import sqlite3

import pytest

from lib.storage_authority_sql.migration.tofudb_migration_import import PLAN
from lib.storage_authority_sql.migration.tofudb_migration_coverage import source_table_coverage
from lib.storage_authority_sql.migration.tofudb_migration_search import verified_retired_search
from lib.storage_authority_sql.migration.tofudb_migration_source import SQLiteMigrationSource, MigrationSourceError

pytestmark = pytest.mark.unit


def seed(path, tokenizer='unicode61'):
    with sqlite3.connect(path) as db:
        db.execute(f"CREATE VIRTUAL TABLE conversations_fts USING fts5(search_text, tokenize='{tokenizer}')")
        db.execute("INSERT INTO conversations_fts(rowid,search_text) VALUES(91,'独有正文 unknown history')")


def test_real_fts_requires_exact_archived_inputs_and_preserves_logical_rowids(tmp_path):
    path = tmp_path / 'source.db'
    seed(path)
    with SQLiteMigrationSource(path) as source:
        result = source_table_coverage(source, PLAN)
        assert result['sourceTableCoverageComplete']
        assert set(result['verifiedRebuilds']) == {
            'conversations_fts', 'conversations_fts_data',
            'conversations_fts_docsize', 'conversations_fts_idx'}
        assert source.point('conversations_fts_content', ['id', 'c0'], ['id'], [91]) == {
            'id': 91, 'c0': '独有正文 unknown history'}
        for missing in ('retired.conversations_fts_config', 'retired_large.conversations_fts_content'):
            plan = copy.deepcopy(PLAN)
            plan['domains'] = [d for d in plan['domains'] if d['name'] != missing]
            assert verified_retired_search(source, plan) == {}
            assert not source_table_coverage(source, plan)['sourceTableCoverageComplete']
    with pytest.raises(MigrationSourceError, match='closed'):
        source.table_definition('conversations_fts')


def test_names_and_mapped_content_alone_cannot_establish_engine_provenance(tmp_path):
    path = tmp_path / 'fake.db'
    with sqlite3.connect(path) as db:
        for suffix in ('', '_config', '_content', '_data', '_docsize', '_idx'):
            db.execute(f'CREATE TABLE conversations_fts{suffix}(id INTEGER PRIMARY KEY, private_data TEXT)')
            db.execute(f"INSERT INTO conversations_fts{suffix} VALUES (1,'must retain')")
    with SQLiteMigrationSource(path) as source:
        assert verified_retired_search(source, PLAN) == {}
        assert 'conversations_fts_data' in source_table_coverage(source, PLAN)['unmappedPopulatedTables']


def test_different_fts_configuration_fails_closed(tmp_path):
    path = tmp_path / 'source.db'
    seed(path, 'porter')
    with SQLiteMigrationSource(path) as source:
        assert verified_retired_search(source, PLAN) == {}
        assert not source_table_coverage(source, PLAN)['sourceTableCoverageComplete']


def test_definition_failure_does_not_partially_admit_internal_tables(tmp_path, monkeypatch):
    path = tmp_path / 'source.db'
    seed(path)
    with SQLiteMigrationSource(path) as source:
        monkeypatch.setattr(source, 'table_definition', lambda name: (_ for _ in ()).throw(MigrationSourceError('budget')))
        result = source_table_coverage(source, PLAN)
        assert result['verifiedRebuilds'] == {}
        assert result['rebuildVerificationFailed']
        assert not result['sourceTableCoverageComplete']
