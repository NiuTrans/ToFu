"""Completed execution evidence survives queue reads and snapshot recovery."""
from __future__ import annotations

import pytest

from lib import message_queue
from lib.storage import get_storage_client
from tests._seed import seed_conversation

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


def test_completion_metadata_matches_queue_and_snapshot_without_changing_model_body():
    conv_id = 'background-completion-projection'
    seed_conversation(conv_id, user_id=1, title='Completion')
    framed = '<background-command id="bg_test" status="completed">failure</background-command>'
    message_queue.enqueue_message(
        conv_id,
        {'text': framed, '_user_msg': {'role': 'user', 'content': framed},
         '_backgroundCommand': 'bg_test',
         '_backgroundCommandCompletion': {
             'command': 'pytest', 'resultPreview': '1 failed [exit code: 1]'}},
        {}, kind=message_queue.KIND_WORKFLOW, user_id=1,
    )
    client = get_storage_client()
    rows = client.query('queue.list', {'conv_id': conv_id, 'user_id': 1})
    public = message_queue.get_queue(conv_id, user_id=1)
    snapshot = client.query('turn.sync.snapshot', {
        'conversation_id': conv_id, 'user_id': 1})
    evidence = public[0]['backgroundCommand']
    assert evidence['status'] == 'completed'  # Finished is not exit success.
    assert evidence['commandId'] == 'bg_test'
    assert evidence['resultPreview'] == '1 failed [exit code: 1]'
    assert rows[0]['backgroundCommand'] == evidence
    assert snapshot['queueItems'][0]['backgroundCommand'] == evidence
    assert rows[0]['payload']['text'] == framed
    assert rows[0]['payload']['_user_msg']['content'] == framed
    assert message_queue.get_queue(conv_id, user_id=2) == []


def test_lost_ack_retry_reuses_one_durable_completion_row(monkeypatch):
    from lib.background_command_delivery import publish_completion
    from lib.storage.errors import StorageError
    conv_id = 'background-completion-lost-ack'
    seed_conversation(conv_id, user_id=1, title='Retry')
    client = get_storage_client()
    calls = []
    class LostAckClient:
        def command(self, *args, **kwargs):
            result = client.command(*args, **kwargs)
            calls.append(result)
            if len(calls) == 1:
                raise StorageError('database_timeout', 'ack lost after commit')
            return result
    monkeypatch.setattr(message_queue, '_queue_client', lambda **_: LostAckClient())
    monkeypatch.setattr('lib.background_command_delivery.time.sleep', lambda _: None)
    result = publish_completion(
        message_queue.enqueue_message, conv_id,
        {'text': 'finished', '_backgroundCommand': 'bg_ack', 'timestamp': 42},
        {}, kind=message_queue.KIND_WORKFLOW, user_id=1)
    rows = client.query('queue.list', {'conv_id': conv_id, 'user_id': 1})
    assert len(rows) == 1
    assert result['queueId'] == rows[0]['queueId']
    assert calls[0] == calls[1]


def test_legacy_completion_is_distinct_from_an_ordinary_queued_message():
    from lib.background_command_projection import background_command_completion
    assert background_command_completion({'text': '<background-command>'}) is None
    legacy = background_command_completion({'_backgroundCommand': 'bg_legacy'})
    assert legacy['commandId'] == 'bg_legacy'
    assert legacy['status'] == 'completed'
    assert legacy['resultPreview'] == ''


def test_completion_preview_has_a_fixed_budget():
    from lib.background_command_projection import background_command_completion
    evidence = background_command_completion({
        '_backgroundCommand': 'x' * 300,
        '_backgroundCommandCompletion': {
            'command': 'c' * 5000, 'resultPreview': 'r' * 5000},
    })
    assert len(evidence['commandId']) == 128
    assert len(evidence['command']) == 2000
    assert len(evidence['resultPreview']) == 2000


def test_snapshot_queue_text_is_a_preview_even_for_large_durable_result():
    conv_id = 'background-completion-bounded-snapshot'
    seed_conversation(conv_id, user_id=1, title='Bounded')
    body = 'x' * 100_000
    message_queue.enqueue_message(
        conv_id,
        {'text': body, '_backgroundCommand': 'bg_large',
         '_backgroundCommandCompletion': {'resultPreview': body[:2000],
                                          'resultTruncated': True}},
        {}, kind=message_queue.KIND_BACKGROUND_RECEIPT, user_id=1,
    )
    snapshot = get_storage_client().query('turn.sync.snapshot', {
        'conversation_id': conv_id, 'user_id': 1})
    assert len(snapshot['queueItems'][0]['text']) == 2000
    assert snapshot['queueItems'][0]['backgroundCommand']['commandId'] == 'bg_large'
