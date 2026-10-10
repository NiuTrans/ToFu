"""Byte-bound receipt pages preserve full Unicode results across deletion."""
import json
import pytest
from lib import message_queue
from lib.storage import get_storage_client
from tests._seed import seed_conversation

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


def test_receipt_pages_continue_after_consumed_rows_are_removed():
    conv_id = 'receipt-page-delete'
    seed_conversation(conv_id, user_id=1)
    text = '汉\\"' * 500
    expected = set()
    for index in range(10):
        expected.add(message_queue.enqueue_message(conv_id,
            {'text': text, '_backgroundCommand': f'bg_{index}', 'timestamp': index + 1},
            {}, kind=message_queue.KIND_BACKGROUND_RECEIPT, user_id=1)['queueId'])
    client = get_storage_client()
    cursor = None
    seen = set()
    pages = 0
    while True:
        page = client.query('queue.list', {'conv_id': conv_id, 'user_id': 1,
            'mode': 'background_page', 'max_bytes': 10_000, 'cursor': cursor})
        assert len(json.dumps(page, ensure_ascii=False, separators=(',', ':')).encode()) <= 10_000
        assert page['items']
        for row in page['items']:
            assert row['payload']['text'] == text
            assert row['queueId'] not in seen
            seen.add(row['queueId'])
        pages += 1
        # Deletion renumbers all queue positions; the cursor must still work.
        client.command('queue.remove', {'conv_id': conv_id, 'user_id': 1,
            'queue_id': page['items'][0]['queueId']}, command_id=f'page-remove-{pages}')
        cursor = page['nextCursor']
        if cursor is None:
            break
        assert pages <= 10
    assert seen == expected
    assert pages > 1
