"""A steer carries its message's reply quotes to the model as Markdown blockquotes.

A queued turn keeps ``replyQuotes`` on its projection for the message builder.
The steer lane delivers only text, so the quotes ride inside that text, both
in the inbox wakeup the model receives and in the durable injection record.

Run with ``python -m pytest tests/conversation/test_conversation_steer_reply_quotes.py``.
"""

from __future__ import annotations

import uuid

import pytest


pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]
pytest_plugins = ('tests._chat_sidecar',)

CONV = 'conv-steer-reply-quotes'


@pytest.fixture()
def steer_conversation(chat_sidecar):
    from tests._seed import delete_conversation, seed_conversation

    delete_conversation(CONV, user_id=1)
    seed_conversation(CONV, user_id=1, title='Steer reply quotes')
    try:
        yield
    finally:
        from lib import agent_inbox
        from tests.support.chat_tasks import chat_task_registry
        agent_inbox.reset_for_test(CONV)
        for task in list(chat_task_registry.values()):
            if task.get('convId') == CONV:
                chat_task_registry.pop(task['id'])
        delete_conversation(CONV, user_id=1)


def _start_running_turn(flask_client, monkeypatch):
    import lib.conversation_sync.task_start as task_start_runtime
    from tests.support.chat_tasks import register_local_executor

    def fake_start(conv_id, config, **kwargs):
        # generation_attempts.task_id is UNIQUE — every start needs its own.
        task_id = f'steer-quotes-{uuid.uuid4().hex[:8]}'
        # The stub executor is this process's executor of the accepted attempt,
        # so the steer publishes its wakeup into this process's inbox.
        register_local_executor(task_id, conv_id=conv_id, attempt_id=config['_attemptId'],
                                turn_id=config['_turnId'])
        kwargs['on_task_registered'](task_id)
        return task_id, None

    monkeypatch.setattr(task_start_runtime, 'start_conversation_attempt_executor', fake_start)
    ack = flask_client.post(
        f'/api/v4/conversations/{CONV}/turns',
        json={'commandId': f'cmd-first-{uuid.uuid4().hex[:8]}', 'inputTurn': {'content': 'first'},
              'config': {'model': 'gpt-4o'}})
    assert ack.status_code == 200


@pytest.mark.parametrize('quotes, expected', [
    (['Line one\nLine two', 'Second quote'],
     '> Line one\n> Line two\n\n> Second quote\n\nUse this instead'),
    ([], 'Use this instead'),
])
def test_steer_text_carries_reply_quotes(flask_client, steer_conversation, monkeypatch,
                                         quotes, expected):
    from lib import agent_inbox
    from lib.turn_lifecycle import list_turns

    _start_running_turn(flask_client, monkeypatch)
    response = flask_client.post(
        f'/api/v4/conversations/{CONV}/turns',
        json={'commandId': f'cmd-steer-quotes-{uuid.uuid4().hex[:8]}',
              'inputTurn': {'content': 'Use this instead', 'replyQuotes': quotes},
              'config': {'model': 'gpt-4o'}, 'injectMode': 'steer'})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['steered'] is True

    [item] = agent_inbox.drain(CONV, modes=['user-steer'])
    assert item['value'] == expected
    # The original message is unchanged for salvage into a queued turn.
    assert item['_user_msg']['content'] == 'Use this instead'
    assert item['_user_msg'].get('replyQuotes', []) == quotes
    turns = list_turns(CONV, user_id=1)['turns']
    injection = turns[-1]['projection']['_userSteerInjects'][0]
    assert injection['previews'] == [{'text': expected}]
