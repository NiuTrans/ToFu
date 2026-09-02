"""Conversation command send-while-running delivery lanes.

Regression coverage for the 2026-08-20 "insert-position prompt never opens"
report. Under the Turn/Attempt cutover the composer prompt's gate excluded
any conversation with a live turn attempt (``_activeAttemptId``), and the turn-native
submit route had no steer/queue semantics at all — a send during generation
hit a bare ``lane_busy`` 409 instead of the legacy
``lib/chat_dispatch.py`` delivery lanes.

The fixed contract:

  * ``injectMode: 'steer'`` → the message rides the conversation-keyed
    ``agent_inbox`` (``mode='user-steer'``) into the running turn's next
    round boundary; ACK carries ``steered: true``; NO turn pair and NO
    queue row are created.
  * ``injectMode: 'queue'`` → the message lands in the durable
    ``message_queue``; ACK carries ``queued: true`` + position; when the
    occupying attempt settles, the settlement drain turns the row into a
    fresh input+output turn pair and starts its attempt.
  * no ``injectMode`` → the ``lane_busy`` 409 contract is preserved for
    programmatic clients.

Run with ``python -m pytest tests/test_conversation_command_inject_mode.py``.
"""

from __future__ import annotations

import threading

import pytest


pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]
pytest_plugins = ('tests._chat_sidecar',)


@pytest.fixture()
def conversation_command_db(chat_sidecar):
    from tests._seed import delete_conversation, seed_conversation

    delete_conversation('conv-command-inject', user_id=1)
    seed_conversation(
        'conv-command-inject', user_id=1, title='Command injection')
    try:
        yield
    finally:
        from lib import agent_inbox
        agent_inbox.reset_for_test('conv-command-inject')
        delete_conversation('conv-command-inject', user_id=1)


def _start_first_turn(flask_client, monkeypatch, task_id='internal-task-run'):
    """Create turn pair #1 with a live (running) attempt; returns the ACK."""
    import lib.conversation_sync.task_start as task_start_runtime

    starts = []
    issued = []

    def fake_start(conv_id, config, **kwargs):
        starts.append((conv_id, dict(config)))
        # generation_attempts.task_id is UNIQUE — every start needs its own.
        issued.append(task_id)
        issued_task_id = f'{task_id}-{len(issued)}'
        kwargs['on_task_registered'](issued_task_id)
        return issued_task_id, None

    monkeypatch.setattr(task_start_runtime, 'start_conversation_attempt_executor', fake_start)
    ack = flask_client.post(
        '/api/v3/conversations/conv-command-inject/turns',
        json={'commandId': 'cmd-first',
              'inputTurn': {'content': 'first'},
              'config': {'model': 'gpt-4o'}})
    assert ack.status_code == 200
    return ack.get_json(), starts


def test_queue_mode_enqueues_without_creating_turn(flask_client, conversation_command_db,
                                                   monkeypatch):
    from lib.message_queue import get_queue_depth
    from lib.turn_lifecycle import list_turns

    first, starts = _start_first_turn(flask_client, monkeypatch)
    assert first['turn']['status'] == 'running'
    assert len(starts) == 1

    resp = flask_client.post(
        '/api/v3/conversations/conv-command-inject/turns',
        json={'commandId': 'cmd-second',
              'inputTurn': {'content': 'hold this', '_msgId': 'message-second'},
              'config': {'model': 'gpt-4o'},
              'injectMode': 'queue'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True
    assert body['queued'] is True
    assert body['position'] == 1
    assert body['queueId']
    assert body['queueItem']['sourceMessageId'] == 'message-second'
    assert body['latestTurn']['turnId'] == first['turn']['turnId']

    # The durable row exists, no executor was started, and NO new turn pair
    # was created (the two turns are the first send's input + output).
    assert get_queue_depth('conv-command-inject', user_id=1) == 1
    assert len(starts) == 1
    assert len(list_turns('conv-command-inject', user_id=1)['turns']) == 2


def test_steer_mode_injects_into_running_turn_inbox(flask_client, conversation_command_db,
                                                    monkeypatch):
    from lib import agent_inbox
    from lib.message_queue import get_queue_depth
    from lib.turn_lifecycle import list_turns

    first, starts = _start_first_turn(flask_client, monkeypatch)

    resp = flask_client.post(
        '/api/v3/conversations/conv-command-inject/turns',
        json={'commandId': 'cmd-steer',
              'inputTurn': {'content': 'steer me'},
              'config': {'model': 'gpt-4o'},
              'injectMode': 'steer'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True
    assert body['steered'] is True
    assert body['latestTurn']['turnId'] == first['turn']['turnId']

    # Delivered to the conversation-keyed inbox under the user-steer mode…
    items = agent_inbox.drain('conv-command-inject', modes=['user-steer'])
    assert len(items) == 1
    assert items[0]['value'] == 'steer me'
    # enqueue() merges `extra` into the item verbatim.
    assert items[0]['_user_msg']['content'] == 'steer me'
    # …and NOT to the durable queue; no new turn, no new executor start.
    assert get_queue_depth('conv-command-inject', user_id=1) == 0
    assert len(starts) == 1
    assert len(list_turns('conv-command-inject', user_id=1)['turns']) == 2


def test_missing_inject_mode_preserves_lane_busy_conflict(flask_client,
                                                          conversation_command_db,
                                                          monkeypatch):
    _start_first_turn(flask_client, monkeypatch)
    resp = flask_client.post(
        '/api/v3/conversations/conv-command-inject/turns',
        json={'commandId': 'cmd-plain',
              'inputTurn': {'content': 'no mode'},
              'config': {'model': 'gpt-4o'}})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body['error']['kind'] == 'lane_busy'
    assert body['latestTurn']


def test_steer_falls_back_to_queue_when_inbox_tombstoned(flask_client,
                                                         conversation_command_db,
                                                         monkeypatch):
    from lib import agent_inbox
    from lib.message_queue import get_queue_depth

    _start_first_turn(flask_client, monkeypatch)
    # A tombstoned inbox slot means the running task is finalizing and will
    # never drain again — the steer must become a queued turn, not vanish.
    agent_inbox._tombstones.add('conv-command-inject')
    try:
        resp = flask_client.post(
            '/api/v3/conversations/conv-command-inject/turns',
            json={'commandId': 'cmd-steer-fallback',
                  'inputTurn': {'content': 'too late for steer'},
                  'config': {'model': 'gpt-4o'},
                  'injectMode': 'steer'})
    finally:
        agent_inbox._tombstones.discard('conv-command-inject')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['queued'] is True
    assert body.get('steered') is not True
    assert get_queue_depth('conv-command-inject', user_id=1) == 1


def test_settlement_drains_queued_row_into_turn_pair(flask_client, conversation_command_db,
                                                     monkeypatch):
    """End-to-end: queue behind a running attempt, settle it, and the drain
    creates the turn pair + starts its attempt through the command service."""
    import lib.message_queue as message_queue
    from lib.turn_lifecycle import list_turns, record_task_event

    first, starts = _start_first_turn(flask_client, monkeypatch)
    queued = flask_client.post(
        '/api/v3/conversations/conv-command-inject/turns',
        json={'commandId': 'cmd-queued',
              'inputTurn': {'content': 'run me next'},
              'config': {'model': 'gpt-4o'},
              'injectMode': 'queue'}).get_json()
    assert queued['queued'] is True

    drained = threading.Event()
    dispatch_errors = []
    dispatch_results = []
    real_dispatch = message_queue.dispatch_next_queued

    def spy_dispatch(conv_id, **kwargs):
        try:
            out = real_dispatch(conv_id, **kwargs)
            dispatch_results.append(out)
            return out
        except Exception:
            import traceback
            dispatch_errors.append(traceback.format_exc())
            raise
        finally:
            drained.set()

    monkeypatch.setattr(message_queue, 'dispatch_next_queued', spy_dispatch)

    task = {
        '_attemptId': first['attempt']['attemptId'],
        '_turnId': first['turn']['turnId'],
        'id': 'internal-task-run', 'status': 'done', 'finishReason': 'stop',
        'content': 'answer', 'thinking': '', 'toolRounds': [],
        'model': 'gpt-4o', 'config': {'model': 'gpt-4o'},
        'convId': 'conv-command-inject',
        '_userId': 1,
    }
    assert record_task_event(task, {'type': 'done', 'finishReason': 'stop'})

    # The settlement hook drains on a daemon thread — wait for it.
    assert drained.wait(timeout=10)
    assert not dispatch_errors, dispatch_errors[0] if dispatch_errors else ''
    assert dispatch_results and dispatch_results[0], (
        f'dispatch returned {dispatch_results!r} — no task started')

    turns = list_turns('conv-command-inject', user_id=1)['turns']
    assert len(turns) == 4  # first pair + queued pair
    human = [t for t in turns if t['actor'] == 'human']
    assert human[-1]['projection']['content'] == 'run me next'
    output = [t for t in turns if t['actor'] == 'assistant']
    assert output[-1]['parentTurnId'] == human[-1]['turnId']
    # The fresh attempt starts through the command service with stable identity.
    assert len(starts) == 2
    turn_config = starts[1][1]
    assert turn_config['_turnId'] == output[-1]['turnId']
    assert turn_config['_attemptId'] == output[-1]['currentAttemptId']
    assert turn_config['excludeLast'] is True
    # …and the queue row is gone.
    assert message_queue.get_queue_depth(
        'conv-command-inject', user_id=1) == 0


def test_drain_defers_while_lane_still_live(flask_client, conversation_command_db,
                                            monkeypatch):
    """A drain firing before the occupying attempt settles must leave the
    row queued (the settlement hook re-drains when the lane frees)."""
    import lib.message_queue as message_queue
    from lib.turn_lifecycle import list_turns

    _start_first_turn(flask_client, monkeypatch)
    flask_client.post(
        '/api/v3/conversations/conv-command-inject/turns',
        json={'commandId': 'cmd-queued-2',
              'inputTurn': {'content': 'wait your turn'},
              'config': {'model': 'gpt-4o'},
              'injectMode': 'queue'})

    # Attempt #1 is still running → create_turn_pair raises lane_busy and
    # the drain releases the lease instead of dropping the row.
    assert message_queue.dispatch_next_queued(
        'conv-command-inject', user_id=1) is None
    assert message_queue.get_queue_depth(
        'conv-command-inject', user_id=1) == 1
    assert len(list_turns('conv-command-inject', user_id=1)['turns']) == 2
