"""Repeated projection CAS races retain exact event ownership until commit."""
from copy import deepcopy

import pytest

from tests.turn_lifecycle.test_task_event_delivery import (
    storage as storage, turn_service as turn_service,
    owner as owner, _task, _discard,
)
from tests.turn_lifecycle.test_turn_event_carried_task_event import _stored_event

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('event_type', ['phase', 'delta', 'done'])
def test_repeated_projection_races_rebase_original_event_before_push(
        storage, turn_service, owner, monkeypatch, event_type):
    from lib.agent_core import push
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager import _events as manager

    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._BASE_DELAY_SECONDS', 0)
    _, task = _task(turn_service, storage, 'repeated-projection-race')
    assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'working'})
    original = storage.client.command
    attempted = []
    external = []
    racing = False
    published = []

    def command(operation, payload, command_id, **kwargs):
        nonlocal racing
        if operation == 'turn.event.record' and not racing:
            attempted.append(deepcopy(payload.get('task_event')))
            if len(external) < 5:
                concurrent = {**task, 'todoState': {'items': [
                    {'id': str(len(external)), 'content': 'external update'}]}}
                concurrent.pop('_turnProjectionState', None)
                concurrent.pop('_turnProjectionStateLock', None)
                racing = True
                try:
                    assert turn_service.record_task_event(
                        concurrent, {'type': 'tool_start', 'toolName': 'todo_write'})
                    external.append(concurrent['todoState'])
                finally:
                    racing = False
        return original(operation, payload, command_id, **kwargs)

    def publish(_channel, _task_id, wire, **kwargs):
        assert _stored_event(storage, task['id'], wire['seq']) is not None
        published.append(wire['seq'])

    monkeypatch.setattr(storage.client, 'command', command)
    monkeypatch.setattr(push, 'push_event', publish)
    task['content'] = 'accepted answer'
    try:
        wire = ({'type': 'phase', 'phase': 'working'} if event_type == 'phase'
                else {'type': 'delta', 'content': 'accepted answer'})
        if event_type == 'done':
            task['status'] = 'done'
            wire = {'type': 'done', 'finishReason': 'stop'}
        manager.append_event(task, wire)
        owner.flush(task['_eventDeliveryLane'], timeout=10, recover=False)
        assert len(external) == 5
        assert len(attempted) == 6
        assert all(carrier == attempted[0] for carrier in attempted)
        assert published == [0]
        turn = turn_service.get_turn(task['convId'], task['_turnId'], user_id=1)
        assert turn['projection']['content'] == 'accepted answer'
        assert turn['projection']['todoState'] == external[-1]
        assert owner.snapshot()['pending_events'] == 0
        assert not task.get('error') and not task.get('aborted')
    finally:
        # A failing pre-fix run still owns its exact head; repair only for teardown.
        monkeypatch.setattr(storage.client, 'command', original)
        event_delivery.flush_task_events(task, timeout=10)
        _discard(task)
