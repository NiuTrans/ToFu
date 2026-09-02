"""One frame = one authority transaction (2026-08-20 double-write root fix).

A live turn frame historically persisted TWICE per push: ``event.append`` into
the cold-replay log (storage_events) and ``turn.event.record`` into the turn
authority — two commands, two writer-queue slots, and no atomicity between
them.  One side could commit while the other timed out, and the retry of the
failed half surfaced as "Event sequence has a conflicting payload".  The
frame's storage_events row now rides INSIDE the turn authority transaction
(``record_task_event(..., task_event=...)`` → return ``'carried'``).  Pinned
here: (a) the carried row commits atomically with the projection, (b) a
conflicting carried row rolls the WHOLE frame back — turn projection
included, fail-closed, and (c) a stale attempt still refuses the authority
write and leaves the standalone append to the caller.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.identity import PrincipalContext
from lib.storage import StorageSupervisor
from lib.storage.errors import StorageError

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
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda write=False: storage.client)
    from lib import turn_lifecycle
    return turn_lifecycle


def _running_task(turn_service, storage, conv_id, task_id):
    created = turn_service.create_turn_pair(
        conv_id, command_id=f'{task_id}-pair',
        input_projection={'content': 'q'}, config={'model': 'gpt-4o'},
        user_id=1,
        conversation_defaults={
            'allowCreate': True, 'title': 'carried', 'settings': {},
        })
    attempt_id = created['attempt']['attemptId']
    turn_service.bind_task(attempt_id, task_id, user_id=1)
    task = {
        '_attemptId': attempt_id, 'id': task_id, 'status': 'running',
        '_userId': 1,
        '_principalContext': PrincipalContext.user(
            subject_id='test-user:1', owner_user_id=1,
        ).to_payload(),
        'content': 'seed', 'thinking': '', 'toolRounds': [],
        'model': 'gpt-4o', 'config': {'model': 'gpt-4o'},
    }
    return created, task


def _task_event(task_id, seq, wire):
    return {'task_id': task_id, 'sequence': seq, 'event': wire}


def _stored_event(storage, task_id, seq):
    events = storage.client.query('event.list', {
        'task_id': task_id, 'after_sequence': seq - 1, 'limit': 1,
    })
    return events[0] if events else None


def test_carried_frame_commits_event_row_and_projection_atomically(
        turn_service, storage):
    created, task = _running_task(turn_service, storage, 'carry-conv', 'carry-task')
    task['content'] = 'streamed text'
    wire = {'type': 'delta', 'content': 'streamed text', 'seq': 0}
    outcome = turn_service.record_task_event(
        task, wire, task_event=_task_event('carry-task', 0, wire))
    assert outcome == 'carried'
    # Both halves landed: the turn authority projection …
    turn = turn_service.get_turn(
        'carry-conv', created['turn']['turnId'], user_id=1)
    assert turn['projection']['content'] == 'streamed text'
    # … and the cold-replay log row, byte-identical to the wire frame.
    stored = _stored_event(storage, 'carry-task', 0)
    assert stored is not None
    assert stored['event']['content'] == 'streamed text'


def test_conflicting_carried_row_rolls_back_the_whole_frame(turn_service, storage):
    created, task = _running_task(turn_service, storage, 'skew-conv', 'skew-task')
    # Pre-seed the replay log with a DIFFERENT payload at the same sequence —
    # the historical skew state the merge must now refuse atomically.
    storage.client.command('event.append', {
        'task_id': 'skew-task', 'sequence': 0,
        'event': {'type': 'delta', 'content': 'OLD bytes', 'seq': 0},
    }, 'skew-seed')
    revision_before = turn_service.get_turn(
        'skew-conv', created['turn']['turnId'], user_id=1,
    )['projectionRevision']

    task['content'] = 'NEW bytes'
    wire = {'type': 'delta', 'content': 'NEW bytes', 'seq': 0}
    with pytest.raises(StorageError) as excinfo:
        turn_service.record_task_event(
            task, wire, task_event=_task_event('skew-task', 0, wire))
    assert excinfo.value.code == 'database_conflict'
    # Fail-closed: the turn projection must NOT have advanced — the whole
    # frame rolled back, so authority and replay log stay in lockstep.
    turn = turn_service.get_turn(
        'skew-conv', created['turn']['turnId'], user_id=1)
    assert turn['projectionRevision'] == revision_before
    assert turn['projection'].get('content') != 'NEW bytes'


def test_stale_attempt_refuses_carrier_and_caller_appends_standalone(
        turn_service, storage):
    created, task = _running_task(turn_service, storage, 'stale-conv', 'stale-task')
    assert turn_service.record_task_event(
        task, {'type': 'done', 'finishReason': 'stop'}) is True
    # The attempt is settled now; a late frame must not mutate authority…
    wire = {'type': 'delta', 'content': 'late', 'seq': 1}
    outcome = turn_service.record_task_event(
        task, wire, task_event=_task_event('stale-task', 1, wire))
    assert outcome is False
    # …and the carried row was NOT written either — the caller's standalone
    # fallback owns that write (pre-merge semantics preserved).
    assert _stored_event(storage, 'stale-task', 1) is None
