"""Conversation command send-while-running delivery lanes.

Regression coverage for the 2026-08-20 "insert-position prompt never opens"
report. Under the Turn/Attempt cutover the composer prompt's gate excluded
any conversation with a live turn attempt (``_activeAttemptId``), and the turn-native
submit route had no steer/queue semantics at all — a send during generation
hit a bare ``lane_busy`` 409 instead of the legacy
``lib/chat_dispatch.py`` delivery lanes.

The fixed contract:

  * ``injectMode: 'steer'`` → a durable injection block commits on the live
    assistant Turn before its conversation-keyed inbox wakeup; ACK carries
    ``steered: true`` and no new Turn pair is created.
  * ``injectMode: 'queue'`` → acceptance atomically creates the durable queue
    row plus its real input/output Turn pair and pending Attempt; dispatch
    later activates those same identities.
  * no ``injectMode`` → the ``lane_busy`` 409 contract is preserved for
    programmatic clients.

Run with ``python -m pytest tests/conversation/test_conversation_command_inject_mode.py``.
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
    """Create turn pair #1 with a live bound attempt; returns the ACK."""
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
        '/api/v4/conversations/conv-command-inject/turns',
        json={'commandId': 'cmd-first',
              'inputTurn': {'content': 'first'},
              'config': {'model': 'gpt-4o'}})
    assert ack.status_code == 200
    return ack.get_json(), starts


def test_queue_mode_accepts_real_pending_turn_pair(flask_client, conversation_command_db,
                                                   monkeypatch):
    from lib.message_queue import get_queue_depth
    from lib.turn_lifecycle import list_turns

    first, starts = _start_first_turn(flask_client, monkeypatch)
    assert first['turn']['status'] == 'pending'
    assert len(starts) == 1

    resp = flask_client.post(
        '/api/v4/conversations/conv-command-inject/turns',
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
    assert body['queueItem']['inputTurnId'] == body['submittedTurn']['turnId']
    assert body['queueItem']['outputTurnId'] == body['turn']['turnId']
    assert body['queueItem']['attemptId'] == body['attempt']['attemptId']
    assert body['latestTurn']['turnId'] == first['turn']['turnId']

    # The durable row and pair exist, but no executor starts before dispatch.
    assert get_queue_depth('conv-command-inject', user_id=1) == 1
    assert len(starts) == 1
    assert len(list_turns('conv-command-inject', user_id=1)['turns']) == 4


def test_steer_mode_injects_into_running_turn_inbox(flask_client, conversation_command_db,
                                                    monkeypatch):
    from lib import agent_inbox
    from lib.message_queue import get_queue_depth
    from lib.turn_lifecycle import list_turns

    first, starts = _start_first_turn(flask_client, monkeypatch)

    resp = flask_client.post(
        '/api/v4/conversations/conv-command-inject/turns',
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
    assert items[0]['blockId'] == 'injection:user-steer:cmd-steer'
    # …and NOT to the durable queue; no new turn, no new executor start.
    assert get_queue_depth('conv-command-inject', user_id=1) == 0
    assert len(starts) == 1
    turns = list_turns('conv-command-inject', user_id=1)['turns']
    assert len(turns) == 2
    injection = turns[-1]['projection']['_userSteerInjects'][0]
    assert injection['blockId'] == 'injection:user-steer:cmd-steer'
    assert injection['deliveryState'] == 'pending'
    assert injection['previews'] == [{'text': 'steer me'}]


def test_steer_preserves_pending_background_completion(
    flask_client, conversation_command_db, monkeypatch,
):
    from lib import agent_inbox
    from lib.message_queue import KIND_WORKFLOW, enqueue_message, get_queue

    first, starts = _start_first_turn(flask_client, monkeypatch)
    completion = enqueue_message(
        'conv-command-inject',
        {'text': 'background result', '_backgroundCommand': 'bg-pending'},
        {'model': 'gpt-4o'}, KIND_WORKFLOW, user_id=1,
    )
    agent_inbox.enqueue(
        'conv-command-inject', 'background result',
        priority='next', mode='background-command',
        extra={'queueId': completion['queueId'], 'commandId': 'bg-pending'},
    )

    response = flask_client.post(
        '/api/v4/conversations/conv-command-inject/turns',
        json={'commandId': 'cmd-steer-with-background',
              'inputTurn': {'content': 'insert now'},
              'config': {'model': 'gpt-4o'}, 'injectMode': 'steer'},
    )
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert result['steered'] is True
    assert result['latestTurn']['turnId'] == first['turn']['turnId']
    assert len(starts) == 1
    assert [row['queueId'] for row in get_queue(
        'conv-command-inject', user_id=1,
    )] == [completion['queueId']]
    items = agent_inbox.drain('conv-command-inject')
    assert [item['mode'] for item in items] == [
        'background-command', 'user-steer',
    ]
    assert items[1]['value'] == 'insert now'


def test_large_steer_receipt_is_bounded_and_replay_is_exactly_once(
    flask_client, conversation_command_db, monkeypatch, chat_sidecar,
):
    import hashlib
    import json
    import zlib

    from lib import agent_inbox
    from lib.turn_lifecycle import LifecycleConflict, commit_user_steer, get_turn

    first, _ = _start_first_turn(flask_client, monkeypatch)
    client = chat_sidecar.client()
    turn_id = first['turn']['turnId']
    attempt_id = first['attempt']['attemptId']
    noise = ''.join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(20000))
    assert len(zlib.compress(noise.encode(), 1)) > 64 * 1024
    # Build real live history in bounded event increments, not an oversized
    # fixture create receipt (which must still fail its independent budget).
    tools = []
    for index in range(20):
        tools.append({'round': index, 'calls': [{'id': f'tool-{index}', 'name': 'test',
                      'result': noise[index * 64000:(index + 1) * 64000]}]})
        assert client.command('turn.event.record', {
            'attempt_id': attempt_id, 'user_id': 1,
            'task_id': first['attempt']['taskId'], 'status': 'running',
            'terminal': False, 'projection': {'toolRounds': list(tools)},
            'event_type': 'projection_updated', 'event_payload': {'updateKind': 'tool_end'},
        }, None)['applied']
    response = flask_client.post(
        '/api/v4/conversations/conv-command-inject/turns',
        json={'commandId': 'large-steer', 'inputTurn': {'content': 'insert now'},
              'config': {'model': 'gpt-4o'}, 'injectMode': 'steer'},
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body['steered'] is True
    assert body['latestTurn']['projection']['toolRounds'] == tools
    assert len(agent_inbox.drain('conv-command-inject', modes=['user-steer'])) == 1
    before = client.query('turn.sync.snapshot', {
        'conversation_id': 'conv-command-inject', 'user_id': 1,
    })
    receipt = commit_user_steer('conv-command-inject', attempt_id,
                              command_id='large-steer', text='insert now', user_id=1)
    assert 'turn' not in receipt
    assert receipt['turnId'] == turn_id
    assert len(json.dumps(receipt).encode()) < 2048

    envelope = client._call(
        'command', 'turn.steer.commit',
        {'conversation_id': 'conv-command-inject', 'attempt_id': attempt_id,
         'command_id': 'large-steer', 'text': 'insert now', 'user_id': 1},
        command_id='turn-steer-commit:conv-command-inject:large-steer',
        deadline=None, priority='user',
    )
    assert envelope['value'] == receipt
    assert len(json.dumps(envelope).encode()) < 2048
    notice = envelope['events'][0]['event']
    assert notice['type'] == 'conversation.activity'
    assert notice['payload']['requiresSnapshot'] is True
    assert client.query('turn.sync.snapshot', {
        'conversation_id': 'conv-command-inject', 'user_id': 1,
    }) == before
    assert len(get_turn('conv-command-inject', turn_id, user_id=1)[
        'projection']['_userSteerInjects']) == 1
    for overrides in ({'text': 'different'}, {'user_id': 2}):
        args = dict(command_id='large-steer', text='insert now', user_id=1)
        args.update(overrides)
        with pytest.raises(LifecycleConflict):
            commit_user_steer('conv-command-inject', attempt_id, **args)
    # The same operation with a new identity must enforce owner filtering too.
    with pytest.raises(LifecycleConflict):
        commit_user_steer('conv-command-inject', attempt_id,
                         command_id='wrong-owner', text='no', user_id=2)
    assert client.query('turn.sync.snapshot', {
        'conversation_id': 'conv-command-inject', 'user_id': 1,
    }) == before


def test_steer_hydration_failure_does_not_lose_committed_inbox_wakeup(
    flask_client, conversation_command_db, monkeypatch,
):
    from lib import agent_inbox
    import lib.turn_lifecycle as lifecycle
    from lib.storage import StorageError

    first, _ = _start_first_turn(flask_client, monkeypatch)
    real_get = lifecycle.get_turn

    def fail_hydration(*args, **kwargs):
        raise StorageError('database_unavailable', 'injected postcommit read failure')

    monkeypatch.setattr(lifecycle, 'get_turn', fail_hydration)
    response = flask_client.post(
        '/api/v4/conversations/conv-command-inject/turns',
        json={'commandId': 'hydrate-fails', 'inputTurn': {'content': 'still deliver'},
              'config': {'model': 'gpt-4o'}, 'injectMode': 'steer'},
    )
    assert response.status_code == 200
    assert response.get_json()['steered'] is True
    assert 'latestTurn' not in response.get_json()
    items = agent_inbox.drain('conv-command-inject', modes=['user-steer'])
    assert [item['value'] for item in items] == ['still deliver']
    turn = real_get('conv-command-inject', first['turn']['turnId'], user_id=1)
    assert turn['projection']['_userSteerInjects'][0]['commandId'] == 'hydrate-fails'


def test_missing_inject_mode_preserves_lane_busy_conflict(flask_client,
                                                          conversation_command_db,
                                                          monkeypatch):
    _start_first_turn(flask_client, monkeypatch)
    resp = flask_client.post(
        '/api/v4/conversations/conv-command-inject/turns',
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
            '/api/v4/conversations/conv-command-inject/turns',
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
    # The human picked "insert now" — the ACK and every downstream queue
    # surface must say WHY the message waits for the next turn instead of
    # injecting mid-reply (2026-09-04 report: steer silently showed as an
    # ordinary queued row after the swarm teardown tombstoned the inbox).
    assert body['steerFallback'] is True
    assert body['queueItem']['steerFallback'] is True
    assert get_queue_depth('conv-command-inject', user_id=1) == 1

    from lib.message_queue import get_queue
    rows = get_queue('conv-command-inject', user_id=1)
    assert rows[0]['steerFallback'] is True

    snapshot = flask_client.get(
        '/api/v4/conversations/conv-command-inject/sync').get_json()
    snapshot_item = next(
        item for item in snapshot['queueItems']
        if item['queueId'] == body['queueId'])
    assert snapshot_item['steerFallback'] is True
    # A plain queued row (injectMode='queue') must NOT carry the marker.
    plain = flask_client.post(
        '/api/v4/conversations/conv-command-inject/turns',
        json={'commandId': 'cmd-plain-queue',
              'inputTurn': {'content': 'ordinary queue'},
              'config': {'model': 'gpt-4o'},
              'injectMode': 'queue'})
    assert plain.status_code == 200
    plain_body = plain.get_json()
    assert plain_body['queued'] is True
    assert 'steerFallback' not in plain_body
    assert 'steerFallback' not in plain_body['queueItem']


def test_settlement_drains_queued_row_into_turn_pair(flask_client, conversation_command_db,
                                                     monkeypatch):
    """End-to-end: queue behind a running attempt, settle it, and the drain
    creates the turn pair + starts its attempt through the command service."""
    import lib.message_queue as message_queue
    from lib.turn_lifecycle import list_turns, record_task_event

    first, starts = _start_first_turn(flask_client, monkeypatch)
    queued = flask_client.post(
        '/api/v4/conversations/conv-command-inject/turns',
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
        '/api/v4/conversations/conv-command-inject/turns',
        json={'commandId': 'cmd-queued-2',
              'inputTurn': {'content': 'wait your turn'},
              'config': {'model': 'gpt-4o'},
              'injectMode': 'queue'})

    # Attempt #1 is still running, so activation defers without replacing or
    # deleting the already accepted pair.
    assert message_queue.dispatch_next_queued(
        'conv-command-inject', user_id=1) is None
    assert message_queue.get_queue_depth(
        'conv-command-inject', user_id=1) == 1
    assert len(list_turns('conv-command-inject', user_id=1)['turns']) == 4


@pytest.mark.parametrize('inject_mode', ['steer', 'queue'])
def test_busy_lane_read_failure_preserves_storage_retry_contract(
    flask_client, conversation_command_db, monkeypatch, inject_mode,
):
    from lib import turn_lifecycle
    from lib.storage.errors import StorageError
    from lib.message_queue import get_queue_depth
    from lib import agent_inbox

    _start_first_turn(flask_client, monkeypatch)
    real_client = turn_lifecycle._turn_client

    class FailedReadClient:
        def query(self, operation, payload):
            assert operation == 'turn.list'
            raise StorageError(
                'database_unavailable', 'database_resource_exhausted',
                retryable=True, retry_after_ms=1500,
                operation_id='busy-read-operation',
            )

    monkeypatch.setattr(turn_lifecycle, '_turn_client',
                        lambda *, write=False: real_client(write=True)
                        if write else FailedReadClient())
    response = flask_client.post(
        '/api/v4/conversations/conv-command-inject/turns',
        headers={'X-Request-ID': 'steer-failed-read'},
        json={'commandId': 'failed-busy-read',
              'inputTurn': {'content': 'keep this draft'},
              'config': {'model': 'gpt-4o'}, 'injectMode': inject_mode},
    )
    assert response.status_code == 503, response.get_json()
    body = response.get_json()
    assert body['error']['kind'] == 'server_busy'
    assert 'database_resource_exhausted' in body['error']['detail']
    assert '请稍后重试' in body['error']['detail']
    assert body['error']['retryable'] is True
    assert body['storageCode'] == 'database_unavailable'
    assert body['retryAfterMs'] == 1500
    assert body['operationId'] == 'busy-read-operation'
    assert response.headers['Retry-After'] == '2'
    assert get_queue_depth('conv-command-inject', user_id=1) == 0
    assert not agent_inbox.drain('conv-command-inject', modes=['user-steer'])


def test_missing_busy_turn_uses_durable_steer_queue(
    flask_client, conversation_command_db, monkeypatch,
):
    from lib import turn_lifecycle
    from lib.message_queue import get_queue
    from lib import agent_inbox

    _start_first_turn(flask_client, monkeypatch)
    real_client = turn_lifecycle._turn_client

    class EmptyReadClient:
        def query(self, operation, payload):
            assert operation == 'turn.list'
            return []

    monkeypatch.setattr(turn_lifecycle, '_turn_client',
                        lambda *, write=False: real_client(write=True)
                        if write else EmptyReadClient())
    response = flask_client.post(
        '/api/v4/conversations/conv-command-inject/turns',
        json={'commandId': 'missing-busy-turn',
              'inputTurn': {'content': 'keep this in the queue'},
              'config': {'model': 'gpt-4o'}, 'injectMode': 'steer'},
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body['queued'] is True
    assert body['steerFallback'] is True
    assert body['queueItem']['steerFallback'] is True
    rows = get_queue('conv-command-inject', user_id=1)
    assert [row['queueId'] for row in rows] == [body['queueId']]
    assert not agent_inbox.drain('conv-command-inject', modes=['user-steer'])
