"""Task config binding and terminal delivery must never borrow another tree."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading

import pytest

from lib.tasks_pkg import workspace_delivery as delivery

pytestmark = pytest.mark.unit


def _git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / 'canonical'
    root.mkdir()
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.name', 'Workspace test')
    _git(root, 'config', 'user.email', 'workspace@example.invalid')
    (root / 'base.txt').write_text('base\n')
    _git(root, 'add', 'base.txt')
    _git(root, 'commit', '-m', 'base')
    selected = tmp_path / 'selected'
    sibling = tmp_path / 'someone-else'
    _git(root, 'worktree', 'add', '-b', 'selected', str(selected))
    _git(root, 'worktree', 'add', '-b', 'someone-else', str(sibling))
    from lib import integration_control as control
    from lib import integration_state_repository as state
    env = {'root': str(root), 'selected': str(selected), 'sibling': str(sibling),
           'registered': {}, 'binds': [], 'settles': [], 'origins': [], 'journal': [], 'created': []}

    def bind(path, task_id, **kwargs):
        env['binds'].append((path, task_id, kwargs))
        actual, canonical = delivery._checkout(path)
        if canonical not in env['registered']:
            if actual == canonical:
                return None
            env['registered'][canonical] = actual
        return {'canonicalRoot': canonical,
                'workspacePath': env['registered'][canonical], 'workId': task_id}

    def settle(path, task_id, **kwargs):
        env['settles'].append((path, task_id, kwargs))
        return {'state': 'pending' if kwargs['outcome'] == 'completed' else 'preserved',
                'phase': 'checkpoint', 'taskOutcome': kwargs['outcome']}

    import lib.project_mod as project_mod
    monkeypatch.setattr(project_mod, 'get_modifications', lambda *a, **kw: list(env['journal']))
    monkeypatch.setattr(control, 'bind_task_workspace', bind, raising=False)

    def create(path, task_id, **kwargs):
        assert path == str(root)
        env['created'].append((path, task_id, kwargs))
        env['registered'][path] = str(selected)
        return {'workspacePath': str(selected)}
    monkeypatch.setattr(control, 'create_task_workspace', create)
    monkeypatch.setattr(control, 'settle_task_workspace', settle, raising=False)
    def find_workspace(root, task_id, *, user_id):
        assert task_id == 'owned-task'
        assert user_id == 7
        path = env['registered'].get(root)
        return {'workspace_path': path} if path else None

    monkeypatch.setattr(state, 'find_workspace', find_workspace)
    monkeypatch.setattr(state, 'set_workspace_meta', lambda **kw: env['origins'].append(kw))
    return env


def _task(path, **values):
    return {'id': 'owned-task', 'convId': 'conversation', '_userId': 7,
            'status': 'done', 'finishReason': 'stop',
            'config': {'projectPath': path}, **values}


def test_canonical_dirty_context_is_automatically_isolated(workspace):
    root = workspace['root']
    Path(root, 'uncommitted.txt').write_text('keep user context')
    task = _task(root)
    receipt = delivery.bind_worker_workspace(task)
    assert receipt['repositories'][0]['workspacePath'] == workspace['selected']
    assert task['config']['projectPath'] == workspace['selected']
    assert task['config']['canonicalProjectPath'] == root
    assert Path(root, 'uncommitted.txt').read_text() == 'keep user context'


def test_explicit_linear_mode_preserves_shared_execution(workspace, monkeypatch):
    from lib import linear_git_checkpoint
    monkeypatch.setattr(linear_git_checkpoint, 'is_enabled', lambda root: True)
    task = _task(workspace['root'])
    assert delivery.bind_worker_workspace(task) is None
    assert task['config']['projectPath'] == workspace['root']
    assert not workspace['registered']


def test_routes_absolute_paths_and_namespaces_cwd_by_task(workspace):
    task = _task(workspace['root'])
    delivery.bind_worker_workspace(task)
    root, selected = workspace['root'], workspace['selected']
    assert delivery.execution_root_key(task) == task['id']
    args = {'edits': [{'path': root + '/base.txt', 'operation': 'replace'}],
            'path': root + '-other/untouched', 'working_dir': root + '/src'}
    routed = delivery.route_project_tool_args(task, 'edit_file', args)
    assert routed['edits'][0]['path'] == selected + '/base.txt'
    assert routed['working_dir'] == selected + '/src'
    assert routed['path'] == args['path']
    assert args['edits'][0]['path'] == root + '/base.txt'


def test_registered_binding_maps_all_config_roots_and_readonly_without_prefix_collision(workspace):
    root, selected = workspace['root'], workspace['selected']
    workspace['registered'][root] = selected
    Path(root, 'src').mkdir()
    Path(selected, 'src').mkdir()
    task = _task(root + '/src', config={
        'projectPath': root + '/src',
        'projectPaths': [root, root + '/src', root + '-unrelated'],
        'readOnlyPaths': [root + '/src', root + '-unrelated'],
    })
    receipt = delivery.bind_worker_workspace(task)
    assert task['config']['projectPath'] == selected + '/src'
    assert task['config']['projectPaths'] == [selected, selected + '/src', root + '-unrelated']
    assert task['config']['canonicalProjectPath'] == root + '/src'
    assert task['config']['canonicalProjectPaths'] == [root, root + '/src', root + '-unrelated']
    assert task['config']['readOnlyPaths'] == [selected + '/src', root + '-unrelated']
    assert receipt['repositories'][0]['status'] == 'bound'
    assert workspace['origins'][0]['patch']['_workspaceDelivery']['workId'] == task['id']
    assert all(call[2]['user_id'] == 7 for call in workspace['binds'])


def test_explicitly_registered_linked_checkout_is_bound_only_for_current_task(workspace):
    task = _task(workspace['selected'])
    workspace['registered'][workspace['root']] = workspace['selected']
    delivery.bind_worker_workspace(task)
    assert workspace['registered'] == {workspace['root']: workspace['selected']}
    assert task['config']['canonicalProjectPath'] == workspace['root']
    assert all(path != workspace['sibling'] for path, *_ in workspace['binds'])


def test_binding_persistence_failure_is_fail_closed_and_config_atomic(workspace, monkeypatch):
    from lib import integration_state_repository as state
    workspace['registered'][workspace['root']] = workspace['selected']
    task = _task(workspace['root'])
    original = dict(task['config'])
    def fail(**_):
        raise RuntimeError('origin store unavailable')
    monkeypatch.setattr(state, 'set_workspace_meta', fail)
    with pytest.raises(RuntimeError, match='origin store unavailable'):
        delivery.bind_worker_workspace(task)
    assert task['config'] == original


def test_mismatched_registered_workspace_cannot_switch_an_explicit_linked_root(workspace):
    workspace['registered'][workspace['root']] = workspace['sibling']
    with pytest.raises(delivery.WorkspaceBindingError, match='different linked workspace'):
        delivery.bind_worker_workspace(_task(workspace['selected']))


@pytest.mark.parametrize('terminal,outcome', [
    ({'status': 'done'}, 'completed'),
    ({'status': 'done', 'finishReason': None}, 'failed'),
    ({'status': 'done', 'finishReason': ''}, 'failed'),
    ({'status': 'done', 'finishReason': 'length'}, 'failed'),
    ({'status': 'done', 'finishReason': 'premature_close'}, 'failed'),
    ({'status': 'done', 'finishReason': 'stop', 'streamState': 'premature_close'}, 'failed'),
    ({'status': 'done', 'finishReason': 'stop', 'streamState': 'malformed_stream'}, 'failed'),
    ({'status': 'done', 'finishReason': 'stop', 'streamState': 'unknown'}, 'failed'),
    ({'status': 'done', 'finishReason': 'stop', 'streamState': 'provider_finished'}, 'completed'),
    ({'status': 'done', 'aborted': True, '_abort_reason': 'storage_authority_integrity'}, 'failed'),
    ({'status': 'done', 'error': 'failed anyway'}, 'failed'),
    ({'status': 'failed'}, 'failed'),
    ({'status': 'interrupted'}, 'failed'),
    ({'status': 'done', 'aborted': True}, 'cancelled'),
    ({'status': 'cancelled'}, 'cancelled'),
    ({'status': 'running'}, 'failed'),
])
def test_all_terminal_outcomes_are_conservative(workspace, terminal, outcome):
    workspace['registered'][workspace['root']] = workspace['selected']
    task = _task(workspace['selected'], **terminal)
    receipt = delivery.settle_task_workspaces(task)
    assert workspace['settles'][0][2]['outcome'] == outcome
    assert receipt['repositories'][0]['outcome'] == outcome
    assert receipt['repositories'][0]['deliveryStatus'] == (
        'pending' if outcome == 'completed' else 'preserved')


@pytest.mark.parametrize('evidence_kind', ['cwd', 'owned-file'])
def test_mid_task_explicit_registration_survives_tool_release(workspace, evidence_kind):
    selected = workspace['selected']
    task = _task(workspace['root'])
    if evidence_kind == 'cwd':
        task['toolRounds'] = [{'toolName': 'run_command', 'status': 'done',
                               'toolArgs': json.dumps({'working_dir': selected, 'command': 'echo ok'})}]
    else:
        workspace['journal'] = [{'path': str(Path(selected, 'deleted-file.txt')),
                                 'taskId': task['id']}]
        task['modifiedFileList'] = [{'path': str(Path(selected, 'deleted-file.txt'))}]
    captured = delivery.capture_workspace_evidence(task)
    task['toolRounds'] = None
    task['modifiedFileList'] = []

    workspace['journal'] = []

    workspace['registered'][workspace['root']] = selected
    result = delivery.settle_task_workspaces(task, evidence=captured)
    assert result['repositories'][0]['workspacePath'] == selected
    assert workspace['registered'] == {workspace['root']: selected}
    assert [call[0] for call in workspace['settles']] == [workspace['root']]
    assert all(call[0] != workspace['sibling'] for call in workspace['binds'])


def test_does_not_infer_worktree_from_shell_text_or_failed_cwd(workspace):
    task = _task(workspace['root'], toolRounds=[
        {'toolName': 'run_command', 'status': 'done',
         'toolArgs': {'command': 'cd ' + workspace['sibling']}},
        {'toolName': 'run_command', 'status': 'error',
         'toolArgs': {'working_dir': workspace['sibling']}},
    ])
    assert delivery.settle_task_workspaces(task) is None
    assert workspace['registered'] == {}
    assert workspace['settles'] == []


def test_multiple_task_stamped_writes_do_not_claim_peer_worktrees(workspace):
    task = _task(workspace['root'], modifiedFileList=[
        {'path': workspace['selected'] + '/base.txt'},
        {'path': workspace['sibling'] + '/base.txt'},
    ])
    workspace['journal'] = [dict(entry, taskId=task['id']) for entry in task['modifiedFileList']]
    receipt = delivery.settle_task_workspaces(task)
    assert receipt is None
    assert not workspace['binds']
    assert not workspace['settles']
    assert not workspace['registered']


def test_managed_workspace_skips_legacy_shared_root_checkpoint(workspace):
    workspace['registered'][workspace['root']] = workspace['selected']
    task = _task(workspace['selected'])
    receipt = delivery.settle_task_workspaces(task)
    assert delivery.checkpoint_roots_without_workspaces(
        [workspace['root'], workspace['selected'], '/unrelated'], receipt) == ['/unrelated']


def test_worker_does_not_enter_runner_when_binding_fails(workspace, monkeypatch):
    from lib.tasks_pkg import spawn
    task = _task(workspace['root'], status='pending')
    ran = []
    monkeypatch.setattr(delivery, 'bind_worker_workspace', lambda task: (_ for _ in ()).throw(
        delivery.WorkspaceBindingError('storage unavailable')))
    call = spawn._owned_task_callable(lambda _: ran.append(True), task,
                                     threading.Event(), threading.Lock())
    with pytest.raises(delivery.WorkspaceBindingError):
        call()
    assert ran == []


def test_pre_thread_capture_is_bounded_and_survives_tool_release(workspace, monkeypatch):
    workspace['registered'][workspace['root']] = workspace['selected']
    from lib.tasks_pkg.commit_round import _commit
    captured = []
    class Thread:
        def __init__(self, **kwargs):
            captured.append(kwargs)
        def start(self):
            pass
    monkeypatch.setattr(_commit.threading, 'Thread', Thread)
    task = _task(workspace['root'], toolRounds=[{
        'toolName': 'run_command', 'status': 'done',
        'toolArgs': {'working_dir': workspace['selected'], 'command': 'x' * 1000}}])
    _commit._spawn_async_commit_round(task, True, workspace['root'])
    task['toolRounds'] = None
    evidence = captured[0]['args'][4]
    assert evidence['workingDirs'] == [workspace['selected']]
    assert 'command' not in repr(evidence)
    assert task['_workspaceDeliveryAcknowledgedOutcome'] == 'completed'
    assert workspace['settles'][0][0] == workspace['root']


def test_presentation_files_and_other_task_journal_are_not_ownership(workspace):
    task = _task(workspace['root'], modifiedFileList=[{
        'path': workspace['sibling'] + '/base.txt'}])
    workspace['journal'] = [{'path': workspace['sibling'] + '/base.txt',
                             'taskId': 'another-task'}]
    assert delivery.settle_task_workspaces(task) is None
    assert workspace['registered'] == {}


def test_incomplete_settlement_receipt_retains_terminal_retry_fence(workspace, monkeypatch):
    workspace['registered'][workspace['root']] = workspace['selected']
    from lib import integration_control as control
    monkeypatch.setattr(control, 'settle_task_workspace', lambda *a, **kw: {})
    task = _task(workspace['selected'])
    assert delivery.persist_terminal_workspace_intent(task) is False
    assert task['_terminalPersistencePending'] is True
    assert task['_terminalPersistenceRetryReady'] is True
    assert '_workspaceDeliveryAcknowledgedOutcome' not in task


def test_recovery_intent_does_not_require_project_journal(workspace, monkeypatch):
    import lib.project_mod

    def unavailable_journal(*args, **kwargs):
        pytest.fail('restart intent must not read project modification history')

    monkeypatch.setattr(lib.project_mod, 'get_modifications', unavailable_journal)
    workspace['registered'][workspace['root']] = workspace['selected']
    task = _task(workspace['selected'], status='interrupted')

    assert delivery.persist_terminal_workspace_intent(task) is True
    assert len(workspace['settles']) == 1
    assert workspace['settles'][0][2]['outcome'] == 'failed'
    assert task['_workspaceDelivery']['repositories'][0]['deliveryStatus'] == 'preserved'


def test_terminal_intent_acknowledgment_is_idempotent(workspace):
    workspace['registered'][workspace['root']] = workspace['selected']
    task = _task(workspace['selected'])
    assert delivery.persist_terminal_workspace_intent(task) is True
    assert delivery.persist_terminal_workspace_intent(task) is True
    assert len(workspace['settles']) == 1
    assert workspace['settles'][0][2]['outcome'] == 'completed'


@pytest.mark.parametrize('dirty', [False, True])
def test_unregistered_linked_root_is_not_ownership_even_when_clean(workspace, dirty):
    selected = workspace['selected']
    if dirty:
        Path(selected, 'peer.txt').write_text('keep peer changes')
    before = _git(selected, 'status', '--porcelain')
    task = _task(selected)
    with pytest.raises(delivery.WorkspaceBindingError, match='not registered'):
        delivery.bind_worker_workspace(task)
    receipt = delivery.settle_task_workspaces(task)
    assert receipt['errors']
    assert not workspace['registered']
    assert not workspace['binds']
    assert not workspace['settles']
    assert _git(selected, 'status', '--porcelain') == before


@pytest.mark.parametrize('evidence_kind', ['readonly-cwd', 'task-stamped-write'])
def test_peer_visit_or_write_does_not_authorize_dirty_worktree_adoption(workspace, evidence_kind):
    sibling = workspace['sibling']
    Path(sibling, 'peer.txt').write_text('unrelated peer changes')
    before = _git(sibling, 'status', '--porcelain')
    task = _task(workspace['root'])
    if evidence_kind == 'readonly-cwd':
        task['toolRounds'] = [{'toolName': 'run_command', 'status': 'done',
                               'toolArgs': {'command': 'git status', 'working_dir': sibling}}]
    else:
        workspace['journal'] = [{'path': sibling + '/task.txt', 'taskId': task['id']}]
    assert delivery.settle_task_workspaces(task) is None
    assert not workspace['registered']
    assert not workspace['binds']
    assert not workspace['settles']
    assert not workspace['origins']
    assert _git(sibling, 'status', '--porcelain') == before


@pytest.mark.parametrize('registered', [False, True])
@pytest.mark.parametrize('path_kind', ['canonical', 'linked', 'subdirectory'])
def test_readonly_configured_root_cannot_bind_or_settle(workspace, registered, path_kind):
    root, selected = workspace['root'], workspace['selected']
    if registered:
        workspace['registered'][root] = selected
    path = root if path_kind == 'canonical' else selected
    if path_kind == 'subdirectory':
        Path(selected, 'src').mkdir()
        path += '/src'
    task = _task(path, config={'projectPath': path, 'projectPaths': [path],
                              'readOnlyPaths': [root if path_kind == 'canonical' else selected]})
    assert delivery.bind_worker_workspace(task) is None
    # Even stale cached evidence must obey the current read-only root boundary.
    receipt = delivery.settle_task_workspaces(task, evidence={
        'roots': [path], 'files': [], 'workingDirs': [path]})
    assert receipt is None
    assert not workspace['binds']
    assert not workspace['settles']
    assert not workspace['origins']


def test_registered_workspace_marked_readonly_is_not_bound_from_canonical_root(workspace):
    root, selected = workspace['root'], workspace['selected']
    workspace['registered'][root] = selected
    task = _task(root, config={'projectPath': root, 'readOnlyPaths': [selected]})
    with pytest.raises(delivery.WorkspaceBindingError, match='workspace is readonly'):
        delivery.bind_worker_workspace(task)
    receipt = delivery.settle_task_workspaces(task)
    assert receipt['errors'] and not receipt['repositories']
    assert not workspace['binds']
    assert not workspace['settles']


def test_readonly_peer_visit_does_not_override_explicit_task_registration(workspace):
    root, selected, sibling = workspace['root'], workspace['selected'], workspace['sibling']
    workspace['registered'][root] = selected
    Path(sibling, 'peer.txt').write_text('leave alone')
    task = _task(root, config={'projectPath': root, 'projectPaths': [sibling],
                              'readOnlyPaths': [sibling]}, toolRounds=[{
        'toolName': 'run_command', 'status': 'done',
        'toolArgs': {'command': 'git status', 'working_dir': sibling}}])
    delivery.bind_worker_workspace(task)
    receipt = delivery.settle_task_workspaces(task)
    assert receipt['repositories'][0]['workspacePath'] == selected
    assert [call[0] for call in workspace['settles']] == [workspace['root']]
    assert all(call[0] != sibling for call in workspace['binds'])
    assert Path(sibling, 'peer.txt').read_text() == 'leave alone'


@pytest.mark.parametrize('mode', ['lookup-error', 'disappeared'])
def test_registration_failure_is_retryable_without_adoption(workspace, monkeypatch, mode):
    from lib import integration_control as control
    from lib import integration_state_repository as state

    root, selected = workspace['root'], workspace['selected']
    workspace['registered'][root] = selected
    if mode == 'lookup-error':
        def fail_lookup(*args, **kwargs):
            raise RuntimeError('workspace registry unavailable')
        monkeypatch.setattr(state, 'find_workspace', fail_lookup)
    else:
        def vanished(path, *args, **kwargs):
            assert path == root  # not linked: never enables adoption fallback
            return None
        monkeypatch.setattr(control, 'bind_task_workspace', vanished)
    task = _task(root)
    assert delivery.persist_terminal_workspace_intent(task) is False
    assert task['_terminalPersistenceRetryReady'] is True
    assert task['_workspaceDelivery']['errors']
    assert not workspace['settles']
    assert not workspace['origins']


def test_readonly_root_symlink_is_not_write_ownership(workspace, tmp_path):
    alias = tmp_path / 'readonly-alias'
    alias.symlink_to(workspace['selected'], target_is_directory=True)
    workspace['registered'][workspace['root']] = workspace['selected']
    task = _task(str(alias), config={
        'projectPath': str(alias), 'readOnlyPaths': [workspace['selected']]})
    assert delivery.bind_worker_workspace(task) is None
    assert delivery.settle_task_workspaces(task) is None
    assert not workspace['binds']
    assert not workspace['settles']


def test_readonly_canonical_root_does_not_bind_through_registered_linked_path(workspace):
    root, selected = workspace['root'], workspace['selected']
    workspace['registered'][root] = selected
    task = _task(selected, config={
        'projectPath': selected, 'readOnlyPaths': [root]})
    # A linked checkout is writable under the registered-root contract, but
    # canonical ownership is readonly: refuse before runner, never fall back.
    with pytest.raises(delivery.WorkspaceBindingError, match='readonly canonical'):
        delivery.bind_worker_workspace(task)
    receipt = delivery.settle_task_workspaces(task)
    assert receipt['errors'] and not receipt['repositories']
    assert not workspace['binds']
    assert not workspace['settles']
    assert not workspace['origins']


@pytest.mark.parametrize('terminal,outcome', [
    ({'finishReason': 'stop'}, 'completed'),
    ({'finishReason': None}, 'failed'),
    ({'finishReason': 'length'}, 'failed'),
    ({'finishReason': 'stop', 'streamState': 'premature_close'}, 'failed'),
])
def test_task_created_managed_workspace_has_durable_publication_authority(
        tmp_path, monkeypatch, terminal, outcome):
    from lib import integration_control as control
    from lib import integration_state_repository as state
    from lib.storage import StorageSupervisor
    from lib.storage.runtime import StorageRuntime
    from lib.storage.service import install_runtime_for_test

    root = tmp_path / 'canonical'
    root.mkdir()
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.name', 'Workspace test')
    _git(root, 'config', 'user.email', 'workspace@example.invalid')
    (root / 'base.txt').write_text('base\n')
    _git(root, 'add', 'base.txt')
    _git(root, 'commit', '-m', 'base')
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'managed'))
    monkeypatch.setattr(control, '_start_or_wake_worker', lambda: None)
    monkeypatch.setattr(control, '_push', lambda *args, **kwargs: None)
    runtime = StorageRuntime(supervisor=StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60), auto_restart=False)
    install_runtime_for_test(runtime)
    try:
        runtime.start()
        created = control.create_workspace(str(root), 'owned-task', user_id=7)
        task = _task(str(root), **terminal)
        delivery.bind_worker_workspace(task)
        assert task['config']['projectPath'] == created['workspacePath']
        Path(created['workspacePath'], 'task.txt').write_text('owned change\n')
        assert delivery.persist_terminal_workspace_intent(task) is True
        row = state.get_workspace(str(root), 'owned-task', user_id=7)
        assert row['origin']['delivery']['taskOutcome'] == outcome
        assert row['origin']['_workspaceDelivery']['outcome'] == outcome
        assert not (root / 'task.txt').exists()
    finally:
        install_runtime_for_test(None)


@pytest.mark.parametrize('command', [
    'git -C {root} add base.txt',
    'cd {root} && printf changed > base.txt',
    'printf changed > {root}/base.txt',
    'cd {sibling} && git add base.txt',
])
def test_literal_cross_checkout_shell_write_is_refused(workspace, command):
    task = _task(workspace['root'])
    delivery.bind_worker_workspace(task)
    refusal = delivery.command_workspace_refusal(task,
        command.format(**workspace), workspace['selected'])
    assert 'workspace-isolation' in refusal
    assert 'not started' in refusal


@pytest.mark.parametrize('command', ['git status', 'git add base.txt', 'printf changed > base.txt'])
def test_owned_checkout_commands_still_admitted(workspace, command):
    task = _task(workspace['root'])
    delivery.bind_worker_workspace(task)
    assert not delivery.command_workspace_refusal(task, command, workspace['selected'])


def test_cancelled_during_binding_does_not_activate_config(workspace, monkeypatch):
    from lib import integration_control as control
    task = _task(workspace['root'])
    config = dict(task['config'])
    def create(path, task_id, **kwargs):
        workspace['registered'][path] = workspace['selected']
        task['aborted'] = True
    monkeypatch.setattr(control, 'create_task_workspace', create)
    with pytest.raises(delivery.WorkspaceBindingError, match='cancelled'):
        delivery.bind_worker_workspace(task)
    assert task['config'] == config
    assert workspace['registered']


@pytest.mark.parametrize('path', ['canonical:base.txt', 'file://{root}/base.txt'])
def test_replayed_root_labels_and_file_uris_route_to_task(workspace, path):
    task = _task(workspace['root'])
    delivery.bind_worker_workspace(task)
    result = delivery.route_project_tool_args(task, 'read_files', {
        'path': path.format(**workspace)})
    assert result['path'] == workspace['selected'] + '/base.txt'


def test_write_symlink_cannot_escape_isolated_checkout(workspace):
    from lib.project_mod.tools import execute_tool
    task = _task(workspace['root'])
    delivery.bind_worker_workspace(task)
    Path(workspace['selected'], 'escape').symlink_to(workspace['root'], target_is_directory=True)
    result = execute_tool('write_file', {'path': 'escape/base.txt', 'content': 'clobber'},
                          workspace['selected'], conv_id=delivery.execution_root_key(task),
                          task_id=task['id'], task=task)
    assert 'workspace-isolation' in result
    assert Path(workspace['root'], 'base.txt').read_text() == 'base\n'
    assert Path(workspace['selected'], 'base.txt').read_text() == 'base\n'


def test_streaming_reads_replayed_canonical_target_in_isolated_root(workspace):
    from lib import write_freshness
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    from lib.tasks_pkg.handlers._write_freshness_gate import check_write_freshness
    task = _task(workspace['root'])
    delivery.bind_worker_workspace(task)
    selected = Path(workspace['selected'], 'base.txt')
    selected.write_text('private snapshot\n')
    write_freshness._reset_for_tests()
    accumulator = StreamingToolAccumulator(task, workspace['selected'], project_enabled=True)
    try:
        result = accumulator._execute_one('read_files', {'path': workspace['root'] + '/base.txt'})
        assert 'private snapshot' in result
        assert check_write_freshness(task, 'write_file', {'path': 'base.txt'},
                                     workspace['selected']) is None
        selected.write_text('concurrent private edit\n')
        assert check_write_freshness(task, 'write_file', {'path': 'base.txt'},
                                     workspace['selected']) is not None
        assert Path(workspace['root'], 'base.txt').read_text() == 'base\n'
    finally:
        accumulator._pool.shutdown(wait=True)
        write_freshness._reset_for_tests()


@pytest.mark.parametrize('cross_checkout', [False, True])
def test_background_command_captures_execution_view_without_parent_task(
        workspace, monkeypatch, cross_checkout):
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.handlers import project as handler
    from lib.tasks_pkg.background_commands import BackgroundCommandTask
    task = _task(workspace['root'])
    delivery.bind_worker_workspace(task)
    seen = {}
    def start(*, task, config, command, execute):
        # The long-lived callback must not pin parent messages/tool rounds.
        assert all(cell.cell_contents is not task for cell in execute.__closure__ or ())
        command_task = BackgroundCommandTask(task)
        seen['result'] = execute(command_task, {'wrapped_command': None})
        assert '_workspaceDelivery' not in command_task
        assert delivery.execution_repositories(command_task) == delivery.execution_repositories(task)
        assert delivery.execution_repositories(command_task) is not delivery.execution_repositories(task)
        assert delivery.execution_root_key(command_task) == task['id']
        return 'bg_isolated_test'
    monkeypatch.setattr(background_commands, 'start_background_command', start)
    monkeypatch.setattr(handler, '_finalize_tool_round', lambda *args, **kwargs: None)
    command = (f"printf unsafe > {workspace['root']}/base.txt" if cross_checkout else 'pwd')
    handler._handle_project_tool(task, {}, 'run_command', 'tc-background', {
        'command': command, 'background': True}, 1, {}, {}, workspace['selected'], True)
    if cross_checkout:
        assert 'workspace-isolation' in seen['result']
        assert 'not started' in seen['result']
    else:
        assert workspace['selected'] in seen['result']
        assert '[exit code: 0]' in seen['result']
    assert Path(workspace['root'], 'base.txt').read_text() == 'base\n'


def test_execution_roots_rehydrate_after_lru_without_losing_readonly_policy(workspace):
    from lib.project_mod import config
    from lib.project_mod.tools import execute_tool
    task = _task(workspace['root'])
    readonly = Path(workspace['root'], 'reference')
    readonly.mkdir()
    Path(workspace['selected'], 'reference').mkdir()
    target = Path(workspace['selected'], 'reference/notes.txt')
    target.write_text('readonly\n')
    task['config']['projectPaths'] = [workspace['root'], str(readonly)]
    task['config']['readOnlyPaths'] = [str(readonly)]
    delivery.bind_worker_workspace(task)
    key = delivery.execution_root_key(task)
    config.clear_conv_state(key)  # same state as LRU eviction
    result = execute_tool('write_file', {'path': 'reference/notes.txt', 'content': 'clobber'},
                          workspace['selected'], conv_id=key, task_id=task['id'], task=task)
    assert 'READ-ONLY' in result
    assert target.read_text() == 'readonly\n'
    assert config.get_conv_roots(key)
    assert config.is_readonly_path(str(target), conv_id=key)


def test_journal_identity_stays_conversation_scoped_with_task_root(workspace, monkeypatch):
    from lib.project_mod.tools import execute_tool
    from lib.project_mod.modifications import get_modifications
    task = _task(workspace['root'])
    delivery.bind_worker_workspace(task)
    key = delivery.execution_root_key(task)
    result = execute_tool('write_file', {'path': 'task.txt', 'content': 'owned task\n'},
                          workspace['selected'], conv_id=key, task_id=task['id'], task=task)
    assert 'Error' not in result and 'failed' not in result
    modifications = get_modifications(workspace['selected'], conv_id=task['convId'])
    assert any(row['path'] == 'task.txt' and row['taskId'] == task['id'] for row in modifications)
    assert not get_modifications(workspace['selected'], conv_id=key)
    monkeypatch.setattr('lib.project_mod.get_modifications', get_modifications)
    evidence = delivery.capture_workspace_evidence(task)
    assert {'path': 'task.txt', 'rootPath': workspace['selected']} in evidence['files']


def test_canonical_dirty_context_is_automatically_bound_without_changing_user_files(workspace):
    root = workspace['root']
    Path(root, 'uncommitted.txt').write_text('keep user context')
    task = _task(root)
    receipt = delivery.bind_worker_workspace(task)
    assert receipt['repositories'][0]['workspacePath'] == workspace['selected']
    assert task['config']['projectPath'] == workspace['selected']
    assert len(workspace['created']) == 1
    assert Path(root, 'uncommitted.txt').read_text() == 'keep user context'



def test_canonical_tool_paths_are_rebound_without_changing_edit_content(workspace):
    root, selected = workspace['root'], workspace['selected']
    task = _task(root)
    delivery.bind_worker_workspace(task)
    arguments = {'edits': [{'path': root + '/base.txt', 'content': root}],
                 'reads': [{'path': 'canonical:base.txt'}],
                 'searches': [{'path': root + '/src', 'pattern': root}],
                 'working_dir': root, 'command': f'git -C "{root}" status; echo {root}-neighbor'}
    mapped = delivery.rebind_project_tool_arguments(task, arguments)
    assert mapped['edits'][0] == {'path': selected + '/base.txt', 'content': root}
    assert mapped['reads'][0]['path'] == selected + '/base.txt'
    assert mapped['searches'][0] == {'path': selected + '/src', 'pattern': root}
    assert mapped['working_dir'] == selected
    assert mapped['command'] == arguments['command']
    assert arguments['edits'][0]['path'] == root + '/base.txt'



def test_path_rebinding_is_idempotent_when_managed_workspace_is_beneath_canonical(workspace):
    root = workspace['root']
    selected = root + '/data/integration/worktrees/task'
    task = _task(root, _workspaceDelivery={'repositories': [{
        'canonicalRoot': root, 'workspacePath': selected, 'workId': 'owned-task'}]})
    arguments = {'path': root + '/base.txt', 'command': f'python3 {root}/script.py'}
    first = delivery.rebind_project_tool_arguments(task, arguments)
    assert first['path'] == selected + '/base.txt'
    assert first['command'] == arguments['command']
    assert delivery.rebind_project_tool_arguments(task, first) == first



def test_child_inherits_path_identity_and_readonly_scope_without_claiming_parent_workspace(workspace):
    from lib.tasks_pkg.child_invocation import inherit_child_config
    from lib.tasks_pkg.invocation import InvocationPolicy
    root, selected = workspace['root'], workspace['selected']
    config = inherit_child_config({'projectPath': selected, 'canonicalProjectPath': root,
                                   'readOnlyPaths': [selected + '/protected']})
    child = {'id': 'child', '_userId': 7, 'config': config,
             '_workspaceExecution': {'repositories': [{'canonicalRoot': root, 'workspacePath': selected, 'workId': 'owned-task'}]},
             '_invocationPolicy': InvocationPolicy.child('owned-task')}
    mapped = delivery.rebind_project_tool_arguments(child, {'path': root + '/base.txt'})
    assert mapped['path'] == selected + '/base.txt'
    assert child['config']['readOnlyPaths'] == [selected + '/protected']
    assert '_workspaceDelivery' not in child



@pytest.mark.parametrize('command,metadata', [
    ('echo success', {'exitCode': 0}),
    ('python3 -c "print(1)"', {'exitCode': 0}),
    ('pytest tests/; touch another', {'exitCode': 0}),
    ('pytest --token=secret', {'exitCode': 0}),
    ('pytest tests/', {'exitCode': 1}),
    ('pytest tests/', {'exitCode': False}),
    ('pytest tests/', {'exitCode': 0, 'timedOut': True}),
    ('pytest tests/', {'exitCode': 0, 'backgrounded': True}),
])
def test_invalid_verifier_evidence_cannot_authorize_semantic_publication(workspace, command, metadata):
    root = workspace['root']
    task = _task(root)
    delivery.bind_worker_workspace(task)
    before = len(workspace['origins'])
    delivery.record_worker_verification(task, {'command': command}, task['config']['projectPath'], metadata)
    assert len(workspace['origins']) == before
    assert 'verification' not in task['_workspaceDelivery']['repositories'][0]



def test_failed_verifier_metadata_write_is_retried_with_terminal_intent(workspace, monkeypatch):
    from lib import integration_state_repository as state
    root = workspace['root']
    task = _task(root)
    delivery.bind_worker_workspace(task)
    original = state.set_workspace_meta
    def unavailable(**kwargs):
        raise RuntimeError('verifier metadata temporarily unavailable')
    monkeypatch.setattr(state, 'set_workspace_meta', unavailable)
    with pytest.raises(RuntimeError, match='temporarily unavailable'):
        delivery.record_worker_verification(task, {'command': 'pytest tests/'},
                                            task['config']['projectPath'], {'exitCode': 0})
    monkeypatch.setattr(state, 'set_workspace_meta', original)
    assert delivery.persist_terminal_workspace_intent(task) is True
    assert workspace['origins'][-1]['patch']['_workspaceDelivery']['verification']['command'] == 'pytest tests/'



def test_runtime_roots_and_attachment_paths_keep_the_original_declared_store(workspace):
    root = workspace['root']
    uploads = Path(root, 'uploads')
    uploads.mkdir()
    Path(root, 'logs').mkdir()
    task = _task(root, config={'projectPath': root, 'projectPaths': [root, str(uploads)],
                              'readOnlyPaths': [str(uploads)]})
    delivery.bind_worker_workspace(task)
    assert task['config']['projectPaths'][1] == str(uploads)
    assert task['config']['readOnlyPaths'] == [str(uploads)]
    assert str(uploads) in task['config']['workspaceRuntimeRoots']
    mapped = delivery.rebind_project_tool_arguments(task, {
        'path': str(uploads / 'owned.png'), 'working_dir': 'logs',
        'command': f'cat "{root}/logs/app.log"; python3 "{root}/script.py"'})
    assert mapped['path'] == str(uploads / 'owned.png')
    assert mapped['working_dir'] == root + '/logs'
    assert mapped['command'] == f'cat "{root}/logs/app.log"; python3 "{root}/script.py"'



def test_explicit_runtime_directory_is_not_mistaken_for_a_source_worktree(workspace):
    path = Path(workspace['root'], 'uploads')
    path.mkdir()
    task = _task(str(path))
    assert delivery.bind_worker_workspace(task) is None
    assert not workspace['created']
    assert task['config']['projectPath'] == str(path)


def test_background_completion_gets_registration_from_canonical_source(workspace, monkeypatch):
    from lib.background_command_delivery import completion_delivery_config
    parent = _task(workspace['root'])
    delivery.bind_worker_workspace(parent)
    config = dict(parent['config'])
    workspace['registered'].clear()  # New physical task has no parent registration.
    task = _task(config['projectPath'])
    with pytest.raises(delivery.WorkspaceBindingError, match='not registered'):
        delivery.bind_worker_workspace(task)
    from lib import integration_control as control, integration_state_repository as state
    fresh_path = str(Path(workspace['root']).parent / 'fresh-task-checkout')
    _git(workspace['root'], 'worktree', 'add', '-b', 'fresh-task', fresh_path)
    registrations = {}
    def create(root, task_id, *, user_id, **kwargs):
        assert root == workspace['root'] and task_id == 'fresh-task' and user_id == 7
        registrations[root] = {'workspace_path': fresh_path}
    monkeypatch.setattr(control, 'create_task_workspace', create)
    monkeypatch.setattr(state, 'find_workspace', lambda root, task_id, **kw: registrations.get(root))
    monkeypatch.setattr(control, 'bind_task_workspace', lambda root, task_id, **kw: {
        'canonicalRoot': root, 'workspacePath': fresh_path, 'workId': task_id})
    task['id'] = 'fresh-task'
    task['config'] = completion_delivery_config(config)
    receipt = delivery.bind_worker_workspace(task)
    assert receipt['repositories'][0]['workId'] == 'fresh-task'
    assert task['config']['projectPath'] == fresh_path
    assert config['projectPath'] == workspace['selected']
