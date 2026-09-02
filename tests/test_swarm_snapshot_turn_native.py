"""Turn-native durable Swarm snapshot persistence regression coverage."""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]
pytest_plugins = ('tests._chat_sidecar',)


def test_snapshot_updates_the_canonical_turn_projection(
        chat_sidecar, monkeypatch):
    """A turn-native conversation's transcript authority is the turn row.

    Persistence uses ``turn.projection.update`` and the conversation read view
    is derived from that same row.
    """
    from lib.storage import get_storage_client
    from lib.swarm.snapshot import persist_snapshot_to_conversation
    from lib.turn_lifecycle import (
        create_turn_pair, get_turn, record_task_event,
    )

    conv_id = 'swarm-turn-native-snapshot'
    created = create_turn_pair(
        conv_id,
        command_id='swarm-turn-native-create',
        input_projection={'content': 'spawn a swarm'},
        config={},
        user_id=1,
        conversation_defaults={
            'allowCreate': True,
            'title': 'Turn-native swarm',
            'createdAt': 1,
            'settings': {'model': 'test-model'},
        },
    )
    handle = {
        'status': 'async_launched',
        'swarm_id': 'swarm-turn-native',
        'agents': [{
            'id': 'agent-a',
            'role': 'researcher',
            'objective': 'survey',
            'output_file': '/tmp/agent-a.log',
        }],
    }
    assert record_task_event(
        {
            '_attemptId': created['attempt']['attemptId'],
            '_userId': 1,
            'content': 'spawned',
            'thinking': '',
            'toolRounds': [{
                'roundNum': 1,
                'toolName': 'spawn_agents',
                '_swarm': True,
                'status': 'done',
                'toolContent': json.dumps(handle),
            }],
            'segments': [],
            'status': 'done',
        },
        {'type': 'done', 'finishReason': 'stop'},
    ) is True

    import lib.conversations as conversations
    monkeypatch.setattr(
        conversations, 'notify_conv_changed', lambda *args, **kwargs: None)

    snapshot = {
        'agents': [{'id': 'agent-a', 'status': 'done', 'tokens': 7}],
        'settled': True,
        'totalTokens': 7,
        'agentCount': 1,
        'doneCount': 1,
        'version': 100001,
    }
    assert persist_snapshot_to_conversation(
        conv_id, ['agent-a'], snapshot, user_id=1) is True
    turn = get_turn(conv_id, created['turn']['turnId'], user_id=1)
    stamped = turn['projection']['toolRounds'][0]
    assert stamped['_swarm'] is True
    assert stamped['_swarmSnapshot'] == snapshot

    document = get_storage_client().query(
        'conversation.get', {'conv_id': conv_id, 'user_id': 1})
    assert len(document['messages']) == 2
    projected = document['messages'][-1]['toolRounds'][0]
    assert projected['_swarmSnapshot'] == snapshot
