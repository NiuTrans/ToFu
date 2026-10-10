"""The browser's v3 cancel command owns pending background result delivery."""
from __future__ import annotations

import uuid

import pytest

from lib import message_queue
from lib.storage import get_storage_client
from tests._seed import seed_conversation

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open'),
              pytest.mark.usefixtures('chat_sidecar')]


def _completion(conv_id):
    return message_queue.enqueue_message(
        conv_id, {'text': 'finished', '_backgroundCommand': 'bg_cancel'},
        {}, kind=message_queue.KIND_WORKFLOW, user_id=1)['queueId']


def test_v3_cancel_completion_removes_queue_and_publishes_replay(flask_client, monkeypatch):
    conv_id = 'cancel-bg-' + uuid.uuid4().hex
    seed_conversation(conv_id, user_id=1, title='Cancel')
    queue_id = _completion(conv_id)
    other = message_queue.enqueue_message(
        conv_id, {'text': 'keep'}, {}, user_id=1)['queueId']
    consumed = []
    monkeypatch.setattr('lib.agent_inbox.consume_peer',
                        lambda conv, ids: consumed.append((conv, ids)))
    client = get_storage_client()
    before = client.query('turn.sync.snapshot', {'conversation_id': conv_id, 'user_id': 1})
    response = flask_client.delete(f'/api/v4/conversations/{conv_id}/queue/{queue_id}')
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert result['cancelled'] is True
    assert result['inputTurn'] is None
    assert result['deletedTurnIds'] == []
    assert consumed == [(conv_id, [queue_id])]
    after = client.query('turn.sync.snapshot', {'conversation_id': conv_id, 'user_id': 1})
    assert after['turns'] == before['turns']
    assert [row['queueId'] for row in after['queueItems']] == [other]
    rows = message_queue.get_queue(conv_id, user_id=1)
    assert rows[0]['position'] == 1
    changes = client.query('turn.sync.changes', {
        'conversation_id': conv_id, 'user_id': 1, 'after': 0})
    assert any(event['payload'].get('removedQueueIds') == [queue_id]
               for event in changes['events'])
    retry = flask_client.delete(f'/api/v4/conversations/{conv_id}/queue/{queue_id}')
    assert retry.status_code == 200
    assert retry.get_json()['cancelled'] is False
    assert consumed == [(conv_id, [queue_id])]


def test_completion_cancel_owner_and_dispatch_fences(flask_client, monkeypatch):
    from lib.turn_lifecycle import LifecycleNotFound, cancel_queued_turn_pair
    conv_id = 'cancel-bg-fence-' + uuid.uuid4().hex
    seed_conversation(conv_id, user_id=1, title='Fence')
    queue_id = _completion(conv_id)
    consumed = []
    monkeypatch.setattr('lib.agent_inbox.consume_peer', lambda *args: consumed.append(args))
    with pytest.raises(LifecycleNotFound):
        cancel_queued_turn_pair(conv_id, queue_id, user_id=2)
    import time
    client = get_storage_client(write=True)
    client.command('queue.lease.bind', {
        'queue_id': queue_id, 'user_id': 1,
        'task_id': 'existing-dispatch',
        'now_ms': int(time.time() * 1000), 'lease_ms': 60000,
    }, 'lease-' + queue_id)
    response = flask_client.delete(f'/api/v4/conversations/{conv_id}/queue/{queue_id}')
    assert response.status_code == 409, response.get_json()
    assert message_queue.get_queue(conv_id, user_id=1)[0]['queueId'] == queue_id
    assert consumed == []


@pytest.mark.parametrize('publish_before_cancel', [False, True])
def test_cancel_fences_background_inbox_twin(flask_client, publish_before_cancel):
    from lib import agent_inbox
    conv_id = 'cancel-bg-inbox-' + uuid.uuid4().hex
    seed_conversation(conv_id, user_id=1, title='Inbox')
    queue_id = _completion(conv_id)
    try:
        assert agent_inbox.enqueue(conv_id, 'unrelated', mode='background-command',
                                   extra={'queueId': 'keep'})
        if publish_before_cancel:
            assert agent_inbox.enqueue(conv_id, 'finished', mode='background-command',
                                       extra={'queueId': queue_id})
        response = flask_client.delete(f'/api/v4/conversations/{conv_id}/queue/{queue_id}')
        assert response.status_code == 200
        assert response.get_json()['cancelled'] is True
        assert not agent_inbox.enqueue(conv_id, 'late twin', mode='background-command',
                                       extra={'queueId': queue_id})
        assert agent_inbox.peek(conv_id) == 1
    finally:
        agent_inbox.clear(conv_id)
