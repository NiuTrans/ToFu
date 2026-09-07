"""Autopilot continuation is one owner-scoped, turn-native transaction."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

import pytest


pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)
USER_ID = 7


@pytest.fixture
def conversation_id(chat_sidecar):
    from tests._seed import delete_conversation

    value = f'autopilot-baton-{uuid.uuid4().hex[:12]}'
    yield value
    delete_conversation(value, user_id=USER_ID)


@pytest.fixture(autouse=True)
def cleanup_test_tasks():
    yield
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks

    with tasks_lock:
        for task_id in [
            task_id for task_id in tasks
            if task_id.startswith('autopilot-baton-task-')
        ]:
            tasks.pop(task_id, None)


def _parent_task(conversation_id: str) -> dict:
    from lib.turn_lifecycle import create_turn_pair

    created = create_turn_pair(
        conversation_id,
        command_id=f'parent:{conversation_id}',
        input_projection={'content': 'ship the feature'},
        config={'model': 'claude-opus', 'assistantMsgId': 'client-parent-id'},
        user_id=USER_ID,
        conversation_defaults={
            'allowCreate': True,
            'title': 'Autopilot baton',
            'settings': {'model': 'claude-opus', 'preset': 'deep'},
        },
    )
    return {
        'id': f'autopilot-baton-task-parent-{uuid.uuid4().hex[:8]}',
        'convId': conversation_id,
        '_userId': USER_ID,
        '_turnId': created['turn']['turnId'],
        '_attemptId': created['attempt']['attemptId'],
        'config': {
            'model': 'claude-opus',
            'preset': 'deep',
            'assistantMsgId': 'client-parent-id',
            'autopilot': True,
        },
    }


def test_append_creates_one_idempotent_virtual_user_successor_pair(
    conversation_id,
):
    from lib.tasks_pkg.autopilot_baton import (
        _append_conversation_autopilot_turns,
    )
    from lib.turn_lifecycle import list_turns

    task = _parent_task(conversation_id)
    first = _append_conversation_autopilot_turns(
        task,
        conversation_id,
        'vu-message-1',
        'continue with the regression test',
        run_id='run-1',
    )
    second = _append_conversation_autopilot_turns(
        task,
        conversation_id,
        'vu-message-1',
        'mutated retry body must not duplicate the turn',
        run_id='run-1',
    )

    assert first is not None and second is not None
    assert second['_turnId'] == first['_turnId']
    snapshot = list_turns(conversation_id, user_id=USER_ID)
    assert [turn['actor'] for turn in snapshot['turns']] == [
        'human', 'assistant', 'virtual_user', 'assistant',
    ]
    virtual_user_turn = snapshot['turns'][2]
    assert virtual_user_turn['projection']['content'] == (
        'continue with the regression test')
    assert '_msgId' not in virtual_user_turn['projection']
    assert virtual_user_turn['projection']['origin']['initiator'] == 'autopilot'
    assert virtual_user_turn['runId'] == 'run-1'
    assert task['_autopilotNextAttempt']['turn']['turnId'] == (
        snapshot['turns'][3]['turnId'])


def test_append_rejects_a_task_with_the_wrong_owner(conversation_id):
    from lib.tasks_pkg.autopilot_baton import (
        _append_conversation_autopilot_turns,
    )
    from lib.turn_lifecycle import list_turns

    task = _parent_task(conversation_id)
    task['_userId'] = USER_ID + 1

    assert _append_conversation_autopilot_turns(
        task, conversation_id, 'vu-wrong-owner', 'should not land') is None
    assert len(list_turns(
        conversation_id, user_id=USER_ID)['turns']) == 2


def test_followup_claims_durable_attempt_and_uses_latest_settings(
    conversation_id,
    monkeypatch,
):
    import lib.tasks_pkg.spawn as task_spawn
    from lib.conversations import set_conversation_settings
    from lib.tasks_pkg.autopilot_baton import (
        _append_conversation_autopilot_turns,
        _start_followup_task,
    )
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks

    task = _parent_task(conversation_id)
    assert _append_conversation_autopilot_turns(
        task,
        conversation_id,
        'vu-message-2',
        'continue',
        run_id='run-2',
    ) is not None
    set_conversation_settings(
        conversation_id,
        {'model': 'kimi-k3', 'preset': 'latest-preset'},
        user_id=USER_ID,
        notify=False,
    )

    spawned: list[dict] = []
    monkeypatch.setattr(
        task_spawn, 'spawn_task', lambda new_task: spawned.append(new_task))
    new_task_id = _start_followup_task(task, conversation_id)

    assert new_task_id
    assert len(spawned) == 1
    with tasks_lock:
        new_task = tasks[new_task_id]
    assert new_task['_userId'] == USER_ID
    assert new_task['_attemptId'] == task['_nextAttemptId']
    assert new_task['_turnId'] == task['_nextTurnId']
    assert new_task['config']['model'] == 'kimi-k3'
    assert new_task['config']['preset'] == 'latest-preset'
    assert 'assistantMsgId' not in new_task['config']
    assert 'msgId' not in new_task['config']


def test_concurrent_followup_replay_spawns_one_successor(
    conversation_id,
    monkeypatch,
):
    import lib.tasks_pkg.spawn as task_spawn
    from lib.tasks_pkg.autopilot_baton import (
        _append_conversation_autopilot_turns,
        _start_followup_task,
    )

    task = _parent_task(conversation_id)
    assert _append_conversation_autopilot_turns(
        task,
        conversation_id,
        'vu-message-concurrent',
        'continue once',
        run_id='run-concurrent',
    ) is not None
    spawned: list[dict] = []
    monkeypatch.setattr(
        task_spawn, 'spawn_task', lambda new_task: spawned.append(new_task))
    start_barrier = threading.Barrier(2)

    def replay() -> str | None:
        start_barrier.wait(timeout=5)
        return _start_followup_task(task, conversation_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: replay(), range(2)))

    assert len([result for result in results if result]) == 1
    assert len(spawned) == 1
