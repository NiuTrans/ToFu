"""Retired transport logs and reconstructible projections unblock the gate."""
import sqlite3

import pytest

from lib.storage_sidecar.tofudb_migration_coverage import source_table_coverage
from lib.storage_sidecar.tofudb_migration_source import SQLiteMigrationSource
from lib.storage_sidecar.tofudb_migration_transport import (
    RETIRED_TRANSPORT_POLICY,
    verified_reconstructible_projections,
    verified_retired_transport,
)

pytestmark = pytest.mark.unit

_DDL = '''CREATE TABLE attempt_events (seq INTEGER PRIMARY KEY, event_id TEXT, payload TEXT);
CREATE TABLE task_events (seq INTEGER PRIMARY KEY, event_id TEXT, payload TEXT);
CREATE TABLE storage_attempt_events (sequence INTEGER PRIMARY KEY, attempt_id TEXT, event_json TEXT);
CREATE TABLE storage_events (sequence INTEGER PRIMARY KEY, task_id TEXT, event_json TEXT);
CREATE TABLE storage_conversation_turns (turn_id TEXT PRIMARY KEY, body TEXT);
CREATE TABLE storage_projection_outbox (turn_id TEXT PRIMARY KEY, marker TEXT);
CREATE TABLE storage_turn_search (turn_id TEXT PRIMARY KEY, search_text TEXT);
CREATE TABLE storage_turn_projection_checkpoints (turn_id TEXT PRIMARY KEY, body BLOB);
CREATE TABLE storage_events_mapped (id INTEGER PRIMARY KEY);
'''


def _write(path, script):
    with sqlite3.connect(path) as connection:
        connection.executescript(_DDL + script)


def _coverage(path, extra_mapped=()):
    mapped = ['storage_events_mapped', *extra_mapped]
    plan = {'id': 'test', 'domains': [{'tables': [{'name': name} for name in mapped]}]}
    with SQLiteMigrationSource(path) as source:
        return source_table_coverage(source, plan)


def test_legacy_transport_logs_classified_with_successor_present(tmp_path):
    path = tmp_path / 'source.db'
    _write(path, '''INSERT INTO attempt_events VALUES (1, 'a1', '{}');
        INSERT INTO attempt_events VALUES (2, 'a2', '{}');
        INSERT INTO task_events VALUES (1, 't1', '{}');
        INSERT INTO storage_attempt_events VALUES (1, 'a1', '{}');
        INSERT INTO storage_events VALUES (1, 't1', '{}');''')
    report = _coverage(path, extra_mapped=['storage_attempt_events', 'storage_events',
                                           'storage_conversation_turns'])
    assert report['sourceTableCoverageComplete'] is True
    assert report['unmappedPopulatedTables'] == []
    rebuilds = report['verifiedRebuilds']
    assert {'attempt_events', 'task_events'}.issubset(rebuilds)
    assert rebuilds['attempt_events']['proof'] == RETIRED_TRANSPORT_POLICY
    assert rebuilds['attempt_events']['archivedInputTables'] == ['storage_attempt_events']
    assert rebuilds['task_events']['archivedInputTables'] == ['storage_events']
    assert 'sha256' not in rebuilds['attempt_events']
    assert report['rebuildVerificationFailed'] is False


def test_legacy_transport_not_classified_without_successor(tmp_path):
    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as connection:
        connection.executescript('''CREATE TABLE attempt_events (seq INTEGER PRIMARY KEY, payload TEXT);
            INSERT INTO attempt_events VALUES (1, '{}');''')
    report = _coverage(path)
    assert report['unmappedPopulatedTables'] == ['attempt_events']
    assert 'attempt_events' not in report['verifiedRebuilds']


def test_reconstructible_projections_populated_classified(tmp_path):
    path = tmp_path / 'source.db'
    _write(path, '''INSERT INTO storage_conversation_turns VALUES ('turn1', 'body');
        INSERT INTO storage_projection_outbox VALUES ('turn1', 'dirty');
        INSERT INTO storage_turn_search VALUES ('turn1', 'text');''')
    report = _coverage(path, extra_mapped=['storage_conversation_turns'])
    rebuilds = report['verifiedRebuilds']
    assert 'storage_projection_outbox' in rebuilds
    assert rebuilds['storage_projection_outbox']['proof'] == 'transient-projection-dirty-marker-queue'
    assert rebuilds['storage_projection_outbox']['action'] == (
        'reproject-from-durable-turns;transient-markers-not-durable')
    assert 'storage_turn_search' in rebuilds
    assert rebuilds['storage_turn_search']['proof'] == (
        'rebuildable-search-materialization-from-durable-turns')
    assert 'storage_projection_outbox' not in report['unmappedPopulatedTables']
    assert 'storage_turn_search' not in report['unmappedPopulatedTables']


def test_populated_checkpoint_blocks_but_empty_checkpoint_classified(tmp_path):
    path = tmp_path / 'source.db'
    _write(path, "INSERT INTO storage_conversation_turns VALUES ('turn1', 'body');")
    with SQLiteMigrationSource(path) as source:
        plan = {'id': 'test', 'domains': []}
        empty = verified_reconstructible_projections(source, plan)
    assert 'storage_turn_projection_checkpoints' in empty
    assert empty['storage_turn_projection_checkpoints']['archivedInputTables'] == [
        'storage_conversation_turns', 'storage_attempt_events']

    populated = tmp_path / 'populated.db'
    _write(populated, '''INSERT INTO storage_conversation_turns VALUES ('turn1', 'body');
        INSERT INTO storage_turn_projection_checkpoints VALUES ('turn1', x'00');''')
    report = _coverage(populated, extra_mapped=['storage_conversation_turns'])
    assert 'storage_turn_projection_checkpoints' not in report['verifiedRebuilds']
    assert report['unmappedPopulatedTables'] == ['storage_turn_projection_checkpoints']
    assert report['sourceTableCoverageComplete'] is False

    from lib.storage_sidecar.tofudb_migration_projection import require_materialized_turns
    from lib.storage_sidecar.tofudb_migration_import import MigrationImportError
    with SQLiteMigrationSource(populated) as source:
        with pytest.raises(MigrationImportError, match='projection checkpoints'):
            require_materialized_turns(source)


def test_genuinely_unknown_populated_table_still_blocks(tmp_path):
    path = tmp_path / 'source.db'
    _write(path, '''INSERT INTO attempt_events VALUES (1, 'a1', '{}');
        INSERT INTO storage_attempt_events VALUES (1, 'a1', '{}');
        INSERT INTO task_events VALUES (1, 't1', '{}');
        INSERT INTO storage_events VALUES (1, 't1', '{}');
        INSERT INTO storage_conversation_turns VALUES ('turn1', 'body');
        INSERT INTO storage_projection_outbox VALUES ('turn1', 'dirty');
        INSERT INTO storage_turn_search VALUES ('turn1', 'text');
        CREATE TABLE mystery_durable (id INTEGER PRIMARY KEY, body TEXT);
        INSERT INTO mystery_durable VALUES (1, 'user data');''')
    report = _coverage(path, extra_mapped=['storage_attempt_events', 'storage_events',
                                           'storage_conversation_turns'])
    assert report['unmappedPopulatedTables'] == ['mystery_durable']
    assert report['sourceTableCoverageComplete'] is False
    assert 'mystery_durable' not in report['verifiedRebuilds']


def test_legacy_log_mapped_in_plan_is_not_reclassified(tmp_path):
    path = tmp_path / 'source.db'
    _write(path, "INSERT INTO storage_attempt_events VALUES (1, 'a1', '{}');")
    with SQLiteMigrationSource(path) as source:
        plan = {'id': 'test',
                'domains': [{'tables': [{'name': name} for name in
                                         ('attempt_events', 'task_events')]}]}
        assert verified_retired_transport(source, plan) == {}
