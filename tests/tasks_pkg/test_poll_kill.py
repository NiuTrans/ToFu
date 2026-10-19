"""Unified poll/kill handlers + background-command durability records."""

from __future__ import annotations

from copy import deepcopy
import json
import os
import subprocess
import threading
import time
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg import background_commands
from lib.tasks_pkg.background_commands import (
    check_background_command,
    kill_background_command,
    start_background_command,
    _lock,
    _registry,
)
from lib.tasks_pkg.executor import _execute_tool_one
from lib.tasks_pkg.handlers import poll as poll_mod

_UID = 777777


@pytest.fixture(autouse=True)
def _clean_registry():
    with _lock:
        _registry.clear()
    yield
    with _lock:
        _registry.clear()


@pytest.fixture
def dispatch_task():
    return {'id': 'poll-dispatch', 'convId': 'poll-conv', '_userId': _UID,
            'aborted': False, 'toolRounds': []}


@pytest.fixture
def dispatch(monkeypatch, tmp_path, dispatch_task):
    task = dispatch_task
    finalized = Mock()
    monkeypatch.setattr(poll_mod, '_finalize_tool_round', finalized)

    def execute(name, args):
        entry = {'toolCallId': 'tc-p', 'toolName': name, 'status': 'searching'}
        result = _execute_tool_one(
            task, {}, name, 'tc-p', args, 1, entry, {},
            str(tmp_path), True)
        assert result[0] == 'tc-p'
        assert result[2] is False
        assert finalized.call_count >= 1
        return result[1]

    return execute


def _task(**overrides):
    task = {'id': 'poll-test', 'convId': '', '_userId': _UID}
    task.update(overrides)
    return task


def _wait_done(cid, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = check_background_command(cid, user_id=_UID)
        if info and info['status'] == 'completed':
            return info
        time.sleep(0.05)
    raise AssertionError(f'{cid} did not complete')


class TestPollHandler:
    def test_dashboard_empty(self, dispatch):
        assert dispatch('poll', {}) == 'No background work is currently running.'

    def test_poll_completed_command(self, dispatch):
        cid = start_background_command(
            task=_task(), command='echo hi',
            execute=lambda t, _ctx: '$ echo hi\nhi\n[exit code: 0]')
        _wait_done(cid)
        content = dispatch('poll', {'id': cid})
        assert 'completed' in content
        assert 'hi' in content

    def test_poll_legacy_command_id_alias(self, dispatch):
        cid = start_background_command(
            task=_task(), command='echo hi',
            execute=lambda t, _ctx: 'done')
        _wait_done(cid)
        assert 'completed' in dispatch('poll', {'command_id': cid})

    def test_poll_unknown_lists_live_handles(self, dispatch):
        blocker = threading.Event()
        cid = start_background_command(
            task=_task(), command='sleepy',
            execute=lambda t, _ctx: blocker.wait(5) or 'done')
        try:
            content = dispatch('poll', {'id': 'bg_doesnotexist'})
            assert 'Unknown handle' in content
            assert cid in content
        finally:
            blocker.set()

    def test_poll_running_snapshot(self, dispatch):
        blocker = threading.Event()
        cid = start_background_command(
            task=_task(), command='sleepy',
            execute=lambda t, _ctx: blocker.wait(5) or 'done')
        try:
            content = dispatch('poll', {'id': cid})
            assert 'still running' in content
        finally:
            blocker.set()


class TestKillHandler:
    def test_kill_running_command(self, dispatch):
        blocker = threading.Event()
        cid = start_background_command(
            task=_task(), command='sleepy',
            execute=lambda t, _ctx: blocker.wait(5) or 'done')
        try:
            content = dispatch('kill', {'id': cid})
            assert 'kill signalled' in content
        finally:
            blocker.set()

    def test_kill_finished_command(self, dispatch):
        cid = start_background_command(
            task=_task(), command='fast', execute=lambda t, _ctx: 'done')
        _wait_done(cid)
        assert 'already finished' in dispatch('kill', {'id': cid})

    def test_kill_unknown(self, dispatch):
        assert 'unknown handle' in dispatch('kill', {'id': 'bg_nope'})

    def test_kill_requires_id(self, dispatch):
        assert 'id is required' in dispatch('kill', {})


@pytest.fixture
def swarm_recovery(monkeypatch, tmp_path, dispatch_task):
    from lib.swarm import persistence
    from lib.swarm.integration import _logs, _rehydrate, _state, _tools
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    dispatch_task['_tenant_id'] = 'poll-tenant'
    row = {
        'swarm_key': dispatch_task['convId'], 'conv_id': dispatch_task['convId'],
        'task_id': 'spawn-turn', 'status': 'terminated',
        'config': {'user_id': _UID, 'parent_cfg': {'_tenant_id': 'poll-tenant'}},
        'agents': [{'agent_id': 'child', 'role': 'coder', 'status': 'completed',
                    'result': {'status': 'completed', 'final_answer': 'stored answer'}}],
    }
    loader = Mock(return_value=row)
    session_lookup = Mock(return_value=None)
    monkeypatch.setattr(persistence, 'load_session', loader)
    monkeypatch.setattr(_logs, 'SWARM_OUTPUT_DIR', str(tmp_path))
    for module in (_state, _tools):
        monkeypatch.setattr(module, '_get_session', session_lookup)
        monkeypatch.setattr(module, 'add_session_alias', Mock())
    monkeypatch.setattr(poll_mod, '_owned_task', Mock(return_value=None))
    monkeypatch.setattr(chat_task_runtime, 'snapshot_owned', Mock(return_value=[]))

    # A result read cannot create/resume workers or change durable evidence.
    mutations = []
    for module, name in ((_rehydrate, 'MasterOrchestrator'),
                         (persistence, 'save_session'),
                         (persistence, 'delete_session'),
                         (poll_mod, '_await_swarm')):
        mutation = Mock(side_effect=AssertionError('recovery must be read-only'))
        monkeypatch.setattr(module, name, mutation)
        mutations.append(mutation)
    yield row, loader, session_lookup, tmp_path
    for mutation in mutations:
        mutation.assert_not_called()


def _swarm_log(root, task_id, text):
    path = root / task_id / 'child.log'
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding='utf-8')


def _swarm_payload(content):
    assert 'recovered swarm evidence' in content
    return json.loads(content.split('\n', 1)[1])


class TestSwarmRecovery:
    @pytest.mark.parametrize('wait_s', [0, 60])
    @pytest.mark.parametrize('status,outcome', [
        ('completed', 'completed'), ('failed', 'failed'), ('running', 'unknown'),
    ])
    def test_owned_checkpoint_retains_outcome_without_waiting(
            self, dispatch, swarm_recovery, status, outcome, wait_s):
        row, loader, _, root = swarm_recovery
        row['agents'][0]['status'] = status
        before = deepcopy(row)
        _swarm_log(root, row['task_id'], 'raw stream is not a final answer')

        payload = _swarm_payload(dispatch('poll', {'id': 'child', 'wait_s': wait_s}))
        assert payload['agent_status'] == outcome
        assert payload['source'] == 'persisted' and payload['recovered'] is True
        assert payload['final_answer'] == ('stored answer' if outcome == 'completed' else '')
        if outcome == 'completed':
            assert 'partial_output' not in payload
        else:
            assert payload['partial_output'] == 'stored answer'
        assert 'raw stream' not in json.dumps(payload)
        loader.assert_called_once_with('poll-conv')
        assert row == before

    def test_log_only_is_unknown_partial_not_success(
            self, dispatch, dispatch_task, swarm_recovery):
        _, loader, _, root = swarm_recovery
        loader.return_value = None
        _swarm_log(root, 'foreign-turn', 'foreign final answer')
        assert 'Unknown handle' in dispatch('poll', {'id': 'child'})
        _swarm_log(root, dispatch_task['id'], 'completed successfully')

        payload = _swarm_payload(dispatch('poll', {'id': 'child', 'wait_s': 60}))
        assert payload['agent_status'] == 'unknown'
        assert payload['source'] == 'disk'
        assert payload['final_answer'] == ''
        assert payload['partial_output'] == 'completed successfully'
        assert 'foreign final answer' not in json.dumps(payload)

    @pytest.mark.parametrize('mismatch', ['owner', 'ownerless', 'tenant', 'conversation'])
    def test_foreign_checkpoint_denies_even_current_task_logs(
            self, dispatch, dispatch_task, swarm_recovery, mismatch):
        row, loader, _, root = swarm_recovery
        row['agents'][0]['result']['final_answer'] = 'private answer'
        _swarm_log(root, dispatch_task['id'], 'private current-task log')
        if mismatch == 'owner':
            row['config']['user_id'] = _UID + 1
        elif mismatch == 'ownerless':
            row['config'].pop('user_id')
        elif mismatch == 'tenant':
            row['config']['parent_cfg']['_tenant_id'] = 'foreign'
        else:
            row['conv_id'] = 'foreign'

        content = dispatch('poll', {'id': 'child'})
        assert 'Unknown handle' in content
        assert 'private' not in content
        loader.assert_called_once_with('poll-conv')

    @pytest.mark.parametrize('same_task', [True, False])
    def test_without_conversation_recovery_requires_exact_task(
            self, dispatch, dispatch_task, swarm_recovery, same_task):
        row, loader, _, _ = swarm_recovery
        dispatch_task['convId'] = ''
        row['task_id'] = dispatch_task['id'] if same_task else 'foreign-turn'
        content = dispatch('poll', {'id': 'child'})
        if same_task:
            assert _swarm_payload(content)['final_answer'] == 'stored answer'
        else:
            assert 'Unknown handle' in content
            assert 'stored answer' not in content
        loader.assert_called_once_with(dispatch_task['id'])

    @pytest.mark.parametrize('mismatch', ['owner', 'tenant', 'conversation'])
    def test_foreign_live_session_denies_before_durable_fallback(
            self, dispatch, swarm_recovery, mismatch):
        _, loader, lookup, _ = swarm_recovery
        session = Mock(user_id=_UID, conv_id='poll-conv', task_id='spawn-turn',
                       _parent_task_proxy={'_tenant_id': 'poll-tenant'})
        if mismatch == 'owner':
            session.user_id = _UID + 1
        elif mismatch == 'tenant':
            session._parent_task_proxy['_tenant_id'] = 'foreign'
        else:
            session.conv_id = 'foreign'
        lookup.return_value = session

        assert 'Unknown handle' in dispatch('poll', {'id': 'child'})
        assert 'unknown handle' in dispatch('kill', {'id': 'child'})
        loader.assert_not_called()
        session.get_status.assert_not_called()
        session.get_agent_result.assert_not_called()
        session.kill_agent.assert_not_called()

    @pytest.mark.parametrize('status', ['completed', 'failed', 'running'])
    def test_recovered_handle_is_not_cancellable(
            self, dispatch, swarm_recovery, status):
        row, loader, _, _ = swarm_recovery
        row['agents'][0]['status'] = status
        before = deepcopy(row)
        assert _swarm_payload(dispatch('poll', {'id': 'child'}))['recovered'] is True
        loader.reset_mock()

        content = dispatch('kill', {'id': 'child'})
        assert 'unknown handle' in content
        assert 'kill signalled' not in content
        loader.assert_not_called()
        assert row == before

    @pytest.mark.parametrize('kind', ['command', 'swarm', 'task'])
    def test_in_memory_handles_do_not_read_durable_recovery(
            self, dispatch, swarm_recovery, monkeypatch, kind):
        _, loader, lookup, _ = swarm_recovery
        from lib.swarm.integration import _recovery
        read_log = Mock(side_effect=AssertionError('live path must not read logs'))
        monkeypatch.setattr(_recovery, '_read_agent_log', read_log)
        if kind == 'command':
            monkeypatch.setattr(background_commands, 'check_background_command', Mock(
                return_value={'command_id': 'child', 'status': 'completed',
                              'elapsed_s': 1, 'result': 'live answer'}))
        elif kind == 'swarm':
            session = Mock(user_id=_UID, conv_id='poll-conv', task_id='spawn-turn',
                           _parent_task_proxy={'_tenant_id': 'poll-tenant'})
            session.get_status.return_value = {'child': {'status': 'completed'}}
            session.get_agent_result.return_value = {
                'found': True, 'status': 'completed', 'final_answer': 'live answer'}
            lookup.return_value = session
        else:
            monkeypatch.setattr(poll_mod, '_owned_task', Mock(return_value={'id': 'child'}))
            monkeypatch.setattr('lib.scheduler.executor._await._execute_await_task',
                                Mock(return_value='live answer'))

        assert 'live answer' in dispatch('poll', {'id': 'child'})
        loader.assert_not_called()
        read_log.assert_not_called()


class TestNameRepair:
    def test_legacy_names_map_to_poll_kill(self):
        from lib.tool_input_repair._classify import _TOOL_NAME_ALIASES
        assert _TOOL_NAME_ALIASES['check_background_command'] == 'poll'
        assert _TOOL_NAME_ALIASES['await_agents'] == 'poll'
        assert _TOOL_NAME_ALIASES['await_task'] == 'poll'
        assert _TOOL_NAME_ALIASES['kill_background_command'] == 'kill'


@pytest.mark.skipif(os.name != 'posix', reason='durable records are POSIX-only')
class TestDurableRecords:
    def _craft_record(self, root, cid, *, command='printf hi', pid=None,
                      pgid=None, starttime=None, exit_code=None,
                      stdout='hi\n', stderr=''):
        import json
        paths = background_commands._record_paths(os.path.join(root, cid))
        os.makedirs(paths['dir'])
        background_commands._write_record(paths, {
            'command_id': cid, 'command': command, 'conv_id': 'conv-x',
            'user_id': _UID, 'started_at': time.time() - 1,
            'pid': pid, 'pgid': pgid, 'pid_starttime': starttime,
        })
        with open(paths['stdout'], 'w') as fh:
            fh.write(stdout)
        with open(paths['stderr'], 'w') as fh:
            fh.write(stderr)
        if exit_code is not None:
            with open(paths['exit'], 'w') as fh:
                fh.write(str(exit_code))
        return paths

    @pytest.fixture
    def records_root(self, monkeypatch, tmp_path):
        root = str(tmp_path / 'bg_records')
        monkeypatch.setattr(background_commands, '_records_root',
                            lambda: root)
        return root

    @pytest.fixture
    def delivered(self, monkeypatch):
        calls = []

        def _queue_completion(**kwargs):
            calls.append(kwargs)
            return True

        monkeypatch.setattr(
            'lib.tasks_pkg.handlers._background_command._queue_completion',
            _queue_completion)
        return calls

    def test_completed_record_delivered_and_removed(self, records_root,
                                                    delivered):
        paths = self._craft_record(records_root, 'bg_done1', exit_code=0)
        settled = background_commands.recover_background_commands_on_startup()
        assert settled == 1
        assert len(delivered) == 1
        assert delivered[0]['command_id'] == 'bg_done1'
        assert 'hi' in delivered[0]['result']
        assert '[exit code: 0]' in delivered[0]['result']
        assert not os.path.exists(paths['dir'])

    def test_dead_pid_record_reports_died(self, records_root, delivered):
        # Deterministic "dead": our own live pid with a bogus start-time —
        # the PID-reuse guard can never match it.
        paths = self._craft_record(
            records_root, 'bg_dead1', pid=os.getpid(), pgid=None,
            starttime='bogus-starttime')
        settled = background_commands.recover_background_commands_on_startup()
        assert settled == 1
        assert len(delivered) == 1
        assert 'no exit receipt is available' in delivered[0]['result']
        assert 'effects are unconfirmed' in delivered[0]['result']
        assert 'do not automatically replay writes' in delivered[0]['result']
        assert 'hi' in delivered[0]['result']  # partial output preserved
        assert not os.path.exists(paths['dir'])

    def test_live_record_reattaches_and_kill_stops_it(self, records_root,
                                                      delivered):
        proc = subprocess.Popen(['sleep', '30'], start_new_session=True)
        try:
            paths = self._craft_record(
                records_root, 'bg_live1', pid=proc.pid, pgid=proc.pid,
                starttime=background_commands._pid_starttime(proc.pid))
            settled = background_commands.recover_background_commands_on_startup()
            assert settled == 1
            assert not delivered  # nothing finished yet
            # Queryable via the ordinary registry path…
            info = check_background_command('bg_live1', user_id=_UID)
            assert info is not None and info['status'] == 'running'
            # …and stoppable via the ordinary kill path.
            assert kill_background_command('bg_live1', user_id=_UID)
            proc.wait(timeout=15)
            assert _registry['bg_live1']['done'].wait(15)
            info = check_background_command('bg_live1', user_id=_UID)
            assert info['status'] == 'completed'
            assert 'aborted' in (info['result'] or '').lower()
            # done.set() precedes delivery/removal inside the worker — poll.
            deadline = time.time() + 10
            while time.time() < deadline and (
                    len(delivered) < 1 or os.path.exists(paths['dir'])):
                time.sleep(0.1)
            assert len(delivered) == 1
            assert not os.path.exists(paths['dir'])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_wrapped_execution_round_trip(self, records_root, delivered):
        """The POSIX wrapper captures streams + exit code into the record."""

        def execute(task, ctx):
            assert ctx['wrapped_command']
            subprocess.run(['sh', '-c', ctx['wrapped_command']], check=False)
            return ctx['build_result']()

        cid = start_background_command(
            task=_task(), command='printf hello', execute=execute)
        info = _wait_done(cid)
        assert info['result'] == '$ printf hello\nhello\n\n[exit code: 0]'
        # Headless (no conversation): delivered nowhere → record settled.
        assert not os.path.exists(os.path.join(records_root, cid))

    def test_wrapper_preserves_failure_exit_code(self, records_root,
                                                 delivered):
        def execute(task, ctx):
            subprocess.run(['sh', '-c', ctx['wrapped_command']], check=False)
            return ctx['build_result']()

        cid = start_background_command(
            task=_task(), command='echo out; echo oops >&2; exit 3',
            execute=execute)
        info = _wait_done(cid)
        assert '[stderr]' in info['result']
        assert 'oops' in info['result']
        assert '[exit code: 3]' in info['result']
