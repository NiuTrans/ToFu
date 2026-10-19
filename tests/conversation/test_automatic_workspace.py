"""Application-only automatic workspace and execution-root isolation contracts."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _task(config, *, owner=7, conversation='chat-a', task_id='task-a'):
    return {'id': task_id, 'convId': conversation, '_userId': owner, 'config': config}


@pytest.fixture
def workspace_base(tmp_path, monkeypatch):
    import lib.conversation_sync.workspace as workspace
    monkeypatch.setattr(workspace, 'get_runtime_base', lambda: str(tmp_path))
    return tmp_path


def test_chat_gets_deterministic_private_root_without_io(workspace_base):
    from lib.conversation_sync.workspace import resolve_workspace_config
    config = {'chatMode': 'chat', 'projectPath': ''}
    resolved = resolve_workspace_config(config, 'chat-a', 7)
    assert resolved['projectPath'] == resolve_workspace_config(config, 'chat-a', 7)['projectPath']
    assert resolved['projectPath'] != resolve_workspace_config(config, 'chat-b', 7)['projectPath']
    assert resolved['projectPath'] != resolve_workspace_config(config, 'chat-a', 8)['projectPath']
    assert not Path(resolved['projectPath']).exists()
    assert config == {'chatMode': 'chat', 'projectPath': ''}


@pytest.mark.parametrize('config', [
    {}, {'profile': 'coding'}, {'codeExecEnabled': True},
    {'projectPath': '/host/project', 'projectPaths': ['/host/project', '/host/reference'], 'readOnlyPaths': ['/host/reference']},
    {'chatMode': 'chat', 'project_remote': {'agent_id': 'host-device', 'root': '/project'}},
    {'chatMode': 'chat', 'projectPath': 'remote:host-device:/project'},
    {'chatMode': 'chat', 'tools': [{'type': 'function', 'function': {'name': 'host_tool'}}]},
])
def test_sdk_server_and_explicit_bindings_remain_host_owned(config, workspace_base):
    from lib.conversation_sync.workspace import resolve_workspace_config
    assert resolve_workspace_config(config, 'chat-a', 7) == config
    assert list(workspace_base.iterdir()) == []


def test_public_request_cannot_forge_managed_or_checkout_authority():
    from lib.conversation_sync.execution_identity import capture_execution_identity
    accepted = capture_execution_identity({'chatMode': 'chat',
        '_conversationWorkspace': {'path': '/foreign'}, 'canonicalProjectPath': '/peer',
        'canonicalProjectPaths': ['/peer'], 'workspaceRuntimeRoots': ['/foreign']}, 7, None)
    assert '_conversationWorkspace' not in accepted
    assert 'canonicalProjectPath' not in accepted
    assert 'workspaceRuntimeRoots' not in accepted


def test_worker_provisions_privately_and_reuses_output(workspace_base):
    from lib.conversation_sync.workspace import resolve_workspace_config, provision_workspace
    config = resolve_workspace_config({'chatMode': 'chat'}, 'chat-a', 7)
    task = _task(config)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(lambda _: provision_workspace(task), range(8)))
    path = Path(config['projectPath'])
    output = path / 'result.txt'
    output.write_text('durable output')
    assert provision_workspace(task)
    assert output.read_text() == 'durable output'
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize('tamper', ['owner', 'conversation', 'path', 'roots'])
def test_provisioning_rejects_scope_changes_before_creating_files(tamper, workspace_base):
    from lib.conversation_sync.workspace import resolve_workspace_config, provision_workspace
    config = resolve_workspace_config({'chatMode': 'chat'}, 'chat-a', 7)
    task = _task(config)
    if tamper == 'owner': task['_userId'] = 8
    if tamper == 'conversation': task['convId'] = 'chat-b'
    if tamper == 'path': config['projectPath'] = str(workspace_base / 'foreign')
    if tamper == 'roots': config['projectPaths'].append(str(workspace_base / 'foreign'))
    with pytest.raises(PermissionError): provision_workspace(task)
    assert list(workspace_base.iterdir()) == []


def test_symlinked_workspace_is_refused(workspace_base):
    from lib.conversation_sync.workspace import resolve_workspace_config, provision_workspace
    config = resolve_workspace_config({'chatMode': 'chat'}, 'chat-a', 7)
    path = Path(config['projectPath'])
    path.parent.mkdir(parents=True)
    outside = workspace_base / 'outside'
    outside.mkdir()
    path.symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, PermissionError)): provision_workspace(_task(config))
    assert list(outside.iterdir()) == []


def test_transient_kernel_never_provisions_chat_workspace(workspace_base):
    from lib.conversation_sync.workspace import resolve_workspace_config, provision_workspace
    task = _task(resolve_workspace_config({'chatMode': 'chat'}, 'chat-a', 7))
    task['_transientRuntime'] = True
    assert provision_workspace(task) is False
    assert list(workspace_base.iterdir()) == []


def test_managed_root_does_not_adopt_server_git_repo(workspace_base, monkeypatch):
    import lib.tasks_pkg.workspace_delivery as delivery
    from lib.tasks_pkg import coordinator_policy
    from lib.conversation_sync.workspace import resolve_workspace_config
    # This provisioning test has no configured coordinator or storage process.
    monkeypatch.setattr(coordinator_policy, 'coordinator_scope', lambda task: None)
    config = resolve_workspace_config({'chatMode': 'chat'}, 'chat-a', 7)
    task = _task(config)
    monkeypatch.setattr(delivery, '_checkout', lambda _: pytest.fail('managed root must never probe Git ancestors'))
    assert delivery.bind_worker_workspace(task) is None
    assert delivery.ensure_execution_roots(task)['primaryPath'] == config['projectPath']
    assert delivery.execution_root_key(task) == 'task-a'


def test_explicit_empty_config_clears_saved_root_and_inactive_ignores_overrides():
    from lib.conv_config import resolve_conv_config
    saved = {'projectPath': '/project-a', 'projectPaths': ['/project-a'], 'readOnlyPaths': ['/project-a']}
    override = {'projectPath': '', 'projectPaths': [], 'readOnlyPaths': []}
    active = resolve_conv_config(saved, override)
    assert active['projectPath'] == '' and active['projectPaths'] == [] and active['readOnlyPaths'] == []
    inactive = resolve_conv_config(saved, {'projectPath': '/project-b'}, is_active=False)
    assert inactive['projectPath'] == '/project-a'


def test_unknown_conversation_never_inherits_global_roots(tmp_path, monkeypatch):
    from lib.project_mod import config
    monkeypatch.setitem(config._state, 'path', str(tmp_path))
    monkeypatch.setitem(config._roots, 'foreign', {'path': str(tmp_path)})
    assert config.get_conv_roots('unbound-chat') == {}
    assert config.get_root_path('foreign', conv_id='unbound-chat') is None
    with pytest.raises(config.UnknownWorkspaceRootError):
        config.resolve_namespaced_path('foreign:file.py', conv_id='unbound-chat')
    with pytest.raises(ValueError):
        config.resolve_namespaced_path('file.py', conv_id='unbound-chat')


def test_sdk_workspace_lists_are_detached_per_invocation():
    from lib.agent_core.run_contract import build_agent_config
    supplied = {'projectPath': '/host/a', 'projectPaths': ['/host/a', '/host/ref'],
                'readOnlyPaths': ['/host/ref']}
    frozen = build_agent_config('synthetic', supplied)
    supplied['projectPaths'].append('/host/b')
    supplied['readOnlyPaths'].clear()
    assert frozen['projectPaths'] == ['/host/a', '/host/ref']
    assert frozen['readOnlyPaths'] == ['/host/ref']
    assert 'projectPath' not in build_agent_config('synthetic', {})


def test_sdk_non_git_tasks_with_same_conversation_keep_independent_roots(tmp_path):
    from lib.tasks_pkg.workspace_delivery import ensure_execution_roots, execution_root_key
    from lib.project_mod.config import execution_root_scope, resolve_namespaced_path, clear_conv_state
    roots = [tmp_path / 'first', tmp_path / 'second']
    for root in roots: root.mkdir()
    tasks = [_task({'projectPath': str(root)}, task_id=f'host-{index}')
             for index, root in enumerate(roots)]
    policies = [ensure_execution_roots(task) for task in tasks]
    for task, policy, root in zip(tasks, policies, roots):
        key = execution_root_key(task)
        clear_conv_state(key)  # LRU loss is not authority loss.
        with execution_root_scope(key, policy):
            assert resolve_namespaced_path('new.py', key) == (str(root), 'new.py')
    assert tasks[0]['convId'] == tasks[1]['convId']
    assert execution_root_key(tasks[0]) != execution_root_key(tasks[1])


def test_managed_commands_cannot_discover_server_repository(tmp_path, monkeypatch):
    import subprocess
    import runtime_path_policy
    from lib.project_mod.run_command import _get_cmd_env
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    monkeypatch.setattr(runtime_path_policy, 'get_runtime_base', lambda: str(tmp_path))
    root = tmp_path / 'data' / 'conversation_workspaces' / 'u7' / ('a' * 64)
    root.mkdir(parents=True)
    env = _get_cmd_env(str(root), owner_user_id=7, project_root=str(root))
    result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=root,
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert str(tmp_path) not in result.stdout
    subprocess.run(['git', 'init', '-q', str(root)], check=True, env=env)
    result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=root,
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == str(root)


def test_list_only_explicit_binding_selects_primary_without_losing_access_modes(workspace_base):
    from lib.conversation_sync.workspace import resolve_workspace_config
    result = resolve_workspace_config({'chatMode': 'chat',
        'projectPaths': ['/host/code', '/host/ref'], 'readOnlyPaths': ['/host/ref']}, 'chat-a', 7)
    assert result['projectPath'] == '/host/code'
    assert result['projectPaths'] == ['/host/code', '/host/ref']
    assert result['readOnlyPaths'] == ['/host/ref']
    assert '_conversationWorkspace' not in result
    assert list(workspace_base.iterdir()) == []


def test_rootless_sdk_task_cannot_borrow_older_conversation_authority(tmp_path):
    from lib.tasks_pkg.workspace_delivery import ensure_execution_roots, execution_root_key
    from lib.project_mod.config import set_conv_roots, get_conv_roots, clear_conv_state, execution_root_scope, resolve_namespaced_path
    from lib.project_mod.abs_path_guard import registered_root_scope, set_restricted, reset_restricted, enforce_abs_read, AbsPathDenied
    old = tmp_path / 'old-project'
    old.mkdir()
    set_conv_roots('reused-sdk-conversation', str(old))
    task = _task({}, conversation='reused-sdk-conversation', task_id='rootless-sdk-task')
    policy = ensure_execution_roots(task)
    key = execution_root_key(task)
    token = set_restricted(True)
    try:
        assert policy == {'primaryPath': '', 'roots': {}}
        assert key == task['id']
        clear_conv_state(key)  # Lost cache must still enforce explicit absence.
        with execution_root_scope(key, policy), registered_root_scope(key):
            with pytest.raises(AbsPathDenied): enforce_abs_read(str(old / 'private.txt'))
            with pytest.raises(ValueError): resolve_namespaced_path('old-project:private.txt', key)
            with pytest.raises(ValueError): resolve_namespaced_path('private.txt', key)
        assert get_conv_roots('reused-sdk-conversation')  # Another task's state was not cleared.
    finally:
        reset_restricted(token)
        clear_conv_state(key)
        clear_conv_state('reused-sdk-conversation')


def test_rootless_dispatch_does_not_read_launch_cwd(tmp_path, monkeypatch):
    from lib.tasks_pkg.handlers import project
    from lib.project_mod.config import clear_conv_state
    monkeypatch.chdir(tmp_path)
    sentinel = tmp_path / 'launch-only.txt'
    sentinel.write_text('LAUNCH_ROOT_WAS_NOT_BOUND')
    monkeypatch.setattr(project, '_finalize_tool_round', lambda *_args, **_kwargs: None)
    task = _task({}, task_id='rootless-relative-read')
    task.update(messages=[], toolRounds=[], _via_agent_run=True)
    try:
        result = project._handle_project_tool(task, {}, 'read_files', 'rootless-read',
            {'path': sentinel.name}, 1, {}, {}, '', False)
        assert 'LAUNCH_ROOT_WAS_NOT_BOUND' not in str(result)
        assert 'No project base path' in str(result)
    finally:
        clear_conv_state(task['id'])
