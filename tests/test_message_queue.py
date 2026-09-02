"""Queue facade and HTTP contracts over the authoritative Sidecar store."""

from __future__ import annotations

import time

import pytest

from lib.message_queue import KIND_PEER_MSG, enqueue_message
from tests._seed import seed_conversation


pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

USER_ID = 1


def _conversation_id() -> str:
    return f'test-queue-{time.time_ns()}'


def _create_conversation() -> str:
    conversation_id = _conversation_id()
    seed_conversation(conversation_id, user_id=USER_ID, title='Queue test')
    return conversation_id


def _enqueue(conversation_id: str, text: str, timestamp: int) -> dict:
    return enqueue_message(
        conversation_id,
        {'text': text, 'timestamp': timestamp},
        {'model': 'test-model'},
        user_id=USER_ID,
    )


def _enqueue_peer(
    conversation_id: str,
    clean_text: str,
    from_conversation_id: str,
    *,
    human: bool = False,
    timestamp: int = 1000,
) -> dict:
    short_id = from_conversation_id[:8]
    if human:
        framed_text = (
            '[Message from the project operator, relayed via a sibling '
            f'conversation (conv {short_id})]\n\n{clean_text}'
        )
    else:
        framed_text = (
            '[Peer message from a sibling conversation of this project '
            f'(conv {short_id})]\n\n{clean_text}'
        )
    payload = {
        'text': framed_text,
        '_peerMessage': True,
        '_fromConv': from_conversation_id,
        '_peerText': clean_text,
        'timestamp': timestamp,
    }
    if human:
        payload['_peerHuman'] = True
    return enqueue_message(
        conversation_id,
        payload,
        {'model': 'test-model'},
        kind=KIND_PEER_MSG,
        user_id=USER_ID,
    )


class TestMessageQueueAPI:
    def test_get_queue_returns_authoritative_order(self, flask_client):
        conversation_id = _create_conversation()
        _enqueue(conversation_id, 'First', 1000)
        _enqueue(conversation_id, 'Second', 2000)

        response = flask_client.get(f'/api/v1/chat/queue/{conversation_id}')

        assert response.status_code == 200
        items = response.get_json()['items']
        assert [item['text'] for item in items] == ['First', 'Second']
        assert [item['position'] for item in items] == [1, 2]

    def test_remove_from_queue(self, flask_client):
        conversation_id = _create_conversation()
        _enqueue(conversation_id, 'Keep me', 1000)
        removed_id = _enqueue(
            conversation_id, 'Remove me', 2000)['queueId']

        response = flask_client.delete(
            f'/api/v1/chat/queue/{conversation_id}/{removed_id}')

        assert response.status_code == 200
        items = flask_client.get(
            f'/api/v1/chat/queue/{conversation_id}').get_json()['items']
        assert [item['text'] for item in items] == ['Keep me']

    def test_remove_unknown_is_404(self, flask_client):
        conversation_id = _create_conversation()
        response = flask_client.delete(
            f'/api/v1/chat/queue/{conversation_id}/missing-queue-item')
        assert response.status_code == 404

    def test_clear_queue(self, flask_client):
        conversation_id = _create_conversation()
        _enqueue(conversation_id, 'A', 1000)
        _enqueue(conversation_id, 'B', 2000)

        response = flask_client.delete(
            f'/api/v1/chat/queue/{conversation_id}')

        assert response.status_code == 200
        assert response.get_json()['cleared'] == 2
        assert flask_client.get(
            f'/api/v1/chat/queue/{conversation_id}').get_json()['items'] == []

    def test_unknown_conversation_has_no_queue(self, flask_client):
        response = flask_client.get('/api/v1/chat/queue/nonexistent')
        assert response.status_code == 200
        assert response.get_json()['items'] == []


def test_peer_preview_is_clean_complete_and_attributed(flask_client):
    conversation_id = _create_conversation()
    from_conversation_id = 'mradmzmdxyz123'
    message = (
        'Done — I shipped the renderErrorEnvelope recover button, the i18n '
        'keys, and the stamp fix at every site; all tests pass. Take a look '
        'when you get a chance.'
    )
    assert len(message) > 100
    _enqueue_peer(conversation_id, message, from_conversation_id)

    item = flask_client.get(
        f'/api/v1/chat/queue/{conversation_id}').get_json()['items'][0]

    assert item['text'] == message
    assert item['isPeerMessage'] is True
    assert item['fromConv'] == from_conversation_id
    assert item['isPeerHuman'] is False
    assert item['kind'] == KIND_PEER_MSG


def test_operator_peer_preview_is_distinguished(flask_client):
    conversation_id = _create_conversation()
    _enqueue_peer(
        conversation_id,
        'Please pause and re-check the board.',
        'operator-conversation',
        human=True,
    )

    item = flask_client.get(
        f'/api/v1/chat/queue/{conversation_id}').get_json()['items'][0]

    assert item['text'] == 'Please pause and re-check the board.'
    assert item['isPeerMessage'] is True
    assert item['isPeerHuman'] is True


def test_plain_message_has_no_peer_projection(flask_client):
    conversation_id = _create_conversation()
    _enqueue(conversation_id, 'ordinary human message', 1000)

    item = flask_client.get(
        f'/api/v1/chat/queue/{conversation_id}').get_json()['items'][0]

    assert item['text'] == 'ordinary human message'
    assert 'isPeerMessage' not in item
    assert 'fromConv' not in item
    assert 'isPeerHuman' not in item


def test_queue_preview_never_leaks_dispatch_documents(monkeypatch):
    import lib.message_queue as message_queue

    row = {
        'queueId': 'q1',
        'position': 1,
        'kind': 'workflow_step',
        'priority': 50,
        'text': 'pick up epic',
        'hasImages': False,
        'hasPdfs': False,
        'hasRefs': False,
        'hasQuotes': False,
        'timestamp': 1,
        'payload': {'text': 'private dispatch body', 'boardTaskId': 'epic-9'},
        'config': {'model': 'm1'},
    }

    class FakeClient:
        def query(self, operation, payload):
            assert operation == 'queue.list'
            assert payload == {'conv_id': 'preview-conv', 'user_id': USER_ID}
            return [row]

    monkeypatch.setattr(
        message_queue, '_queue_client', lambda **_kwargs: FakeClient())

    preview = message_queue.get_queue(
        'preview-conv', user_id=USER_ID)[0]
    assert set(preview) <= set(message_queue._QUEUE_PREVIEW_KEYS)
    assert 'payload' not in preview
    assert 'config' not in preview
