"""The legacy Project Brain fold reproduces the engine cutover read-only."""
import json
import sqlite3

import pytest

from lib.storage_authority_sql.migration.tofudb_migration_coverage import source_table_coverage
from lib.storage_authority_sql.migration.tofudb_migration_project_brain import (
    CONSUMED_TABLES,
    consumed_populated_tables,
    fold_legacy_projections,
)
from lib.storage_authority_sql.migration.tofudb_migration_source import SQLiteMigrationSource

pytestmark = pytest.mark.unit


def _source(tmp_path, script):
    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as connection:
        connection.executescript(script)
    return path


_WATCH_SCHEMA = '''
CREATE TABLE storage_watch_items (
    item_id TEXT PRIMARY KEY, project_path TEXT, kind TEXT, text TEXT,
    status TEXT, promoted BIGINT, response_fingerprint TEXT,
    created_by_conv TEXT, created_at BIGINT, updated_at BIGINT, user_id BIGINT);
CREATE TABLE storage_watch_responses (
    item_id TEXT, sequence BIGINT, project_path TEXT, response TEXT,
    pillar_state_json TEXT, trigger TEXT, ts BIGINT,
    PRIMARY KEY (item_id, sequence));
'''


def test_fold_matches_engine_cutover_shape_and_bounds(tmp_path):
    path = _source(tmp_path, _WATCH_SCHEMA + '''
INSERT INTO storage_watch_items VALUES
    ('i1', '/proj/a/', 'concern', 'watch a', 'open', 0, '', 'conv1', 10, 20, 1),
    ('i2', '/proj/a', 'concern', 'watch b', 'done', 0, '', 'conv1', 11, 21, 1),
    ('i3', '/proj/b', 'risk', 'watch c', 'open', 0, '', 'conv2', 12, 22, 1);
INSERT INTO storage_watch_responses VALUES
    ('i1', 1, '/proj/a', 'old', '', 'manual', 100),
    ('i1', 2, '/proj/a', 'newest', '', 'auto', 200);
''')
    with SQLiteMigrationSource(path) as source:
        docs = {d['project_key']: d
                for d in fold_legacy_projections(source, now_ms=424242)}
    assert set(docs) == {'/proj/a', '/proj/b'}
    a = docs['/proj/a']
    assert a['owner_user_id'] == 1
    # One legacy_migrated event per (owner, project): head is 1, checkpoint 0.
    assert a['head_sequence'] == 1
    assert a['checkpoint_sequence'] == 0
    assert a['updated_at_ms'] == 424242
    projection = a['projection']
    assert projection['projectKey'] == '/proj/a'
    assert projection['headSequence'] == 1
    # Trailing separator normalized away: i1 and i2 share one bucket.
    assert [w['id'] for w in projection['watch']] == ['i1', 'i2']
    assert projection['watch'][0]['status'] == 'active'
    assert projection['watch'][1]['status'] == 'resolved'
    # Only the newest non-empty response is carried as latestResult.
    assert projection['watch'][0]['latestResult'] == {
        'text': 'newest', 'trigger': 'auto', 'timestamp': 200}
    assert projection['watch'][1]['latestResult'] is None
    # projection_json is canonical and agrees with the declared sequences.
    assert json.loads(a['projection_json']) == projection
    assert a['projection_json'] == json.dumps(
        projection, sort_keys=True, separators=(',', ':'))


def test_fold_skips_empty_project_scope_like_engine(tmp_path):
    path = _source(tmp_path, _WATCH_SCHEMA + '''
INSERT INTO storage_watch_items VALUES
    ('i1', '', 'concern', 'no scope', 'open', 0, '', 'c', 10, 20, 1),
    ('i2', '/kept', 'concern', 'scoped', 'open', 0, '', 'c', 10, 20, 1);
''')
    with SQLiteMigrationSource(path) as source:
        docs = list(fold_legacy_projections(source, now_ms=1))
    assert [d['project_key'] for d in docs] == ['/kept']


def test_fold_rejects_ownerless_or_boolean_timestamps(tmp_path):
    path = _source(tmp_path, _WATCH_SCHEMA + '''
INSERT INTO storage_watch_items VALUES
    ('i1', '/p', 'concern', 'x', 'open', 0, '', 'c', 10, 20, 0);
''')
    with SQLiteMigrationSource(path) as source:
        with pytest.raises(ValueError, match='no owner'):
            list(fold_legacy_projections(source))


def test_consumed_populated_tables_only_flags_present_rows(tmp_path):
    path = _source(tmp_path, _WATCH_SCHEMA + '''
CREATE TABLE project_events (id INTEGER);
INSERT INTO project_events VALUES (1);
''')
    with SQLiteMigrationSource(path) as source:
        # storage_watch_items/responses exist but are empty; only the
        # populated execution-history table is reported.
        assert consumed_populated_tables(source) == ['project_events']
        assert set(CONSUMED_TABLES) >= {
            'project_events', 'project_tasks', 'storage_board_tasks',
            'storage_watch_items', 'storage_watch_responses', 'storage_watch_runs'}


def _cutover_plan(consumed):
    return {
        'id': 'test',
        'domains': [],
        'legacyProjectBrainCutoverPolicy': {
            'consumedTables': consumed,
            'fold': 'watch-intent-into-projection;execution-history-dropped',
            'semantics': 'one-legacy-migrated-event-per-owner-project',
        },
    }


def test_gate_covers_populated_legacy_tables_via_declared_fold(tmp_path):
    path = _source(tmp_path, _WATCH_SCHEMA + '''
CREATE TABLE project_events (id INTEGER);
INSERT INTO project_events VALUES (1);
INSERT INTO storage_watch_items VALUES
    ('i1', '/p', 'concern', 'x', 'open', 0, '', 'c', 10, 20, 1);
''')
    with SQLiteMigrationSource(path) as source:
        report = source_table_coverage(source, _cutover_plan(list(CONSUMED_TABLES)))
    assert report['sourceTableCoverageComplete'] is True
    assert report['legacyCutoverVerificationFailed'] is False
    assert report['verifiedLegacyCutover']['project_events'] == {
        'disposition': 'execution-history-dropped-by-cutover'}
    assert report['verifiedLegacyCutover']['storage_watch_items'] == {
        'carriedWatchItems': 1, 'foldedProjections': 1}


def test_gate_fails_closed_when_policy_missing_or_fold_breaks(tmp_path):
    path = _source(tmp_path, _WATCH_SCHEMA + '''
CREATE TABLE project_events (id INTEGER);
INSERT INTO project_events VALUES (1);
''')
    with SQLiteMigrationSource(path) as source:
        # No policy declared: the populated retired table stays a blocker.
        missing = source_table_coverage(source, {'id': 't', 'domains': []})
        assert missing['sourceTableCoverageComplete'] is False
        assert missing['unmappedPopulatedTables'] == ['project_events']
        # A consumedTables list that disagrees with the fold fails closed.
        broken = source_table_coverage(source, _cutover_plan(['project_events']))
        assert broken['sourceTableCoverageComplete'] is False
        assert broken['legacyCutoverVerificationFailed'] is True


def test_legacy_watch_fold_keeps_every_record_above_the_former_tail_limit(tmp_path):
    path = _source(tmp_path, _WATCH_SCHEMA)
    with sqlite3.connect(path) as connection:
        connection.executemany("INSERT INTO storage_watch_items VALUES (?,?,?,?,?,?,?,?,?,?,?)",[
            (f"watch-{n:04}","/complete","concern",f"intent-{n}","open",0,"","conversation",n,n,1) for n in range(151)
        ])
    with SQLiteMigrationSource(path) as source:
        rows = list(fold_legacy_projections(source,now_ms=1000))
    assert len(rows) == 1
    watch = rows[0]["projection"]["watch"]
    assert len(watch) == 151
    assert {item["id"] for item in watch} == {f"watch-{n:04}" for n in range(151)}
