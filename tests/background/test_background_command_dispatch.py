"""Background controls use their execution owner, not the project schema group."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.tasks_pkg import background_commands
from lib.tasks_pkg.executor import _execute_tool_one, tool_registry
from lib.tasks_pkg.handlers import code_exec, project
from lib.tools.project import PROJECT_TOOL_NAMES

pytestmark = pytest.mark.unit


@pytest.fixture(params=['standalone', 'project', 'remote'])
def dispatch(request, monkeypatch, tmp_path):
    """Keep production registration/lookup; isolate only domain I/O and settlement."""
    task = {'id': 'background-dispatch', 'convId': 'dispatch-conv',
            '_userId': 7, 'aborted': False, 'toolRounds': []}
    finalized = Mock()
    monkeypatch.setattr(code_exec, '_finalize_tool_round', finalized)
    monkeypatch.setattr(project, '_finalize_tool_round', finalized)
    # Project-scoped admission reads the owner's Coordinator binding first
    # (failing closed without one); this is an ordinary unbound conversation.
    from lib.conversations import project_coordination
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: SimpleNamespace(read=lambda path: {
                            'projectPath': path, 'conversationId': '', 'revision': 0,
                            'availability': 'unbound'}))
    mode = request.param
    cfg = ({'project_remote': {'agent_id': 'remote-device', 'root': '/repo'}}
           if mode == 'remote' else {})

    def execute(name, args):
        entry = {'toolCallId': 'tc-bg', 'toolName': name, 'status': 'searching'}
        result = _execute_tool_one(
            task, {}, name, 'tc-bg', args, 1, entry, cfg,
            str(tmp_path) if mode == 'project' else None,
            mode != 'standalone',
        )
        assert result[0] == 'tc-bg'
        assert result[2] is False
        assert entry['_runtimeState'] == 'done'
        finalized.assert_called_once()
        assert finalized.call_args.args[:3] == (task, 1, entry)
        assert len(finalized.call_args.args[3]) == 1
        return result[1]

    return execute


@pytest.mark.parametrize('status', ['running', 'completed'])
def test_check_dispatches_to_background_service(dispatch, monkeypatch, status):
    check = Mock(return_value={
        'status': status, 'elapsed_s': 2.5, 'command': 'echo dispatch',
        'result': 'dispatch output' if status == 'completed' else None,
    })
    monkeypatch.setattr(background_commands, 'check_background_command', check)
    content = dispatch('check_background_command', {'command_id': 'bg-owned'})
    check.assert_called_once_with('bg-owned', user_id=7, wait_s=0.0)
    assert ('still running' if status == 'running' else 'completed') in content
    if status == 'completed':
        assert 'dispatch output' in content


def test_check_unknown_dispatch(dispatch, monkeypatch):
    check = Mock(return_value=None)
    monkeypatch.setattr(background_commands, 'check_background_command', check)
    content = dispatch('check_background_command', {'command_id': 'bg-missing'})
    check.assert_called_once_with('bg-missing', user_id=7, wait_s=0.0)
    assert 'Unknown background command: bg-missing' in content


@pytest.mark.parametrize('killed', [True, False])
def test_kill_dispatches_to_background_service(dispatch, monkeypatch, killed):
    kill = Mock(return_value=killed)
    monkeypatch.setattr(background_commands, 'kill_background_command', kill)
    content = dispatch('kill_background_command', {'command_id': 'bg-owned'})
    kill.assert_called_once_with('bg-owned', user_id=7)
    assert ('kill signalled' if killed else 'not found or already finished') in content


@pytest.mark.parametrize('name', ['check_background_command', 'kill_background_command'])
def test_missing_command_id_dispatch(dispatch, monkeypatch, name):
    service = Mock()
    monkeypatch.setattr(background_commands, name, service)
    assert 'command_id is required' in dispatch(name, {})
    service.assert_not_called()


def test_standalone_explicit_background_runs_under_the_worker_call_shape(monkeypatch):
    """The detached worker calls execute(bg_task, exec_ctx), as in project mode."""
    captured = {}
    standalone = Mock(return_value='standalone result')

    def start(**kwargs):
        captured.update(kwargs)
        return 'bg-standalone'

    monkeypatch.setattr(background_commands, 'start_background_command', start)
    monkeypatch.setattr(code_exec, '_finalize_tool_round', Mock())
    monkeypatch.setattr('lib.project_mod.execute_standalone_command', standalone)
    task = {'id': 'task-bg-standalone', 'convId': 'conv', '_userId': 7}
    result = code_exec._handle_code_exec(
        task, {}, 'run_command', 'tc', {'command': 'echo hi', 'background': True},
        1, {}, {}, None, False)
    assert 'bg-standalone' in result[1]
    proxy = {'id': 'task-bg-standalone'}
    exec_ctx = {'command_id': 'bg-standalone', 'wrapped_command': None,
                'note_spawn': lambda *a: None,
                'build_result': lambda aborted=False: 'record result'}
    assert captured['execute'](proxy, exec_ctx) == 'standalone result'
    assert standalone.call_args.kwargs['task'] is proxy


def test_project_explicit_background_preserves_domain_execution(monkeypatch, tmp_path):
    runtime_roots = [str(tmp_path / 'runtime')]
    task = {'id': 'task-bg-project', 'convId': 'conv-bg-project', '_userId': 7,
            'config': {'workspaceRuntimeRoots': runtime_roots}}
    captured = {}
    finalized = Mock()
    def domain_run(*args, **kwargs):
        proxy = kwargs['task']
        proxy['_subprocess_pid'] = 123
        kwargs['on_spawn']()
        proxy.pop('_subprocess_pid')
        return 'domain result'

    domain = Mock(side_effect=domain_run)

    def start(**kwargs):
        captured.update(kwargs)
        return 'bg-project'

    monkeypatch.setattr(background_commands, 'start_background_command', start)
    monkeypatch.setattr(project, '_finalize_tool_round', finalized)
    monkeypatch.setattr('lib.project_mod.execute_tool', domain)
    args = {'command': 'echo test', 'background': True, 'working_dir': str(tmp_path)}
    entry = {'toolName': 'run_command', 'toolCallId': 'tc'}
    result = project._handle_project_tool(
        task, {}, 'run_command', 'tc', args, 1, entry, {}, str(tmp_path), True)
    assert 'bg-project' in result[1]
    domain.assert_not_called()
    proxy = {'id': 'task-bg-project', 'convId': 'conv-bg-project', '_userId': 7}

    runtime_roots.append(str(tmp_path / 'later-root'))
    runner_results = []
    exec_ctx = {'command_id': 'bg-project', 'wrapped_command': None,
                'note_spawn': lambda *a: None,
                'build_result': lambda aborted=False, runner_result='': (
                    runner_results.append(runner_result) or 'record result')}
    assert captured['execute'](proxy, exec_ctx) == 'domain result'
    assert domain.call_args.kwargs['task'] is proxy

    assert proxy['config']['workspaceRuntimeRoots'] == [str(tmp_path / 'runtime')]
    assert proxy['config']['workspaceRuntimeRoots'] is not runtime_roots
    # Runtime workspace authority is task-scoped, independent of conversation identity.
    assert domain.call_args.kwargs['conv_id'] == 'task-bg-project'
    assert domain.call_args.kwargs['on_chunk'] is None
    assert domain.call_args.kwargs['runtime_context'] is None
    # POSIX durable wrapper replaces the executed command and the result is
    # rebuilt from the record files instead of the wrapper's (empty) output.
    wrapped_ctx = dict(exec_ctx, wrapped_command='( echo test\n) > /r/out')
    assert captured['execute'](proxy, wrapped_ctx) == 'record result'
    # The runner's own result travels with it: when the wrapper never recorded
    # an exit status, it is the only evidence of what the shell reported.
    assert runner_results == ['domain result']
    assert domain.call_args.args[1]['command'] == '( echo test\n) > /r/out'
    from lib.project_mod.tool_admission import RecordedCommandSource
    assert domain.call_args.kwargs['recorded_command_source'] == RecordedCommandSource(
        'echo test', wrapped_ctx['wrapped_command'])
    domain.side_effect = None
    domain.return_value = 'Error [workspace-busy]: writer active. Command was not started.'
    assert captured['execute'](proxy, wrapped_ctx) == domain.return_value
    closure_values = [cell.cell_contents for cell in captured['execute'].__closure__]
    assert all(value is not task and value is not entry for value in closure_values)
    assert finalized.call_args.args[3][0]['exitCode'] == 'background'


def test_project_explicit_background_binds_the_execution_source(monkeypatch, tmp_path):
    from lib.project_mod.run_command import command_execution_source
    from lib.project_mod.tool_admission import RecordedCommandSource

    captured = {}
    domain = Mock(return_value='domain result')

    def start(**kwargs):
        captured.update(kwargs)
        return 'bg-project'

    monkeypatch.setattr(background_commands, 'start_background_command', start)
    monkeypatch.setattr(project, '_finalize_tool_round', Mock())
    monkeypatch.setattr('lib.project_mod.execute_tool', domain)
    original = 'git log --oneline -3 && python3 -m pytest -q'
    task = {'id': 'task-bg-git', 'convId': 'conv', '_userId': 7, 'config': {}}
    result = project._handle_project_tool(
        task, {}, 'run_command', 'tc', {'command': original, 'background': True},
        1, {'toolName': 'run_command', 'toolCallId': 'tc'}, {}, str(tmp_path), True)
    executed = command_execution_source(original)
    assert executed != original
    assert captured['command'] == executed  # the durable wrapper is built from it
    assert f'$ {original}' in result[1]  # the model still sees its own command
    wrapped = f'( {executed}\n) > /r/out'
    captured['execute']({'id': 'task-bg-git'}, {
        'command_id': 'bg-project', 'wrapped_command': wrapped,
        'note_spawn': lambda *a: None, 'build_result': lambda aborted=False: 'record'})
    assert domain.call_args.args[1]['command'] == wrapped
    assert domain.call_args.kwargs['recorded_command_source'] == RecordedCommandSource(
        executed, wrapped)


def test_remote_explicit_background_is_not_silently_ignored(monkeypatch):
    remote = Mock()
    monkeypatch.setattr(project, 'remote_worktree_binding', lambda cfg: {'agent_id': 'a'})
    monkeypatch.setattr(project, '_execute_remote_project_tool', remote)
    monkeypatch.setattr(project, '_finalize_tool_round', Mock())
    result = project._handle_project_tool(
        {'id': 't', '_userId': 7}, {}, 'run_command', 'tc',
        {'command': 'echo test', 'background': True}, 1, {}, {}, None, True)
    assert 'no command ran' in result[1]
    remote.assert_not_called()


def test_schema_group_does_not_claim_background_execution():
    for name in ('check_background_command', 'kill_background_command'):
        assert name in PROJECT_TOOL_NAMES  # Keep disclosure/approval grouping intact.
        assert tool_registry.lookup(name, {'toolName': name}) is not project._handle_project_tool
    for name in ('run_command', 'write_file', 'edit_file', 'grep_search', 'read_files'):
        assert tool_registry.lookup(name, {'toolName': name}) is project._handle_project_tool
