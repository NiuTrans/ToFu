"""Atomic continuation admission through the real owner-scoped Sidecar."""
from __future__ import annotations

import pytest

from tests.storage.test_storage_authority_sql_contract import storage

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('status', [
    'cancelled', 'aborted', 'interrupted', 'running', 'completed', 'failed',
])
def test_parent_settlement_is_checked_at_creation(storage, monkeypatch, status):  # noqa: F811
    from lib.turn_lifecycle import LifecycleConflict, create_turn_pair

    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda write=False: storage.client)
    conv_id = 'atomic-continuation-parent'
    initial = create_turn_pair(
        conv_id, command_id='initial', input_projection={'content': 'work'},
        config={}, user_id=87,
        conversation_defaults={'allowCreate': True},
    )
    # The producer has already read this ID. Stop changes settlement, not ID.
    parent_id = initial['turn']['turnId']
    if status != 'running':
        settled = storage.client.command('turn.event.record', {
            'attempt_id': initial['attempt']['attemptId'], 'user_id': 87,
            'terminal': True, 'status': status,
            'projection': {'content': 'settled'},
            'settlement': {'outcome': status, 'cause': 'test'},
        }, f'settle-{status}')
        assert settled['applied'] is True

    def create(command_id, guarded=True):
        return create_turn_pair(
            conv_id, command_id=command_id,
            input_projection={'content': 'continue'}, config={}, user_id=87,
            input_actor='virtual_user', parent_turn_id=parent_id,
            require_parent_not_stopped=guarded,
        )

    if status in {'completed', 'failed'}:
        result = create('continuation')
        assert result['submittedTurn']['parentTurnId'] == parent_id
        replay = create('continuation')
        assert replay['idempotentReplay'] is True
        assert replay['turn']['turnId'] == result['turn']['turnId']
    else:
        with pytest.raises(LifecycleConflict) as rejected:
            create('continuation')
        assert rejected.value.code == 'lane_advanced'
        rows = storage.client.query('turn.list', {
            'conversation_id': conv_id, 'user_id': 87, 'lane_id': 'main',
        })
        assert len(rows) == 2
        assert rows[-1]['turnId'] == parent_id
        # Opt-in only: orchestration callers without this fence keep semantics.
        assert create('unguarded', guarded=False)['turn']['turnId'] != parent_id


@pytest.mark.parametrize('guarded', [False, True])
def test_scheduler_forwards_parent_fence(monkeypatch, guarded):
    from types import SimpleNamespace
    from lib.scheduler import conversation_dispatch as dispatch
    from lib.turn_lifecycle import LifecycleConflict

    monkeypatch.setattr(dispatch, 'get_storage_client', lambda: SimpleNamespace(
        query=lambda *args: {'metadata': {'settings': {}}}))
    received = []

    def create_turn(conv_id, owner_id, payload, **kwargs):
        received.append(payload)
        raise LifecycleConflict('lane_advanced', 'Parent stopped')

    monkeypatch.setattr(dispatch, 'conversation_turn_commands', SimpleNamespace(
        create_turn=create_turn))
    options = {'require_parent_not_stopped': True} if guarded else {}
    with pytest.raises(LifecycleConflict) as rejected:
        dispatch.dispatch_scheduled_turn(
            'scheduler-parent-fence', {'content': 'continue'}, {}, user_id=87,
            command_id='guard-forward', parent_turn_id='parent', **options,
        )
    assert rejected.value.code == 'lane_advanced'
    assert received[0]['requireParentNotStopped'] is guarded
    assert received[0]['parentTurnId'] == 'parent'


def test_continuation_guard_rejects_missing_parent(storage, monkeypatch):  # noqa: F811
    from lib.turn_lifecycle import LifecycleConflict, create_turn_pair

    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda write=False: storage.client)
    with pytest.raises(LifecycleConflict) as rejected:
        create_turn_pair(
            'missing-continuation-parent', command_id='missing-parent',
            input_projection={'content': 'continue'}, config={}, user_id=88,
            input_actor='virtual_user', require_parent_not_stopped=True,
            conversation_defaults={'allowCreate': True},
        )
    assert rejected.value.code == 'lane_advanced'
    assert storage.client.query('conversation.get', {
        'conv_id': 'missing-continuation-parent', 'user_id': 88,
    }) is None
