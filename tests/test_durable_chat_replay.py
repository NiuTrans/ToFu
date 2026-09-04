"""Owner-scoped cold replay for chat tasks after hot-registry eviction."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest

from lib.task_replay import project_bounded_replay_payload
from lib.tasks_pkg.durable_chat_replay import (
    load_durable_chat_replay,
    load_durable_chat_task,
    persisted_chat_task_owner_matches,
)


pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit


def _seed_task(
    *,
    user_id: int = 7,
    status: str = 'done',
    content: str = 'durable answer',
    thinking: str = 'durable reasoning',
    error=None,
    event_sequences=(3, 9, 12),
):
    from lib.storage import get_storage_client

    client = get_storage_client(write=True)
    task_id = f'cold-chat-{uuid.uuid4().hex}'
    now_ms = int(time.time() * 1000)
    metadata = {
        'finishReason': 'stop',
        'model': 'model-cold',
        'requestId': 'request-cold',
        'toolOrchestrationDecisions': [{'round': 1, 'selected': ['tools']}],
        'flowTrace': [{'large': 'dedicated-route-only'}],
        'userId': str(user_id),
        'turnId': 'private-turn',
        'affinityKey': 'private-affinity',
    }
    value = {
        'task_id': task_id,
        'conv_id': 'conv-cold',
        'user_id': user_id,
        'content': content,
        'thinking': thinking,
        'error': error,
        'status': status,
        'tool_rounds': None,
        'metadata': json.dumps(metadata),
        'segments': None,
        'created_at': now_ms - 5_000,
        'completed_at': now_ms,
    }
    client.command(
        'task_results.checkpoint', {
            'key': task_id,
            'value': value,
            'expected_version': 0,
        },
        None,
    )
    if event_sequences:
        events = []
        for index, sequence in enumerate(event_sequences):
            event_type = 'done' if index == len(event_sequences) - 1 else 'phase'
            events.append({
                'task_id': task_id,
                'sequence': sequence,
                'event': {'type': event_type, 'detail': f'event-{sequence}'},
            })
        client.command(
            'event.append_batch', {'events': events}, None, priority='event')
    return task_id, now_ms


def _tasks_app(monkeypatch):
    from quart import Quart, g, request

    import routes.api_v1.tasks as tasks_mod
    from lib.api_keys import local_admin_context

    app = Quart(__name__)
    app.config['TESTING'] = True

    @app.before_request
    async def _grant():
        context = local_admin_context()
        context.owner_user_id = int(request.headers.get('X-Test-User', '1'))
        g.auth_ctx = context
        g.rate_decision = None

    monkeypatch.setattr(tasks_mod, '_registries', lambda: {})
    app.register_blueprint(tasks_mod.api_v1_tasks_bp)
    return app


def test_cold_task_state_is_owner_scoped_and_uses_exact_event_bounds(
    chat_sidecar,
):
    task_id, now_ms = _seed_task()

    assert load_durable_chat_task(task_id, user_id=8) is None
    snapshot = load_durable_chat_task(task_id, user_id=7)
    assert snapshot is not None
    assert snapshot.event_replay == {
        'retained_count': 3,
        'base_cursor': 3,
        'next_cursor': 13,
    }
    public = snapshot.public_task()
    assert public['id'] == task_id
    assert public['kind'] == 'chat'
    assert public['status'] == 'done'
    assert public['content'] == 'durable answer'
    assert public['thinking'] == 'durable reasoning'
    assert public['finished_at'] == now_ms / 1000
    assert public['model'] == 'model-cold'
    assert public['toolOrchestrationDecisions'][0]['round'] == 1
    assert 'flowTrace' not in public
    assert 'userId' not in public
    assert 'turnId' not in public
    assert 'affinityKey' not in public


def test_cold_replay_clamps_stale_and_future_cursors_and_enriches_terminal(
    chat_sidecar,
):
    task_id, _now_ms = _seed_task()
    snapshot = load_durable_chat_task(task_id, user_id=7)
    assert snapshot is not None

    replay = snapshot.replay_payload(0)
    assert [event['seq'] for event in replay['events']] == [3, 9, 12]
    assert replay['next_cursor'] == 13
    assert replay['cursor'] == {'requested': 0, 'next': 13, 'reset': True}
    assert replay['done'] is True
    assert replay['content'] == 'durable answer'
    assert replay['thinking'] == 'durable reasoning'
    assert replay['events'][-1]['content'] == 'durable answer'
    assert replay['events'][-1]['thinking'] == 'durable reasoning'

    first = project_bounded_replay_payload(
        replay, max_events=1, max_event_bytes=100_000)
    assert [event['seq'] for event in first['events']] == [3]
    assert first['next_cursor'] == 4
    assert first['done'] is False
    assert 'content' not in first
    assert 'thinking' not in first

    future = snapshot.replay_payload(999)
    assert future['events'] == []
    assert future['next_cursor'] == 13
    assert future['cursor'] == {
        'requested': 999,
        'next': 13,
        'reset': True,
    }


def test_compact_replay_snapshot_loads_terminal_payload_only_when_requested(
    chat_sidecar,
):
    task_id, _now_ms = _seed_task()

    compact = load_durable_chat_replay(task_id, user_id=7)
    assert compact is not None
    assert compact.terminal_payload_loaded is False
    assert compact.content == ''
    assert compact.thinking == ''
    first = project_bounded_replay_payload(
        compact.replay_payload(0), max_events=1, max_event_bytes=100_000)
    assert first['caught_up'] is False
    assert 'content' not in first

    full = compact.with_terminal_payload()
    assert full.terminal_payload_loaded is True
    assert full.content == 'durable answer'
    assert full.thinking == 'durable reasoning'
    enriched = full.enrich_terminal_payload(
        project_bounded_replay_payload(full.replay_payload(13)))
    assert enriched['content'] == 'durable answer'
    assert enriched['thinking'] == 'durable reasoning'


def test_interrupted_cold_task_without_events_still_has_terminal_snapshot(
    chat_sidecar,
):
    task_id, _now_ms = _seed_task(
        status='interrupted', event_sequences=(), thinking='')

    snapshot = load_durable_chat_task(task_id, user_id=7)
    assert snapshot is not None
    assert snapshot.terminal is True
    replay = project_bounded_replay_payload(snapshot.replay_payload(0))
    assert replay['events'] == []
    assert replay['status'] == 'interrupted'
    assert replay['done'] is True
    assert replay['caught_up'] is True
    assert replay['content'] == 'durable answer'


def test_swarm_child_access_reuses_positive_parent_owner(chat_sidecar):
    task_id, _now_ms = _seed_task(user_id=11)

    assert persisted_chat_task_owner_matches(
        f'{task_id}#agent:research', user_id=11)
    assert not persisted_chat_task_owner_matches(
        f'{task_id}#agent:research', user_id=12)


def test_generic_task_api_replays_after_hot_registry_eviction(
    chat_sidecar,
    monkeypatch,
):
    task_id, _now_ms = _seed_task(user_id=1, event_sequences=range(130))
    app = _tasks_app(monkeypatch)

    async def exercise():
        client = app.test_client()
        state_response = await client.get(f'/api/v1/tasks/{task_id}')
        first_response = await client.get(
            f'/api/v1/tasks/{task_id}/events?cursor=0')
        first = await first_response.get_json()
        assert first_response.status_code == 200, first
        final_response = await client.get(
            f'/api/v1/tasks/{task_id}/events'
            f'?cursor={first["next_cursor"]}')
        foreign_state = await client.get(
            f'/api/v1/tasks/{task_id}', headers={'X-Test-User': '2'})
        foreign_events = await client.get(
            f'/api/v1/tasks/{task_id}/events',
            headers={'X-Test-User': '2'},
        )
        return (
            state_response,
            await state_response.get_json(),
            first_response,
            first,
            final_response,
            await final_response.get_json(),
            foreign_state,
            foreign_events,
        )

    (
        state_response,
        state,
        first_response,
        first,
        final_response,
        final,
        foreign_state,
        foreign_events,
    ) = asyncio.run(exercise())
    assert state_response.status_code == 200
    assert state['status'] == 'done'
    assert state['content'] == 'durable answer'
    assert state['event_replay'] == {
        'retained_count': 130,
        'base_cursor': 0,
        'next_cursor': 130,
    }
    assert first_response.status_code == 200
    assert len(first['events']) == 128
    assert first['next_cursor'] == 128
    assert first['caught_up'] is False
    assert first['done'] is False
    assert 'content' not in first
    assert 'thinking' not in first
    assert final_response.status_code == 200
    assert [event['seq'] for event in final['events']] == [128, 129]
    assert final['next_cursor'] == 130
    assert final['caught_up'] is True
    assert final['done'] is True
    assert final['content'] == 'durable answer'
    assert final['thinking'] == 'durable reasoning'
    assert foreign_state.status_code == 404
    assert foreign_events.status_code == 404


def test_generic_task_sse_uses_durable_sparse_ids_and_terminal_content(
    chat_sidecar,
    monkeypatch,
):
    task_id, _now_ms = _seed_task(
        user_id=1, event_sequences=(3, 9, 12))
    app = _tasks_app(monkeypatch)

    async def exercise():
        response = await app.test_client().get(
            f'/api/v1/tasks/{task_id}/stream?cursor=0')
        return response, (await response.get_data()).decode('utf-8')

    response, body = asyncio.run(exercise())
    assert response.status_code == 200
    assert response.content_type.startswith('text/event-stream')
    assert [line for line in body.splitlines() if line.startswith('id: ')] == [
        'id: 3', 'id: 9', 'id: 12',
    ]
    assert 'durable answer' in body
    assert 'durable reasoning' in body


def test_large_cold_terminal_event_occupies_its_own_http_page(
    chat_sidecar,
    monkeypatch,
):
    large_content = '界' * 500_000
    task_id, _now_ms = _seed_task(
        user_id=1,
        content=large_content,
        thinking='',
        event_sequences=(0, 1),
    )
    app = _tasks_app(monkeypatch)

    async def exercise():
        client = app.test_client()
        first_response = await client.get(
            f'/api/v1/tasks/{task_id}/events?cursor=0')
        first = await first_response.get_json()
        final_response = await client.get(
            f'/api/v1/tasks/{task_id}/events'
            f'?cursor={first["next_cursor"]}')
        return (
            first_response,
            first,
            final_response,
            await final_response.get_json(),
        )

    first_response, first, final_response, final = asyncio.run(exercise())
    assert first_response.status_code == 200
    assert [event['seq'] for event in first['events']] == [0]
    assert first['next_cursor'] == 1
    assert first['caught_up'] is False
    assert first['done'] is False
    assert 'content' not in first
    assert final_response.status_code == 200
    assert [event['seq'] for event in final['events']] == [1]
    assert final['events'][0]['content'] == large_content
    assert final['content'] == large_content
    assert final['caught_up'] is True
    assert final['done'] is True
