"""Timer dispatch crosses real owner-scoped storage and the shared Turn command."""
from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


@pytest.fixture()
def continuation(flask_client, monkeypatch):
    import lib.conversation_sync.task_start as task_start
    from lib.scheduler.timer import create_timer, cancel_timer
    from tests._seed import seed_conversation, delete_conversation

    conv_id = 'timer-continuation-' + uuid.uuid4().hex
    seed_conversation(conv_id, user_id=1, settings={'model': 'gpt-4o'})
    started = []

    def start(conv_id, config, **kwargs):
        task_id = 'timer-executor-' + uuid.uuid4().hex
        started.append(task_id)
        kwargs['on_task_registered'](task_id)
        return task_id, None

    monkeypatch.setattr(task_start, 'start_conversation_attempt_executor', start)
    timer = create_timer(user_id=1, conv_id=conv_id,
                         check_instruction='Verify completion',
                         continuation_message='Read the result', poll_interval=3600)
    try:
        yield timer, started
    finally:
        cancel_timer(timer['id'], user_id=1)
        delete_conversation(conv_id, user_id=1)


@pytest.mark.parametrize('committed_before_failure', [False, True])
def test_trigger_receipt_retry_never_restarts_executor(continuation, monkeypatch,
                                                      committed_before_failure):
    from lib.scheduler.timer import _loop, get_timer
    from lib.storage import get_storage_client
    from lib.turn_lifecycle import list_turns

    timer, started = continuation
    client = get_storage_client(write=True)
    client.command('timer.update', {'timer_id': timer['id'], 'user_id': 1,
        'last_poll_decision': 'ready'}, 'ready:' + timer['id'])

    class FaultClient:
        failed = False

        def command(self, operation, payload, command_id):
            if payload.get('status') == 'triggered' and not self.failed:
                self.failed = True
                if committed_before_failure:
                    client.command(operation, payload, command_id)
                raise RuntimeError('lost trigger receipt')
            return client.command(operation, payload, command_id)

    faulty = FaultClient()
    monkeypatch.setattr(_loop, '_timer_client', lambda **kwargs: faulty)
    assert _loop._execute_continuation(timer).kind == 'retry'
    assert len(started) == 1
    retry = _loop._execute_continuation(timer)
    if committed_before_failure:
        # The receipt committed but its ACK was lost: the row is already
        # terminal, so the replay must not announce a second delivery.
        assert retry.kind == 'closed'
    else:
        assert retry.kind == 'started'
        assert retry.task_id == started[0]
    assert len(started) == 1
    row = get_timer(timer['id'], user_id=1)
    assert row['status'] == 'triggered'
    assert row['execution_task_id'] == started[0]
    assert row['poll_count'] == 0
    turns = list_turns(timer['conv_id'], user_id=1)['turns']
    assert len(turns) == 2
    assert turns[0]['projection']['origin']['initiator'] == 'timer'


def test_poll_finishing_after_delivery_keeps_the_receipt(continuation):
    """A background poll in flight while an operator delivers commits its
    verdict afterwards; the settled watcher's receipt and budget must hold."""
    from lib.scheduler.timer import _loop, get_timer, get_timer_poll_log

    timer, started = continuation
    assert _loop._execute_continuation(timer).kind == 'started'
    _loop._record_poll(timer['id'], 'wait', 'Timer no longer active', 0,
                       poll_id=f"{timer['id']}.p1", user_id=1)
    row = get_timer(timer['id'], user_id=1)
    assert row['status'] == 'triggered'
    assert row['last_poll_decision'] == 'started'
    assert row['poll_count'] == 0
    assert get_timer_poll_log(timer['id'], user_id=1) == []
    assert len(started) == 1


def test_busy_agent_lane_delivers_without_extra_turn(continuation):
    """The second timer meets a lane that the first continuation still owns.

    A watcher never owns that lane, so the due obligation must be delivered
    into it rather than dropped. The first continuation's attempt is still
    live and task-bound, so the steer window is open: the continuation is
    committed into that Turn as a durable injection block and no Turn is
    added. The durable queue is only the fallback once that window closed.
    """
    from lib.scheduler.timer import _loop, create_timer, cancel_timer, get_timer
    from lib.storage import get_storage_client
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import list_turns

    timer, started = continuation
    first = _loop._execute_continuation(timer)
    assert first.kind == 'started' and first.task_id == started[0]
    waiting = create_timer(user_id=1, conv_id=timer['conv_id'],
                           check_instruction='Verify another result',
                           continuation_message='Read another result', max_polls=1)
    try:
        get_storage_client(write=True).command('timer.update', {
            'timer_id': waiting['id'], 'user_id': 1,
            'last_poll_decision': 'ready', 'poll_count': 1,
        }, 'ready:' + waiting['id'])
        delivered = _loop._execute_continuation(waiting)
        assert delivered.kind == 'steered'
        assert delivered.accepted
        row = get_timer(waiting['id'], user_id=1)
        assert row['status'] == 'triggered'
        assert row['last_poll_decision'] == 'steered'
        assert row['poll_count'] == 1
        assert len(started) == 1
        turns = list_turns(timer['conv_id'], user_id=1)['turns']
        assert len(turns) == 2
        injections = turns[-1]['projection']['_userSteerInjects']
        assert [item['blockId'] for item in injections] == [
            f"injection:user-steer:timer:{waiting['id']}"]
        assert 'Read another result' in injections[0]['previews'][0]['text']
        try:
            denied = _loop._execute_continuation({**waiting, 'user_id': 2})
        except StorageError as error:
            # Native authority rejects a mismatched scope before reading;
            # owner-filtered SQL reads instead return an absent timer.
            assert error.code == 'database_forbidden'
        else:
            assert denied.kind == 'closed'
        assert len(started) == 1
    finally:
        cancel_timer(waiting['id'], user_id=1)
