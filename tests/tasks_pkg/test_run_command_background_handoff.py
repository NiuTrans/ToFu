"""Focused contracts for steer-triggered ``run_command`` background handoff."""

from __future__ import annotations

import threading
import time

import pytest

from lib import agent_inbox
from lib.tasks_pkg import background_commands
from lib.tasks_pkg.handlers import _background_command as background

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_inbox():
    agent_inbox.reset_for_test()
    with background_commands._lock:
        background_commands._registry.clear()
    yield
    agent_inbox.reset_for_test()
    with background_commands._lock:
        background_commands._registry.clear()


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


def test_elapsed_command_hands_off_and_remains_owner_queryable(monkeypatch):
    monkeypatch.setattr(background, 'AUTO_BACKGROUND_AFTER_S', 0.05)
    task = _task()
    release = threading.Event()
    delivered = threading.Event()
    monkeypatch.setattr(
        background, '_queue_completion',
        lambda **_kwargs: delivered.set() or True)

    def execute(command_task):
        command_task['_subprocess_pid'] = 321
        release.wait(2)
        command_task.pop('_subprocess_pid', None)
        return '$ slow-test\npassed\n[exit code: 0]'

    handoffs = []
    result = background.run_with_steer_handoff(
        task=task, config={}, command='slow-test', execute=execute,
        on_handoff=handoffs.append)
    assert background.is_background_command_result(result)
    command_id = result.split(background.BACKGROUND_COMMAND_MARKER, 1)[1].split('.', 1)[0].strip()
    assert task.get('_subprocess_pid') is None
    assert handoffs == [command_id]
    assert background_commands.check_background_command(
        command_id, user_id=7)['status'] == 'running'
    assert background_commands.check_background_command(
        command_id, user_id=8) is None
    release.set()
    settled = background_commands.check_background_command(
        command_id, user_id=7, wait_s=2)
    assert settled['status'] == 'completed'
    assert settled['result'].endswith('[exit code: 0]')
    assert delivered.wait(2)


def test_manual_handoff_is_command_scoped_and_preserves_process(monkeypatch):
    monkeypatch.setattr(background, 'AUTO_BACKGROUND_AFTER_S', 1000)
    task = _task()
    task['status'] = 'running'
    release = threading.Event()
    ready = threading.Event()
    settled = []

    def execute(command_task):
        command_task['_subprocess_pid'] = 321
        ready.set()
        release.wait(3)
        command_task.pop('_subprocess_pid', None)
        return '$ sleep 30\npassed\n[exit code: 0]'

    worker = threading.Thread(target=lambda: settled.append(
        background.run_with_steer_handoff(
            task=task, config={}, command='sleep 30', execute=execute,
            round_num=3, tool_call_id='call-3')))
    worker.start()
    try:
        assert ready.wait(2)
        control = task['_cmd_handoff_control']
        assert control.request(round_num=4, tool_call_id='call-3') == {
            'backgrounded': False, 'reason': 'command_changed'}
        assert control.request(round_num=3, tool_call_id='old-call') == {
            'backgrounded': False, 'reason': 'command_changed'}
        assert control.request(round_num=3, tool_call_id='call-3') == {
            'backgrounded': True}
        worker.join(2)
        assert not worker.is_alive()
        assert background.is_background_command_result(settled[0])
        assert task['aborted'] is False
        assert release.is_set() is False
        assert '_cmd_handoff_control' not in task
        assert control.request(round_num=3, tool_call_id='call-3') == {
            'backgrounded': False, 'reason': 'no_active_command'}
    finally:
        release.set()
        worker.join(3)


def test_manual_handoff_accepts_with_more_than_32_live_commands(monkeypatch):
    monkeypatch.setattr(background, 'AUTO_BACKGROUND_AFTER_S', 1000)
    for i in range(40):
        background_commands.register_handed_off_command(
            command_id=f'existing-{i}', task=_task(), command='live', done=threading.Event())
    delivered = threading.Event()
    monkeypatch.setattr(background, '_queue_completion', lambda **kwargs: delivered.set() or True)
    task = _task()
    task['status'] = 'running'
    release = threading.Event()
    ready = threading.Event()
    settled = []

    def execute(command_task):
        command_task['_subprocess_pid'] = 321
        ready.set()
        release.wait(3)
        command_task.pop('_subprocess_pid', None)
        return '$ sleep 30\npassed\n[exit code: 0]'

    worker = threading.Thread(target=lambda: settled.append(
        background.run_with_steer_handoff(
            task=task, config={}, command='sleep 30', execute=execute,
            round_num=3, tool_call_id='call-3')))
    worker.start()
    try:
        assert ready.wait(2)
        assert task['_cmd_handoff_control'].request(
            round_num=3, tool_call_id='call-3') == {
                'backgrounded': True}
        worker.join(2)
        assert not worker.is_alive()
        assert background.is_background_command_result(settled[0])
        assert not release.is_set()
    finally:
        release.set()
        worker.join(3)
    assert delivered.wait(3)
    assert len(background_commands.list_background_commands(user_id=7)) >= 40


def test_manual_handoff_refused_for_stdin_and_interrupt():
    task = _task()
    task['status'] = 'running'
    task['_subprocess_pid'] = 321
    control = background.CommandHandoffControl(task, 3, 'call-3')
    control.note_pid(321)
    task['toolRounds'].append({'status': 'awaiting_stdin'})
    assert control.request(round_num=3, tool_call_id='call-3') == {
        'backgrounded': False, 'reason': 'awaiting_stdin'}
    task['toolRounds'].clear()
    task['_cmd_interrupt'] = {'pid': 321, 'source': 'user'}
    assert control.request(round_num=3, tool_call_id='call-3') == {
        'backgrounded': False, 'reason': 'command_interrupting'}
    task['aborted'] = True
    assert control.request(round_num=3, tool_call_id='call-3') == {
        'backgrounded': False, 'reason': 'task_not_running'}


def test_manual_request_does_not_transfer_to_next_pid():
    task = _task()
    task.update(status='running', _subprocess_pid=321)
    control = background.CommandHandoffControl(task, 3, 'call-3')
    control.note_pid(321)
    control._resolved.wait = lambda _seconds: False
    assert control.request(round_num=3, tool_call_id='call-3')['reason'] == 'handoff_pending'
    task['_subprocess_pid'] = 322
    control.note_pid(322)
    assert control.requested_for(322) is False
    assert control.request(round_num=3, tool_call_id='call-3')['reason'] == 'no_active_command'
    control.note_pid(None)
    assert control.requested_for(322) is False


def test_stop_during_registry_adoption_keeps_command_foreground(monkeypatch):
    monkeypatch.setattr(background, 'AUTO_BACKGROUND_AFTER_S', 1000)
    task = _task()
    task.update(status='running', abort_event=threading.Event())
    started = threading.Event()
    release = threading.Event()
    settled = []

    def execute(command_task):
        command_task['_subprocess_pid'] = 321
        started.set()
        assert release.wait(5)
        command_task.pop('_subprocess_pid', None)
        return '$ sleep 30\n[Command aborted by user]\n[exit code: -1]'

    def abort_during_adoption(**kwargs):
        registered = original_register(**kwargs)
        task['abort_event'].set()
        return registered

    original_register = background_commands.register_handed_off_command
    monkeypatch.setattr(background_commands, 'register_handed_off_command', abort_during_adoption)
    worker = threading.Thread(target=lambda: settled.append(
        background.run_with_steer_handoff(
            task=task, config={}, command='sleep 30', execute=execute,
            round_num=3, tool_call_id='call-3')))
    worker.start()
    try:
        assert started.wait(2)
        assert task['_cmd_handoff_control'].request(
            round_num=3, tool_call_id='call-3') == {
                'backgrounded': False, 'reason': 'task_not_running'}
        assert background_commands.list_background_commands(user_id=7) == []
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not background.is_background_command_result(settled[0])


def test_manual_handoff_cannot_register_after_worker_clears_pid(monkeypatch):
    monkeypatch.setattr(background, 'AUTO_BACKGROUND_AFTER_S', 1000)
    task = _task()
    task['status'] = 'running'
    started = threading.Event()
    release = threading.Event()
    result = []

    def execute(command_task):
        command_task['_subprocess_pid'] = 321
        started.set()
        assert release.wait(5)
        command_task.pop('_subprocess_pid', None)
        return '$ echo done\n[exit code: 0]'

    worker = threading.Thread(target=lambda: result.append(
        background.run_with_steer_handoff(
            task=task, config={}, command='echo done', execute=execute,
            round_num=3, tool_call_id='call-3')))
    worker.start()
    try:
        assert started.wait(2)
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert result[0].endswith('[exit code: 0]')
        assert background_commands.list_background_commands(user_id=7) == []
    finally:
        release.set()
        worker.join(5)


def test_elapsed_handoff_can_kill_only_its_command(monkeypatch, tmp_path):
    from lib.project_mod.run_command import tool_run_command

    monkeypatch.setattr(background, 'AUTO_BACKGROUND_AFTER_S', 0.05)
    task = _task()
    delivered = threading.Event()
    monkeypatch.setattr(background, '_queue_completion',
                        lambda **_kwargs: delivered.set() or True)
    result = background.run_with_steer_handoff(
        task=task, config={}, command='sleep 30',
        execute=lambda command_task: tool_run_command(
            str(tmp_path), 'sleep 30', task=command_task))
    command_id = result.split(background.BACKGROUND_COMMAND_MARKER, 1)[1].split('.', 1)[0].strip()
    assert background_commands.kill_background_command(command_id, user_id=8) is False
    assert background_commands.kill_background_command(command_id, user_id=7) is True
    settled = background_commands.check_background_command(
        command_id, user_id=7, wait_s=5)
    assert settled['status'] == 'completed'
    assert '[Command aborted by user]' in settled['result']
    assert task['aborted'] is False
    assert delivered.wait(2)


def test_waiting_for_stdin_does_not_auto_handoff(monkeypatch):
    monkeypatch.setattr(background, 'AUTO_BACKGROUND_AFTER_S', 0.05)
    task = _task()
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    result = []

    def execute(command_task):
        command_task['_subprocess_pid'] = 321
        task['toolRounds'].append({'status': 'awaiting_stdin'})
        started.set()
        release.wait(2)
        return '$ prompt\nanswered\n[exit code: 0]'

    def run():
        result.append(background.run_with_steer_handoff(
            task=task, config={}, command='prompt', execute=execute))
        finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert started.wait(2)
        time.sleep(0.2)
        assert not finished.is_set()
        assert background_commands.list_background_commands(user_id=7) == []
    finally:
        release.set()
        worker.join(2)
    assert finished.is_set()
    assert not background.is_background_command_result(result[0])


def test_elapsed_handoff_accepts_with_more_than_32_live_commands(monkeypatch):
    monkeypatch.setattr(background, 'AUTO_BACKGROUND_AFTER_S', 0.05)
    for i in range(40):
        background_commands.register_handed_off_command(
            command_id=f'existing-{i}', task=_task(), command='live', done=threading.Event())
    delivered = threading.Event()
    monkeypatch.setattr(background, '_queue_completion', lambda **kwargs: delivered.set() or True)
    task = _task()

    def execute(command_task):
        command_task['_subprocess_pid'] = 321
        time.sleep(0.2)
        return '$ slow-test\npassed\n[exit code: 0]'

    result = background.run_with_steer_handoff(
        task=task, config={}, command='slow-test', execute=execute)
    assert background.is_background_command_result(result)
    assert delivered.wait(3)


def test_no_subprocess_does_not_auto_handoff(monkeypatch):
    monkeypatch.setattr(background, 'AUTO_BACKGROUND_AFTER_S', 0.05)

    def execute(_command_task):
        time.sleep(0.2)
        return '$ fast-path\npassed\n[exit code: 0]'

    result = background.run_with_steer_handoff(
        task=_task(), config={}, command='fast-path', execute=execute)
    assert result.endswith('[exit code: 0]')
    assert not background.is_background_command_result(result)


def test_stop_wins_over_elapsed_handoff(monkeypatch):
    monkeypatch.setattr(background, 'AUTO_BACKGROUND_AFTER_S', 0.05)
    task = _task()
    task['aborted'] = True

    def execute(command_task):
        command_task['_subprocess_pid'] = 321
        time.sleep(0.2)
        return '$ slow-test\n[Command aborted by user]\n[exit code: -1]'

    result = background.run_with_steer_handoff(
        task=task, config={}, command='slow-test', execute=execute)
    assert '[Command aborted by user]' in result
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
    assert enqueued[4] == 'background_receipt'
    assert enqueued[5] == task['_userId']
    assert '_user_msg' not in enqueued[2]
    assert len(calls) == 1  # Completion alone cannot start a model turn.


def test_completion_config_excludes_process_private_cache_before_durable_enqueue(
    monkeypatch,
):
    import json
    from types import MappingProxyType

    captured = []

    def enqueue(conv_id, message, config, kind, *, user_id):
        # Exercise the real storage wire's JSON requirement at the queue edge.
        json.dumps({'message': message, 'config': config})
        captured.append(config)
        return {'queueId': 'queue-1'}

    monkeypatch.setattr('lib.message_queue.enqueue_message', enqueue)
    monkeypatch.setattr(
        'lib.message_queue.dispatch_next_queued',
        lambda conv_id, *, user_id: None,
    )
    queued = background._queue_completion(
        task=_task(),
        config={
            'model': 'test',
            '_mcpToolSearchTextByName': MappingProxyType({'tool': 'description'}),
        },
        command='pytest', command_id='bg_cache', result='passed',
    )
    assert queued is True
    assert captured == [{'model': 'test'}]


@pytest.mark.parametrize('trigger', ['steer', 'elapsed'])
def test_code_exec_handler_returns_background_meta(monkeypatch, trigger):
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
    if trigger == 'steer':
        agent_inbox.enqueue(
            task['convId'], 'new operator request', mode='user-steer',
            priority='next')
    else:
        monkeypatch.setattr(background, 'AUTO_BACKGROUND_AFTER_S', 0.05)

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


@pytest.mark.parametrize('trigger', ['steer', 'elapsed'])
def test_project_handler_hands_a_real_subprocess_to_background(
        monkeypatch, tmp_path, trigger):
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
    if trigger == 'steer':
        agent_inbox.enqueue(
            task['convId'], 'interrupt the wait', mode='user-steer',
            priority='next')
    else:
        monkeypatch.setattr(background, 'AUTO_BACKGROUND_AFTER_S', 0.05)

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


def test_project_command_failure_keeps_original_error_and_partial_output(monkeypatch, tmp_path):
    import lib.tasks_pkg.handlers.project as handler
    import lib.tasks_pkg.handlers.code_exec as code_exec
    from types import SimpleNamespace
    complete = []
    artifact = SimpleNamespace(spilled=False)
    progress = SimpleNamespace(flush=lambda: None,
        finalize_output=lambda **kwargs: (complete.append(kwargs['complete']), artifact)[1])
    monkeypatch.setattr(handler, '_make_run_command_progress_cb', lambda *a, **k: progress)
    monkeypatch.setattr(handler, '_make_run_command_spawn_cb', lambda *a, **k: None)
    monkeypatch.setattr(code_exec, '_project_output_artifact', lambda content, result: content)
    monkeypatch.setattr(code_exec, '_register_output_artifact_origin', lambda *a, **k: None)
    failure = RuntimeError('synthetic execution failure')
    def fail(**kwargs):
        raise failure
    monkeypatch.setattr(background, 'run_with_steer_handoff', fail)
    with pytest.raises(RuntimeError) as caught:
        handler._handle_project_tool(_task(), None, 'run_command', 'tc-fail',
            {'command': 'fail'}, 1, {'status': 'searching'}, {}, str(tmp_path), True)
    assert caught.value is failure
    assert complete == [False]
