"""Background receipts stay passive until the batch coordinator wakes them."""

from __future__ import annotations

import pytest

from lib import agent_inbox, message_queue
from lib.storage import get_storage_client
from lib.tasks_pkg.orchestrator._turn_prelude import run_turn_prelude
from lib.tasks_pkg.orchestrator._swarm_inbox import drain_and_inject_inbox
from tests._seed import seed_conversation

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


@pytest.fixture(autouse=True)
def _reset_inbox():
    agent_inbox.reset_for_test()
    yield
    agent_inbox.reset_for_test()


def _enqueue_result(conv_id: str, command_id: str, *, legacy: bool = False):
    text = (f'<background-command id="{command_id}" status="completed">'
            f'{command_id} done</background-command>')
    return message_queue.enqueue_message(
        conv_id,
        {'text': text, '_backgroundCommand': command_id,
         '_backgroundCommandCompletion': {'command': command_id,
                                          'resultPreview': 'done'}},
        {}, kind=(message_queue.KIND_WORKFLOW if legacy else
                  message_queue.KIND_BACKGROUND_RECEIPT), user_id=1,
    )['queueId']


def test_completion_only_recovery_does_not_create_model_turn():
    conv_id = 'passive-completion-only'
    seed_conversation(conv_id, user_id=1, title='Background')
    ids = [_enqueue_result(conv_id, f'bg_{i}', legacy=(i == 0))
           for i in range(11)]
    assert message_queue.dispatch_next_queued(conv_id, user_id=1) is None
    assert [row['queueId'] for row in get_storage_client().query(
        'queue.list', {'conv_id': conv_id, 'user_id': 1})] == ids
    assert message_queue.next_dispatchable_turn(conv_id, user_id=1) is None
    preview = get_storage_client().query('queue.list', {
        'conv_id': conv_id, 'user_id': 1, 'mode': 'preview'})
    assert all('payload' not in row for row in preview)
    pending = get_storage_client().query('queue.list', {
        'conv_id': conv_id, 'user_id': 1, 'mode': 'background_pending'})
    assert len(pending) == 11
    assert all(row['payload']['_backgroundCommand'] for row in pending)


def test_new_receipt_only_conversation_is_absent_from_startup_scan():
    conv_id = 'passive-completion-startup'
    seed_conversation(conv_id, user_id=1, title='Background')
    _enqueue_result(conv_id, 'bg_startup')
    assert conv_id not in {
        row['convId'] for row in message_queue.list_orphaned_dispatchable_conversations()
    }
    assert message_queue.redispatch_orphaned_queue_on_startup() == []


def test_next_real_turn_rehydrates_one_deduplicated_receipt_batch():
    conv_id = 'passive-completion-rehydrate'
    seed_conversation(conv_id, user_id=1, title='Background')
    ids = [_enqueue_result(conv_id, f'bg_{i}', legacy=(i == 0))
           for i in range(3)]
    task = {'id': 'receipt-task', 'convId': conv_id, '_userId': 1,
            'aborted': False, 'toolRounds': []}
    run_turn_prelude(task, {}, 'receipt')
    run_turn_prelude(task, {}, 'receipt')
    messages = [{'role': 'user', 'content': 'Review completed tests'}]
    drain_and_inject_inbox(task=task, messages=messages, round_num=0,
                           tid='receipt')
    assert len(messages) == 2
    assert messages[-1]['role'] == 'user'
    assert all(f'bg_{i}' in messages[-1]['content'] for i in range(3))
    assert [item['queueId'] for item in task['_bgcmd_inject_pending']] == ids


def test_background_receipt_does_not_block_real_queued_work():
    conv_id = 'passive-completion-priority'
    seed_conversation(conv_id, user_id=1, title='Background')
    _enqueue_result(conv_id, 'bg_old', legacy=True)
    _enqueue_result(conv_id, 'bg_new')
    queued = message_queue.enqueue_message(
        conv_id, {'text': 'actual request'}, {}, user_id=1)['queueId']
    assert message_queue.next_dispatchable_turn(conv_id, user_id=1)['queueId'] == queued
    leased = message_queue.dequeue_next(conv_id, user_id=1)
    assert leased['queueId'] == queued
