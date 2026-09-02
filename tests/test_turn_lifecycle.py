"""Domain-facade contracts for turn-native conversations.

Transaction semantics are exercised against the real authority in
``test_storage_sidecar_contract.py``. This suite pins the public facade and
its pure projection/settlement behavior without constructing a second store.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)


def _new_conversation_id(prefix='turn-facade'):
    return f'{prefix}-{uuid.uuid4().hex[:10]}'


def _create(conversation_id, *, command_id='create', user_id=1):
    from lib.turn_lifecycle import create_turn_pair
    return create_turn_pair(
        conversation_id,
        command_id=f'{command_id}-{conversation_id}',
        input_projection={'content': 'hello'},
        config={'model': 'gpt-4o'},
        user_id=user_id,
        conversation_defaults={
            'allowCreate': True,
            'title': 'Turn facade',
            'settings': {'model': 'gpt-4o'},
        },
    )



def test_facade_rejects_missing_owner_before_storage_access(monkeypatch):
    import lib.turn_lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle,
        '_turn_client',
        lambda **_kwargs: pytest.fail('invalid owner reached storage'),
    )

    with pytest.raises(ValueError, match='numeric user_id'):
        lifecycle.get_attempt('attempt', user_id=None)
    with pytest.raises(ValueError, match='positive user_id'):
        lifecycle.list_turns('conversation', user_id=0)
    with pytest.raises(ValueError, match='numeric user_id'):
        lifecycle.read_events('attempt', user_id='not-an-owner')


def test_turn_pair_is_owner_scoped_atomic_and_idempotent(chat_sidecar):
    from lib.turn_lifecycle import (
        LifecycleNotFound,
        claim_attempt_start,
        create_turn_pair,
        get_turn,
        list_turns,
        read_events,
    )

    conversation_id = _new_conversation_id()
    first = _create(conversation_id, user_id=7)
    second = create_turn_pair(
        conversation_id,
        command_id=f'create-{conversation_id}',
        input_projection={'content': 'mutated retry body'},
        config={'model': 'different'},
        user_id=7,
    )

    assert second['idempotentReplay'] is True
    assert second['turn']['turnId'] == first['turn']['turnId']
    assert second['attempt']['attemptId'] == first['attempt']['attemptId']
    snapshot = list_turns(conversation_id, user_id=7)
    assert [(turn['actor'], turn['ordinal']) for turn in snapshot['turns']] == [
        ('human', 0), ('assistant', 1),
    ]
    events = read_events(first['attempt']['attemptId'], user_id=7)
    assert events[0]['turnId'] == first['turn']['turnId']
    assert claim_attempt_start(
        first['attempt']['attemptId'], user_id=7) is True
    assert claim_attempt_start(
        first['attempt']['attemptId'], user_id=7) is False
    with pytest.raises(LifecycleNotFound):
        get_turn(
            conversation_id, first['turn']['turnId'], user_id=8)


def test_terminal_projection_and_attempt_cas_share_one_turn(chat_sidecar):
    from lib.turn_lifecycle import (
        LifecycleConflict,
        bind_task,
        create_attempt,
        get_turn,
        record_task_event,
    )

    conversation_id = _new_conversation_id()
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'facade-task', user_id=1)
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        'id': 'facade-task',
        'status': 'done',
        'finishReason': 'stop',
        'content': 'final answer',
        'thinking': '',
        'toolRounds': [],
        'segments': [],
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(
        task, {'type': 'done', 'finishReason': 'stop'}) is True
    settled = get_turn(conversation_id, turn_id, user_id=1)
    assert settled['status'] == 'completed'
    assert settled['projection']['content'] == 'final answer'

    with pytest.raises(LifecycleConflict) as stale:
        create_attempt(
            conversation_id, turn_id,
            command_id=f'stale-{conversation_id}',
            operation='regenerate',
            expected_projection_revision=settled['projectionRevision'] - 1,
            user_id=1,
        )
    assert stale.value.code == 'stale_projection'

    regenerated = create_attempt(
        conversation_id, turn_id,
        command_id=f'regenerate-{conversation_id}',
        operation='regenerate',
        expected_projection_revision=settled['projectionRevision'],
        user_id=1,
    )
    assert regenerated['turn']['turnId'] == turn_id
    assert regenerated['attempt']['attemptId'] != attempt_id

    task.update(status='running', content='stale overwrite')
    assert record_task_event(
        task, {'type': 'delta', 'content': 'stale overwrite'}) is False
    assert get_turn(conversation_id, turn_id, user_id=1)['projection']['content'] == ''


@pytest.mark.parametrize(
    ('task_error', 'raw_event', 'expected_message'),
    [
        ('provider socket closed', {'type': 'error'}, 'provider socket closed'),
        (None, {'type': 'error', 'error': {
            'message': 'executor exited before reply',
            'kind': 'executor_failure',
        }}, 'executor exited before reply'),
    ],
)
def test_failed_settlement_always_has_actionable_error(
    task_error, raw_event, expected_message,
):
    from lib.turn_lifecycle import _settlement

    task = {
        'status': 'error',
        'error': task_error,
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }
    status, settlement = _settlement(
        task, raw_event, {'content': 'partial', 'toolRounds': []})
    assert status == 'failed'
    error = settlement['error']
    assert error['kind']
    assert expected_message in (error['detail'] or error['raw'])
    assert isinstance(error['retryable'], bool)
    assert error['hint']


@pytest.mark.parametrize('finish_reason', ['premature_close', 'abnormal_stop'])
def test_provider_stream_failure_never_settles_as_completed(finish_reason):
    """Missing terminal stream frames are provider failures even when the
    executor preserved prose and emitted a nominal ``done`` event."""
    from lib.turn_lifecycle import _settlement

    task = {
        'status': 'done',
        'finishReason': finish_reason,
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }
    status, settlement = _settlement(
        task,
        {'type': 'done', 'finishReason': finish_reason},
        {'content': 'preserved partial', 'toolRounds': []},
    )

    assert status == 'failed'
    assert settlement['outcome'] == 'failed'
    assert settlement['cause'] == 'provider_stream_error'
    assert settlement['providerFinishReason'] == finish_reason
    assert settlement['error']['kind'] == finish_reason
    assert any(option['operation'] == 'continue'
               for option in settlement['resumeOptions'])


def test_malformed_stream_prefix_and_verdict_commit_together(chat_sidecar):
    """The authority persists the exact parsed prefix and the failed stream
    verdict atomically; storage must neither truncate text nor relabel it as a
    completed provider response."""
    from lib.turn_lifecycle import bind_task, get_turn, record_task_event

    conversation_id = _new_conversation_id('malformed-stream')
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'malformed-stream-task', user_id=1)
    prefix = 'The diagnosis is solid. Let me pull the exact pending question'
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        'id': 'malformed-stream-task',
        'status': 'done',
        'finishReason': 'premature_close',
        'streamState': 'malformed_stream',
        'content': prefix,
        'thinking': '',
        'toolRounds': [],
        'segments': [],
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }

    assert record_task_event(task, {
        'type': 'done',
        'finishReason': 'premature_close',
        'streamState': 'malformed_stream',
    }) is True

    turn = get_turn(conversation_id, turn_id, user_id=1)
    assert turn['status'] == 'failed'
    assert turn['projection']['content'] == prefix
    assert turn['settlement']['streamState'] == 'malformed_stream'
    assert turn['settlement']['evidence'] == 'provider_stream_failure'
    assert turn['settlement']['error']['kind'] == 'premature_close'


def test_projection_sanitizes_transport_only_round_fields():
    from lib.turn_lifecycle import _task_projection

    projected = _task_projection({
        "id": "task-a",
        'content': 'answer',
        'thinking': '',
        'toolRounds': [{'toolName': 'read_files', 'status': 'done'}],
        'apiRounds': [{
            'model': 'gpt-4o',
            'usage': {
                'input_tokens': 10,
                '_wire_fp': 'not durable',
                '_wire_bytes': {'huge': 'not durable'},
            },
        }],
        'config': {},
    }, {})
    assert projected['content'] == 'answer'
    assert projected['toolRounds'][0]['toolName'] == 'read_files'
    usage = projected['apiRounds'][0]['usage']
    assert usage['input_tokens'] == 10
    assert not any(key.startswith('_wire_') for key in usage)


def test_projection_exposes_file_changes_as_one_stable_content_block():
    from lib.turn_lifecycle import _task_projection

    files = [{"path": "src/app.ts", "action": "modified", "root": "web"}]
    projected = _task_projection({
        "id": "task-a",
        "content": "done",
        "modifiedFiles": 1,
        "modifiedFileList": files,
        "config": {},
    }, {})

    assert projected["fileChanges"] == {
        "blockId": "file-changes",
        "taskId": "task-a",
        "count": 1,
        "state": "applied",
        "files": files,
    }
    assert projected["fileChanges"]["files"] is not files
    assert projected["fileChanges"]["files"][0] is not files[0]

    undone = {**projected, "fileChanges": {
        **projected["fileChanges"], "state": "undone", "commandId": "cmd-a",
    }}
    late = _task_projection({
        "id": "task-a",
        "content": "done",
        "modifiedFiles": 1,
        "modifiedFileList": files,
        "_preferencesLearned": [{"kind": "added"}],
        "config": {},
    }, undone)
    assert late["fileChanges"]["state"] == "undone"
    assert late["fileChanges"]["commandId"] == "cmd-a"


def test_commit_round_file_changes_fold_into_a_settled_turn(chat_sidecar):
    """The async commit round derives the file list AFTER the terminal
    settlement, so the done-time projection lacks it and the authority
    rightly refuses the late ``round_committed`` frame.  The dedicated CAS
    seam must still land the list on the durable projection — otherwise the
    turn-native UI never renders the files-changed card (2026-08-26
    regression)."""
    from lib.turn_lifecycle import (
        apply_commit_round_file_changes,
        bind_task,
        get_turn,
        record_task_event,
    )

    conversation_id = _new_conversation_id('commit-round-fold')
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'commit-round-task', user_id=1)
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        'id': 'commit-round-task',
        'status': 'done',
        'finishReason': 'stop',
        'content': 'final answer',
        'thinking': '',
        'toolRounds': [],
        'segments': [],
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(
        task, {'type': 'done', 'finishReason': 'stop'}) is True
    settled = get_turn(conversation_id, turn_id, user_id=1)
    assert settled['status'] == 'completed'
    assert 'fileChanges' not in settled['projection']

    files = [{'path': 'src/app.ts', 'action': 'written'}]
    # The post-settlement event frame stays refused — only the CAS seam folds.
    assert record_task_event(
        task, {'type': 'round_committed', 'modifiedFileList': files}) is False

    result = apply_commit_round_file_changes(
        conversation_id, turn_id,
        files=files, modified_count=1, task_id='commit-round-task', user_id=1)
    assert result is not None

    folded = get_turn(conversation_id, turn_id, user_id=1)
    assert folded['projectionRevision'] == settled['projectionRevision'] + 1
    assert folded['projection']['content'] == 'final answer'
    assert folded['projection']['modifiedFileList'] == files
    assert folded['projection']['fileChanges'] == {
        'blockId': 'file-changes',
        'taskId': 'commit-round-task',
        'count': 1,
        'state': 'applied',
        'files': files,
    }

    # Idempotent: the identical fold (e.g. a duplicate commit-round retry) is
    # a no-op that does not burn a projection revision.
    assert apply_commit_round_file_changes(
        conversation_id, turn_id,
        files=files, modified_count=1, task_id='commit-round-task',
        user_id=1) is None
    assert (get_turn(conversation_id, turn_id, user_id=1)['projectionRevision']
            == folded['projectionRevision'])


def test_projection_merges_provenance_sidecars_behind_one_stable_block():
    from lib.turn_lifecycle import _task_projection

    learned = [{"kind": "added", "summary": "Prefer focused tests"}]
    first = _task_projection({
        "content": "answer",
        "_memoryPrefetch": {"phase": "done", "selected": 2},
        "_preferencesApplied": {"chars": 30, "items": ["concise"]},
        "config": {},
    }, {})
    second = _task_projection({
        "content": "answer",
        "_preferencesLearned": learned,
        "config": {},
    }, first)

    assert second["provenance"] == {
        "blockId": "provenance",
        "memoryPrefetch": {"phase": "done", "selected": 2},
        "preferencesApplied": {"chars": 30, "items": ["concise"]},
        "preferencesLearned": learned,
    }
    assert second["provenance"]["preferencesLearned"] is not learned
