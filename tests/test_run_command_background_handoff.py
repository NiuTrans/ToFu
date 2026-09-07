"""Focused contracts for steer-triggered ``run_command`` background handoff."""

from __future__ import annotations

import threading

import pytest

from lib import agent_inbox
from lib.tasks_pkg.handlers import _background_command as background

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_inbox():
    agent_inbox.reset_for_test()
    yield
    agent_inbox.reset_for_test()


def _task():
    return {
        'id': 'task-1',
        'convId': 'conv-1',
        '_userId': 7,
        'aborted': False,
        'toolRounds': [],
    }


def test_no_steer_preserves_synchronous_result():
    task = _task()

    result = background.run_with_steer_handoff(
        task=task,
        config={'model': 'test'},
        command='echo ok',
        execute=lambda command_task: '$ echo ok\nok\n[exit code: 0]',
    )

    assert result.endswith('[exit code: 0]')
    assert not background.is_background_command_result(result)


def test_steer_hands_off_without_consuming_message_or_clobbering_next_pid(
        monkeypatch):
    task = _task()
    release = threading.Event()
    delivered = threading.Event()
    queued = []

    def execute(command_task):
        command_task['_subprocess_pid'] = 321
        command_task['_subprocess_pgid'] = 321
        release.wait(2)
        command_task.pop('_subprocess_pid', None)
        command_task.pop('_subprocess_pgid', None)
        return '$ slow-test\npassed\n[exit code: 0]'

    def queue_completion(**kwargs):
        queued.append(kwargs)
        delivered.set()

    monkeypatch.setattr(background, '_queue_completion', queue_completion)
    agent_inbox.enqueue(
        task['convId'], 'please handle this first', mode='user-steer',
        priority='next')

    result = background.run_with_steer_handoff(
        task=task,
        config={'model': 'test'},
        command='slow-test',
        execute=execute,
    )

    assert background.is_background_command_result(result)
    assert task.get('_subprocess_pid') is None
    assert agent_inbox.has_pending(task['convId'], modes=['user-steer'])

    # A later foreground command owns this PID; cleanup from the detached
    # command must not remove it.
    task['_subprocess_pid'] = 999
    release.set()
    assert delivered.wait(2)
    assert task['_subprocess_pid'] == 999
    assert queued[0]['result'].endswith('[exit code: 0]')


def test_sync_execution_error_is_re_raised():
    def fail(_command_task):
        raise RuntimeError('boom')

    with pytest.raises(RuntimeError, match='boom'):
        background.run_with_steer_handoff(
            task=_task(),
            config={},
            command='broken',
            execute=fail,
        )


def test_real_subprocess_keeps_draining_and_observes_whole_task_stop(
        monkeypatch, tmp_path):
    from lib.project_mod.run_command import tool_run_command

    task = _task()
    delivered = threading.Event()
    queued = []
    monkeypatch.setattr(
        background, '_queue_completion',
        lambda **kwargs: (queued.append(kwargs), delivered.set()))
    agent_inbox.enqueue(
        task['convId'], 'take over the foreground', mode='user-steer',
        priority='next')

    result = background.run_with_steer_handoff(
        task=task,
        config={},
        command='sleep 30',
        execute=lambda command_task: tool_run_command(
            str(tmp_path), 'sleep 30', task=command_task),
    )

    assert background.is_background_command_result(result)
    task['aborted'] = True
    assert delivered.wait(5)
    assert '[Command aborted by user]' in queued[0]['result']


def test_completion_uses_durable_non_human_queue_row(monkeypatch):
    task = _task()
    calls = []

    def enqueue(conv_id, message, config, kind, *, user_id):
        calls.append(('enqueue', conv_id, message, config, kind, user_id))
        return {'queueId': 'queue-1'}

    def dispatch(conv_id, *, user_id):
        calls.append(('dispatch', conv_id, user_id))

    monkeypatch.setattr('lib.message_queue.enqueue_message', enqueue)
    monkeypatch.setattr('lib.message_queue.dispatch_next_queued', dispatch)

    background._queue_completion(
        task=task,
        config={'model': 'test', '_turnId': 'old-turn'},
        command='pytest',
        command_id='bg_123',
        result='$ pytest\npassed\n[exit code: 0]',
    )

    enqueued = calls[0]
    assert enqueued[0:2] == ('enqueue', task['convId'])
    assert enqueued[3] == {'model': 'test'}
    assert enqueued[4] == 'workflow_step'
    assert enqueued[5] == task['_userId']
    assert enqueued[2]['_user_msg']['_initiator'] == 'proactive'
    assert calls[1] == ('dispatch', task['convId'], task['_userId'])


def test_code_exec_handler_returns_background_meta_on_steer(monkeypatch):
    import lib.project_mod
    import lib.tasks_pkg.handlers.code_exec as code_exec

    task = _task()
    release = threading.Event()
    delivered = threading.Event()
    captured = {}

    class Progress:
        detached = False

        def __call__(self, _stream, _text):
            return None

        def flush(self):
            return None

        def detach(self):
            self.detached = True

    class Lifecycle:
        def __call__(self, _started, _deadline):
            return None

        def finish(self):
            return None

    progress = Progress()

    def execute(_name, _args, **kwargs):
        command_task = kwargs['task']
        command_task['_subprocess_pid'] = 456
        command_task['_subprocess_pgid'] = 456
        release.wait(2)
        command_task.pop('_subprocess_pid', None)
        command_task.pop('_subprocess_pgid', None)
        return '$ slow-test\npassed\n[exit code: 0]'

    monkeypatch.setattr(
        code_exec, '_make_run_command_progress_cb',
        lambda *args, **kwargs: progress)
    monkeypatch.setattr(
        code_exec, '_make_run_command_spawn_cb',
        lambda *args, **kwargs: Lifecycle())
    monkeypatch.setattr(lib.project_mod, 'execute_standalone_command', execute)
    monkeypatch.setattr(
        code_exec, '_finalize_tool_round',
        lambda _task, _rn, _entry, metas, **_kwargs:
        captured.setdefault('meta', metas[0]))
    monkeypatch.setattr(
        background, '_queue_completion',
        lambda **_kwargs: delivered.set())
    agent_inbox.enqueue(
        task['convId'], 'new operator request', mode='user-steer',
        priority='next')

    _tc_id, content, _ = code_exec._handle_code_exec(
        task, None, 'run_command', 'tc-1',
        {'command': 'slow-test'}, 1,
        {'toolCallId': 'tc-1', 'toolName': 'code_exec', 'status': 'searching'},
        {'model': 'test'}, '/tmp', False,
    )

    assert background.is_background_command_result(content)
    assert progress.detached is True
    assert captured['meta']['backgrounded'] is True
    assert captured['meta']['exitCode'] == 'background'
    release.set()
    assert delivered.wait(2)


def test_project_handler_hands_a_real_subprocess_to_background(
        monkeypatch, tmp_path):
    import lib.tasks_pkg.handlers.project as project_handler

    task = _task()
    delivered = threading.Event()
    queued = []
    captured = {}
    monkeypatch.setattr(
        background, '_queue_completion',
        lambda **kwargs: (queued.append(kwargs), delivered.set()))
    monkeypatch.setattr(
        project_handler, '_finalize_tool_round',
        lambda _task, _rn, _entry, metas, **_kwargs:
        captured.setdefault('meta', metas[0]))
    agent_inbox.enqueue(
        task['convId'], 'interrupt the wait', mode='user-steer',
        priority='next')

    _tc_id, content, _ = project_handler._handle_project_tool(
        task, None, 'run_command', 'tc-project',
        {'command': 'sleep 30'}, 1,
        {
            'query': 'run_command', 'toolCallId': 'tc-project',
            'toolName': 'run_command', 'status': 'searching',
        },
        {}, str(tmp_path), True,
    )

    assert background.is_background_command_result(content)
    assert captured['meta']['backgrounded'] is True
    assert captured['meta']['exitCode'] == 'background'
    task['aborted'] = True
    assert delivered.wait(5)
    assert '[Command aborted by user]' in queued[0]['result']
