"""Steered turns publish their verified model view; unconsumed carriers never do.

The Turn projection keeps only a display preview of an operator steer, so a
cold rebuild cannot restore what the model consumed. Publication must carry the
consumed steer in the checkpoint, while a carrier the provider never consumed
(salvaged to the queue or redelivered from its durable row) never becomes a
reusable prefix.
"""
from __future__ import annotations

import copy
import uuid

import pytest

from lib import agent_inbox
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.inbox_delivery import UNVERIFIED_INBOX_DELIVERY
from tests.llm.test_llm_round_delivery_acknowledgement import _provider_runtime
from tests.llm.test_llm_round_delivery_acknowledgement import delivery as delivery_fixture
from tests.model_routing.test_model_continuation_store import authority as authority_fixture
from tests.tasks_pkg.test_continuation_publication_order import _task

pytestmark = pytest.mark.unit

authority = authority_fixture
delivery = delivery_fixture

_STEER_TEXT = 'Use the staging database instead of production.'


def _steer_carrier(text=_STEER_TEXT):
    return {'role': 'user', 'content': text, '_contextInjectionId': 'inbox_delivery',
            '_isInboxInject': True, '_containsHumanSteer': True,
            '_contextInjection': {'id': 'inbox_delivery', 'eventId': uuid.uuid4().hex}}


def _steered_task(**extra):
    task = {'convId': 'c', '_turnId': 'a1', '_userId': 1, 'model': 'kimi-k3',
            'content': 'Switched to staging.',
            'config': {'model': 'kimi-k3', '_modelReplayBase': []},
            '_userSteerInjects': [{'blockId': 'injection:user-steer:cmd', 'commandId': 'cmd',
                                   'round': 2, 'count': 1, 'deliveryState': 'delivered',
                                   'previews': [{'text': _STEER_TEXT}]}],
            'messages': [{'role': 'user', 'content': 'migrate the schema'},
                         {'role': 'assistant', 'content': 'Planning the migration.'},
                         _steer_carrier(),
                         {'role': 'assistant', 'content': 'Switched to staging.'}]}
    task.update(extra)
    return task


def test_verified_steer_publishes_with_the_model_view():
    from lib.tasks_pkg.model_replay import prepare_completed_task
    task = _steered_task()
    candidate = prepare_completed_task(task)
    assert task['_modelReplayPublishStatus'] == 'prepared'
    assert candidate['projection']['messages'] == task['messages']
    assert any(message.get('_containsHumanSteer') and _STEER_TEXT in message['content']
               for message in candidate['projection']['messages'])


def test_unverified_inbox_carrier_never_publishes():
    from lib.tasks_pkg.model_replay import prepare_completed_task
    task = _steered_task(**{UNVERIFIED_INBOX_DELIVERY: True})
    task['_userSteerInjects'][0]['deliveryState'] = 'queued'
    assert prepare_completed_task(task) is None
    assert task['_modelReplayPublishStatus'] == 'unverified_inbox_delivery'


def test_round_drain_marks_and_verified_flush_clears_unconsumed_carrier(monkeypatch):
    from lib.tasks_pkg.orchestrator._deferred_inbox_flush import flush_deferred_peer_and_steer
    from lib.tasks_pkg.orchestrator._swarm_inbox import drain_and_inject_inbox
    agent_inbox.reset_for_test()
    events = []
    monkeypatch.setattr('lib.tasks_pkg.orchestrator._deferred_inbox_flush.append_event',
                        lambda _task, event: events.append(event))
    task = {'id': 'steer-task', 'convId': 'steer-conversation', '_userId': 7,
            'config': {}, 'aborted': False, 'toolRounds': []}
    try:
        agent_inbox.enqueue(task['convId'], _STEER_TEXT, priority='next', mode='user-steer',
                            extra={'injectionId': 'cmd', 'blockId': 'injection:user-steer:cmd'})
        messages = [{'role': 'user', 'content': 'migrate the schema'}]
        drain_and_inject_inbox(task=task, messages=messages, round_num=1, tid='steer')
        assert messages[-1]['_containsHumanSteer'] is True
        assert task[UNVERIFIED_INBOX_DELIVERY] is True
        # Nothing new to drain: an existing unconsumed carrier stays marked.
        drain_and_inject_inbox(task=task, messages=messages, round_num=2, tid='steer')
        assert task[UNVERIFIED_INBOX_DELIVERY] is True
        flush_deferred_peer_and_steer(task, round_num=2, tid='steer')
        assert UNVERIFIED_INBOX_DELIVERY not in task
        assert task['_userSteerInjects'][0]['deliveryState'] == 'delivered'
        assert [event['type'] for event in events] == ['user_steer_inject']
    finally:
        agent_inbox.reset_for_test()


@pytest.mark.parametrize('verified', [False, True])
def test_only_verified_provider_return_clears_the_carrier_mark(monkeypatch, delivery, verified):
    from unittest.mock import Mock
    from lib.llm import BadRequestError
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    assert delivery.task[UNVERIFIED_INBOX_DELIVERY] is True
    outcome = ProviderStreamResult(
        message={'role': 'assistant', 'content': 'consumed'},
        compatibility_finish_reason='stop', usage=None,
        state=ProviderStreamState.PROVIDER_FINISHED,
        provider_finish_reason='stop', saw_finish_reason=True,
    ) if verified else BadRequestError('API HTTP 400: invalid request')
    _provider_runtime(monkeypatch, Mock(side_effect=[outcome]))
    assert delivery.call() == ('proceed' if verified else 'break')
    assert (UNVERIFIED_INBOX_DELIVERY in delivery.task) is not verified


def test_consumed_steer_restores_next_turn_and_missed_steer_stays_queued(authority, monkeypatch):
    from lib import storage, turn_lifecycle as lifecycle
    from lib.storage.model_continuation import ModelContinuationStore, settled_witness
    from lib.tasks_pkg.model_replay import restore_prefix
    monkeypatch.setattr(storage, 'get_storage_client', lambda **kwargs: authority.client)
    task = _task()
    conversation = task['convId']
    rows = authority.client.query('turn.list', {'conversation_id': conversation, 'user_id': 1})
    task['model'] = 'gpt-4o'
    task['config']['_modelReplayBase'] = [settled_witness(rows[0])]
    consumed = lifecycle.commit_user_steer(
        conversation, task['_attemptId'], command_id='consumed-steer', text=_STEER_TEXT,
        user_id=1)
    missed_text = 'Also notify the on-call channel.'
    missed = lifecycle.commit_user_steer(
        conversation, task['_attemptId'], command_id='missed-steer', text=missed_text,
        user_id=1)
    task['messages'] = [{'role': 'user', 'content': 'request'},
                        {'role': 'assistant', 'content': 'Planning.'},
                        _steer_carrier(),
                        {'role': 'assistant', 'content': 'answer'}]
    # The second steer arrived after the final provider step: finalize queued it
    # as the next Turn and it never entered the working messages.
    task['_userSteerInjects'] = [
        {'blockId': consumed['blockId'], 'commandId': 'consumed-steer', 'round': 2,
         'count': 1, 'previews': [{'text': _STEER_TEXT}], 'deliveryState': 'delivered'},
        {'blockId': missed['blockId'], 'deliveryState': 'queued', 'queueId': 'queued-next'}]
    assert lifecycle.record_task_event(task, build_event(EventType.DONE, finishReason='stop'))
    assert task['_modelReplayPublishStatus'] == 'stored'
    settled = lifecycle.get_turn(conversation, task['_turnId'], user_id=1)
    states = {item['blockId']: item['deliveryState']
              for item in settled['projection']['_userSteerInjects']}
    assert states == {consumed['blockId']: 'delivered', missed['blockId']: 'queued'}
    checkpoint = ModelContinuationStore(client=authority.client).get(
        user_id=1, conversation_id=conversation, lane_id='main')
    assert checkpoint['turns'][-1] == settled_witness(settled)

    lifecycle.create_turn_pair(
        conversation, command_id=uuid.uuid4().hex,
        input_projection={'content': missed_text}, config={'model': 'gpt-4o'}, user_id=1)
    rows = authority.client.query('turn.list', {'conversation_id': conversation, 'user_id': 1})
    config = {'model': 'gpt-4o'}
    restored, covered = restore_prefix(copy.deepcopy(rows), config,
                                       conversation_id=conversation, user_id=1)
    assert config['_modelReplayStatus'] == 'restored'
    assert covered == 2
    assert restored == task['messages']
    assert sum(_STEER_TEXT in str(message.get('content')) for message in restored) == 1
    # Exactly once: the missed steer arrives only as the queued next Turn.
    assert not any(missed_text in str(message.get('content')) for message in restored)
