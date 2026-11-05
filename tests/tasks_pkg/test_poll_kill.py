"""Unified poll/kill handlers + background-command durability records."""

from __future__ import annotations

from copy import deepcopy
import json
import os
import re
import subprocess
import threading
import time
from types import SimpleNamespace
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
    # Execution admission reads the owner's Coordinator binding for the project
    # first (failing closed without one); this is an ordinary unbound conversation.
    from lib.conversations import project_coordination
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: SimpleNamespace(read=lambda path: {
                            'projectPath': path, 'conversationId': '', 'revision': 0,
                            'availability': 'unbound'}))

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


def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError('condition was not reached')


def _wait_done(cid, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = check_background_command(cid, user_id=_UID)
        if info and info['status'] == 'completed':
            return info
        time.sleep(0.05)
    raise AssertionError(f'{cid} did not complete')


def _process_running(pid):
    try:
        with open(f'/proc/{pid}/stat', 'rb') as stream:
            state = stream.read().rsplit(b')', 1)[-1].split()[0]
    except FileNotFoundError:
        return False
    return state not in (b'Z', b'X')


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
            # A command that is not waiting for admission keeps its exact bytes.
            assert re.fullmatch(
                rf'== {cid} \(background command\) ==\n'
                r'still running \(elapsed \d+(\.\d+)?s\)\. Command: sleepy', content)
        finally:
            blocker.set()


    def test_storage_free_command_publishes_nothing_and_stays_pollable(
            self, dispatch, monkeypatch):
        task = _task(config={'_storageFreeRuntime': True})
        monkeypatch.setattr(background_commands, 'deliver_completion',
                            lambda **kwargs: pytest.fail(
                                'storage-free completion entered application delivery'))
        command_id = start_background_command(
            task=task, command='echo storage-free',
            execute=lambda _task, _ctx: 'storage-free result')
        content = dispatch('poll', {'id': command_id, 'wait_s': 5})
        assert 'completed' in content
        assert 'storage-free result' in content
        assert check_background_command(command_id, user_id=_UID)[
            'delivery_status'] == 'not_applicable'


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


def _worker_catalog():
    from lib.tools.poll import KILL_TOOL, POLL_TOOL
    from lib.tools.project import PROJECT_TOOL_RUN_COMMAND
    return deepcopy([PROJECT_TOOL_RUN_COMMAND, POLL_TOOL, KILL_TOOL])


@pytest.fixture
def leaf_worker(monkeypatch):
    """A real delegated child invocation holding run_command, poll and kill."""
    from lib.tasks_pkg.child_invocation import create_child_invocation
    from lib.tasks_pkg.manager import discard_task
    child = create_child_invocation(
        {'id': 'coordinator-task', 'convId': 'coordinator-conv', '_userId': _UID,
         '_transientRuntime': True,
         'config': {'memoryEnabled': False, 'searchMode': 'off'}},
        model='fixture-model', tools=_worker_catalog(), objective='worker objective',
        event_sink=lambda event: None, abort_check=lambda: False)
    child['_executable_tool_catalog'] = _worker_catalog()
    monkeypatch.setattr(poll_mod, '_finalize_tool_round', Mock())
    yield child
    discard_task(child['id'])


def _handle(name, task, **args):
    handler = poll_mod._handle_poll if name == 'poll' else poll_mod._handle_kill
    return handler(task, {}, name, f'tc-{name}', args, 1, {}, {}, None, False)[1]


class TestLeafWorkerScope:
    """A leaf worker's poll/kill reach only the bg_* commands it launched."""

    def test_poll_waits_on_its_own_command_and_returns_the_result(
            self, leaf_worker, monkeypatch):
        release = threading.Event()
        command_id = start_background_command(
            task=leaf_worker, command='make test',
            execute=lambda t, _ctx: release.wait(30) and '$ make test\n42 passed\n[exit code: 0]')
        real_check = background_commands.check_background_command

        def check(handle_id, **kwargs):
            if kwargs.get('wait_s'):
                release.set()  # Finish only once poll is actually waiting.
            return real_check(handle_id, **kwargs)

        monkeypatch.setattr(background_commands, 'check_background_command', check)
        content = _handle('poll', leaf_worker, id=command_id, wait_s=600)
        assert content.startswith(f'== {command_id} (background command) ==\ncompleted')
        assert '42 passed' in content
        listing = _handle('poll', leaf_worker)
        assert command_id in listing

    @pytest.mark.parametrize('foreign', [
        'coordinator_command', 'sibling_command', 'other_owner_command', 'agent', 'task'])
    @pytest.mark.parametrize('name', ['poll', 'kill'])
    def test_other_handles_are_refused_before_any_read_wait_or_signal(
            self, leaf_worker, monkeypatch, foreign, name):
        from lib.swarm.integration import _tools as swarm_tools
        from lib.tools.result_envelope import tool_result_error
        for module, attribute in ((poll_mod, '_swarm_session'), (poll_mod, '_owned_task'),
                                  (swarm_tools, '_resolve_one_agent_result')):
            monkeypatch.setattr(module, attribute, Mock(
                side_effect=AssertionError('a leaf worker must not reach another lane')))
        release = threading.Event()
        own = start_background_command(task=leaf_worker, command='own',
                                       execute=lambda t, _ctx: release.wait(30) and 'own')
        handle = {'agent': 'agent-coder-sibling', 'task': 'other-conversation-task'}.get(foreign)
        if handle is None:
            launcher = {
                'coordinator_command': {'id': 'coordinator-task', '_userId': _UID},
                'sibling_command': {'id': 'sibling-worker-task', '_userId': _UID},
                'other_owner_command': {'id': leaf_worker['id'], '_userId': _UID + 1},
            }[foreign]
            handle = start_background_command(
                task=launcher, command='foreign',
                execute=lambda t, _ctx: release.wait(30) and 'foreign')
        try:
            args = {'ids': [own, handle]}
            if name == 'poll':
                args['wait_s'] = 600
            content = _handle(name, leaf_worker, **args)
            error = tool_result_error(content)
            assert error is not None and error.code == 'background_handle_not_owned'
            assert error.retryable is False
            assert handle in error.message and own not in error.message
            assert 'poll with no id lists them' in error.next_action
            for command_id in (own, handle):
                with _lock:
                    entry = _registry.get(command_id)
                if entry is None:
                    continue  # An agent or task handle has no command entry.
                # Neither waited to completion nor signalled.
                assert not entry['done'].is_set()
                assert not entry['task'].get('aborted')
        finally:
            release.set()

    @pytest.mark.skipif(os.name != 'posix', reason='POSIX process tree')
    def test_kill_stops_its_own_process_tree(self, leaf_worker, monkeypatch, tmp_path):
        from lib.tasks_pkg.handlers import code_exec
        monkeypatch.setattr(code_exec, '_finalize_tool_round', Mock())
        pids = tmp_path / 'tree.pids'
        command = f'sleep 300 & echo $! >> {pids}; echo $$ >> {pids}; wait'
        _, receipt, _ = code_exec._handle_code_exec(
            leaf_worker, {}, 'run_command', 'tc-bg', {'command': command, 'background': True},
            1, {}, {}, None, False)
        command_id = re.search(r'bg_[0-9a-f]{12}', receipt).group(0)
        assert '`poll`' in receipt and '`kill`' in receipt
        assert 'collect its result before you finish' in receipt
        assert 'shown in the conversation' not in receipt
        tree = _until(lambda: (pids.exists() and len(pids.read_text().split()) == 2
                               and [int(pid) for pid in pids.read_text().split()]), timeout=60)
        assert all(_process_running(pid) for pid in tree)

        content = _handle('kill', leaf_worker, id=command_id)
        assert content == (f'{command_id}: kill signalled (background command; '
                           'process tree receives SIGTERM then SIGKILL).')
        info = _wait_done(command_id, timeout=60)
        assert '[Command aborted by user]' in info['result']
        assert not any(_process_running(pid) for pid in tree)

    def test_root_keeps_every_owner_scoped_handle(self, leaf_worker, monkeypatch):
        """The coordinator still reads and stops a worker's command by its ID."""
        monkeypatch.setattr(poll_mod, '_swarm_session', lambda task: None)
        monkeypatch.setattr(poll_mod, '_owned_task', lambda handle_id, task: None)
        release = threading.Event()
        command_id = start_background_command(
            task=leaf_worker, command='worker job',
            execute=lambda t, _ctx: release.wait(30) and 'done')
        root = {'id': 'coordinator-task', 'convId': '', '_userId': _UID,
                '_executable_tool_catalog': _worker_catalog()}
        try:
            assert command_id in _handle('poll', root)
            assert 'still running' in _handle('poll', root, id=command_id)
            assert 'kill signalled' in _handle('kill', root, id=command_id)
        finally:
            release.set()

    def test_delegated_run_that_coordinates_agents_keeps_its_handles(self, leaf_worker):
        from lib.swarm.tools import SPAWN_AGENTS_TOOL
        assert poll_mod._own_command_scope(leaf_worker) == leaf_worker['id']
        leaf_worker['_executable_tool_catalog'].append(deepcopy(SPAWN_AGENTS_TOOL))
        assert poll_mod._own_command_scope(leaf_worker) is None


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
        paths = background_commands._record_paths(os.path.join(root, cid))
        os.makedirs(paths['dir'])
        background_commands._write_record(paths, {
            'command_id': cid, 'command': command, 'conv_id': 'conv-x',
            'user_id': _UID, 'started_at': time.time() - 1, 'credentials': {},
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
        assert '\nhi\n' in delivered[0]['result']
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
        assert '\nhi\n' in delivered[0]['result']  # partial output preserved
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
        # Completion is published before settlement inside the worker, so
        # wait for the settled record; the per-test hang guard bounds it.
        record = os.path.join(records_root, cid)
        while os.path.exists(record):
            time.sleep(0.05)

    def test_group_warning_reaches_the_durable_result(self, records_root, delivered,
                                                     tmp_path):
        """A member the command leaves behind is stopped and reported in the result
        rebuilt from the record, beside the command's own output and exit code."""
        from lib.project_mod.run_command import tool_run_command
        child_file = tmp_path / 'child.pid'

        def execute(task, ctx):
            return ctx['build_result'](runner_result=tool_run_command(
                str(tmp_path), ctx['wrapped_command'], task=task))

        cid = start_background_command(
            task=_task(), command=f'sleep 300 & echo $! > {child_file}; echo done',
            execute=execute)
        info = _wait_done(cid, timeout=60)
        assert re.search(r'^done$', info['result'], re.M)
        assert info['result'].rstrip().endswith(
            '\n[warning process-group-lingering-stopped: the command exited but '
            'left processes running in its process group; they were stopped '
            '(SIGTERM, then SIGKILL). Start long-lived processes with '
            'run_command background=true.]\n[exit code: 0]')
        assert not _process_running(int(child_file.read_text()))

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


class TestLongPollWait:
    """A blocking poll costs no model request (owner decision 2026-10-28).

    It may wait up to the tool's declared cap, clamped below the stuck-task
    reaper, and it must still return on completion, a pending steer, or Stop.
    """

    @pytest.fixture
    def blocked_command(self):
        blocker = threading.Event()
        cid = start_background_command(
            task=_task(), command='sleepy',
            execute=lambda t, _ctx: blocker.wait(30) and 'done')
        yield cid, blocker
        blocker.set()

    def test_wait_cap_is_long_and_reaper_safe(self, monkeypatch):
        from lib.tasks_pkg.manager import reaper_safe_wait_s
        from lib.tools.poll import POLL_WAIT_CAP_S
        monkeypatch.delenv('TOFU_STUCK_TASK_MAX_SILENT_SECS', raising=False)
        assert POLL_WAIT_CAP_S == 1500
        assert poll_mod._coerce_wait_s({'wait_s': 10 ** 6}) == 1500
        assert poll_mod._coerce_wait_s({'wait_s': 90}) == 90
        assert poll_mod._coerce_wait_s({}) == 0
        monkeypatch.setenv('TOFU_STUCK_TASK_MAX_SILENT_SECS', '600')
        assert poll_mod._coerce_wait_s({'wait_s': 10 ** 6}) == 300
        assert reaper_safe_wait_s(POLL_WAIT_CAP_S) == 300

    def test_declared_timeout_outlives_the_cap_and_the_pool_ceiling(self):
        from lib.tools.poll import POLL_WAIT_CAP_S
        from lib.tools.registry import declared_tool_timeout_ms
        from lib.tools.registry._spec import TOOL_TIMEOUT_MS_MAX
        from lib.tools.poll import POLL_TOOL
        declared = declared_tool_timeout_ms('poll', tool_schemas=[POLL_TOOL])
        assert POLL_WAIT_CAP_S * 1000 < declared <= TOOL_TIMEOUT_MS_MAX
        assert declared > 300_000  # TOOL_PARALLEL_TIMEOUT default

    def test_long_wait_returns_as_soon_as_the_command_finishes(
            self, dispatch, blocked_command):
        cid, blocker = blocked_command
        threading.Timer(0.3, blocker.set).start()
        started = time.monotonic()
        content = dispatch('poll', {'id': cid, 'wait_s': 600})
        assert time.monotonic() - started < 10
        assert 'completed' in content and 'Wait cap reached' not in content

    def test_pending_user_steer_ends_the_wait_early(
            self, dispatch, dispatch_task, blocked_command):
        from lib import agent_inbox
        cid, _blocker = blocked_command
        conv_id = dispatch_task['convId']
        agent_inbox.reset_for_test(conv_id)
        threading.Timer(0.3, lambda: agent_inbox.enqueue(
            conv_id, 'stop waiting', mode='user-steer')).start()
        try:
            started = time.monotonic()
            content = dispatch('poll', {'id': cid, 'wait_s': 600})
            assert time.monotonic() - started < 10
        finally:
            agent_inbox.reset_for_test(conv_id)
        assert 'still running' in content
        assert 'Wait ended early: new input is waiting' in content
        assert 'Wait cap reached' not in content

    def test_call_cancellation_ends_the_wait_but_not_the_command(self, dispatch_task):
        from lib.tasks_pkg.tool_runtime import context_for_task, unregister_context
        blocker, entered = threading.Event(), threading.Event()

        def execute(_task, _ctx):
            entered.set()
            return blocker.wait(30) and 'done'

        cid = start_background_command(task=_task(), command='sleepy', execute=execute)
        entry = {'toolCallId': 'tc-p', 'status': 'searching', 'query': 'poll'}
        context = context_for_task(
            dispatch_task, round_num=1, tool_call_id='tc-p', tool_name='poll',
            round_entry=entry)
        results = []
        thread = threading.Thread(target=lambda: results.append(poll_mod._handle_poll(
            dispatch_task, {}, 'poll', 'tc-p', {'id': cid, 'wait_s': 1500},
            1, entry, {}, None, False)[1]))
        thread.start()
        try:
            assert entered.wait(5)
            context.request_resource_cancellation()
            thread.join(5)
            assert not thread.is_alive() and results
            assert 'the command remains running' in results[0], results
            assert not dispatch_task.get('aborted')
            assert check_background_command(cid, user_id=_UID)['status'] == 'running'
        finally:
            blocker.set()
            thread.join(5)
            unregister_context(context)

    def test_stop_ends_the_wait(self, dispatch, dispatch_task, blocked_command):
        cid, _blocker = blocked_command
        threading.Timer(0.3, lambda: dispatch_task.update(aborted=True)).start()
        started = time.monotonic()
        content = dispatch('poll', {'id': cid, 'wait_s': 600})
        assert time.monotonic() - started < 10
        assert 'Wait cancelled: the task was stopped.' in content

    def test_only_the_conversation_owner_yields_to_its_inbox(self, dispatch_task):
        """A task without conversation delivery (headless, child invocation)
        never drains that inbox, so yielding to it would spin extra rounds."""
        from lib import agent_inbox
        from lib.tasks_pkg.conversation_delivery import mark_no_saved_conversation
        headless = dict(dispatch_task, id='poll-headless')
        mark_no_saved_conversation(headless)
        conv_id = dispatch_task['convId']
        agent_inbox.reset_for_test(conv_id)
        agent_inbox.enqueue(conv_id, 'owner input', mode='user-steer')
        try:
            assert poll_mod._wait_interruption(dispatch_task)() == 'pending_input'
            assert poll_mod._wait_interruption(headless)() == ''
        finally:
            agent_inbox.reset_for_test(conv_id)

    def test_swarm_wait_receives_the_cap_and_yields_on_input_or_stop(
            self, monkeypatch, dispatch_task):
        """Stop ends the wait but never cancels the longer-lived swarm."""
        from lib.swarm.integration import _tools
        seen = {}

        def fake_await(fn_args, **kwargs):
            seen.update(kwargs, timeout=fn_args['timeout_seconds'])
            return 'awaited'

        monkeypatch.setattr(_tools, '_handle_await_agents', fake_await)
        interrupted = poll_mod._wait_interruption(dispatch_task)
        result = poll_mod._await_swarm(
            ['child'], dispatch_task, 'any', 1200.0, 1500.0, interrupted)
        assert result == 'awaited'
        assert seen['max_wait_s'] == 1500.0 and seen['timeout'] == 1200
        assert 'abort_check' not in seen
        assert seen['pending_input_check']() is False
        dispatch_task['aborted'] = True
        assert seen['pending_input_check']() is True
        stopped = poll_mod._await_swarm(
            ['child'], dispatch_task, 'any', 1200.0, 1500.0, interrupted)
        assert stopped == 'Wait cancelled: the task was stopped.\nawaited'

    def test_task_handle_wait_ends_early_on_pending_input(self, monkeypatch):
        from lib.scheduler.executor._await import _execute_await_task
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        running = {'id': 'other-task', 'status': 'running', 'convId': 'c2',
                   'created_at': time.time(), 'content': ''}
        monkeypatch.setattr(chat_task_runtime, 'get_owned',
                            lambda task_id, user_id: dict(running))
        started = time.monotonic()
        text = _execute_await_task(
            {'action': 'wait', 'task_id': 'other-task', 'timeout': 600,
             'poll_interval': 2}, user_id=_UID, max_wait_s=1500,
            interrupted=lambda: 'pending_input')
        assert time.monotonic() - started < 10
        assert text.startswith('Wait ended early: new input is waiting')
        assert 'Status: running' in text
