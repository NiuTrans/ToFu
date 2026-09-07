"""Lease, retry, ordering, and recovery contracts for queued turns."""

from __future__ import annotations

import time

import pytest

import lib.message_queue as queue
from tests._seed import delete_conversation, seed_conversation


pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit

USER_ID = 1


@pytest.fixture
def conversation_factory(chat_sidecar):
    created: list[str] = []

    def create() -> str:
        conversation_id = f'test-lease-{time.time_ns()}'
        seed_conversation(
            conversation_id, user_id=USER_ID, title='Queue lease test')
        created.append(conversation_id)
        return conversation_id

    yield create

    for conversation_id in created:
        delete_conversation(conversation_id, user_id=USER_ID)


def _storage_client(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


def _stub_dispatch(monkeypatch, *, error: Exception | None = None):
    recorded = {'attempts': [], 'commands': [], 'conversations': []}

    def submit(conversation_id, user_id, command):
        assert user_id == USER_ID
        if error is not None:
            raise error
        attempt_id = f'attempt-{len(recorded["attempts"])}-{time.time_ns()}'
        recorded['attempts'].append(attempt_id)
        recorded['commands'].append(command)
        recorded['conversations'].append(conversation_id)
        return {'attempt': {'attemptId': attempt_id}}

    monkeypatch.setattr(queue, '_submit_queued_turn_command', submit)
    monkeypatch.setattr(
        queue, '_conv_has_live_task',
        lambda conversation_id, *, user_id: False,
    )
    return recorded


def _enqueue(conversation_id: str, text: str) -> str:
    return queue.enqueue_message(
        conversation_id,
        {'text': text, 'timestamp': 1000},
        {'model': 'm'},
        user_id=USER_ID,
    )['queueId']


def test_submit_failure_keeps_intent_and_maintenance_retries_it(
    conversation_factory, monkeypatch,
):
    conversation_id = conversation_factory()
    queue_id = _enqueue(conversation_id, 'hello')
    _stub_dispatch(monkeypatch, error=RuntimeError('submit failed'))

    assert queue.dispatch_next_queued(
        conversation_id, user_id=USER_ID) is None
    assert [item['queueId'] for item in queue.get_queue(
        conversation_id, user_id=USER_ID)] == [queue_id]

    recorded = _stub_dispatch(monkeypatch)
    dispatched = queue.reap_expired_queue_leases()

    assert dispatched == recorded['attempts']
    assert len(dispatched) == 1
    assert queue.get_queue(conversation_id, user_id=USER_ID) == []


def test_expired_lease_is_reclaimed_and_redispatched(
    conversation_factory, monkeypatch,
):
    conversation_id = conversation_factory()
    queue_id = _enqueue(conversation_id, 'orphan')
    leased = _storage_client(write=True).command(
        'queue.dequeue', {
            'conv_id': conversation_id,
            'user_id': USER_ID,
            'now_ms': 1,
            'lease_ms': 1,
        }, None,
    )
    assert leased['queueId'] == queue_id
    recorded = _stub_dispatch(monkeypatch)

    dispatched = queue.reap_expired_queue_leases()

    assert dispatched == recorded['attempts']
    assert len(dispatched) == 1
    assert queue.get_queue(conversation_id, user_id=USER_ID) == []


def test_fresh_lease_is_invisible_until_released(
    conversation_factory, monkeypatch,
):
    conversation_id = conversation_factory()
    queue_id = _enqueue(conversation_id, 'in flight')
    now_ms = int(time.time() * 1000)
    leased = _storage_client(write=True).command(
        'queue.dequeue', {
            'conv_id': conversation_id,
            'user_id': USER_ID,
            'now_ms': now_ms,
            'lease_ms': 60_000,
        }, None,
    )
    assert leased['queueId'] == queue_id

    assert queue.dequeue_next(conversation_id, user_id=USER_ID) is None

    _storage_client(write=True).command(
        'queue.lease.release', {
            'queue_id': queue_id,
            'user_id': USER_ID,
        }, f'test-release:{queue_id}',
    )
    assert queue.dequeue_next(
        conversation_id, user_id=USER_ID)['queueId'] == queue_id


def test_autopilot_marker_is_never_dispatched(
    conversation_factory, monkeypatch,
):
    conversation_id = conversation_factory()
    marker = queue.arm_autopilot_marker(
        conversation_id, {'model': 'm'}, user_id=USER_ID)
    recorded = _stub_dispatch(monkeypatch)

    assert queue.reap_expired_queue_leases(force_reclaim=True) == []
    assert recorded['attempts'] == []
    assert queue.has_autopilot_marker(
        conversation_id, user_id=USER_ID) is True
    assert queue.clear_autopilot_marker(
        conversation_id, user_id=USER_ID) is True
    assert marker['queueId']


def test_messages_dispatch_in_priority_position_order(
    conversation_factory, monkeypatch,
):
    conversation_id = conversation_factory()
    for text in ('first', 'second', 'third'):
        _enqueue(conversation_id, text)
    recorded = _stub_dispatch(monkeypatch)

    attempt_ids = [
        queue.dispatch_next_queued(conversation_id, user_id=USER_ID)
        for _ in range(3)
    ]

    assert all(attempt_ids)
    assert [
        command['inputTurn']['content'] for command in recorded['commands']
    ] == ['first', 'second', 'third']
    assert queue.get_queue(conversation_id, user_id=USER_ID) == []


def test_finalize_renumbers_remaining_positions(
    conversation_factory, monkeypatch,
):
    conversation_id = conversation_factory()
    _enqueue(conversation_id, 'a')
    _enqueue(conversation_id, 'b')
    _stub_dispatch(monkeypatch)

    assert queue.dispatch_next_queued(
        conversation_id, user_id=USER_ID) is not None

    remaining = queue.get_queue(conversation_id, user_id=USER_ID)
    assert [(item['text'], item['position']) for item in remaining] == [('b', 1)]


def test_maintenance_cap_is_oldest_first(
    conversation_factory, monkeypatch,
):
    conversations = [conversation_factory() for _ in range(6)]
    client = _storage_client(write=True)
    for index, conversation_id in enumerate(conversations):
        client.command(
            'queue.enqueue', {
                'conv_id': conversation_id,
                'user_id': USER_ID,
                'queue_id': f'oldest-first-{index}',
                'message': {'text': f'm{index}'},
                'config': {'model': 'm'},
                'kind': queue.KIND_REAL,
                'priority': 10,
                'created_at_ms': 1000 + index,
            }, f'oldest-first-{index}',
        )
    recorded = _stub_dispatch(monkeypatch)

    first_tick = queue.reap_expired_queue_leases()

    assert len(first_tick) == 4
    assert recorded['conversations'] == conversations[:4]

    monkeypatch.setenv('TOFU_QUEUE_REAPER_MAX_DISPATCH_PER_TICK', '2')
    recorded['conversations'].clear()
    second_tick = queue.reap_expired_queue_leases()
    assert len(second_tick) == 2
    assert recorded['conversations'] == conversations[4:]


def test_explicit_cancel_and_disarm_delete_only_their_target(
    conversation_factory,
):
    conversation_id = conversation_factory()
    keep_id = _enqueue(conversation_id, 'keep')
    remove_id = _enqueue(conversation_id, 'cancel')

    assert queue.remove_from_queue(
        conversation_id, remove_id, user_id=USER_ID) is True
    assert [item['queueId'] for item in queue.get_queue(
        conversation_id, user_id=USER_ID)] == [keep_id]

    queue.arm_autopilot_marker(
        conversation_id, {'model': 'm'}, user_id=USER_ID)
    assert queue.clear_autopilot_marker(
        conversation_id, user_id=USER_ID) is True
    assert [item['queueId'] for item in queue.get_queue(
        conversation_id, user_id=USER_ID)] == [keep_id]


def test_goal_continuation_is_deduped_and_kind_clear_preserves_human_intent(
    conversation_factory,
):
    conversation_id = conversation_factory()
    human_id = _enqueue(conversation_id, 'keep this human turn')
    first = queue.enqueue_message(
        conversation_id,
        {'text': 'continue', '_goalContinuation': True},
        {'autopilot': True, '_goalObjective': 'durable objective'},
        kind=queue.KIND_GOAL_CONTINUATION,
        user_id=USER_ID,
    )
    second = queue.enqueue_message(
        conversation_id,
        {'text': 'duplicate continue', '_goalContinuation': True},
        {'autopilot': True, '_goalObjective': 'durable objective'},
        kind=queue.KIND_GOAL_CONTINUATION,
        user_id=USER_ID,
    )

    assert second['deduped'] is True
    assert second['queueId'] == first['queueId']
    assert queue.clear_queue_kind(
        conversation_id,
        queue.KIND_GOAL_CONTINUATION,
        user_id=USER_ID,
    ) == 1
    assert [item['queueId'] for item in queue.get_queue(
        conversation_id, user_id=USER_ID)] == [human_id]


def test_new_human_queue_intent_supersedes_stale_goal_continuation(
    conversation_factory,
):
    conversation_id = conversation_factory()
    queue.enqueue_message(
        conversation_id,
        {'text': 'continue old objective', '_goalContinuation': True},
        {'autopilot': True, '_goalObjective': 'old objective'},
        kind=queue.KIND_GOAL_CONTINUATION,
        user_id=USER_ID,
    )
    workflow = queue.enqueue_message(
        conversation_id,
        {'text': 'preserve workflow'},
        {'model': 'test'},
        kind=queue.KIND_WORKFLOW,
        user_id=USER_ID,
    )

    human = _enqueue(conversation_id, 'new human objective')

    rows = queue.get_queue(conversation_id, user_id=USER_ID)
    assert [row['queueId'] for row in rows] == [human, workflow['queueId']]
    assert [row['kind'] for row in rows] == [queue.KIND_REAL, queue.KIND_WORKFLOW]
    assert sorted(row['position'] for row in rows) == [1, 2]


def test_goal_continuation_dispatch_restores_authoritative_objective(
    conversation_factory, monkeypatch,
):
    conversation_id = conversation_factory()
    queue.enqueue_message(
        conversation_id,
        {
            'text': 'continue',
            '_user_msg': {
                'role': 'user', 'content': 'continue',
                '_goalContinuation': True,
            },
        },
        {'autopilot': True, '_goalObjective': 'durable objective'},
        kind=queue.KIND_GOAL_CONTINUATION,
        user_id=USER_ID,
    )
    seen = []

    def submit(conversation_id, user_id, command, **kwargs):
        seen.append((conversation_id, user_id, command, kwargs))
        return {'attempt': {'attemptId': 'goal-attempt'}}

    monkeypatch.setattr(queue, '_submit_queued_turn_command', submit)
    monkeypatch.setattr(
        queue, '_conv_has_live_task',
        lambda _conversation_id, *, user_id: False,
    )

    assert queue.dispatch_next_queued(
        conversation_id, user_id=USER_ID) == 'goal-attempt'
    assert seen[0][3] == {'trusted_goal_objective': 'durable objective'}
    assert seen[0][2]['inputTurn']['_goalContinuation'] is True
