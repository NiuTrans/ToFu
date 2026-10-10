"""Task policy is authority; evictable root projections and aliases are not.

Regressions for writable children below readonly parents, detached call LRU
races, colliding root labels and acknowledged workspace expansions. Integration
ownership/storage is replaced at its boundary; file/command authorization and
writes are real on disposable directories.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import subprocess

import pytest

from lib.project_mod import config
from lib.project_mod.tools import execute_tool
from lib.tasks_pkg import workspace_delivery as delivery
from lib.tasks_pkg.background_commands import BackgroundCommandTask

pytestmark = pytest.mark.unit


def _git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo(path):
    path.mkdir(parents=True)
    _git(path, 'init', '-b', 'main')
    _git(path, 'config', 'user.name', 'Policy regression')
    _git(path, 'config', 'user.email', 'policy@example.invalid')
    (path / 'f.txt').write_text('protected\n')
    _git(path, 'add', 'f.txt')
    _git(path, 'commit', '-m', 'initial')
    return path


@pytest.fixture
def bindings(monkeypatch):
    from lib import integration_control as control
    from lib import integration_state_repository as repository
    registered = {}
    monkeypatch.setattr(repository, 'find_workspace',
                        lambda root, task, **kw: {'workspace_path': registered[root]}
                        if root in registered else None)
    monkeypatch.setattr(repository, 'set_workspace_meta', lambda **kw: None)
    monkeypatch.setattr(control, 'bind_task_workspace',
                        lambda root, task, **kw: {'canonicalRoot': root,
                            'workspacePath': registered[root], 'workId': task})
    def create(root, task_id, **kwargs):
        canonical = Path(root)
        selected = canonical.parent.parent / f'private-{canonical.name}'
        if selected.exists():
            selected = canonical.parent.parent / f'private-{canonical.name}-{task_id}'
        _git(canonical, 'worktree', 'add', '--detach', str(selected))
        registered[root] = str(selected)
    monkeypatch.setattr(control, 'create_task_workspace', create)
    return registered


@pytest.fixture(autouse=True)
def clear_policy_roots():
    with config._lock:
        before_roots = deepcopy(config._conv_roots)
        before_primary = dict(config._conv_primary)
        before_cwd = dict(config._conv_cwd)
    yield
    with config._lock:
        config._conv_roots.clear()
        config._conv_roots.update(before_roots)
        config._conv_primary.clear()
        config._conv_primary.update(before_primary)
        config._conv_cwd.clear()
        config._conv_cwd.update(before_cwd)


def _task(primary, extras=(), readonly=()):
    return {'id': 'policy-task', 'convId': 'policy-conversation', '_userId': 7,
            'config': {'projectPath': str(primary),
                       'projectPaths': [str(path) for path in extras],
                       'readOnlyPaths': [str(path) for path in readonly]}}


def test_writable_child_below_readonly_declared_parent_is_isolated(tmp_path, bindings):
    parent = tmp_path / 'parent'
    repo = _repo(parent / 'repo')
    task = _task(parent, [repo], [parent])
    config.set_conv_roots(task['id'], str(parent), [str(repo)], [str(parent)])
    assert not config.is_readonly_path(str(repo / 'f.txt'), task['id'])
    receipt = delivery.bind_worker_workspace(task)
    assert receipt and len(receipt['repositories']) == 1
    selected = receipt['repositories'][0]['workspacePath']
    args = delivery.route_project_tool_args(task, 'write_file',
                                           {'path': 'repo:f.txt', 'content': 'private\n'})
    result = execute_tool('write_file', args, str(parent), conv_id=task['id'], task=task)
    assert 'failed' not in result.lower(), result
    assert (repo / 'f.txt').read_text() == 'protected\n'
    assert Path(selected, 'f.txt').read_text() == 'private\n'
    assert config.is_readonly_path(str(parent / 'reference.txt'), task['id'])


def test_mixed_tilde_parent_and_absolute_child_agree_on_isolation_and_writes(
        tmp_path, monkeypatch, bindings):
    home = tmp_path / 'fixture'
    primary, reference = home / 'primary', home / 'reference'
    primary.mkdir(parents=True)
    child = _repo(reference / 'child')
    target = reference / 'notes.txt'
    target.write_text('reference\n')
    monkeypatch.setenv('HOME', str(home))
    task = _task(primary, ['~/reference', child], ['~/reference'])
    cfg = task['config']
    config.set_conv_roots(task['id'], cfg['projectPath'], cfg['projectPaths'],
                          cfg['readOnlyPaths'])
    roots = config.get_conv_roots(task['id'])
    assert roots['reference']['path'] == str(reference)
    assert roots['reference']['access'] == 'ro'
    assert roots['child']['access'] == 'rw'
    assert config.is_readonly_path(str(target), task['id'])
    assert not config.is_readonly_path(str(child / 'f.txt'), task['id'])
    assert delivery._roots(task) == [str(primary), str(child)]

    receipt = delivery.bind_worker_workspace(task)
    assert receipt and len(receipt['repositories']) == 1
    assert set(bindings) == {str(child)}
    selected = receipt['repositories'][0]['workspacePath']
    args = delivery.route_project_tool_args(task, 'write_file',
        {'path': 'child:f.txt', 'content': 'private\n'})
    assert args['path'] == str(Path(selected, 'f.txt'))
    result = execute_tool('write_file', args, str(primary),
                          conv_id=task['id'], task=task)
    assert 'failed' not in result.lower(), result
    assert (child / 'f.txt').read_text() == 'protected\n'
    assert Path(selected, 'f.txt').read_text() == 'private\n'
    result = execute_tool('write_file',
        {'path': 'reference:notes.txt', 'content': 'clobber'}, str(primary),
        conv_id=task['id'], task=task)
    assert 'read-only' in result.lower(), result
    assert target.read_text() == 'reference\n'
    assert config.is_readonly_path(str(target), task['id'])


@pytest.mark.parametrize('tilde_child', [False, True], ids=['absolute-child', 'tilde-child'])
def test_mixed_tilde_undeclared_readonly_parent_remains_a_fence(
        tmp_path, monkeypatch, tilde_child):
    home = tmp_path / 'fixture'
    primary, child = home / 'primary', home / 'reference' / 'child'
    primary.mkdir(parents=True)
    child.mkdir(parents=True)
    target = child / 'notes.txt'
    target.write_text('protected\n')
    monkeypatch.setenv('HOME', str(home))
    task = _task('~/primary', ['~/reference/child' if tilde_child else child],
                 ['~/reference'])
    cfg = task['config']
    config.set_conv_roots(task['id'], cfg['projectPath'], cfg['projectPaths'],
                          cfg['readOnlyPaths'])
    roots = config.get_conv_roots(task['id'])
    assert roots['child']['path'] == str(child)
    assert roots['child']['access'] == 'ro'
    assert config.is_readonly_path(str(target), task['id'])
    assert delivery._roots(task) == [str(primary)]
    assert delivery.bind_worker_workspace(task) is None
    result = execute_tool('write_file',
        {'path': str(target), 'content': 'clobber'}, str(primary),
        conv_id=task['id'], task=task)
    assert 'read-only' in result.lower(), result
    assert target.read_text() == 'protected\n'


def test_mixed_tilde_readonly_aliases_preserve_collision_safe_labels(tmp_path, monkeypatch):
    home = tmp_path / 'fixture'
    primary, reference = home / 'primary' / 'reference', home / 'reference'
    primary.mkdir(parents=True)
    child = reference / 'child'
    child.mkdir(parents=True)
    target = reference / 'notes.txt'
    target.write_text('protected\n')
    alias = home / 'aliases' / 'reference'
    alias.parent.mkdir()
    alias.symlink_to(reference, target_is_directory=True)
    monkeypatch.setenv('HOME', str(home))
    task = _task(primary, ['~/aliases/reference', alias, '~/reference', child],
                 ['~/reference'])
    cfg = task['config']
    config.set_conv_roots(task['id'], cfg['projectPath'], cfg['projectPaths'],
                          cfg['readOnlyPaths'])
    roots = config.get_conv_roots(task['id'])
    assert {label: (state['path'], state['access']) for label, state in roots.items()} == {
        'reference': (str(primary), 'rw'),
        'reference_2': (str(alias), 'ro'),
        'reference_3': (str(reference), 'ro'),
        'child': (str(child), 'rw'),
    }
    assert config.is_readonly_path(str(alias / 'notes.txt'), task['id'])
    assert not config.is_readonly_path(str(child / 'notes.txt'), task['id'])
    assert delivery._roots(task) == [str(primary), str(child)]
    result = execute_tool('write_file',
        {'path': 'reference_2:notes.txt', 'content': 'clobber'}, str(primary),
        conv_id=task['id'], task=task)
    assert 'read-only' in result.lower(), result
    assert target.read_text() == 'protected\n'


def test_readonly_collision_namespace_is_never_canonical_alias(tmp_path, bindings):
    canonical = _repo(tmp_path / 'canonical' / 'repo')
    reference = tmp_path / 'reference' / 'repo'
    reference.mkdir(parents=True)
    (reference / 'f.txt').write_text('reference\n')
    task = _task(canonical, [reference], [reference])
    delivery.bind_worker_workspace(task)
    policy = task['_workspaceRootPolicy']
    assert policy['roots']['repo_2']['path'] == str(reference)
    assert policy['roots']['repo_2']['access'] == 'ro'
    # Simulate an acknowledged current label that shadows a historical alias.
    private = policy['roots'].pop('repo')
    readonly = policy['roots'].pop('repo_2')
    policy['roots'].update({'private': private, 'repo': readonly})
    delivery.ensure_execution_roots(task)
    args = delivery.route_project_tool_args(task, 'write_file',
                                           {'path': 'repo:f.txt', 'content': 'clobber'})
    assert args['path'] == str(reference / 'f.txt')
    result = execute_tool('write_file', args, task['config']['projectPath'],
                          conv_id=task['id'], task=task)
    assert 'read-only' in result.lower(), result
    assert (reference / 'f.txt').read_text() == 'reference\n'


def test_duplicate_canonical_basenames_retain_both_labels(tmp_path, bindings):
    first = _repo(tmp_path / 'first' / 'repo')
    second = _repo(tmp_path / 'second' / 'repo')
    task = _task(first, [second])
    delivery.bind_worker_workspace(task)
    roots = task['_workspaceRootPolicy']['roots']
    assert {'repo', 'repo_2'} == set(roots)
    for label, canonical in [('repo', first), ('repo_2', second)]:
        args = delivery.route_project_tool_args(task, 'write_file',
            {'path': label + ':f.txt', 'content': label + '\n'})
        assert args['path'] == str(Path(roots[label]['path'], 'f.txt'))
        result = execute_tool('write_file', args, task['config']['projectPath'],
                              conv_id=task['id'], task=task)
        assert 'failed' not in result.lower(), result
        assert (canonical / 'f.txt').read_text() == 'protected\n'
        assert Path(roots[label]['path'], 'f.txt').read_text() == label + '\n'
    roots.pop('repo')
    roots['first-private'] = config._make_root_state(task['config']['projectPath'])
    with pytest.raises(config.UnknownWorkspaceRootError, match='Ambiguous'):
        delivery.route_project_tool_args(task, 'read_files', {'path': 'repo:f.txt'})


def test_detached_readonly_guard_survives_real_lru_eviction(tmp_path, monkeypatch):
    private = tmp_path / 'private'
    reference = tmp_path / 'readonly'
    private.mkdir()
    reference.mkdir()
    target = reference / 'notes.txt'
    target.write_text('protected\n')
    parent = _task(private, [reference], [reference])
    parent['_workspaceDelivery'] = {'repositories': [{
        'canonicalRoot': str(tmp_path / 'canonical'), 'workspacePath': str(private)}]}
    background = BackgroundCommandTask(parent)
    assert '_workspaceDelivery' not in background
    assert background['_workspaceRootPolicy'] is not parent['_workspaceRootPolicy']
    # The settled parent id is not live. This is the production live-task probe,
    # with no mocked pin result: a competing insertion really removes its cache.
    monkeypatch.setattr(config, 'MAX_CONV_ROOTS', 1)
    real_guard = config.is_readonly_path
    observed = []
    def evict_before_guard(path, conv_id=None):
        config.set_conv_roots('unrelated-competitor', str(tmp_path))
        observed.append(conv_id not in config._conv_roots)
        return real_guard(path, conv_id=conv_id)
    monkeypatch.setattr(config, 'is_readonly_path', evict_before_guard)
    # Opaque interpreter is not in the literal target parser. The readonly cwd
    # guard must independently keep its per-call snapshot after eviction.
    result = execute_tool('run_command', {
        'command': "python3 -c \"from pathlib import Path; Path('notes.txt').write_text('clobbered')\"",
        'working_dir': str(reference)}, str(private), task=background, conv_id=background['id'])
    assert observed and all(observed)
    assert 'read-only' in result.lower(), result
    assert target.read_text() == 'protected\n'
    # Original literal redirection must also refuse before subprocess admission.
    result = execute_tool('run_command', {'command': 'printf clobbered > notes.txt',
        'working_dir': str(reference)}, str(private), task=background, conv_id=background['id'])
    assert 'read-only' in result.lower(), result
    assert target.read_text() == 'protected\n'


def test_acknowledged_expansion_and_label_survive_rehydration_and_eviction(tmp_path, monkeypatch):
    private, extra, peer = (tmp_path / name for name in ['private', 'extra', 'peer'])
    for path in [private, extra, peer]:
        path.mkdir()
    task = _task(private)
    task['_workspaceDelivery'] = {'repositories': [{
        'canonicalRoot': str(tmp_path / 'canonical'), 'workspacePath': str(private)}]}
    monkeypatch.setattr(config, 'MAX_CONV_ROOTS', 1)
    policy = delivery.ensure_execution_roots(task)
    label = config.add_conv_root(task['id'], str(extra), name='acknowledged', access='ro')
    assert label == 'acknowledged'
    config.set_conv_roots('unrelated-peer', str(peer))
    assert task['id'] not in config._conv_roots
    delivery.ensure_execution_roots(task)
    assert config.get_root_path(label, task['id']) == str(extra)
    assert config.is_readonly_path(str(extra / 'f.txt'), task['id'])
    assert str(peer) not in {state['path'] for state in policy['roots'].values()}
    with config.execution_root_scope(task['id'], policy):
        config.set_conv_roots('unrelated-peer', str(peer))
        assert task['id'] not in config._conv_roots
        added = tmp_path / 'added-during-eviction'
        added.mkdir()
        assert config.add_conv_root(task['id'], str(added), name='second') == 'second'
        assert config.get_root_path('second', task['id']) == str(added)
    delivery.ensure_execution_roots(task)
    assert config.get_root_path('second', task['id']) == str(added)


def test_call_snapshot_cannot_borrow_peer_or_global_roots(tmp_path):
    private, reference = tmp_path / 'private', tmp_path / 'readonly'
    private.mkdir()
    reference.mkdir()
    task = _task(private, [reference], [reference])
    task['_workspaceExecution'] = {'repositories': [{
        'canonicalRoot': str(tmp_path / 'canonical'), 'workspacePath': str(private)}]}
    policy = delivery.ensure_execution_roots(task)
    with config.execution_root_scope(task['id'], policy):
        config.clear_conv_state(task['id'])
        assert config.get_conv_roots(task['id'])['readonly']['access'] == 'ro'
        assert config.is_readonly_path(str(reference / 'f.txt'), task['id'])
        with pytest.raises(config.UnknownWorkspaceRootError):
            config.resolve_namespaced_path('peer:f.txt', task['id'])
