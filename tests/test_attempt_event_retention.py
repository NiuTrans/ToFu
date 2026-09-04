"""Turn attempt-event transport: slim frames, read hydration, TTL prune, reclaim.

2026-08-20 postmortem contracts.  ``storage_attempt_events`` carried a full
projection copy per streaming delta and grew to 281 GiB (71% of the 395 GiB
authority).  The root fix is four-layered and every layer is pinned here:

1. ``turn.event.record`` persists compact revision-to-revision patches — the
   full projection lands transactionally only on the turn-row authority.
2. Legacy ``turn.events.list`` readers get one hydrated page tail; patch-mode
   readers replay only the compact patches, with no multi-MB wire expansion.
3. After retained Conversation Sync references expire,
   ``turn.events.prune`` deletes OLD settled attempts' streams in bounded,
   resumable slices; live attempts are structurally untouchable.
4. ``system.reclaim`` returns the freelist to the filesystem through bounded
   incremental_vacuum slices, and fresh authorities are born with
   ``auto_vacuum=INCREMENTAL`` so reclamation is always available.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

import pytest

from lib.storage import StorageError, StorageSupervisor
from lib.storage.errors import http_status_for_storage_error

pytestmark = pytest.mark.unit


@pytest.fixture
def storage(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '2')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    try:
        yield supervisor
    finally:
        supervisor.stop()


@pytest.fixture
def turn_service(storage, monkeypatch):
    """turn_lifecycle wired at the real sidecar, coalescing disabled."""
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: storage.client)
    from lib import turn_lifecycle
    return turn_lifecycle


def _running_attempt(turn_service, storage, conv_id, task_id, *, content='seed'):
    created = turn_service.create_turn_pair(
        conv_id, command_id=f'{task_id}-pair',
        input_projection={'content': 'q'}, config={'model': 'gpt-4o'},
        user_id=1,
        conversation_defaults={
            'allowCreate': True, 'title': 'retention', 'settings': {},
        })
    attempt_id = created['attempt']['attemptId']
    turn_service.bind_task(attempt_id, task_id, user_id=1)
    task = {
        '_attemptId': attempt_id, 'id': task_id, 'status': 'running',
        '_userId': 1,
        'content': content, 'thinking': '', 'toolRounds': [],
        'model': 'gpt-4o', 'config': {'model': 'gpt-4o'},
    }
    return created, task


def _record_delta(turn_service, task, content):
    task['content'] = content
    assert turn_service.record_task_event(task, {'type': 'delta', 'content': content}) is True


def test_nonterminal_frames_are_slim_and_page_tail_is_hydrated(turn_service, storage):
    created, task = _running_attempt(turn_service, storage, 'slim-conv', 'slim-task')
    attempt_id = created['attempt']['attemptId']
    _record_delta(turn_service, task, 'partial one')
    _record_delta(turn_service, task, 'partial one two')
    _record_delta(turn_service, task, 'partial one two three')

    events = turn_service.read_events(attempt_id, user_id=1)
    projection_frames = [
        e for e in events if e.get('type') == 'projection_updated']
    assert len(projection_frames) == 3
    # Every persisted frame except the hydrated page tail is slim…
    for frame in projection_frames[:-1]:
        assert 'projection' not in frame['payload']
        assert frame['payload']['projectionBytes'] > 0
    # …and the tail carries the CURRENT authority projection, which is what
    # a reconnecting client folds to.  Older slim frames are folded past.
    tail = projection_frames[-1]
    assert tail['payload']['projection']['content'] == 'partial one two three'

    patch_events = turn_service.read_events(
        attempt_id, user_id=1, projection_mode='patch')
    patch_frames = [
        e for e in patch_events if e.get('type') == 'projection_updated']
    assert len(patch_frames) == 3
    assert all('projection' not in frame['payload'] for frame in patch_frames)
    assert all(frame['payload']['projectionPatch']['version'] == 1
               for frame in patch_frames)
    assert patch_frames[-1]['payload']['projectionPatch'][
        'targetRevision'] == patch_frames[-1]['projectionRevision']
    # The Turn authority still rehydrates the complete logical projection.
    turn = turn_service.get_turn(
        'slim-conv', created['turn']['turnId'], user_id=1)
    assert turn['projection']['content'] == 'partial one two three'


def test_patch_frame_size_does_not_scale_with_existing_projection(turn_service, storage):
    created, task = _running_attempt(
        turn_service, storage, 'patch-size-conv', 'patch-size-task')
    attempt_id = created['attempt']['attemptId']
    task['toolRounds'] = [
        {'index': index, 'status': 'done', 'toolContent': 'x' * (32 * 1024)}
        for index in range(64)
    ]
    assert turn_service.record_task_event(task, {'type': 'tool_start'}) is True

    # A new structural frame retains >2 MiB of logical projection evidence,
    # while the physical checkpoint/head and SSE replay patch stay compact.
    task['toolRounds'].append({
        'index': 64, 'status': 'running', 'toolContent': 'new round',
    })
    assert turn_service.record_task_event(task, {'type': 'tool_start'}) is True
    frames = [
        event for event in turn_service.read_events(
            attempt_id, user_id=1, projection_mode='patch')
        if event.get('type') == 'projection_updated'
    ]
    assert len(frames) == 2
    frame = frames[-1]
    assert frame['payload']['projectionBytes'] > 2 * 1024 * 1024
    assert 'projection' not in frame['payload']
    assert len(json.dumps(frame, separators=(',', ':'))) < 4096


def test_oversized_nonterminal_frame_rolls_back_and_is_observable(
        turn_service, storage, tmp_path):
    from lib.storage_sidecar.operations_pkg._turns import (
        _ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES,
    )

    created, task = _running_attempt(
        turn_service, storage, 'frame-limit-conv', 'frame-limit-task')
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    before_turn = turn_service.get_turn(
        'frame-limit-conv', turn_id, user_id=1)
    before_events = turn_service.read_events(
        attempt_id, user_id=1, projection_mode='patch')

    # One newly appended structural value must be represented in the patch,
    # making this frame larger than the durable transport budget.
    task['toolRounds'] = [{
        'index': 0,
        'status': 'running',
        'toolContent': 'x' * (_ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES + 4096),
    }]
    with pytest.raises(StorageError) as raised:
        turn_service.record_task_event(task, {'type': 'tool_start'})

    assert raised.value.code == 'storage_payload_too_large'
    assert http_status_for_storage_error(raised.value) == 413
    after_turn = turn_service.get_turn(
        'frame-limit-conv', turn_id, user_id=1)
    assert after_turn['projectionRevision'] == before_turn['projectionRevision']
    assert after_turn['projection'] == before_turn['projection']
    assert turn_service.read_events(
        attempt_id, user_id=1, projection_mode='patch') == before_events

    metrics = storage.client.metrics()['attempt_events']
    assert metrics['max_nonterminal_payload_bytes'] == (
        _ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES)
    rejected = metrics['by_type']['projection_updated']
    assert rejected['rejected_events'] >= 1
    assert rejected['max_rejected_payload_bytes'] > (
        _ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES)

    # Encoded byte sizes are persisted for accepted new rows; migration-era
    # rows use zero rather than forcing a startup backfill of the huge table.
    connection = sqlite3.connect(
        f'file:{tmp_path / "data" / "tofu.db"}?mode=ro', uri=True)
    try:
        stored = connection.execute(
            'SELECT payload_bytes, length(payload_json) '
            'FROM storage_attempt_events WHERE attempt_id=? ORDER BY sequence',
            (attempt_id,),
        ).fetchall()
    finally:
        connection.close()
    assert stored
    assert all(payload_bytes > 0 for payload_bytes, _ in stored)
    assert all(payload_bytes == encoded_length
               for payload_bytes, encoded_length in stored)


def test_carried_oversized_frame_degrades_once_then_uses_slim_circuit(
        turn_service, storage):
    from lib.storage_sidecar.operations_pkg._turns import (
        _ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES,
    )

    created, task = _running_attempt(
        turn_service, storage, 'carried-limit-conv', 'carried-limit-task')
    task['toolRounds'] = [{
        'index': 0,
        'status': 'running',
        'toolContent': 'x' * (_ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES + 4096),
    }]
    first_event = {'type': 'tool_start', 'toolCallId': 'call-1'}
    assert turn_service.record_task_event(
        task,
        first_event,
        task_event={
            'task_id': task['id'], 'sequence': 0, 'event': first_event,
        },
    ) == 'carried'

    first_metrics = storage.client.metrics()['attempt_events'][
        'by_type']['projection_updated']
    assert first_metrics['rejected_events'] == 1
    turn = turn_service.get_turn(
        'carried-limit-conv', created['turn']['turnId'], user_id=1)
    assert turn['projection']['content'] == 'seed'
    assert turn['projection']['toolRounds'] == []

    second_event = {'type': 'tool_progress', 'chunk': 'still alive'}
    assert turn_service.record_task_event(
        task,
        second_event,
        task_event={
            'task_id': task['id'], 'sequence': 1, 'event': second_event,
        },
    ) == 'carried'
    second_metrics = storage.client.metrics()['attempt_events'][
        'by_type']['projection_updated']
    assert second_metrics['rejected_events'] == 1
    latest = storage.client.query(
        'event.latest', {'task_id': task['id']})
    assert latest['sequence'] == 1
    assert latest['event'] == second_event


def test_completed_tool_round_drops_redundant_partial_output(turn_service, storage):
    created, task = _running_attempt(
        turn_service, storage, 'trimmed-round-conv', 'trimmed-round-task')
    task['toolRounds'] = [{
        'index': 0,
        'status': 'done',
        'toolContent': 'settled output',
        '_partialOutput': 'x' * (5 * 1024 * 1024),
        '_partialOutputTotalChars': 5 * 1024 * 1024,
        '_partialOutputTruncated': True,
    }]

    assert turn_service.record_task_event(task, {'type': 'tool_result'}) is True
    turn = turn_service.get_turn(
        'trimmed-round-conv', created['turn']['turnId'], user_id=1)
    assert turn['projection']['toolRounds'] == [{
        'attemptId': task['_attemptId'],
        'index': 0,
        'status': 'done',
        'taskId': task['id'],
        'toolContent': 'settled output',
    }]


def test_event_size_migration_does_not_backfill_historical_payloads(tmp_path):
    from lib.storage_sidecar.schema import SCHEMA_VERSION, initialize_schema

    class MigrationSession:
        backend = 'sqlite'

        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, params=()):
            return self.connection.execute(sql, tuple(params)).rowcount

        def fetch_one(self, sql, params=()):
            row = self.connection.execute(sql, tuple(params)).fetchone()
            return dict(row) if row is not None else None

        def fetch_all(self, sql, params=()):
            return [dict(row) for row in self.connection.execute(sql, tuple(params))]

    connection = sqlite3.connect(tmp_path / 'schema-v22.db')
    connection.row_factory = sqlite3.Row
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, meta_value TEXT)')
    connection.execute(
        'INSERT INTO storage_meta VALUES (?, ?)', ('schema_version', '22'))
    connection.execute('''
        CREATE TABLE storage_attempt_events (
            attempt_id TEXT NOT NULL, sequence INTEGER NOT NULL,
            conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            projection_revision INTEGER NOT NULL, type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL,
            PRIMARY KEY(attempt_id, sequence)
        )
    ''')
    historical_payload = json.dumps({'payload': 'legacy-fat-frame'})
    connection.execute(
        'INSERT INTO storage_attempt_events VALUES (?,?,?,?,?,?,?,?)',
        ('attempt', 1, 'conversation', 'turn', 1, 'projection_updated',
         historical_payload, 1),
    )

    initialize_schema(MigrationSession(connection))

    row = connection.execute(
        'SELECT payload_json, payload_bytes FROM storage_attempt_events').fetchone()
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    connection.commit()
    connection.close()
    assert row['payload_json'] == historical_payload
    assert row['payload_bytes'] == 0
    assert int(version) == SCHEMA_VERSION
    with sqlite3.connect(tmp_path / 'schema-v22.db') as migrated:
        tables = {
            row[0] for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        'storage_conversation_trash',
        'storage_conversation_trash_turns',
    } <= tables


def test_terminal_frame_keeps_full_projection(turn_service, storage):
    created, task = _running_attempt(turn_service, storage, 'fat-conv', 'fat-task')
    attempt_id = created['attempt']['attemptId']
    _record_delta(turn_service, task, 'final answer')
    assert turn_service.record_task_event(
        task, {'type': 'done', 'finishReason': 'stop'}) is True

    events = turn_service.read_events(attempt_id, user_id=1)
    terminal = events[-1]
    assert terminal['type'] == 'terminal_settlement'
    assert terminal['payload']['projection']['content'] == 'final answer'
    assert terminal['payload']['settlement']
    assert turn_service.get_attempt(attempt_id, user_id=1)['status'] == 'completed'

    patch_terminal = turn_service.read_events(
        attempt_id, user_id=1, projection_mode='patch')[-1]
    assert 'projection' not in patch_terminal['payload']
    assert patch_terminal['payload']['projectionPatch']['targetRevision'] == (
        patch_terminal['projectionRevision'])


def _prune(storage, *, cutoff_ms, max_attempts=16, max_rows=4096):
    while True:
        sync_result = storage.client.command(
            'turn.sync.prune',
            {'created_before_ms': cutoff_ms, 'max_rows': max_rows},
            None, priority='maintenance', deadline=60,
        )
        if not sync_result['remaining']:
            break
    return storage.client.command(
        'turn.events.prune',
        {'settled_before_ms': cutoff_ms, 'max_attempts': max_attempts,
         'max_rows': max_rows},
        None, priority='maintenance', deadline=60)


def test_prune_deletes_only_old_settled_streams(turn_service, storage):
    import time

    def now_ms():
        return int(time.time() * 1000)

    # A cutoff from BEFORE any settlement makes nothing eligible.
    old_created, old_task = _running_attempt(
        turn_service, storage, 'old-conv', 'old-task', content='old partial')
    old_attempt = old_created['attempt']['attemptId']
    _record_delta(turn_service, old_task, 'old partial')
    past = now_ms() - 1
    assert turn_service.record_task_event(
        old_task, {'type': 'done', 'finishReason': 'stop'}) is True
    assert _prune(storage, cutoff_ms=past)['deleted_rows'] == 0
    assert turn_service.read_events(
        old_attempt, user_id=1), 'premature prune must not delete'

    # Once the cutoff passes the settlement, the stream goes — and the turn
    # authority row is never touched by transport retention.
    result = _prune(storage, cutoff_ms=now_ms() + 1000)
    assert result['deleted_rows'] > 0
    assert turn_service.read_events(old_attempt, user_id=1) == []
    assert turn_service.get_turn(
        'old-conv', old_created['turn']['turnId'], user_id=1)[
        'projection']['content'] == 'old partial'

    # An attempt settled AFTER that prune is retained until the next pass.
    fresh_created, fresh_task = _running_attempt(
        turn_service, storage, 'fresh-conv', 'fresh-task', content='fresh partial')
    fresh_attempt = fresh_created['attempt']['attemptId']
    assert turn_service.record_task_event(
        fresh_task, {'type': 'done', 'finishReason': 'stop'}) is True
    assert turn_service.read_events(fresh_attempt, user_id=1), (
        'no prune pass ran since this settlement')
    assert _prune(storage, cutoff_ms=now_ms() + 1000)['deleted_rows'] > 0
    assert turn_service.read_events(fresh_attempt, user_id=1) == []

    # A live attempt is structurally untouchable across every pass.
    live_created, live_task = _running_attempt(
        turn_service, storage, 'live-conv', 'live-task', content='live partial')
    live_attempt = live_created['attempt']['attemptId']
    _record_delta(turn_service, live_task, 'live partial')
    _prune(storage, cutoff_ms=now_ms() + 1000)
    assert turn_service.read_events(
        live_attempt, user_id=1), 'live attempt stream must survive'
    # Idempotent: with nothing newly eligible, a pass deletes nothing.
    assert _prune(storage, cutoff_ms=now_ms() + 1000)['deleted_rows'] == 0


def test_prune_advances_past_settled_attempts_already_drained(turn_service, storage):
    """A LIMIT window must not pin retention to the oldest empty attempts."""
    import time

    attempt_ids = []
    for index in range(3):
        created, task = _running_attempt(
            turn_service, storage, f'advance-conv-{index}', f'advance-task-{index}')
        attempt_ids.append(created['attempt']['attemptId'])
        assert turn_service.record_task_event(
            task, {'type': 'done', 'finishReason': 'stop'}) is True
        # settled_at is millisecond-granular; make the intended retention
        # order deterministic without reaching into the authority directly.
        time.sleep(0.002)

    cutoff = int(time.time() * 1000) + 1000
    results = [
        _prune(storage, cutoff_ms=cutoff, max_attempts=1)
        for _ in attempt_ids
    ]

    assert all(result['deleted_rows'] > 0 for result in results)
    assert all(turn_service.read_events(attempt_id, user_id=1) == []
               for attempt_id in attempt_ids)
    assert _prune(
        storage, cutoff_ms=cutoff, max_attempts=1)['deleted_rows'] == 0


def test_prune_is_bounded_and_resumable(turn_service, storage):
    created, task = _running_attempt(turn_service, storage, 'bulk-conv', 'bulk-task')
    attempt_id = created['attempt']['attemptId']
    for index in range(40):
        _record_delta(turn_service, task, f'content-{index}')
    assert turn_service.record_task_event(
        task, {'type': 'done', 'finishReason': 'stop'}) is True

    import time
    cutoff = int(time.time() * 1000) + 1000
    total = 0
    passes = 0
    while True:
        result = _prune(storage, cutoff_ms=cutoff, max_rows=7)
        total += result['deleted_rows']
        passes += 1
        if not result['remaining']:
            break
        assert passes < 100, 'prune must make progress every pass'
    assert total >= 41  # 40 slim deltas + the fat terminal frame
    assert turn_service.read_events(attempt_id, user_id=1) == []


def test_reclaim_returns_freelist_pages_on_sqlite(turn_service, storage, tmp_path):
    # Fresh authorities are born auto_vacuum=INCREMENTAL (armed in
    # SQLiteBackend.start before the schema lands) — reclamation exists.
    import sqlite3
    db_path = tmp_path / 'data' / 'tofu.db'
    raw = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    mode = raw.execute('PRAGMA auto_vacuum').fetchone()[0]
    raw.close()
    assert mode == 2, 'fresh authority must be born INCREMENTAL'

    # Build freelist: write then delete ~8 MiB of record payloads.
    blob = 'x' * (256 * 1024)
    for index in range(32):
        storage.client.command('record.put', {
            'namespace': 'reclaim-test', 'key': f'k{index}',
            'value': {'blob': blob}}, f'reclaim-put-{index}')
    for index in range(32):
        storage.client.command('record.delete', {
            'namespace': 'reclaim-test', 'key': f'k{index}'},
            f'reclaim-del-{index}')

    # In WAL mode the physical truncation lands at the next checkpoint; the
    # logical guarantee this op owns is the freelist drain itself.
    last_freelist = None
    reclaimed_total = 0
    for _ in range(200):
        result = storage.client.command(
            'system.reclaim',
            {'max_pages': 512, 'min_free_pages': 1, 'budget_ms': 500},
            None, priority='maintenance', deadline=60)
        reclaimed_total += int(result.get('reclaimed') or 0)
        last_freelist = int(result.get('freelist') or 0)
        if not result.get('reclaimed'):
            break
    assert reclaimed_total > 0, 'incremental_vacuum made no progress'
    assert last_freelist == 0, (
        f'reclaim loop must drain the freelist, got {last_freelist}')


def test_reclaim_skips_non_sqlite_and_non_incremental_backends():
    from lib.storage_sidecar.operations import _system_reclaim

    class _PgSession:
        backend = 'postgres'

    result = _system_reclaim(_PgSession(), {})
    assert result['reclaimed'] == 0
    assert result['backend'] == 'postgres'

    class _SqliteNoneSession:
        backend = 'sqlite'

        def fetch_one(self, sql, params=()):
            assert sql == 'PRAGMA auto_vacuum'
            return {'auto_vacuum': 0}

        def execute(self, sql, params=()):  # pragma: no cover - must not run
            raise AssertionError('vacuum must not run in NONE mode')

    result = _system_reclaim(_SqliteNoneSession(), {})
    assert result['reclaimed'] == 0
    assert result['auto_vacuum'] == 0
