"""Tests for the background command registry and tool handlers."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg.background_commands import (
    check_background_command,
    kill_background_command,
    list_background_commands,
    start_background_command,
    _registry,
    _lock,
)


def _make_task(**overrides):
    task = {
        'id': 'test-task-bg',
        'convId': '',
    }
    task.update(overrides)
    return task


@pytest.fixture(autouse=True)
def _clean_registry():
    with _lock:
        _registry.clear()
    yield
    with _lock:
        _registry.clear()


class TestStartBackgroundCommand:
    def test_returns_command_id(self):
        task = _make_task()
        cid = start_background_command(
            task=task, command='echo hello',
            execute=lambda t, _ctx: '$ echo hello\nhello\n[exit code: 0]')
        assert cid.startswith('bg_')

    def test_command_completes(self):
        task = _make_task()
        done = threading.Event()

        def _execute(t, _ctx):
            done.wait(timeout=5)
            return '$ echo done\ndone\n[exit code: 0]'

        cid = start_background_command(
            task=task, command='echo done', execute=_execute)
        assert check_background_command(cid)['status'] == 'running'
        done.set()
        # Wait for the worker thread to finish
        for _ in range(50):
            if check_background_command(cid)['status'] == 'completed':
                break
            time.sleep(0.05)
        info = check_background_command(cid)
        assert info['status'] == 'completed'
        assert 'done' in info['result']

    def test_error_in_execute_captured(self):
        task = _make_task()

        def _fail(t, _ctx):
            raise RuntimeError('boom')

        cid = start_background_command(
            task=task, command='fail', execute=_fail)
        for _ in range(50):
            if check_background_command(cid)['status'] == 'completed':
                break
            time.sleep(0.05)
        info = check_background_command(cid)
        assert info['status'] == 'completed'
        assert 'RuntimeError' in info['result']


def test_delivery_failure_is_queryable_separately_from_execution(monkeypatch):
    monkeypatch.setattr(
        'lib.tasks_pkg.handlers._background_command._queue_completion',
        lambda **kwargs: False)
    cid = start_background_command(
        task=_make_task(convId='conv-delivery', _userId=7),
        command='echo finished', execute=lambda _, _ctx: 'finished [exit code: 0]')
    for _ in range(50):
        info = check_background_command(cid, user_id=7)
        if info['delivery_status'] == 'failed':
            break
        time.sleep(0.01)
    assert info['status'] == 'completed'
    assert info['delivery_status'] == 'failed'
    assert info['result'] == 'finished [exit code: 0]'
    assert check_background_command(cid, user_id=8) is None


class TestCheckBackgroundCommand:
    def test_unknown_returns_none(self):
        assert check_background_command('bg_nonexistent') is None

    def test_running_status(self):
        task = _make_task()
        blocker = threading.Event()
        cid = start_background_command(
            task=task, command='sleep 60',
            execute=lambda t, _ctx: blocker.wait(10) or 'done')
        info = check_background_command(cid)
        assert info['status'] == 'running'
        assert info['elapsed_s'] >= 0
        blocker.set()

    def test_completed_has_result(self):
        task = _make_task()
        cid = start_background_command(
            task=task, command='echo test',
            execute=lambda t, _ctx: '$ echo test\ntest\n[exit code: 0]')
        for _ in range(50):
            info = check_background_command(cid)
            if info['status'] == 'completed':
                break
            time.sleep(0.05)
        assert info['result'] is not None
        assert 'test' in info['result']


class TestWaitBackgroundCommand:
    def test_wait_blocks_until_completion(self):
        task = _make_task()
        release = threading.Event()

        def _execute(t, _ctx):
            release.wait(5)
            return 'finished'

        cid = start_background_command(
            task=task, command='slow', execute=_execute)
        threading.Timer(0.2, release.set).start()
        t0 = time.monotonic()
        info = check_background_command(cid, wait_s=5)
        waited = time.monotonic() - t0
        assert info['status'] == 'completed'
        assert 'finished' in info['result']
        assert 0.1 < waited < 3

    def test_wait_timeout_returns_running_snapshot(self):
        task = _make_task()
        blocker = threading.Event()
        cid = start_background_command(
            task=task, command='stuck',
            execute=lambda t, _ctx: blocker.wait(5) or 'done')
        t0 = time.monotonic()
        info = check_background_command(cid, wait_s=0.2)
        waited = time.monotonic() - t0
        assert info['status'] == 'running'
        assert 0.1 < waited < 2
        blocker.set()

    def test_wait_cap_clamps(self, monkeypatch):
        monkeypatch.setattr(
            'lib.tasks_pkg.background_commands.WAIT_BACKGROUND_COMMAND_MAX_S',
            0.1)
        task = _make_task()
        blocker = threading.Event()
        cid = start_background_command(
            task=task, command='stuck',
            execute=lambda t, _ctx: blocker.wait(5) or 'done')
        t0 = time.monotonic()
        info = check_background_command(cid, wait_s=60)
        waited = time.monotonic() - t0
        assert info['status'] == 'running'
        assert waited < 2
        blocker.set()

    def test_wait_unknown_returns_none_immediately(self):
        t0 = time.monotonic()
        assert check_background_command('bg_nope', wait_s=5) is None
        assert time.monotonic() - t0 < 1

    def test_completed_result_reread_is_idempotent(self):
        task = _make_task()
        cid = start_background_command(
            task=task, command='echo x', execute=lambda t, _ctx: 'result-x')
        first = check_background_command(cid, wait_s=5)
        assert first['status'] == 'completed'
        second = check_background_command(cid, wait_s=5)
        assert second['result'] == first['result'] == 'result-x'


class TestKillBackgroundCommand:
    def test_kill_running(self):
        task = _make_task()
        blocker = threading.Event()

        def _execute(t, _ctx):
            # Simulate the abort-poll pattern
            for _ in range(200):
                if t.get('aborted'):
                    return '$ cmd\n\n[Command aborted by user]\n[exit code: -1]'
                time.sleep(0.01)
            return '$ cmd\n\n[exit code: 0]'

        cid = start_background_command(
            task=task, command='long cmd', execute=_execute)
        assert kill_background_command(cid) is True
        for _ in range(50):
            if check_background_command(cid)['status'] == 'completed':
                break
            time.sleep(0.05)
        info = check_background_command(cid)
        assert 'aborted' in info['result'].lower()

    def test_kill_unknown(self):
        assert kill_background_command('bg_nonexistent') is False

    def test_kill_already_finished(self):
        task = _make_task()
        cid = start_background_command(
            task=task, command='echo fast',
            execute=lambda t, _ctx: 'done')
        for _ in range(50):
            if check_background_command(cid)['status'] == 'completed':
                break
            time.sleep(0.05)
        assert kill_background_command(cid) is False


class TestListBackgroundCommands:
    def test_empty(self):
        assert list_background_commands() == []

    def test_lists_running_and_completed(self):
        task = _make_task()
        blocker = threading.Event()
        cid1 = start_background_command(
            task=task, command='fast',
            execute=lambda t, _ctx: 'done')
        cid2 = start_background_command(
            task=task, command='slow',
            execute=lambda t, _ctx: blocker.wait(10) or 'done')
        for _ in range(50):
            if check_background_command(cid1)['status'] == 'completed':
                break
            time.sleep(0.05)
        cmds = list_background_commands()
        assert len(cmds) == 2
        statuses = {c['command_id']: c['status'] for c in cmds}
        assert statuses[cid1] == 'completed'
        assert statuses[cid2] == 'running'
        blocker.set()


class TestHistoryAndAdmission:
    def test_live_commands_exceed_old_ceiling_and_remain_controllable(self, monkeypatch):
        from lib.tasks_pkg import background_commands as bg
        monkeypatch.setattr(bg, 'tool_result_cache_capacity', lambda: 2)
        release = threading.Event()
        ids = []
        workers = []
        try:
            for i in range(40):
                ids.append(start_background_command(
                    task=_make_task(_userId=7), command=f'wait {i}',
                    execute=lambda task, ctx: release.wait(10) and 'done'))
            names = {f'bg-cmd-{cid[3:]}' for cid in ids}
            workers = [t for t in threading.enumerate() if t.name in names]
            assert len(list_background_commands(user_id=7)) == 40
            assert all(check_background_command(cid, user_id=7)['status'] == 'running'
                       for cid in ids)
            assert kill_background_command(ids[0], user_id=7)
            assert not kill_background_command(ids[1], user_id=8)
        finally:
            release.set()
            for worker in workers:
                worker.join(5)
                assert not worker.is_alive()
        assert len(list_background_commands(user_id=7)) == 2

    def test_failed_delivery_does_not_block_execution_or_handoff(self, monkeypatch):
        from lib.tasks_pkg import background_commands as bg
        monkeypatch.setattr(bg, 'tool_result_cache_capacity', lambda: 2)
        monkeypatch.setattr('lib.tasks_pkg.handlers._background_command._queue_completion',
                            lambda **kwargs: False)
        release = threading.Event()
        ids = []
        workers = []
        try:
            for i in range(40):
                ids.append(start_background_command(
                    task=_make_task(convId='conv', _userId=7), command=f'wait {i}',
                    execute=lambda task, ctx: release.wait(10) and 'done'))
            names = {f'bg-cmd-{cid[3:]}' for cid in ids}
            workers = [t for t in threading.enumerate() if t.name in names]
        finally:
            release.set()
            for worker in workers:
                worker.join(5)
                assert not worker.is_alive()
        assert all(check_background_command(cid, user_id=7)['delivery_status'] == 'failed'
                   for cid in ids)
        assert not bg.conversation_results_complete(conv_id='conv', user_id=7)
        extra = start_background_command(task={}, command='next', execute=lambda t, ctx: 'ok')
        assert check_background_command(extra, wait_s=3)['result'] == 'ok'
        task = _make_task(convId='conv', _userId=7)
        assert bg.register_handed_off_command(command_id='handoff', task=task,
                                               command='live', done=threading.Event())
        assert check_background_command('handoff', user_id=7)['status'] == 'running'
        bg.unregister_handed_off_command(command_id='handoff', task=task)
        monkeypatch.setattr('lib.tasks_pkg.handlers._background_command._queue_completion',
                            lambda **kwargs: True)
        monkeypatch.setattr(bg, 'wake_completed_background_batch', lambda *args: None)
        assert bg.retry_failed_background_deliveries() == 40
        assert bg.conversation_results_complete(conv_id='conv', user_id=7)
        assert len(list_background_commands(user_id=7)) <= 2

    def test_handoff_exceeds_old_ceiling(self):
        from lib.tasks_pkg import background_commands as bg
        task = _make_task(_userId=7)
        for i in range(40):
            assert bg.register_handed_off_command(command_id=f'handoff-{i}', task=task,
                                                   command='live', done=threading.Event())
        assert len(list_background_commands(user_id=7)) == 40
        for i in range(40):
            bg.unregister_handed_off_command(command_id=f'handoff-{i}', task=task)
        assert list_background_commands(user_id=7) == []


class TestSchema:
    def test_run_command_has_background_param(self):
        from lib.tools.project import PROJECT_TOOL_RUN_COMMAND
        props = PROJECT_TOOL_RUN_COMMAND['function']['parameters'][
            'properties']
        assert 'background' in props
        assert props['background']['type'] == 'boolean'

    def test_check_background_schema(self):
        from lib.tools.project import PROJECT_TOOL_CHECK_BACKGROUND
        assert PROJECT_TOOL_CHECK_BACKGROUND['function'][
            'name'] == 'check_background_command'
        assert 'command_id' in PROJECT_TOOL_CHECK_BACKGROUND[
            'function']['parameters']['required']

    def test_check_background_has_wait_s(self):
        from lib.tools.project import PROJECT_TOOL_CHECK_BACKGROUND
        props = PROJECT_TOOL_CHECK_BACKGROUND['function']['parameters'][
            'properties']
        assert props['wait_s']['type'] == 'integer'
        assert 'command_id' in PROJECT_TOOL_CHECK_BACKGROUND[
            'function']['parameters']['required']
        assert 'wait_s' not in PROJECT_TOOL_CHECK_BACKGROUND[
            'function']['parameters']['required']

    def test_kill_background_schema(self):
        from lib.tools.project import PROJECT_TOOL_KILL_BACKGROUND
        assert PROJECT_TOOL_KILL_BACKGROUND['function'][
            'name'] == 'kill_background_command'
        assert 'command_id' in PROJECT_TOOL_KILL_BACKGROUND[
            'function']['parameters']['required']

    def test_tools_in_project_names(self):
        from lib.tools.project import PROJECT_TOOL_NAMES
        assert 'check_background_command' in PROJECT_TOOL_NAMES
        assert 'kill_background_command' in PROJECT_TOOL_NAMES

    def test_tools_in_unified_list(self):
        from lib.tools.project import PROJECT_TOOLS_UNIFIED
        names = [t['function']['name'] for t in PROJECT_TOOLS_UNIFIED]
        # Retired from the visible surface: unified poll/kill absorb them.
        assert 'check_background_command' not in names
        assert 'kill_background_command' not in names

    def test_poll_kill_schemas(self):
        from lib.tools.poll import KILL_TOOL, POLL_TOOL
        assert POLL_TOOL['function']['name'] == 'poll'
        assert KILL_TOOL['function']['name'] == 'kill'
        poll_props = POLL_TOOL['function']['parameters']['properties']
        assert {'id', 'ids', 'wait_s'} <= set(poll_props)
        assert 'id' in KILL_TOOL['function']['parameters']['properties']

    def test_run_command_has_no_timeout_param(self):
        from lib.tools.project import PROJECT_TOOL_RUN_COMMAND
        props = PROJECT_TOOL_RUN_COMMAND['function']['parameters'][
            'properties']
        assert 'timeout_s' not in props
        assert 'timeout' not in props


class TestLifecycle:
    def test_fast_completion_uses_existing_delivery_once(self):
        delivered = threading.Event()
        calls = []

        def delivery(**kwargs):
            calls.append(kwargs)
            delivered.set()

        with patch('lib.tasks_pkg.handlers._background_command._queue_completion',
                   side_effect=delivery):
            cid = start_background_command(
                task=_make_task(convId='conv-owner', _userId=7),
                config={'_turnId': 'old', 'model': 'test'},
                command='fast', execute=lambda t, _ctx: 'result')
            assert delivered.wait(3)
        assert len(calls) == 1
        assert calls[0]['command_id'] == cid
        assert calls[0]['task'] == {'convId': 'conv-owner', '_userId': 7}
        assert calls[0]['config'] == {'model': 'test'}
        assert check_background_command(cid, user_id=7)['result'] == 'result'
        assert check_background_command(cid) is None
        assert check_background_command(cid, user_id=8) is None
        assert list_background_commands(user_id=8) == []
        with _lock:
            assert _registry[cid]['task'] is None

    def test_owner_and_stop_isolation_without_parent_objects(self):
        ready, release = threading.Event(), threading.Event()
        observed = {}
        parent = _make_task(_userId=7, aborted=True, _subprocess_pid=123,
                            _subprocess_pgid=123, _cmd_interrupt='stop',
                            messages=[object()], toolRounds=[object()])

        def execute(task, _ctx):
            observed.update(task)
            ready.set()
            release.wait(3)
            return str(task.get('aborted'))

        cid = start_background_command(task=parent, command='wait', execute=execute)
        try:
            assert ready.wait(3)
            assert observed['aborted'] is False
            assert not {'messages', 'toolRounds', '_subprocess_pid',
                        '_subprocess_pgid', '_cmd_interrupt'} & observed.keys()
            assert not kill_background_command(cid, user_id=8)
            assert not kill_background_command(cid)
            assert kill_background_command(cid, user_id=7)
            assert parent['_subprocess_pid'] == 123
        finally:
            release.set()
        assert _registry[cid]['done'].wait(3)
        assert check_background_command(cid, user_id=7)['result'] == 'True'

    def test_thread_start_failure_removes_control_and_durable_record(self, monkeypatch, tmp_path):
        monkeypatch.setattr("lib.tasks_pkg.background_commands._records_root", lambda: str(tmp_path))
        with patch('lib.tasks_pkg.background_commands.threading.Thread.start',
                   side_effect=RuntimeError('cannot start')):
            with pytest.raises(RuntimeError, match='cannot start'):
                start_background_command(task={}, command='never',
                                         execute=lambda t, _ctx: pytest.fail('executed'))
        assert list_background_commands() == []
        assert list(tmp_path.iterdir()) == []

    def test_failure_stores_text_not_traceback(self):
        def execute(task, _ctx):
            raise ValueError('failed')
        cid = start_background_command(task={}, command='fail', execute=execute)
        assert _registry[cid]['done'].wait(3)
        assert _registry[cid]['state']['error'] == 'ValueError: failed'
