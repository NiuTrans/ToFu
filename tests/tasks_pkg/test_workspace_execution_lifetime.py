"""Execution-root authority must outlive parent tasks and LRU projections.

These regressions use acknowledged execution rows, not workspace allocation or
storage. Only background scheduling/presentation and runtime lookup are faked;
project dispatch, root registration/eviction, path routing and the shell stay
real. No durable background record, task worker or live database is created.
"""
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.project_mod import config as roots
from lib.project_mod.tools import execute_tool
from lib.tasks_pkg import workspace_delivery as delivery


pytestmark = pytest.mark.unit

@pytest.fixture
def execution_workspace(tmp_path, monkeypatch):
    from lib.tasks_pkg.manager import runtime

    # Isolate registry/recency/cwd mutations from other tests in this process.
    # Do not stub the liveness probe or the eviction implementation itself.
    monkeypatch.setattr(roots, '_roots', {})
    monkeypatch.setattr(roots, '_conv_roots', OrderedDict())
    monkeypatch.setattr(roots, '_conv_primary', {})
    monkeypatch.setattr(roots, '_conv_cwd', OrderedDict())
    monkeypatch.setattr(roots, '_state', {**roots._state, 'path': None,
                                        'modifications': [], 'sessionId': None})
    monkeypatch.setattr(roots, 'MAX_CONV_ROOTS', 1)

    env = SimpleNamespace()
    for name in ('canonical', 'selected', 'reference', 'extra', 'archive',
                 'peer', 'global_root'):
        path = tmp_path / name
        path.mkdir()
        setattr(env, name, path)
    env.protected = b'protected reference\n'
    (env.reference / 'notes.txt').write_bytes(env.protected)
    (env.extra / 'notes.txt').write_text('acknowledged extra\n')
    (env.archive / 'notes.txt').write_text('acknowledged archive\n')
    (env.peer / 'notes.txt').write_text('peer bytes\n')
    (env.global_root / 'notes.txt').write_text('global bytes\n')
    env.task = {
        'id': 'workspace-lifetime-task', 'convId': 'workspace-lifetime-conv',
        '_userId': 7, 'status': 'running', 'finishReason': 'stop',
        'config': {'projectPath': str(env.selected),
                   'projectPaths': [str(env.selected), str(env.reference)],
                   'readOnlyPaths': [str(env.reference)]},
        '_workspaceDelivery': {'repositories': [{
            'canonicalRoot': str(env.canonical),
            'workspacePath': str(env.selected),
            'status': 'bound',
        }]},
    }
    env.key = delivery.execution_root_key(env.task)
    assert env.key == env.task['id']

    # A settled foreground task remains visible to the REAL liveness probe.
    # Its detached command is deliberately not another chat task registration.
    monkeypatch.setattr(runtime.chat_task_runtime, 'get',
                        lambda task_id: env.task if task_id == env.key else None)
    monkeypatch.setattr(runtime, '_latest_task_for_conv',
                        lambda conv_id: env.key if conv_id == env.task['convId'] else None)

    # Legacy globals contain writable roots, including the SAME protected path.
    # Falling back to them must not turn the borrowed readonly reference RW.
    roots._state['path'] = str(env.global_root)
    roots._roots.update({
        'global-only': {'path': str(env.global_root), 'access': 'rw'},
        'reference': {'path': str(env.reference), 'access': 'rw'},
    })
    env.global_snapshot = deepcopy(roots.get_roots())
    delivery.ensure_execution_roots(env.task)
    return env


def test_background_readonly_reference_refuses_after_guard_time_lru_eviction(
        execution_workspace, monkeypatch):
    """P2: guard-time eviction cannot allow an opaque interpreter write.

    Literal redirection is already refused by the earlier target parser; the
    interpreter reaches the independent readonly-cwd guard under test here.
    """
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.background_commands import BackgroundCommandTask
    from lib.tasks_pkg.handlers import project as handler

    env = execution_workspace
    scheduled = {}

    def capture_background(*, task, config, command, execute):
        # Capture the real handler callback without durable scheduling/storage.
        scheduled.update(task=BackgroundCommandTask(task), execute=execute)
        assert all(cell.cell_contents is not task
                   for cell in execute.__closure__ or ())
        return 'bg_workspace_lifetime'

    monkeypatch.setattr(background_commands, 'start_background_command', capture_background)
    monkeypatch.setattr(handler, '_finalize_tool_round', lambda *args, **kwargs: None)
    handler._handle_project_tool(env.task, {}, 'run_command', 'tc-lifetime', {
        'command': "python3 -c \"from pathlib import Path; Path('notes.txt').write_text('clobbered')\"",
        'working_dir': str(env.reference), 'background': True,
    }, 1, {}, {}, str(env.selected), True)
    assert scheduled, 'explicit background dispatch must capture its execution callback'

    # The parent is settled before the long-lived callback starts. Running
    # status used to hide this race by pinning the parent's registry in the LRU.
    env.task['status'] = 'done'
    assert not roots._conv_has_live_task(env.key)
    readonly_probe = roots.is_readonly_path
    evictions = []

    def evict_immediately_before_readonly(abs_target, conv_id=None):
        if conv_id == env.key and Path(abs_target) == env.reference and not evictions:
            assert env.key in roots._conv_roots, 'dispatch must rehydrate before the guard'
            roots.set_conv_roots('workspace-lifetime-peer', str(env.peer))
            assert env.key not in roots._conv_roots, 'exercise actual cap-driven eviction'
            assert 'workspace-lifetime-peer' in roots._conv_roots
            evictions.append(env.key)
        return readonly_probe(abs_target, conv_id=conv_id)

    monkeypatch.setattr(roots, 'is_readonly_path', evict_immediately_before_readonly)
    # If the bug permits a real shell write, keep undo bookkeeping hermetic.
    # This is not a command-runner mock: the interpreter can execute for real.
    from lib.project_mod import tools
    monkeypatch.setattr(tools, '_record_run_command_changes', lambda *args, **kwargs: [])
    result = scheduled['execute'](scheduled['task'], {'wrapped_command': None})

    assert evictions == [env.key], 'the test must evict during the readonly guard'
    assert '_workspaceDelivery' not in scheduled['task']
    assert delivery.execution_repositories(scheduled['task']) == delivery.execution_repositories(env.task)
    assert delivery.execution_repositories(scheduled['task']) is not delivery.execution_repositories(env.task)
    assert (env.reference / 'notes.txt').read_bytes() == env.protected, result
    assert 'READ-ONLY' in result, result
    assert '[exit code: 0]' not in result, result
    assert roots.get_roots() == env.global_snapshot


def test_acknowledged_task_root_labels_survive_dispatch_and_real_lru_eviction(
        execution_workspace):
    """P4: acknowledged additions/labels/access are authority, not LRU config."""
    env = execution_workspace
    # These labels intentionally differ from basenames and are NOT added to
    # config.projectPaths: add_conv_root is the acknowledgement boundary.
    assert roots.add_conv_root(env.key, str(env.extra), name='acknowledged-notes') == 'acknowledged-notes'
    assert roots.add_conv_root(env.key, str(env.archive), name='acknowledged-archive',
                               access='ro') == 'acknowledged-archive'
    expected = {
        'selected': (str(env.selected), 'rw'),
        'reference': (str(env.reference), 'ro'),
        'acknowledged-notes': (str(env.extra), 'rw'),
        'acknowledged-archive': (str(env.archive), 'ro'),
    }

    def assert_owned_dispatch():
        reads = [{'path': f'{label}:notes.txt'}
                 for label in ('acknowledged-notes', 'acknowledged-archive')]
        result = execute_tool('read_files', {'reads': reads}, str(env.selected),
                              conv_id=env.key, task_id=env.key, task=env.task)
        assert 'acknowledged extra' in result, result
        assert 'acknowledged archive' in result, result
        view = roots.get_conv_roots(env.key)
        assert {label: (state['path'], state['access'])
                for label, state in view.items()} == expected
        assert roots.is_readonly_path(str(env.archive / 'notes.txt'), conv_id=env.key)
        for unowned in ('global-only', 'peer'):
            with pytest.raises(roots.UnknownWorkspaceRootError):
                roots.resolve_namespaced_path(f'{unowned}:notes.txt', conv_id=env.key)
        assert roots.get_roots() == env.global_snapshot

    # Regression stage 1: ensure_execution_roots used to reset this registry on
    # every call, losing an addition even BEFORE an eviction occurred.
    assert_owned_dispatch()

    # Regression stage 2: do not simulate eviction with clear_conv_state. Let a
    # competing registration evict this settled task under the actual LRU cap.
    env.task['status'] = 'done'
    assert not roots._conv_has_live_task(env.key)
    roots.set_conv_roots('workspace-lifetime-peer', str(env.peer))
    assert env.key not in roots._conv_roots
    peer_snapshot = deepcopy(roots.get_conv_roots('workspace-lifetime-peer'))
    assert set(peer_snapshot) == {'peer'}
    assert_owned_dispatch()

    # Rehydrating one task cannot import or mutate a peer's projection. Reinsert
    # an evicted peer only through its own public registration boundary.
    if 'workspace-lifetime-peer' not in roots._conv_roots:
        roots.set_conv_roots('workspace-lifetime-peer', str(env.peer))
    assert roots.get_conv_roots('workspace-lifetime-peer') == peer_snapshot
    assert (env.peer / 'notes.txt').read_text() == 'peer bytes\n'
    assert (env.global_root / 'notes.txt').read_text() == 'global bytes\n'
    assert (env.reference / 'notes.txt').read_bytes() == env.protected
    assert (env.extra / 'notes.txt').read_text() == 'acknowledged extra\n'
    assert (env.archive / 'notes.txt').read_text() == 'acknowledged archive\n'
