"""Byte-limited snapshots/pages are contiguous transaction-owned history."""
import uuid

import pytest

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]
pytest_plugins = ('tests._chat_sidecar',)


def test_byte_window_drains_exact_history_including_one_oversized_turn(flask_client, chat_sidecar, monkeypatch):
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService
    from lib.storage import get_storage_client
    from routes import conversation_sync_v3 as routes
    client = get_storage_client(write=True)
    service = ConversationSyncService(SidecarConversationSyncRepository(client_factory=lambda write=False: client))
    monkeypatch.setattr(routes, '_service', service)
    cid = 'byte-window-' + uuid.uuid4().hex
    client.command('conversation.create', {'conv_id': cid, 'user_id': 1, 'title': 'Byte window',
                   'created_at': 1, 'updated_at': 1, 'settings': {}}, cid + '-create')
    try:
        for index in range(5):
            client.command('turn.append_settled', {'conversation_id': cid, 'user_id': 1,
                'command_id': f'{cid}-append-{index}', 'actor': 'assistant', 'kind': 'fixture',
                'projection': {'content': '界' * (40000 if index == 2 else 14000)},
                'created_at': index + 2}, f'{cid}-append-{index}')
        root = f'/api/v4/conversations/{cid}'
        response = flask_client.get(root + '/sync', query_string={
            'turnWindow': 'tail-96', 'windowLimit': 14, 'windowBytes': 65536,
        })
        assert response.status_code == 200, response.get_json()
        snapshot = response.get_json()
        assert 'snapshot;dur=' in response.headers['Server-Timing']
        assert snapshot['turnWindow']['hasMore']
        assert snapshot['turnWindow']['totalTurns'] == 5
        assert len(snapshot['turns']) == 1
        seen = [turn['ordinal'] for turn in snapshot['turns']]
        before = snapshot['turnWindow']['nextBeforeOrdinal']
        while before is not None:
            response = flask_client.get(root + '/turns/history', query_string={
                'laneId': 'main', 'syncSeq': snapshot['syncSeq'],
                'beforeOrdinal': before, 'limit': 64, 'windowBytes': 65536,
            })
            assert response.status_code == 200, response.get_json()
            page = response.get_json()
            assert page['turns'] and len(page['turns']) == 1
            seen.extend(turn['ordinal'] for turn in page['turns'])
            before = page['nextBeforeOrdinal'] if page['hasMore'] else None
        assert sorted(seen) == list(range(5)) and len(set(seen)) == 5
        legacy = flask_client.get(root + '/sync?turnWindow=tail-96').get_json()
        assert len(legacy['turns']) == 5
        assert service._repository.snapshot(cid, 2, turn_limit=14, max_projection_bytes=65536) is None
        stale = flask_client.get(root + '/turns/history', query_string={
            'laneId': 'main', 'syncSeq': snapshot['syncSeq'] + 1, 'windowBytes': 65536,
        })
        assert stale.status_code == 409
        for query in ('windowBytes=65536', 'turnWindow=full&windowBytes=65536',
                      'turnWindow=tail-96&windowBytes=65536&windowBytes=131072'):
            assert flask_client.get(root + '/sync?' + query).status_code == 400
    finally:
        client.command('conversation.purge', {'conv_id': cid, 'user_id': 1}, cid + '-purge')
