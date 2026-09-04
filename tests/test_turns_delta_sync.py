"""Turn delta-sync protocol (the 2026-08-21 resync-storm root fix).

Incident: every `conv_changed` notify frame per open tab re-downloaded the
FULL turns projection (multi-MB for long conversations); the server built
each body synchronously, the event loop stalled, heartbeats dropped, the
frontend declared BACKEND OFFLINE and every tab re-fetched — an exponential
feedback loop.  The protocol fix: watermark-scoped delta reads
(``turn.list_delta`` with an overlap window), explicit deletion tombstones,
and a client-side revision gate.  These tests pin the server half:

- ``_turn_list_delta`` filters by the overlapped watermark and reports
  tombstoned deletions;
- both deletion paths (turn.delete, turn.branch_delete) write tombstones;
- tombstone pruning bounds the table without breaking fresh deltas;
- ``list_turns`` wiring: delta only for unfiltered sidecar reads with a
  sane watermark, overflow degrades to a full snapshot, the legacy
  authority always answers full (with a watermark seed).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time

import pytest


pytestmark = pytest.mark.unit


class _SqliteSession:
    """Minimal Session over a raw sqlite3 connection (single-writer test)."""

    backend = 'sqlite'

    def __init__(self, conn):
        self._conn = conn

    def lock_key(self, namespace, key):
        return None

    def execute(self, sql, params=()):
        cursor = self._conn.execute(sql, tuple(params))
        self._conn.commit()
        return cursor.rowcount

    def fetch_one(self, sql, params=()):
        return self._conn.execute(sql, tuple(params)).fetchone()

    def fetch_all(self, sql, params=()):
        return self._conn.execute(sql, tuple(params)).fetchall()


def _seed_conv(session, conv_id='conv-d', rev=3):
    from lib.storage_sidecar.operations_pkg._common import _dump
    session.execute(
        'INSERT INTO storage_conversations '
        '(id,user_id,title,messages_json,created_at_ms,updated_at_ms,'
        'settings_json,msg_count,search_text,rev) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (conv_id, 1, 'delta', _dump([]), 1000, 1000, _dump({}), 0, '', rev),
    )


def _seed_turn(session, turn_id, ordinal, updated_at, *,
               conv_id='conv-d', lane='main', parent=None, projection=None,
               actor='assistant'):
    from lib.storage_sidecar.operations_pkg._common import _dump
    session.execute(
        'INSERT INTO storage_conversation_turns '
        '(turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,'
        'actor,kind,run_id,status,current_attempt_id,projection_json,'
        'projection_revision,settlement_json,created_at,updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (turn_id, conv_id, 1, lane, parent, ordinal, actor, 'reply', '',
         'completed', None, _dump(projection or {'content': turn_id}),
         1, _dump({}), updated_at, updated_at),
    )


def _materialize_turn_search(session, turn_id, *, token='test-generation'):
    """Apply one canonical row to the derived projection test tables."""
    from lib.storage_sidecar.turn_search_projection import (
        _apply_turn_to_session,
    )

    row = session.fetch_one(
        "SELECT t.*,c.updated_at_ms AS conversation_updated_at_ms "
        "FROM storage_conversation_turns AS t "
        "JOIN storage_conversations AS c "
        "ON c.id=t.conversation_id AND c.user_id=t.user_id "
        "WHERE t.turn_id=?",
        (turn_id,),
    )
    _apply_turn_to_session(
        session,
        {'user_id': 1, 'entity_key': turn_id},
        row,
        token,
    )


def _materialize_conversation_search(
        session, conv_id='conv-d', *, token='test-conversation-generation'):
    """Rebuild one conversation exactly as the independent worker does."""
    from lib.storage_sidecar.turn_search_projection import (
        _apply_page_in_session,
        _begin_conversation_in_session,
        _finalize_conversation_in_session,
    )

    identity = {'user_id': 1, 'entity_key': conv_id}
    header = session.fetch_one(
        "SELECT id,user_id,updated_at_ms FROM storage_conversations "
        "WHERE id=? AND user_id=1", (conv_id,))
    _begin_conversation_in_session(session, identity, header, token)
    if header is None:
        return
    rows = session.fetch_all(
        "SELECT t.*,c.updated_at_ms AS conversation_updated_at_ms "
        "FROM storage_conversation_turns AS t "
        "JOIN storage_conversations AS c "
        "ON c.id=t.conversation_id AND c.user_id=t.user_id "
        "WHERE t.conversation_id=? AND t.user_id=1", (conv_id,))
    _apply_page_in_session(session, rows, token)
    _finalize_conversation_in_session(session, identity, token)


def test_python_projection_patch_applier_matches_the_wire_contract():
    from lib.turn_projection_patch import (
        ProjectionPatchError,
        apply_projection_patch,
        build_projection_patch,
    )

    before = {
        'content': 'abc',
        'items': [{'status': 'running'}, 2, 3],
        'removeMe': True,
    }
    after = {
        'content': 'abcdef',
        'items': [{'status': 'done'}, 2],
        'added': {'ok': True},
    }
    patch = build_projection_patch(
        before, after, base_revision=7, target_revision=8)

    assert apply_projection_patch(before, patch) == after
    assert before == {
        'content': 'abc',
        'items': [{'status': 'running'}, 2, 3],
        'removeMe': True,
    }

    appended = {'content': 'abcdef', 'items': [*after['items'], 4]}
    append_patch = build_projection_patch(
        after, appended, base_revision=8, target_revision=9)
    assert apply_projection_patch(after, append_patch) == appended

    with pytest.raises(ProjectionPatchError, match='out of bounds'):
        apply_projection_patch(before, {
            'version': 1,
            'baseRevision': 7,
            'targetRevision': 8,
            'operations': [{'op': 'set', 'path': ['items', 99], 'value': 0}],
        })


def test_turn_event_record_applies_only_revision_checked_projection_patch(
        session):
    from lib.storage.errors import StorageError
    from lib.storage_sidecar.operations_pkg._common import _load
    from lib.storage_sidecar.operations_pkg._turns import (
        _turn_create_pair,
        _turn_event_record,
        _turn_get,
    )
    from lib.turn_projection_patch import build_projection_patch
    from lib.turn_projection_segments import projection_with_stable_segments

    created = _turn_create_pair(session, {
        'conversation_id': 'conv-d', 'user_id': 1,
        'command_id': 'projection-patch-create',
        'input_projection': {'content': 'question'},
        'config': {},
    })
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    session.execute(
        "UPDATE storage_generation_attempts SET task_id=? WHERE attempt_id=?",
        ('projection-patch-task', attempt_id),
    )
    before_row = session.fetch_one(
        "SELECT projection_json,projection_revision "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    before = projection_with_stable_segments(
        _load(before_row['projection_json']),
        actor='assistant',
        status='pending',
    )
    after = {
        **before,
        'content': 'answer',
        'thinking': '',
        'toolRounds': [{
            'roundNum': 1,
            'toolCallId': 'patch-call',
            'toolName': 'read_files',
            'toolArgs': '{}',
            'status': 'searching',
        }],
    }
    patch = build_projection_patch(
        before,
        after,
        base_revision=before_row['projection_revision'],
        target_revision=before_row['projection_revision'] + 1,
    )
    result = _turn_event_record(session, {
        'attempt_id': attempt_id,
        'user_id': 1,
        'task_id': 'projection-patch-task',
        'status': 'running',
        'terminal': False,
        'projection_patch': patch,
        'event_type': 'projection_updated',
        'event_payload': {'updateKind': 'tool_start'},
    })

    assert result['applied'] is True
    stored = session.fetch_one(
        "SELECT projection_json,projection_revision,"
        "projection_checkpoint_revision "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    assert _load(stored['projection_json']) == {}
    assert stored['projection_revision'] == before_row['projection_revision'] + 1
    assert stored['projection_checkpoint_revision'] == stored['projection_revision']
    checkpoint = session.fetch_one(
        "SELECT projection_json FROM storage_turn_projection_checkpoints "
        "WHERE turn_id=?",
        (turn_id,),
    )
    assert _load(checkpoint['projection_json']) == after
    assert _turn_get(session, {
        'conversation_id': 'conv-d', 'user_id': 1, 'turn_id': turn_id,
    })['projection'] == after
    assert result['_conversationSyncAttemptEvents'][0]['payload'][
        'projectionPatch']['operations']

    unchanged = _turn_event_record(session, {
        'attempt_id': attempt_id,
        'user_id': 1,
        'task_id': 'projection-patch-task',
        'status': 'running',
        'terminal': False,
        'slim': True,
        'content': 'answer',
        'thinking': '',
        'event_type': 'projection_updated',
        'event_payload': {'updateKind': 'unchanged-heartbeat'},
    })
    assert unchanged['applied'] is True
    checkpoint_head = session.fetch_one(
        "SELECT projection_revision,projection_checkpoint_revision,"
        "projection_materialized_revision,projection_patch_count "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    assert checkpoint_head['projection_revision'] == (
        before_row['projection_revision'] + 2)
    assert checkpoint_head['projection_checkpoint_revision'] == (
        before_row['projection_revision'] + 1)
    assert checkpoint_head['projection_materialized_revision'] == (
        before_row['projection_revision'] + 1)
    assert checkpoint_head['projection_patch_count'] == 1
    assert _turn_get(session, {
        'conversation_id': 'conv-d', 'user_id': 1, 'turn_id': turn_id,
    })['projection'] == after

    with pytest.raises(StorageError) as stale:
        _turn_event_record(session, {
            'attempt_id': attempt_id,
            'user_id': 1,
            'task_id': 'projection-patch-task',
            'status': 'running',
            'terminal': False,
            'projection_patch': patch,
            'event_type': 'projection_updated',
            'event_payload': {'updateKind': 'tool_start'},
        })
    assert stale.value.code == 'turn_projection_stale'


def test_turn_event_record_reuses_revision_cache_and_incoming_replay_patch(
        session, monkeypatch):
    import lib.storage_sidecar.operations_pkg._turns_events as event_operations
    import lib.storage_sidecar.turn_projection_write as projection_write_operations
    from lib.storage_sidecar.operations_pkg._common import _load
    from lib.storage_sidecar.operations_pkg._turns import (
        _turn_create_pair,
        _turn_event_record,
        _turn_get,
    )
    from lib.storage_sidecar.turn_projection_cache import TurnProjectionCache
    from lib.turn_projection_patch import build_projection_patch
    from lib.turn_projection_segments import projection_with_stable_segments

    created = _turn_create_pair(session, {
        'conversation_id': 'conv-d', 'user_id': 1,
        'command_id': 'projection-cache-create',
        'input_projection': {'content': 'question'},
        'config': {},
    })
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    session.execute(
        "UPDATE storage_generation_attempts SET task_id=? WHERE attempt_id=?",
        ('projection-cache-task', attempt_id),
    )
    before_row = session.fetch_one(
        "SELECT projection_json,projection_revision "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    before = projection_with_stable_segments(
        _load(before_row['projection_json']),
        actor='assistant',
        status='pending',
    )
    first = projection_with_stable_segments(
        {**before, 'content': 'first'}, actor='assistant', status='running')
    second = projection_with_stable_segments(
        {**first, 'content': 'second'}, actor='assistant', status='running')
    first_patch = build_projection_patch(
        before,
        first,
        base_revision=before_row['projection_revision'],
        target_revision=before_row['projection_revision'] + 1,
    )
    second_patch = build_projection_patch(
        first,
        second,
        base_revision=before_row['projection_revision'] + 1,
        target_revision=before_row['projection_revision'] + 2,
    )

    cache = TurnProjectionCache(1024 * 1024, max_entries=4)
    session.turn_projection_cache = cache
    projection_selects = []
    real_fetch_one = session.fetch_one

    def tracked_fetch_one(sql, params=()):
        if sql.startswith('SELECT projection_json FROM storage_conversation_turns'):
            projection_selects.append(sql)
        return real_fetch_one(sql, params)

    monkeypatch.setattr(session, 'fetch_one', tracked_fetch_one)

    def unexpected_projection_diff(*_args, **_kwargs):
        raise AssertionError('validated incoming patch should be reused')

    monkeypatch.setattr(
        event_operations, 'build_projection_patch', unexpected_projection_diff)

    def record(patch, update_kind):
        return _turn_event_record(session, {
            'attempt_id': attempt_id,
            'user_id': 1,
            'task_id': 'projection-cache-task',
            'status': 'running',
            'terminal': False,
            'projection_patch': patch,
            'projection_segments_stable': True,
            'event_type': 'projection_updated',
            'event_payload': {'updateKind': update_kind},
        })

    assert record(first_patch, 'tool_start')['applied'] is True

    real_projection_dump = projection_write_operations._dump

    def dump_without_full_target(value):
        if value == second:
            raise AssertionError(
                'deferred projection writes must not encode the full target')
        return real_projection_dump(value)

    monkeypatch.setattr(
        projection_write_operations, '_dump', dump_without_full_target)
    assert record(second_patch, 'tool_result')['applied'] is True

    assert len(projection_selects) == 1
    assert cache.stats()['hits'] == 1
    assert cache.stats()['misses'] == 1
    head_row = session.fetch_one(
        "SELECT projection_json,projection_revision,"
        "projection_checkpoint_revision,"
        "projection_materialized_revision,projection_patch_count,"
        "projection_patch_bytes FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    assert _load(head_row['projection_json']) == {}
    assert head_row['projection_checkpoint_revision'] == (
        before_row['projection_revision'] + 1)
    assert head_row['projection_materialized_revision'] == (
        before_row['projection_revision'] + 1)
    assert head_row['projection_revision'] == before_row['projection_revision'] + 2
    assert head_row['projection_patch_count'] == 1
    assert head_row['projection_patch_bytes'] > 0
    checkpoint = session.fetch_one(
        "SELECT projection_json,projection_revision,projection_bytes "
        "FROM storage_turn_projection_checkpoints WHERE turn_id=?",
        (turn_id,),
    )
    assert _load(checkpoint['projection_json']) == first
    assert checkpoint['projection_revision'] == (
        before_row['projection_revision'] + 1)
    assert checkpoint['projection_bytes'] == len(checkpoint['projection_json'])
    assert _turn_get(session, {
        'conversation_id': 'conv-d', 'user_id': 1, 'turn_id': turn_id,
    })['projection'] == second

    monkeypatch.setattr(
        projection_write_operations, '_dump', real_projection_dump)
    monkeypatch.setattr(
        event_operations, 'build_projection_patch', build_projection_patch)
    final = projection_with_stable_segments(
        {**second, 'content': 'final'}, actor='assistant', status='completed')
    final_patch = build_projection_patch(
        second,
        final,
        base_revision=before_row['projection_revision'] + 2,
        target_revision=before_row['projection_revision'] + 3,
    )
    terminal = _turn_event_record(session, {
        'attempt_id': attempt_id,
        'user_id': 1,
        'task_id': 'projection-cache-task',
        'status': 'completed',
        'terminal': True,
        'projection_patch': final_patch,
        'projection_segments_stable': True,
        'settlement': {'outcome': 'completed', 'resumeOptions': []},
        'event_type': 'terminal_settlement',
        'event_payload': {'status': 'completed'},
    })
    assert terminal['applied'] is True
    assert len(projection_selects) == 1
    assert cache.stats()['entries'] == 0

    stored = session.fetch_one(
        "SELECT projection_json,projection_checkpoint_revision,"
        "projection_materialized_revision,"
        "projection_patch_count,projection_patch_bytes "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    assert _load(stored['projection_json']) == final
    assert stored['projection_checkpoint_revision'] is None
    assert stored['projection_materialized_revision'] is None
    assert stored['projection_patch_count'] == 0
    assert stored['projection_patch_bytes'] == 0
    assert session.fetch_one(
        "SELECT COUNT(*) AS n FROM storage_turn_projection_checkpoints "
        "WHERE turn_id=?",
        (turn_id,),
    )['n'] == 0


def test_activity_and_trash_resolve_durable_projection_head(session):
    """Secondary readers and recoverable delete never expose the slim hot row."""
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar.operations_pkg._common import _load
    from lib.storage_sidecar.operations_pkg._conversations import (
        _conversation_activity_dates,
        _conversation_delete,
        _conversation_restore,
    )
    from lib.storage_sidecar.operations_pkg._turns import (
        _turn_create_pair,
        _turn_event_record,
    )
    from lib.storage_sidecar.turn_projection_cache import TurnProjectionCache
    from lib.turn_projection_patch import build_projection_patch
    from lib.turn_projection_segments import projection_with_stable_segments

    cache = TurnProjectionCache(1024 * 1024, max_entries=4)
    authority = SQLiteSession(session._conn, turn_projection_cache=cache)
    created = _turn_create_pair(authority, {
        'conversation_id': 'conv-d',
        'user_id': 1,
        'command_id': 'projection-trash-create',
        'input_projection': {'content': 'question'},
        'config': {},
    })
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    authority.execute(
        "UPDATE storage_generation_attempts SET task_id=? WHERE attempt_id=?",
        ('projection-trash-task', attempt_id),
    )
    before_row = authority.fetch_one(
        "SELECT projection_json,projection_revision "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    before = projection_with_stable_segments(
        _load(before_row['projection_json']),
        actor='assistant',
        status='pending',
    )
    checkpoint_projection = {
        **before,
        'content': 'checkpoint content',
        'timestamp': 150,
    }
    head_projection = {
        **checkpoint_projection,
        'content': 'head content',
        'timestamp': 250,
    }
    first_patch = build_projection_patch(
        before,
        checkpoint_projection,
        base_revision=before_row['projection_revision'],
        target_revision=before_row['projection_revision'] + 1,
    )
    second_patch = build_projection_patch(
        checkpoint_projection,
        head_projection,
        base_revision=before_row['projection_revision'] + 1,
        target_revision=before_row['projection_revision'] + 2,
    )

    def record(patch, update_kind):
        return _turn_event_record(authority, {
            'attempt_id': attempt_id,
            'user_id': 1,
            'task_id': 'projection-trash-task',
            'status': 'running',
            'terminal': False,
            'projection_patch': patch,
            'projection_segments_stable': True,
            'event_type': 'projection_updated',
            'event_payload': {'updateKind': update_kind},
        })

    assert record(first_patch, 'tool_start')['applied'] is True
    assert record(second_patch, 'tool_result')['applied'] is True
    head = authority.fetch_one(
        "SELECT projection_checkpoint_revision,"
        "projection_materialized_revision,projection_patch_count "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    assert head['projection_checkpoint_revision'] is not None
    assert head['projection_materialized_revision'] is not None
    assert head['projection_patch_count'] == 1

    cache.clear()
    activity = _conversation_activity_dates(authority, {
        'user_id': 1,
        'updated_at_gte': 0,
        'day_boundaries_ms': [100, 200, 300],
        'limit': 10,
    })
    assert activity == {'candidate_count': 1, 'counts': [0, 1]}
    assert cache.stats()['entries'] == 1

    cache.clear()
    deleted = _conversation_delete(
        authority, {'conv_id': 'conv-d', 'user_id': 1})
    assert deleted['deleted'] is True
    trashed = authority.fetch_one(
        "SELECT status,projection_json FROM storage_conversation_trash_turns "
        "WHERE conversation_id=? AND user_id=? AND turn_id=?",
        ('conv-d', 1, turn_id),
    )
    assert trashed['status'] == 'interrupted'
    assert _load(trashed['projection_json']) == head_projection
    assert authority.fetch_one(
        "SELECT COUNT(*) AS n FROM storage_turn_projection_checkpoints "
        "WHERE turn_id=?",
        (turn_id,),
    )['n'] == 0
    assert cache.stats()['entries'] == 0

    restored = _conversation_restore(
        authority, {'conv_id': 'conv-d', 'user_id': 1})
    assert restored['restored'] is True
    restored_turn = authority.fetch_one(
        "SELECT status,projection_json,projection_checkpoint_revision,"
        "projection_materialized_revision,projection_patch_count "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    assert restored_turn['status'] == 'interrupted'
    assert _load(restored_turn['projection_json']) == head_projection
    assert restored_turn['projection_checkpoint_revision'] is None
    assert restored_turn['projection_materialized_revision'] is None
    assert restored_turn['projection_patch_count'] == 0


def test_turn_event_record_does_not_trust_unattested_segment_stability(session):
    from lib.storage_sidecar.operations_pkg._common import _load
    from lib.storage_sidecar.operations_pkg._turns import (
        _turn_create_pair,
        _turn_event_record,
    )
    from lib.storage_sidecar.turn_projection_cache import (
        TurnProjectionCache,
        projection_cache_key,
    )
    from lib.turn_projection_patch import build_projection_patch
    from lib.turn_projection_segments import projection_with_stable_segments

    created = _turn_create_pair(session, {
        'conversation_id': 'conv-d', 'user_id': 1,
        'command_id': 'projection-cache-evidence-create',
        'input_projection': {'content': 'question'},
        'config': {},
    })
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    session.execute(
        "UPDATE storage_generation_attempts SET task_id=? WHERE attempt_id=?",
        ('projection-cache-evidence-task', attempt_id),
    )
    before_row = session.fetch_one(
        "SELECT projection_json,projection_revision "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    before = projection_with_stable_segments(
        _load(before_row['projection_json']),
        actor='assistant',
        status='pending',
    )
    # Deliberately change a segment input without updating its mirror.  This
    # models an old or non-canonical private producer that cannot attest the
    # target shape.
    unstabilized = {**before, 'content': 'unattested'}
    patch = build_projection_patch(
        before,
        unstabilized,
        base_revision=before_row['projection_revision'],
        target_revision=before_row['projection_revision'] + 1,
    )
    cache = TurnProjectionCache(1024 * 1024, max_entries=4)
    session.turn_projection_cache = cache

    result = _turn_event_record(session, {
        'attempt_id': attempt_id,
        'user_id': 1,
        'task_id': 'projection-cache-evidence-task',
        'status': 'running',
        'terminal': False,
        'projection_patch': patch,
        'event_type': 'projection_updated',
        'event_payload': {'updateKind': 'legacy-producer'},
    })

    key = projection_cache_key(
        session.backend, 1, 'conv-d', turn_id, attempt_id)
    entry = cache.get(key, revision=result['projection_revision'])
    assert entry is not None
    assert entry.stable_segments is False


def test_turn_recover_materializes_durable_projection_head_after_cache_loss(
        session, monkeypatch):
    import lib.storage_sidecar.turn_projection_head as projection_head_module
    from lib.storage_sidecar.operations_pkg._common import _load
    from lib.storage_sidecar.operations_pkg._turns import (
        _turn_create_pair,
        _turn_event_record,
        _turn_events_prune,
        _turn_recover,
    )
    from lib.storage_sidecar.turn_projection_cache import TurnProjectionCache
    from lib.turn_projection_patch import build_projection_patch
    from lib.turn_projection_segments import projection_with_stable_segments

    created = _turn_create_pair(session, {
        'conversation_id': 'conv-d', 'user_id': 1,
        'command_id': 'projection-head-recovery-create',
        'input_projection': {'content': 'question'},
        'config': {},
    })
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    session.execute(
        "UPDATE storage_generation_attempts SET task_id=? WHERE attempt_id=?",
        ('projection-head-recovery-task', attempt_id),
    )
    before_row = session.fetch_one(
        "SELECT projection_json,projection_revision "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    before = projection_with_stable_segments(
        _load(before_row['projection_json']),
        actor='assistant',
        status='pending',
    )
    first = projection_with_stable_segments(
        {**before, 'content': 'first durable value'},
        actor='assistant',
        status='running',
    )
    second = projection_with_stable_segments(
        {**first, 'content': 'latest durable value'},
        actor='assistant',
        status='running',
    )
    cache = TurnProjectionCache(1024 * 1024, max_entries=4)
    session.turn_projection_cache = cache
    for index, (source, target) in enumerate(
        ((before, first), (first, second)), start=0,
    ):
        revision = before_row['projection_revision'] + index
        result = _turn_event_record(session, {
            'attempt_id': attempt_id,
            'user_id': 1,
            'task_id': 'projection-head-recovery-task',
            'status': 'running',
            'terminal': False,
            'projection_patch': build_projection_patch(
                source,
                target,
                base_revision=revision,
                target_revision=revision + 1,
            ),
            'projection_segments_stable': True,
            'event_type': 'projection_updated',
            'event_payload': {'updateKind': f'recovery-{index}'},
        })
        assert result['applied'] is True
    head = session.fetch_one(
        "SELECT projection_checkpoint_revision,"
        "projection_materialized_revision,projection_patch_count "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    assert head['projection_materialized_revision'] is not None
    assert head['projection_checkpoint_revision'] is not None
    assert head['projection_patch_count'] == 1

    third = projection_with_stable_segments(
        {**second, 'content': 'checkpoint rollover value'},
        actor='assistant',
        status='running',
    )
    monkeypatch.setattr(
        projection_head_module, 'PROJECTION_HEAD_MAX_PATCHES', 1)
    rollover = _turn_event_record(session, {
        'attempt_id': attempt_id,
        'user_id': 1,
        'task_id': 'projection-head-recovery-task',
        'status': 'running',
        'terminal': False,
        'projection_patch': build_projection_patch(
            second,
            third,
            base_revision=before_row['projection_revision'] + 2,
            target_revision=before_row['projection_revision'] + 3,
        ),
        'projection_segments_stable': True,
        'event_type': 'projection_updated',
        'event_payload': {'updateKind': 'checkpoint-rollover'},
    })
    assert rollover['applied'] is True
    rolled = session.fetch_one(
        "SELECT projection_json,projection_revision,"
        "projection_checkpoint_revision,projection_materialized_revision,"
        "projection_patch_count,projection_patch_bytes "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    assert _load(rolled['projection_json']) == {}
    assert rolled['projection_checkpoint_revision'] == rolled['projection_revision']
    assert rolled['projection_materialized_revision'] is None
    assert rolled['projection_patch_count'] == 0
    assert rolled['projection_patch_bytes'] == 0
    rolled_checkpoint = session.fetch_one(
        "SELECT projection_json FROM storage_turn_projection_checkpoints "
        "WHERE turn_id=?",
        (turn_id,),
    )
    assert _load(rolled_checkpoint['projection_json']) == third

    session.execute(
        "UPDATE storage_generation_attempts SET status='completed',settled_at=1 "
        "WHERE attempt_id=?",
        (attempt_id,),
    )
    event_count_before_prune = session.fetch_one(
        "SELECT COUNT(*) AS n FROM storage_attempt_events WHERE attempt_id=?",
        (attempt_id,),
    )['n']
    prune_result = _turn_events_prune(session, {
        'settled_before_ms': 2,
        'max_attempts': 8,
        'max_rows': 64,
    })
    assert prune_result['deleted_rows'] == 0
    assert session.fetch_one(
        "SELECT COUNT(*) AS n FROM storage_attempt_events WHERE attempt_id=?",
        (attempt_id,),
    )['n'] == event_count_before_prune
    session.execute(
        "UPDATE storage_generation_attempts SET status='running',settled_at=NULL "
        "WHERE attempt_id=?",
        (attempt_id,),
    )

    cache.clear()
    recovered = _turn_recover(session, {
        'max_rows': 8,
        'max_bytes': 16 * 1024 * 1024,
    })
    assert recovered['recovered'] == 1
    stored = session.fetch_one(
        "SELECT status,projection_json,projection_checkpoint_revision,"
        "projection_materialized_revision,"
        "projection_patch_count,projection_patch_bytes "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    assert stored['status'] == 'interrupted'
    assert _load(stored['projection_json']) == third
    assert stored['projection_checkpoint_revision'] is None
    assert stored['projection_materialized_revision'] is None
    assert stored['projection_patch_count'] == 0
    assert stored['projection_patch_bytes'] == 0
    assert session.fetch_one(
        "SELECT COUNT(*) AS n FROM storage_turn_projection_checkpoints "
        "WHERE turn_id=?",
        (turn_id,),
    )['n'] == 0
    assert cache.stats()['entries'] == 0


def test_first_event_projection_fence_failure_raises_for_transaction_rollback(
        session, monkeypatch):
    from lib.storage.errors import StorageError
    from lib.storage_sidecar.operations_pkg._turns import (
        _turn_create_pair,
        _turn_event_record,
    )

    created = _turn_create_pair(session, {
        'conversation_id': 'conv-d', 'user_id': 1,
        'command_id': 'projection-cache-fence-create',
        'input_projection': {'content': 'question'},
        'config': {},
    })
    attempt_id = created['attempt']['attemptId']
    session.execute(
        "UPDATE storage_generation_attempts SET task_id=? WHERE attempt_id=?",
        ('projection-cache-fence-task', attempt_id),
    )
    real_fetch_one = session.fetch_one

    def missing_projection_after_attempt_start(sql, params=()):
        if (
            sql.startswith('SELECT projection_json FROM storage_conversation_turns')
            and 'current_attempt_id' in sql
        ):
            return None
        return real_fetch_one(sql, params)

    monkeypatch.setattr(
        session, 'fetch_one', missing_projection_after_attempt_start)
    with pytest.raises(StorageError) as conflict:
        _turn_event_record(session, {
            'attempt_id': attempt_id,
            'user_id': 1,
            'task_id': 'projection-cache-fence-task',
            'status': 'running',
            'terminal': False,
            'projection_patch': {
                'version': 1,
                'baseRevision': 0,
                'targetRevision': 1,
                'operations': [],
            },
            'event_type': 'projection_updated',
            'event_payload': {'updateKind': 'fence-test'},
        })
    assert conflict.value.code == 'database_conflict'


@pytest.fixture()
def session(tmp_path):
    from lib.storage_sidecar.schema import initialize_schema

    conn = sqlite3.connect(str(tmp_path / 'sidecar.db'))
    conn.row_factory = sqlite3.Row
    sess = _SqliteSession(conn)
    initialize_schema(sess)
    _seed_conv(sess)
    yield sess
    conn.close()


def _delta(session, since_ms, conv_id='conv-d', known_revisions=None):
    from lib.storage_sidecar.operations_pkg._turns import _turn_list_delta
    return _turn_list_delta(session, {
        'conversation_id': conv_id, 'user_id': 1, 'since_ms': since_ms,
        **({'known_revisions': known_revisions} if known_revisions else {})})


def test_list_delta_filters_rows_by_watermark_with_overlap(session):
    _seed_turn(session, 't1', 0, 10_000)
    _seed_turn(session, 't2', 1, 20_000)
    _seed_turn(session, 't3', 2, 30_000)

    result = _delta(session, 25_000)

    # lower bound = 25_000 - 5_000 overlap = 20_000 → t1 excluded, t2+t3 in.
    assert [t['turnId'] for t in result['turns']] == ['t2', 't3']
    assert result['deletedTurnIds'] == []
    assert int(result['serverNowMs']) >= 25_000


def test_list_delta_zero_watermark_returns_everything(session):
    _seed_turn(session, 't1', 0, 10_000)
    _seed_turn(session, 't2', 1, 20_000)

    result = _delta(session, 0)

    assert [t['turnId'] for t in result['turns']] == ['t1', 't2']


def test_list_delta_overlap_dedupes_known_projection_revisions(session):
    _seed_turn(session, 'unchanged-heavy', 0, 20_000)
    _seed_turn(session, 'advanced', 1, 21_000)
    session.execute(
        'UPDATE storage_conversation_turns SET projection_revision=2 '
        "WHERE turn_id='advanced'")

    result = _delta(
        session, 25_000,
        known_revisions={'unchanged-heavy': 1, 'advanced': 1})

    assert [turn['turnId'] for turn in result['turns']] == ['advanced'], (
        'the overlap safety window must not retransmit an unchanged heavy row')


def test_list_delta_dedupe_does_not_materialize_unchanged_projection(session):
    _seed_turn(
        session, 'unchanged-heavy', 0, 20_000,
        projection={'content': 'x' * (2 * 1024 * 1024)})
    statements = []
    session._conn.set_trace_callback(statements.append)
    try:
        result = _delta(
            session, 25_000, known_revisions={'unchanged-heavy': 1})
    finally:
        session._conn.set_trace_callback(None)

    assert result['turns'] == []
    assert not any(
        'SELECT * FROM storage_conversation_turns' in statement
        for statement in statements
    ), 'revision dedupe must happen before the multi-MB projection column read'


def test_turn_delete_writes_tombstones_and_delta_reports_deletions(session):
    from lib.storage_sidecar.operations_pkg._turns import _turn_delete

    _seed_turn(session, 't1', 0, 10_000)
    _seed_turn(session, 't2', 1, 20_000)
    _seed_turn(session, 't3', 2, 30_000)

    result = _turn_delete(session, {
        'conversation_id': 'conv-d', 'user_id': 1, 'turn_ids': ['t2']})

    assert result['deletedTurnIds'] == ['t2']
    assert result['conversationRevision'] == 4  # seed rev 3 + 1

    delta = _delta(session, 0)
    assert [t['turnId'] for t in delta['turns']] == ['t1', 't3']
    assert delta['deletedTurnIds'] == ['t2']


def test_turn_compact_is_one_atomic_authority_rewrite(session):
    """Summary insert, cold projection fold and deletion share one rev-CAS.

    The retained legacy-compatible message overlay deliberately carries
    identity fields in the update. They must not leak into projection_json.
    """
    from lib.storage_sidecar.operations import resolve_operation
    from lib.storage_sidecar.operations_pkg._common import _load

    _seed_turn(
        session, 'anchor', 0, 10_000, actor='human',
        projection={'content': 'original objective'})
    _seed_turn(
        session, 'folded-reply', 1, 11_000, parent='anchor',
        projection={'content': 'old answer'})
    _seed_turn(
        session, 'reserve-user', 2, 12_000, actor='human',
        parent='folded-reply', projection={'content': 'continue'})
    _seed_turn(
        session, 'heavy-reply', 3, 13_000, parent='reserve-user',
        projection={
            'content': 'latest answer',
            'toolRounds': [{'id': 'cold'}, {'id': 'hot'}],
        })

    payload = {
        'conversation_id': 'conv-d',
        'user_id': 1,
        'expected_conversation_revision': 3,
        'summary_turn_id': 'compact-summary',
        'summary_projection': {
            'role': 'assistant',
            'content': '## compacted',
            '_isCompactionSummary': True,
            '_turnId': 'must-not-persist',
        },
        'delete_turn_ids': ['folded-reply'],
        'projection_updates': [{
            'turn_id': 'heavy-reply',
            'expected_projection_revision': 1,
            'projection': {
                'role': 'assistant',
                '_turnId': 'heavy-reply',
                '_projectionRevision': 1,
                'content': 'latest answer',
                'toolRounds': [{'id': 'hot'}],
                '_intraTurnFolded': 1,
            },
        }],
        'insert_after_turn_id': 'anchor',
        'insert_before_turn_id': 'reserve-user',
    }
    receipt_required, execute = resolve_operation(
        'turn.compact', 'command', payload)
    assert receipt_required is True
    wrapped = execute(session)
    result = wrapped['value']

    assert result['applied'] is True
    assert result['conversationRevision'] == 4
    assert result['deletedTurnIds'] == ['folded-reply']
    assert wrapped['events'][0]['event']['payload'] == {
        'requiresSnapshot': True,
        'conversationRevision': 4,
    }

    rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns "
        "WHERE conversation_id='conv-d' AND lane_id='main' ORDER BY ordinal")
    assert [row['turn_id'] for row in rows] == [
        'anchor', 'compact-summary', 'reserve-user', 'heavy-reply']
    assert [row['ordinal'] for row in rows] == [0, 1, 2, 3]
    assert rows[1]['parent_turn_id'] == 'anchor'
    assert rows[2]['parent_turn_id'] == 'compact-summary'
    assert _load(rows[1]['projection_json']) == {
        'content': '## compacted',
        'compaction': {'blockId': 'compaction'},
    }
    assert _load(rows[3]['projection_json']) == {
        'content': 'latest answer', 'toolRounds': [{'id': 'hot'}]}
    assert rows[3]['projection_revision'] == 2
    assert session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id='conv-d'")['rev'] == 4
    assert _delta(session, 0)['deletedTurnIds'] == ['folded-reply']


def test_turn_compact_stale_revision_has_zero_side_effects(session):
    from lib.storage_sidecar.operations_pkg._turns import _turn_compact

    _seed_turn(session, 'keep', 0, 10_000, actor='human')
    result = _turn_compact(session, {
        'conversation_id': 'conv-d', 'user_id': 1,
        'expected_conversation_revision': 2,
        'summary_turn_id': 'never-created',
        'summary_projection': {
            'content': 'summary', '_isCompactionSummary': True},
        'delete_turn_ids': [], 'projection_updates': [],
        'insert_before_turn_id': 'keep',
    })

    assert result == {'applied': False, 'conversationRevision': 3}
    assert session.fetch_one(
        "SELECT turn_id FROM storage_conversation_turns "
        "WHERE turn_id='never-created'") is None
    assert session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id='conv-d'")['rev'] == 3


def test_turn_delete_tombstone_survives_the_overlap_window(session):
    """A watermark captured just AFTER the delete must still see the
    tombstone — the overlap margin covers the watermark/write race."""
    from lib.storage_sidecar.operations_pkg._turns import _turn_delete

    _seed_turn(session, 't1', 0, 10_000)
    _turn_delete(session, {
        'conversation_id': 'conv-d', 'user_id': 1, 'turn_ids': ['t1']})

    future_watermark = int(time.time() * 1000) + 1_000
    delta = _delta(session, future_watermark)

    assert delta['turns'] == []
    assert delta['deletedTurnIds'] == ['t1']


def test_branch_delete_tombstones_lane_children(session):
    from lib.storage_sidecar.operations_pkg._turns import _turn_branch_delete

    _seed_turn(session, 'parent', 0, 10_000, projection={
        'content': 'p', '_branchLanes': [{'laneId': 'b1', 'label': 'B'}]})
    _seed_turn(session, 'c1', 1, 20_000, lane='b1', parent='parent')
    _seed_turn(session, 'c2', 2, 30_000, lane='b1', parent='parent')

    _turn_branch_delete(session, {
        'conversation_id': 'conv-d', 'user_id': 1,
        'parent_turn_id': 'parent', 'lane_id': 'b1'})

    delta = _delta(session, 0)
    assert [t['turnId'] for t in delta['turns']] == ['parent']
    assert sorted(delta['deletedTurnIds']) == ['c1', 'c2']
    parent = [t for t in delta['turns']][0]
    assert parent['projection']['_branchLanes'] == []


def test_branch_delete_removes_nested_lane_closure(session):
    """A branch row can itself own another lane; deleting only the first lane
    leaves unreachable durable turns and attempts behind."""
    from lib.storage_sidecar.operations_pkg._turns import _turn_branch_delete

    _seed_turn(session, 'parent', 0, 10_000, projection={
        'content': 'p', '_branchLanes': [{'laneId': 'b1'}]})
    _seed_turn(session, 'child', 1, 20_000, lane='b1', parent='parent',
               projection={
                   'content': 'c', '_branchLanes': [{'laneId': 'b2'}]})
    _seed_turn(session, 'grandchild', 2, 30_000, lane='b2', parent='child')

    result = _turn_branch_delete(session, {
        'conversation_id': 'conv-d', 'user_id': 1,
        'parent_turn_id': 'parent', 'lane_id': 'b1'})

    assert sorted(result['deletedTurnIds']) == ['child', 'grandchild']
    assert session.fetch_all(
        "SELECT turn_id FROM storage_conversation_turns "
        "WHERE lane_id IN ('b1','b2')") == []
    assert sorted(_delta(session, 0)['deletedTurnIds']) == [
        'child', 'grandchild']


def test_tombstone_pruning_drops_stale_entries_only(session):
    from lib.storage_sidecar.operations_pkg._turns import (
        _TOMBSTONE_RETENTION_MS, _turn_delete)

    _seed_turn(session, 't1', 0, 10_000)
    stale = int(time.time() * 1000) - _TOMBSTONE_RETENTION_MS - 60_000
    session.execute(
        'INSERT INTO storage_turn_tombstones '
        '(conversation_id, user_id, turn_id, deleted_at) VALUES (?,?,?,?)',
        ('conv-d', 1, 'ancient', stale))

    _turn_delete(session, {
        'conversation_id': 'conv-d', 'user_id': 1, 'turn_ids': ['t1']})

    delta = _delta(session, 0)
    assert delta['deletedTurnIds'] == ['t1']


def _search_ids(session, query):
    from lib.storage_sidecar.operations_pkg._conversations import (
        _conversation_search_op,
    )

    return [item['id'] for item in _conversation_search_op(session, {
        'query': query, 'user_id': 1, 'limit': 50, 'snippet_radius': 20,
    })]


def test_turn_event_record_skips_unchanged_projection_blob_assignment(session):
    """A diagnostic frame may advance replay without rewriting a large BLOB."""
    from lib.storage_sidecar.operations_pkg._common import _load
    from lib.storage_sidecar.operations_pkg._turns import (
        _turn_create_pair,
        _turn_event_record,
    )

    created = _turn_create_pair(session, {
        'conversation_id': 'conv-d', 'user_id': 1,
        'command_id': 'unchanged-projection-create',
        'input_projection': {'content': 'question'},
        'config': {},
    })
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    session.execute(
        "UPDATE storage_generation_attempts SET task_id=? WHERE attempt_id=?",
        ('unchanged-projection-task', attempt_id),
    )
    before = session.fetch_one(
        "SELECT projection_json,projection_revision "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    projection = _load(before['projection_json'])

    executed_sql = []
    original_execute = session.execute

    def capture_execute(sql, params=()):
        executed_sql.append(sql)
        return original_execute(sql, params)

    session.execute = capture_execute
    result = _turn_event_record(session, {
        'attempt_id': attempt_id,
        'user_id': 1,
        'task_id': 'unchanged-projection-task',
        'status': 'running',
        'terminal': False,
        'projection': projection,
        'event_type': 'projection_updated',
        'event_payload': {'updateKind': 'phase'},
    })

    assert result['applied'] is True
    turn_updates = [
        sql for sql in executed_sql
        if sql.startswith('UPDATE storage_conversation_turns SET')
    ]
    assert len(turn_updates) == 1
    assert 'projection_json' not in turn_updates[0]
    after = session.fetch_one(
        "SELECT projection_json,projection_revision "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    assert _load(after['projection_json']) == projection
    assert after['projection_revision'] == before['projection_revision'] + 1
    stored_event = session.fetch_one(
        "SELECT payload_json FROM storage_attempt_events "
        "WHERE attempt_id=? ORDER BY sequence DESC LIMIT 1",
        (attempt_id,),
    )
    assert _load(stored_event['payload_json'])['payload'][
        'projectionPatch']['operations'] == []


def test_turn_event_record_skips_unchanged_slim_projection_blob_assignment(
        session):
    """Text-only cadence frames avoid JSON mutation when both fields match."""
    from lib.storage_sidecar.operations_pkg._common import _load
    from lib.storage_sidecar.operations_pkg._turns import (
        _turn_create_pair,
        _turn_event_record,
    )

    created = _turn_create_pair(session, {
        'conversation_id': 'conv-d', 'user_id': 1,
        'command_id': 'unchanged-slim-create',
        'input_projection': {'content': 'question'},
        'config': {},
    })
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    session.execute(
        "UPDATE storage_generation_attempts SET task_id=? WHERE attempt_id=?",
        ('unchanged-slim-task', attempt_id),
    )
    before = session.fetch_one(
        "SELECT projection_json,projection_revision "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )

    executed_sql = []
    original_execute = session.execute

    def capture_execute(sql, params=()):
        executed_sql.append(sql)
        return original_execute(sql, params)

    session.execute = capture_execute
    result = _turn_event_record(session, {
        'attempt_id': attempt_id,
        'user_id': 1,
        'task_id': 'unchanged-slim-task',
        'status': 'running',
        'terminal': False,
        'projection': {'content': '', 'thinking': ''},
        'slim': True,
        'content': '',
        'thinking': '',
        'event_type': 'projection_updated',
        'event_payload': {'updateKind': 'tool_progress'},
    })

    assert result['applied'] is True
    turn_updates = [
        sql for sql in executed_sql
        if sql.startswith('UPDATE storage_conversation_turns SET')
    ]
    assert len(turn_updates) == 1
    assert 'projection_json' not in turn_updates[0]
    after = session.fetch_one(
        "SELECT projection_json,projection_revision "
        "FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    assert after['projection_json'] == before['projection_json']
    assert after['projection_revision'] == before['projection_revision'] + 1
    stored_event = session.fetch_one(
        "SELECT payload_json FROM storage_attempt_events "
        "WHERE attempt_id=? ORDER BY sequence DESC LIMIT 1",
        (attempt_id,),
    )
    assert _load(stored_event['payload_json'])['payload'][
        'projectionPatch']['operations'] == []


def test_turn_event_record_repairs_non_mapping_legacy_projection(session):
    """A decoded fallback must not masquerade as canonical stored equality."""
    from lib.storage_sidecar.operations_pkg._common import _dump, _load
    from lib.storage_sidecar.operations_pkg._turns import (
        _turn_create_pair,
        _turn_event_record,
    )

    created = _turn_create_pair(session, {
        'conversation_id': 'conv-d', 'user_id': 1,
        'command_id': 'legacy-projection-create',
        'input_projection': {'content': 'question'},
        'config': {},
    })
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    session.execute(
        "UPDATE storage_generation_attempts SET task_id=? WHERE attempt_id=?",
        ('legacy-projection-task', attempt_id),
    )
    session.execute(
        "UPDATE storage_conversation_turns SET projection_json=? "
        "WHERE turn_id=?",
        (_dump(['legacy-non-mapping']), turn_id),
    )

    executed_sql = []
    original_execute = session.execute

    def capture_execute(sql, params=()):
        executed_sql.append(sql)
        return original_execute(sql, params)

    session.execute = capture_execute
    result = _turn_event_record(session, {
        'attempt_id': attempt_id,
        'user_id': 1,
        'task_id': 'legacy-projection-task',
        'status': 'running',
        'terminal': False,
        'projection': {},
        'event_type': 'projection_updated',
        'event_payload': {'updateKind': 'phase'},
    })

    assert result['applied'] is True
    turn_updates = [
        sql for sql in executed_sql
        if sql.startswith('UPDATE storage_conversation_turns SET')
    ]
    assert len(turn_updates) == 1
    assert 'projection_json' in turn_updates[0]
    stored = session.fetch_one(
        "SELECT projection_json FROM storage_conversation_turns WHERE turn_id=?",
        (turn_id,),
    )
    assert _load(stored['projection_json']) == {}


def test_turn_search_indexes_submission_and_terminal_projection_only(session):
    """Draft deltas must not leak into search; terminal settlement replaces
    the per-turn fragment without rewriting a conversation-sized blob."""
    from lib.storage_sidecar.operations_pkg._turns import (
        _turn_create_pair,
        _turn_event_record,
    )

    created = _turn_create_pair(session, {
        'conversation_id': 'conv-d', 'user_id': 1,
        'command_id': 'search-create',
        'input_projection': {'content': 'question needle'},
        'config': {},
    })
    _materialize_turn_search(
        session, created['submittedTurn']['turnId'])
    assert _search_ids(session, 'question needle') == ['conv-d']

    attempt_id = created['attempt']['attemptId']
    session.execute(
        "UPDATE storage_generation_attempts SET task_id=? WHERE attempt_id=?",
        ('search-index-task', attempt_id),
    )
    _turn_event_record(session, {
        'attempt_id': attempt_id, 'user_id': 1,
        'task_id': 'search-index-task',
        'status': 'running', 'terminal': False,
        'projection': {'content': 'private draft needle'},
    })
    assert _search_ids(session, 'private draft') == []

    _turn_event_record(session, {
        'attempt_id': attempt_id, 'user_id': 1,
        'task_id': 'search-index-task',
        'status': 'completed', 'terminal': True,
        'projection': {'content': 'settled answer needle'},
        'settlement': {'outcome': 'completed'},
    })
    _materialize_turn_search(session, created['turn']['turnId'])
    assert _search_ids(session, 'settled answer') == ['conv-d']
    assert _search_ids(session, 'private draft') == []


def test_turn_search_backfill_ignores_stale_legacy_blob_and_spans_turns(session):
    """Once main turns exist, the frozen v1 aggregate is never authoritative.
    The word top-up may match different settled turn fragments."""
    from lib.storage_sidecar.operations_pkg._turns import _turn_search_backfill

    session.execute(
        "UPDATE storage_conversations SET search_text='stale ghost phrase' "
        "WHERE id='conv-d' AND user_id=1")
    _seed_turn(session, 'red-turn', 0, 10_000, actor='human',
               projection={'content': 'canonical red phrase'})
    _seed_turn(session, 'blue-turn', 1, 20_000,
               projection={'content': 'canonical blue phrase'})

    result = _turn_search_backfill(session, {
        'cursor': '', 'max_rows': 8, 'max_bytes': 2_000_000})
    _materialize_conversation_search(session)

    assert result['scheduled'] is True
    assert result['indexed'] == 0
    assert result['remaining'] is False
    assert _search_ids(session, 'stale ghost') == []
    assert _search_ids(session, 'canonical red') == ['conv-d']
    assert _search_ids(session, 'red blue') == ['conv-d']


def test_turn_search_edit_and_delete_remove_stale_terms(session):
    from lib.storage_sidecar.operations_pkg._turns import (
        _turn_delete,
        _turn_projection_update,
    )

    _seed_turn(session, 'editable', 0, 10_000,
               projection={'content': 'obsolete searchable phrase'})
    _materialize_turn_search(session, 'editable')
    assert _search_ids(session, 'obsolete searchable') == ['conv-d']

    _turn_projection_update(session, {
        'conversation_id': 'conv-d', 'user_id': 1, 'turn_id': 'editable',
        'expected_projection_revision': 1,
        'projection': {'content': 'replacement searchable phrase'},
    })
    marker = session.fetch_one(
        "SELECT version_token FROM storage_projection_outbox "
        "WHERE projection_name='turn_search.v1' AND entity_kind='turn' "
        "AND entity_key='editable'")
    assert marker is not None
    _materialize_turn_search(
        session, 'editable', token=str(marker['version_token']))
    assert _search_ids(session, 'obsolete searchable') == []
    assert _search_ids(session, 'replacement searchable') == ['conv-d']

    _turn_delete(session, {
        'conversation_id': 'conv-d', 'user_id': 1,
        'turn_ids': ['editable'],
    })
    _materialize_conversation_search(session)
    assert _search_ids(session, 'replacement searchable') == []
    assert session.fetch_one(
        "SELECT turn_id FROM storage_search_turns WHERE turn_id='editable'"
    ) is None


def test_turn_search_fragment_is_byte_bounded_and_recoverable_delete_detaches_it(
        session):
    """The derived index cannot duplicate an unbounded projection, and its
    rows become unreachable while retained for conversation recovery."""
    from lib.storage_sidecar.operations_pkg._conversations import (
        _conversation_delete,
    )
    from lib.storage_sidecar.operations_pkg._turns import (
        _TURN_SEARCH_TEXT_MAX_BYTES,
    )

    _seed_turn(
        session, 'bounded-search', 0, 10_000, actor='human',
        projection={'content': 'visible-prefix ' + ('界' * 20_000)
                    + ' unreachable-tail-needle'},
    )
    _materialize_turn_search(session, 'bounded-search')

    indexed = session.fetch_one(
        "SELECT search_text FROM storage_search_turns "
        "WHERE turn_id='bounded-search'")
    assert indexed is not None
    assert len(indexed['search_text'].encode('utf-8')) \
        <= _TURN_SEARCH_TEXT_MAX_BYTES
    assert _search_ids(session, 'visible-prefix') == ['conv-d']
    assert _search_ids(session, 'unreachable-tail-needle') == []

    deleted = _conversation_delete(session, {
        'conv_id': 'conv-d', 'user_id': 1})
    assert deleted['deleted'] is True
    assert deleted['recoverable'] is True
    assert deleted['deletedAt'] > 0
    _materialize_conversation_search(session)
    assert _search_ids(session, 'visible-prefix') == []
    assert session.fetch_one(
        "SELECT turn_id FROM storage_search_turns "
        "WHERE conversation_id='conv-d'") is None


def test_turn_search_backfill_worker_retries_only_transient_storage_errors(
        monkeypatch):
    """A brief startup-sidecar contention cannot defer convergence to reboot."""
    from lib.storage.errors import StorageError
    from lib import turn_lifecycle

    outcomes = [
        StorageError('database_busy', 'writer occupied', True, 125),
        {'scanned': 2, 'indexed': 2, 'failed': 0, 'complete': True},
    ]
    calls = []
    sleeps = []

    def _backfill():
        calls.append(True)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(turn_lifecycle, 'backfill_turn_search_index', _backfill)
    monkeypatch.setattr(turn_lifecycle.time, 'sleep', sleeps.append)

    turn_lifecycle._run_turn_search_backfill_worker()

    assert len(calls) == 2
    assert sleeps == [0.25]


def test_turn_search_backfill_worker_defers_startup_maintenance(monkeypatch):
    from lib import turn_lifecycle

    calls = []
    sleeps = []
    monkeypatch.setattr(
        turn_lifecycle, 'backfill_turn_search_index',
        lambda: calls.append(True) or {
            'scanned': 0, 'indexed': 0, 'failed': 0, 'complete': True,
        })
    monkeypatch.setattr(turn_lifecycle.time, 'sleep', sleeps.append)

    turn_lifecycle._run_turn_search_backfill_worker(
        initial_delay_seconds=60.0)

    assert calls == [True]
    assert sleeps == [60.0]


def test_turn_search_backfill_worker_stops_on_nonretryable_error(monkeypatch):
    from lib.storage.errors import StorageError
    from lib import turn_lifecycle

    calls = []

    def _backfill():
        calls.append(True)
        raise StorageError(
            'database_protocol_error', 'bad maintenance response', False)

    monkeypatch.setattr(turn_lifecycle, 'backfill_turn_search_index', _backfill)
    monkeypatch.setattr(
        turn_lifecycle.time, 'sleep',
        lambda _delay: pytest.fail('nonretryable failures must not be retried'))

    turn_lifecycle._run_turn_search_backfill_worker()

    assert len(calls) == 1


# ── list_turns wiring (fake semantic-storage client) ───────────────────


_UNSET = object()


class _FakeTurnClient:
    def __init__(self, *, delta=None, full=None, revision=7,
                 conversation=_UNSET):
        self._delta = delta or {}
        self._full = full if full is not None else []
        self._revision = revision
        # Default: conversation exists.  Pass None explicitly for the 404 arm.
        self._conversation = {} if conversation is _UNSET else conversation
        self.ops = []
        self.calls = []

    def query(self, op, payload):
        self.ops.append(op)
        self.calls.append((op, dict(payload)))
        if op == 'turn.list_delta':
            return self._delta
        if op == 'turn.list':
            return self._full
        if op == 'turn.revision':
            return self._revision
        if op == 'conversation.get':
            return self._conversation
        raise AssertionError(f'unexpected op {op}')


@pytest.fixture()
def fake_sidecar(monkeypatch):
    from lib import turn_lifecycle

    holder = {}

    def install(client):
        holder['client'] = client
        monkeypatch.setattr(
            turn_lifecycle, '_turn_client', lambda write=False: client)

    return install


def _delta_row(turn_id, content='x'):
    return {
        'turnId': turn_id, 'conversationId': 'conv-d', 'laneId': 'main',
        'parentTurnId': None, 'ordinal': 0, 'actor': 'assistant',
        'kind': 'reply', 'runId': '', 'status': 'completed',
        'currentAttemptId': None,
        'projection': {'content': content, 'tools': ['heavy'],
                       'model': 'm', 'usage': {}, 'thinking': '',
                       'segments': []},
        'projectionRevision': 1, 'settlement': {},
        'createdAt': 1000, 'updatedAt': 2000,
    }


def test_list_turns_delta_shape_and_light_projection(fake_sidecar):
    from lib.turn_lifecycle import list_turns

    client = _FakeTurnClient(delta={
        'turns': [_delta_row('t1'), _delta_row('t2')],
        'deletedTurnIds': ['t0'],
        'serverNowMs': 123_456,
    })
    fake_sidecar(client)

    result = list_turns(
        'conv-d', since_ms=100_000, light=True,
        known_revisions={'t1': 4}, user_id=1)

    assert result['delta'] is True
    assert result['authoritativeFull'] is False
    assert result['cutoverActive'] is True
    assert result['deletedTurnIds'] == ['t0']
    assert result['serverNowMs'] == 123_456
    assert result['conversationRevision'] == 7
    assert [t['turnId'] for t in result['turns']] == ['t1', 't2']
    # Light projection keeps only the cheap keys.
    assert 'tools' not in result['turns'][0]['projection']
    assert result['turns'][0]['projection']['content'] == 'x'
    assert 'turn.list' not in client.ops  # stayed on the delta path
    delta_call = next(payload for op, payload in client.calls
                      if op == 'turn.list_delta')
    assert delta_call['known_revisions'] == {'t1': 4}


def test_list_turns_delta_skipped_for_filtered_reads(fake_sidecar):
    from lib.turn_lifecycle import list_turns

    client = _FakeTurnClient(
        delta={'turns': [_delta_row('t1')], 'serverNowMs': 1},
        full=[_delta_row('t1')], revision=9)
    fake_sidecar(client)

    # lane filter → full semantics even with a watermark present.
    result = list_turns('conv-d', lane_id='main', since_ms=100_000, user_id=1)
    assert 'delta' not in result
    assert result['authoritativeFull'] is False  # filtered → not authoritative
    assert 'turn.list' in client.ops and 'turn.list_delta' not in client.ops


def test_list_turns_delta_rejects_clock_confused_watermark(fake_sidecar):
    from lib.turn_lifecycle import list_turns

    client = _FakeTurnClient(
        delta={'turns': [], 'serverNowMs': 1}, full=[_delta_row('t1')])
    fake_sidecar(client)

    future = int(time.time() * 1000) + 3_600_000  # > _DELTA_MAX_SKEW_MS ahead
    result = list_turns('conv-d', since_ms=future, user_id=1)

    assert 'delta' not in result
    assert result['authoritativeFull'] is True
    assert result['serverNowMs'] > 0
    assert 'turn.list_delta' not in client.ops


def test_list_turns_delta_overflow_degrades_to_full_snapshot(fake_sidecar):
    from lib.turn_lifecycle import list_turns

    client = _FakeTurnClient(
        delta={'turns': [_delta_row(f't{i}') for i in range(5)],
               'serverNowMs': 1},
        full=[_delta_row('full')], revision=11)
    fake_sidecar(client)

    result = list_turns('conv-d', since_ms=100_000, limit=2, user_id=1)

    # 5 changed rows > limit 2 → truncation would silently drop rows the
    # client never re-fetches, so the answer must be the full snapshot.
    assert 'delta' not in result
    assert result['authoritativeFull'] is True
    assert [t['turnId'] for t in result['turns']] == ['full']
    assert result['conversationRevision'] == 11


def test_list_turns_delta_missing_conversation_raises(fake_sidecar):
    from lib.turn_lifecycle import LifecycleNotFound, list_turns

    client = _FakeTurnClient(
        delta={'turns': [], 'serverNowMs': 1}, conversation=None)
    fake_sidecar(client)

    with pytest.raises(LifecycleNotFound):
        list_turns('conv-d', since_ms=100_000, user_id=1)
