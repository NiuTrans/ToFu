"""Atomic legacy-to-signal Project Brain cutover tests."""

from __future__ import annotations

import sqlite3

import pytest

from lib.storage_sidecar.operations_pkg._common import _dump, _load
from lib.storage_sidecar.operations_pkg._project_brain import (
    _empty_projection,
    _fold_event,
    _project_brain_checker_register,
    _project_brain_cutover,
    _project_brain_decision_promote,
    _project_brain_narrative_add,
    _project_brain_rebuild,
)


pytestmark = pytest.mark.unit


class _Session:
    backend = 'sqlite'

    def __init__(self, connection: sqlite3.Connection, *, fail_drop: str = ''):
        self.connection = connection
        self.fail_drop = fail_drop

    def lock_key(self, _namespace, _key):
        return None

    def execute(self, sql, params=()):
        if self.fail_drop and self.fail_drop in sql:
            raise RuntimeError('injected cutover failure')
        cursor = self.connection.execute(sql, tuple(params))
        return cursor.rowcount

    def fetch_one(self, sql, params=()):
        return self.connection.execute(sql, tuple(params)).fetchone()

    def fetch_all(self, sql, params=()):
        return self.connection.execute(sql, tuple(params)).fetchall()


def _legacy_database() -> sqlite3.Connection:
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE storage_meta (
          meta_key TEXT PRIMARY KEY,
          meta_value TEXT NOT NULL
        );
        CREATE TABLE storage_records (
          namespace TEXT NOT NULL,
          record_key TEXT NOT NULL,
          value_json BLOB NOT NULL,
          PRIMARY KEY(namespace, record_key)
        );
        CREATE TABLE storage_project_brain_projects (
          owner_user_id INTEGER NOT NULL,
          project_key TEXT NOT NULL,
          head_sequence INTEGER NOT NULL,
          checkpoint_sequence INTEGER NOT NULL,
          projection_json BLOB NOT NULL,
          updated_at_ms INTEGER NOT NULL,
          PRIMARY KEY(owner_user_id, project_key)
        );
        CREATE TABLE storage_events (
          task_id TEXT NOT NULL,
          sequence INTEGER NOT NULL,
          stream_kind TEXT NOT NULL,
          event_type TEXT NOT NULL,
          event_kind TEXT NOT NULL,
          owner_user_id INTEGER NOT NULL DEFAULT 0,
          project_key TEXT NOT NULL DEFAULT '',
          project_sequence INTEGER NOT NULL DEFAULT 0,
          event_json BLOB NOT NULL,
          created_at_ms INTEGER NOT NULL,
          PRIMARY KEY(task_id, sequence)
        );
        CREATE UNIQUE INDEX idx_test_project_sequence
          ON storage_events(owner_user_id, project_key, project_sequence)
          WHERE project_sequence > 0;
        CREATE TABLE storage_watch_items (
          item_id TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL,
          project_path TEXT NOT NULL,
          kind TEXT NOT NULL,
          text TEXT NOT NULL,
          status TEXT NOT NULL,
          created_by_conv TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE storage_watch_responses (
          item_id TEXT NOT NULL,
          sequence INTEGER NOT NULL,
          response TEXT NOT NULL,
          trigger TEXT NOT NULL,
          ts INTEGER NOT NULL,
          PRIMARY KEY(item_id, sequence)
        );
        CREATE TABLE storage_watch_runs (run_id TEXT PRIMARY KEY);
        CREATE TABLE storage_board_tasks (task_id TEXT PRIMARY KEY);
        CREATE TABLE project_events (event_id TEXT PRIMARY KEY);
        CREATE TABLE project_tasks (task_id TEXT PRIMARY KEY);
        CREATE TABLE storage_queue_items (
          id TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          payload_json BLOB NOT NULL
        );
        """
    )
    charter = {
        'content': '  Ship safely  ',
        'decisions': [
            {'text': 'Use checker-backed releases'},
            {'text': ' use   checker-backed RELEASES '},
        ],
    }
    connection.execute(
        'INSERT INTO storage_records(namespace,record_key,value_json) '
        'VALUES (?,?,?)',
        ('project_charter', '7:/workspace/demo/', _dump(charter)),
    )
    connection.execute(
        'INSERT INTO storage_watch_items VALUES (?,?,?,?,?,?,?,?,?)',
        ('watch-1', 7, '/workspace/demo/', 'concern', 'Keep latency bounded',
         'open', 'conv-watch', 10, 20),
    )
    connection.execute(
        'INSERT INTO storage_watch_responses VALUES (?,?,?,?,?)',
        ('watch-1', 1, 'older result', 'refresh', 30),
    )
    connection.execute(
        'INSERT INTO storage_watch_responses VALUES (?,?,?,?,?)',
        ('watch-1', 2, 'latest result', 'manual', 40),
    )
    for sequence, stream in enumerate(('project_feed', 'project_status'), 1):
        connection.execute(
            'INSERT INTO storage_events VALUES (?,?,?,?,?,?,?,?,?,?)',
            (f'legacy-{stream}', sequence, stream, stream, 'legacy', 7,
             '/workspace/demo', 0, _dump({'legacy': True}), 50 + sequence),
        )
    connection.execute('INSERT INTO storage_board_tasks VALUES (?)', ('old-board',))
    connection.execute('INSERT INTO project_events VALUES (?)', ('old-event',))
    connection.execute('INSERT INTO project_tasks VALUES (?)', ('old-task',))
    connection.execute(
        'INSERT INTO storage_queue_items VALUES (?,?,?)',
        ('old-kickoff', 'workflow', _dump({'boardTaskId': 'old-board'})),
    )
    connection.execute(
        'INSERT INTO storage_queue_items VALUES (?,?,?)',
        ('old-peer', 'peer_msg', _dump({'text': 'legacy peer advice'})),
    )
    connection.execute(
        'INSERT INTO storage_queue_items VALUES (?,?,?)',
        ('human', 'real', _dump({'message': 'keep me'})),
    )
    connection.commit()
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row['name'] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_cutover_keeps_watch_and_drops_legacy_intent_and_histories():
    connection = _legacy_database()
    connection.execute('BEGIN')
    result = _project_brain_cutover(_Session(connection), {'timestamp': 100})
    connection.commit()

    assert result == {
        'ok': True, 'alreadyComplete': False, 'projects': 1, 'verified': True,
        'retiredQueuedTurns': 2,
    }
    row = connection.execute(
        'SELECT projection_json FROM storage_project_brain_projects '
        'WHERE owner_user_id=7 AND project_key=?',
        ('/workspace/demo',),
    ).fetchone()
    projection = _load(row['projection_json'])
    assert projection['workItems'] == []
    assert projection['narratives'] == []
    assert 'attention' not in projection
    assert len(projection['watch']) == 1
    assert projection['watch'][0]['latestResult']['text'] == 'latest result'
    assert connection.execute(
        "SELECT COUNT(*) AS n FROM storage_events WHERE stream_kind IN "
        "('project_feed','project_status')").fetchone()['n'] == 0
    assert connection.execute(
        "SELECT COUNT(*) AS n FROM storage_records "
        "WHERE namespace='project_charter'").fetchone()['n'] == 0
    assert {
        'storage_board_tasks', 'storage_watch_items',
        'storage_watch_responses', 'storage_watch_runs',
        'project_events', 'project_tasks',
    }.isdisjoint(_table_names(connection))
    marker = connection.execute(
        "SELECT meta_value FROM storage_meta "
        "WHERE meta_key='project_brain_cutover_v1'").fetchone()
    assert marker['meta_value'] == 'complete'
    assert [row['id'] for row in connection.execute(
        'SELECT id FROM storage_queue_items ORDER BY id')] == ['human']


def test_cutover_failure_rolls_back_without_half_authority():
    connection = _legacy_database()
    session = _Session(connection, fail_drop='DROP TABLE IF EXISTS storage_board_tasks')
    connection.execute('BEGIN')
    with pytest.raises(RuntimeError, match='injected cutover failure'):
        _project_brain_cutover(session, {'timestamp': 100})
    connection.rollback()

    assert connection.execute(
        'SELECT COUNT(*) AS n FROM storage_project_brain_projects'
    ).fetchone()['n'] == 0
    assert connection.execute(
        "SELECT COUNT(*) AS n FROM storage_records "
        "WHERE namespace='project_charter'").fetchone()['n'] == 1
    assert connection.execute(
        "SELECT COUNT(*) AS n FROM storage_events WHERE stream_kind IN "
        "('project_feed','project_status')").fetchone()['n'] == 2
    assert 'storage_board_tasks' in _table_names(connection)
    assert connection.execute(
        "SELECT COUNT(*) AS n FROM storage_meta "
        "WHERE meta_key='project_brain_cutover_v1'").fetchone()['n'] == 0
    assert connection.execute(
        'SELECT COUNT(*) AS n FROM storage_queue_items').fetchone()['n'] == 3


def test_checkpoint_reclaims_only_rebuildable_prefix_and_keeps_long_lived_state(
        monkeypatch):
    import lib.storage_sidecar.operations_pkg._project_brain as brain_storage

    connection = _legacy_database()
    session = _Session(connection)
    connection.execute('BEGIN')
    _project_brain_cutover(session, {'timestamp': 100})
    monkeypatch.setattr(brain_storage, 'EVENT_CHECKPOINT_THRESHOLD', 4)
    monkeypatch.setattr(brain_storage, 'EVENT_CHECKPOINT_TAIL', 2)
    scope = {'owner_user_id': 7, 'project_key': '/workspace/demo'}
    _project_brain_checker_register(session, {
        **scope,
        'definition': {
            'checkerId': 'release', 'version': 1, 'label': 'Release gate',
            'argv': ['python3', '-m', 'pytest'], 'cwd': '.',
            'pathGlobs': ['**'], 'timeoutMs': 5000, 'enabled': True,
        },
        'timestamp': 110,
    })
    _project_brain_decision_promote(session, {
        **scope,
        'decision': {
            'decisionId': 'release-policy',
            'text': 'Release only after the registered gate passes.',
            'checkerRef': {'id': 'release', 'version': 1},
            'sourceConversationId': 'conv-owner',
            'sourceTurnId': 'turn-owner',
            'latestVerification': None,
        },
        'timestamp': 120,
    })
    _project_brain_narrative_add(session, {
        **scope, 'kind': 'decision', 'text': 'Checkpoint boundary reached.',
        'timestamp': 130,
    })
    connection.commit()

    retained_sequences = [row['project_sequence'] for row in connection.execute(
        "SELECT project_sequence FROM storage_events "
        "WHERE stream_kind='project_brain' ORDER BY project_sequence")]
    assert retained_sequences == [4, 5]
    rebuilt = _project_brain_rebuild(session, scope)['projection']
    assert rebuilt['checkpointSequence'] == 5
    assert rebuilt['watch'][0]['text'] == 'Keep latency bounded'
    assert rebuilt['checkers'][0]['checkerId'] == 'release'
    assert rebuilt['charter']['decisions'][0]['decisionId'] == 'release-policy'
    assert rebuilt['narratives'][-1]['text'] == 'Checkpoint boundary reached.'


def test_historical_attention_events_fold_to_nothing():
    # Retired Attention collection: pre-removal events stay in the immutable
    # log but must not re-materialize state during rebuild.
    projection = _empty_projection(7, '/workspace/demo')
    _fold_event(projection, {
        'kind': 'attention_added',
        'payload': {
            'attentionId': 'legacy:abc', 'attentionKind': 'legacy_decision',
            'text': 'Ship safely', 'workId': '',
        },
        'projectSequence': 1, 'timestamp': 100,
    })
    _fold_event(projection, {
        'kind': 'legacy_migrated',
        'payload': {
            'watch': [{'id': 'w1', 'text': 'Keep latency bounded'}],
            'attention': [{'id': 'legacy:def', 'text': 'Old charter prose'}],
        },
        'projectSequence': 2, 'timestamp': 110,
    })
    assert 'attention' not in projection
    assert projection['watch'][0]['id'] == 'w1'
    assert projection['headSequence'] == 2
