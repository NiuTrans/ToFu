"""Real child pipeline and selected authorities recover committed Inspector ACKs."""
from copy import deepcopy
from uuid import uuid4

import pytest

from tests.goals.test_goal_invocation_integration import runtime as runtime, ANSWER
from tests.storage.test_storage_application_contract import application_storage as application_storage
from tests.swarm.test_swarm_invocation import agent_for

pytestmark = pytest.mark.api


@pytest.fixture
def application_storage_owner_id():
    return 771


def test_swarm_committed_snapshot_retry_does_not_repeat_effects(
        runtime, application_storage, monkeypatch):
    from lib.identity import PrincipalContext
    from lib.storage import StorageEventBatcher, StorageError
    from lib import turn_lifecycle
    from lib.tasks_pkg import event_log, snapshot_delta
    from lib.tasks_pkg.manager import create_task, discard_task

    client = application_storage
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._BASE_DELAY_SECONDS', 0)
    projector = snapshot_delta.SnapshotProjector()
    monkeypatch.setattr(snapshot_delta, 'get_projector', lambda: projector)
    monkeypatch.setattr(snapshot_delta, 'forget_projector_task', projector.forget)
    conv = 'evidence-recovery-' + uuid4().hex
    created = turn_lifecycle.create_turn_pair(conv, command_id=conv, user_id=771,
        input_projection={'content': 'Verify synthetic evidence'}, config={},
        conversation_defaults={'allowCreate': True, 'title': 'Evidence recovery', 'settings': {}})
    original_parent = runtime.parent
    parent = create_task(conv, deepcopy(original_parent['messages']),
        {**original_parent['config'], '_turnId': created['turn']['turnId'],
         '_attemptId': created['attempt']['attemptId']},
        principal=PrincipalContext.from_payload(original_parent['_principalContext']),
        supersede=False)
    parent['_tool_env'] = original_parent['_tool_env']
    runtime.parent = parent
    discard_task(original_parent['id'])
    turn_lifecycle.bind_task(parent['_attemptId'], parent['id'], user_id=771)
    command = client.command
    attempts = []
    failed_key = None
    failures_remaining = 2

    def lose_ack(operation, payload, command_id=None, **kwargs):
        nonlocal failed_key, failures_remaining
        result = command(operation, payload, command_id, **kwargs)
        events = payload.get('events', []) if operation == 'event.append_batch' else (
            [payload] if operation == 'event.append' else [])
        for carrier in events:
            event = carrier['event']
            key = (carrier['task_id'], carrier['sequence'])
            compressed = (event.get('prefixLen', 0) > 0
                          or event.get('toolsCount', 0) > 0 and 'tools' not in event)
            if failed_key is None and compressed:
                failed_key = key
            if key == failed_key:
                attempts.append((deepcopy(carrier), len(runtime.calls), len(runtime.tool_calls)))
                if failures_remaining:
                    failures_remaining -= 1
                    raise StorageError('database_timeout', 'committed but acknowledgement lost')
        return result

    monkeypatch.setattr(client, 'command', lose_ack)
    batcher = StorageEventBatcher(client_provider=lambda **kwargs: client,
        on_commit=event_log._invalidate_event_read_caches)
    monkeypatch.setattr(event_log, '_SIDECAR_BATCHER', batcher)
    try:
        result = agent_for(runtime).run()
        assert result.status == 'completed', result.error_message
        assert ANSWER in result.final_answer
        # The exact frozen carrier replays after both lost ACKs. The child's
        # next paid request may overlap that replay (request diagnostics never
        # block it), but no model call or tool effect is repeated.
        carriers = [carrier for carrier, _calls, _tools in attempts]
        assert len(attempts) == 3 and all(carrier == carriers[0] for carrier in carriers)
        first_calls, first_tools = attempts[0][1], attempts[0][2]
        assert all(tools == first_tools for _carrier, _calls, tools in attempts)
        assert all(first_calls <= calls <= first_calls + 1 for _carrier, calls, _tools in attempts)
        assert len(runtime.children) == 1 and len(runtime.calls) == 2
        assert len(runtime.tool_calls) == 1 and result.total_tokens == 140
        child = runtime.children[0]
        assert not child.get('_invocationDeliveryError') and not child['aborted']
        rows = client.query('event.list', {
            'task_id': child['_inspectorTaskId'], 'after': -1, 'limit': 100})
        rebuilt = snapshot_delta.rebuild_snapshots([
            {'type': row['event']['type'], 'payload': row['event']} for row in rows])
        expected = [event for event in runtime.snapshots if event['type'] == 'messages_snapshot']
        assert rebuilt and len(rebuilt) == len(expected)
        assert not any(event.get('degraded') for event in rebuilt)
        assert [event['messages'] for event in rebuilt] == [event['messages'] for event in expected]
        assert [event.get('tools', []) for event in rebuilt] == [event.get('tools', []) for event in expected]
    finally:
        assert batcher.close(timeout=5)
