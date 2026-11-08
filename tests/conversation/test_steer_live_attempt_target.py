"""A steer reaches the attempt that makes its lane busy, never merely the lane tail.

Settled Turns can be appended behind a live reply (a Coordinator's
``report_to_user`` progress report) and queued pairs wait behind it with
queue-bound attempts. Both authorities define the busy attempt as a
pending/running attempt that is not queue-bound; the lane-busy conflict, and
the steer that resolves its target from it, must name that attempt's Turn.
"""
from __future__ import annotations

import time

import pytest

from tests.support.response_bound import BoundedReads
from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
# Native runs when TOFUDB_BIN names a source-matched engine, and skips otherwise.
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]


def _service():
    from lib.conversation_sync.command_service import ConversationTurnCommandService

    def never(*args, **kwargs):
        raise AssertionError('input for a busy lane starts no executor')
    return ConversationTurnCommandService(build_user_message=never, was_aborted_after=lambda *a, **k: False,
                                          start_task=never)


@pytest.fixture
def lane(semantic_authority, monkeypatch):
    from lib import agent_inbox
    from lib.turn_lifecycle import bind_task, create_turn_pair
    from tests.support.chat_tasks import chat_task_registry, register_local_executor
    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    conversation = 'coordinator-steer-live'
    pair = create_turn_pair(conversation, command_id='human-request',
                            input_projection={'content': 'Review the recent work'}, config={},
                            user_id=owner, conversation_defaults={'allowCreate': True})
    bind_task(pair['attempt']['attemptId'], 'coordinator-task', user_id=owner)
    # The bound executor is live in this process, not in the other server process.
    register_local_executor('coordinator-task', conv_id=conversation,
                            attempt_id=pair['attempt']['attemptId'],
                            turn_id=pair['turn']['turnId'], user_id=owner)
    yield owner, conversation, pair['turn']['turnId']
    chat_task_registry.pop('coordinator-task', None)
    agent_inbox.reset_for_test(conversation)


_LARGE_REPORT_TEXT = 'Milestone evidence. ' * 1600


@pytest.fixture
def oversized_lane(lane, semantic_authority, monkeypatch):
    """A busy lane whose whole-history read exceeds the response bound."""
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import append_settled_turn
    owner, conversation, _reply = lane
    for index in range(4):
        append_settled_turn(conversation, command_id=f'large-report-{index}', actor='assistant',
                            user_id=owner, kind='coordinator_report.progress',
                            projection={'content': _LARGE_REPORT_TEXT,
                                        'origin': {'initiator': 'brain'}})
    client = semantic_authority[0]
    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda **kwargs: BoundedReads(client(**kwargs)))
    with pytest.raises(StorageError, match='bounded streamed payload'):
        BoundedReads(client()).query('turn.list', {
            'conversation_id': conversation, 'user_id': owner, 'lane_id': 'main'})
    return lane


def _report(owner, conversation):
    from lib.turn_lifecycle import append_settled_turn
    return append_settled_turn(conversation, command_id='coordinator-report', actor='assistant',
                               user_id=owner, kind='coordinator_report.progress',
                               projection={'content': 'Milestone reached',
                                           'origin': {'initiator': 'brain'}})['turn']


def _command(owner, conversation, command, mode):
    return _service().create_turn(conversation, owner, {
        'commandId': command, 'inputTurn': {'content': 'Answer me first'}, 'config': {},
        'injectMode': mode}, request_started_at=time.time()).value


def _steers_on(owner, conversation, reply):
    from lib.turn_lifecycle import get_turn
    projection = get_turn(conversation, reply, user_id=owner)['projection']
    return [record['commandId'] for record in projection.get('_userSteerInjects') or []]


def test_lane_busy_names_the_live_turn_behind_an_appended_report(lane):
    from lib.turn_lifecycle import LifecycleConflict, create_turn_pair
    owner, conversation, reply = lane
    report = _report(owner, conversation)
    with pytest.raises(LifecycleConflict) as busy:
        create_turn_pair(conversation, command_id='human-second', input_projection={'content': 'Hello?'},
                         config={}, user_id=owner)
    assert busy.value.code == 'lane_busy'
    assert busy.value.turn['turnId'] == reply != report['turnId']


def test_a_steer_reaches_the_live_reply_behind_a_coordinator_report(lane):
    from lib import agent_inbox
    owner, conversation, reply = lane
    _report(owner, conversation)
    result = _command(owner, conversation, 'steer-after-report', 'steer')
    assert result.get('steered') is True and not result.get('steerFallback'), result
    assert _steers_on(owner, conversation, reply) == ['steer-after-report']
    [item] = agent_inbox.drain(conversation, modes=['user-steer'])
    assert item['injectionId'] == 'steer-after-report'


def test_lane_busy_names_the_live_turn_when_the_whole_lane_exceeds_the_response_bound(
        oversized_lane):
    from lib.turn_lifecycle import LifecycleConflict, create_turn_pair
    owner, conversation, reply = oversized_lane
    with pytest.raises(LifecycleConflict) as busy:
        create_turn_pair(conversation, command_id='human-oversized',
                         input_projection={'content': 'Hello?'}, config={}, user_id=owner)
    assert busy.value.code == 'lane_busy'
    assert busy.value.turn['turnId'] == reply


def test_queue_and_steer_deliver_when_the_whole_lane_exceeds_the_response_bound(
        oversized_lane):
    owner, conversation, reply = oversized_lane
    queued = _command(owner, conversation, 'queued-oversized', 'queue')
    assert queued.get('queued') is True, queued
    assert queued['latestTurn']['turnId'] == reply
    result = _command(owner, conversation, 'steer-oversized', 'steer')
    assert result.get('steered') is True and not result.get('steerFallback'), result
    assert _steers_on(owner, conversation, reply) == ['steer-oversized']


def test_a_steer_passes_queued_input_waiting_behind_the_live_reply(lane):
    owner, conversation, reply = lane
    queued = _command(owner, conversation, 'queued-input', 'queue')
    assert queued.get('queued') is True, queued
    result = _command(owner, conversation, 'steer-after-queue', 'steer')
    assert result.get('steered') is True and not result.get('steerFallback'), result
    assert _steers_on(owner, conversation, reply) == ['steer-after-queue']
