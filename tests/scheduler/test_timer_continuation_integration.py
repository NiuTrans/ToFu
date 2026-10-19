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
    assert _loop._execute_continuation(timer) is None
    assert len(started) == 1
    retry = _loop._execute_continuation(timer)
    assert retry == (None if committed_before_failure else started[0])
    assert len(started) == 1
    row = get_timer(timer['id'], user_id=1)
    assert row['status'] == 'triggered'
    assert row['execution_task_id'] == started[0]
    assert row['poll_count'] == 0
    turns = list_turns(timer['conv_id'], user_id=1)['turns']
    assert len(turns) == 2
    assert turns[0]['projection']['origin']['initiator'] == 'timer'


def test_busy_agent_lane_keeps_ready_timer_without_extra_turn(continuation):
    from lib.scheduler.timer import _loop, create_timer, cancel_timer, get_timer
    from lib.storage import get_storage_client
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import list_turns

    timer, started = continuation
    assert _loop._execute_continuation(timer) == started[0]
    waiting = create_timer(user_id=1, conv_id=timer['conv_id'],
                           check_instruction='Verify another result',
                           continuation_message='Read another result', max_polls=1)
    try:
        get_storage_client(write=True).command('timer.update', {
            'timer_id': waiting['id'], 'user_id': 1,
            'last_poll_decision': 'ready', 'poll_count': 1,
        }, 'ready:' + waiting['id'])
        assert _loop._execute_continuation(waiting) is None
        row = get_timer(waiting['id'], user_id=1)
        assert row['status'] == 'active' and row['last_poll_decision'] == 'ready'
        assert row['poll_count'] == 1
        assert len(started) == 1
        assert len(list_turns(timer['conv_id'], user_id=1)['turns']) == 2
        try:
            denied = _loop._execute_continuation({**waiting, 'user_id': 2})
        except StorageError as error:
            # Native authority rejects a mismatched scope before reading;
            # owner-filtered SQL reads instead return an absent timer.
            assert error.code == 'database_forbidden'
        else:
            assert denied is None
        assert len(started) == 1
    finally:
        cancel_timer(waiting['id'], user_id=1)
