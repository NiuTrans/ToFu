"""Workspace admission must not enter the checkout to inspect Git metadata."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from lib.git_runtime import git_executable
from lib.project_mod import activity_supervised_process as supervised
from lib.project_mod.workspace_policy import resolve_workspace_mode
from lib.tasks_pkg import workspace_delivery as delivery

pytestmark = pytest.mark.unit


@pytest.fixture
def repository(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.upper().startswith('GIT_'):
            monkeypatch.delenv(name)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    monkeypatch.delenv('TOFU_WORKSPACE_MODE', raising=False)
    executable = git_executable()
    root = tmp_path / 'repository'
    root.mkdir()

    def git(*args):
        return subprocess.run([executable, '-C', str(root), *args],
            check=True, text=True, capture_output=True).stdout.strip()

    git('init', '-b', 'main')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '--allow-empty', '-m', 'initial')
    monkeypatch.setattr(delivery, 'git_executable', lambda: executable)
    monkeypatch.setattr('lib.project_mod.workspace_policy.git_executable', lambda: executable)
    return root, git


def _stall_checkout_entry(monkeypatch, *checkouts):
    roots = {str(path) for path in checkouts}

    def enters_checkout(command, cwd):
        if cwd is not None and any(Path(cwd).is_relative_to(root) for root in roots):
            return True
        return any(word == '-C' and any(Path(command[index + 1]).is_relative_to(root)
                                        for root in roots)
                   for index, word in enumerate(command[:-1]))

    class Guarded(subprocess.Popen):
        def __init__(self, args, *rest, **kwargs):
            if enters_checkout(list(args), kwargs.get('cwd')):
                raise subprocess.TimeoutExpired(args, 10)
            super().__init__(args, *rest, **kwargs)

    # Every child starts through Popen: subprocess.run and supervised Git alike.
    monkeypatch.setattr(subprocess, 'Popen', Guarded)


@pytest.mark.parametrize('linked', [False, True])
def test_identity_and_policy_survive_stalled_checkout_entry(repository, tmp_path, monkeypatch, linked):
    root, git = repository
    checkout = root
    if linked:
        checkout = tmp_path / 'linked'
        git('worktree', 'add', '--detach', str(checkout))
        marker = checkout / '.git'
        admin = Path(marker.read_text().strip()[8:])
        marker.write_text('gitdir: ' + os.path.relpath(admin, checkout) + '\n')
    nested = checkout / 'nested'
    nested.mkdir()
    _stall_checkout_entry(monkeypatch, root, checkout, nested)

    assert delivery._checkout(str(nested / 'deleted' / 'file.py')) == (str(checkout), str(root))
    assert resolve_workspace_mode(nested) == 'direct'


def test_direct_worker_binds_before_runner_without_entering_checkout(repository, monkeypatch):
    root, _git = repository
    monkeypatch.setattr('lib.tasks_pkg.coordinator_policy.coordinator_scope', lambda task: None)
    monkeypatch.setattr('lib.conversation_sync.workspace.provision_workspace', lambda task: None)
    monkeypatch.setattr('lib.integration_state_repository.find_workspace', lambda *args, **kwargs: None)
    monkeypatch.setattr('lib.project_mod.config.register_root_policy', lambda *args: None)
    _stall_checkout_entry(monkeypatch, root)
    task = {'id': 'metadata-admission', '_userId': 7, 'config': {'projectPath': str(root)}}

    assert delivery.bind_worker_workspace(task) is None
    assert task['config']['projectPath'] == str(root)
    assert task['_workspaceExecutionModes'] == {str(root): 'direct'}


@pytest.mark.parametrize('relative', [False, True])
def test_configured_worktree_keeps_git_identity(repository, tmp_path, relative):
    root, git = repository
    actual = tmp_path / 'actual worktree'
    actual.mkdir()
    configured = os.path.relpath(actual, root / '.git') if relative else str(actual)
    git('config', 'core.worktree', configured)

    assert delivery._checkout(str(root)) == (str(actual), str(root))


@pytest.mark.parametrize('failure', ['bare', 'config', 'empty', 'pointer'])
def test_invalid_repository_never_becomes_an_ordinary_folder(repository, failure):
    root, git = repository
    if failure == 'bare':
        git('config', 'core.bare', 'true')
    elif failure == 'config':
        (root / '.git/config').write_text('[broken\n')
    elif failure == 'empty':
        root = root / 'nested'
        (root / '.git').mkdir(parents=True)
    else:
        root = root / 'nested'
        root.mkdir()
        (root / '.git').write_text('gitdir: /missing/tofu-test-repository\n')

    with pytest.raises(delivery.WorkspaceBindingError):
        delivery._checkout(str(root))


def test_metadata_stall_preserves_typed_failure_and_recovery_hint(repository, monkeypatch):
    root, _git = repository

    def stalled(command, **kwargs):
        raise supervised.ProcessStalled('No thread of the process tree executed for 90s')

    monkeypatch.setattr(supervised, 'run_activity_supervised', stalled)
    with pytest.raises(delivery.WorkspaceBindingError, match='(?i)git.*no progress') as raised:
        delivery._checkout(str(root))
    assert isinstance(raised.value.__cause__, supervised.ProcessStalled)
    assert str(raised.value.__cause__) in str(raised.value)
    assert 'Git' in raised.value.user_hint and '重试' in raised.value.user_hint


@pytest.mark.parametrize('relative', ['.git', '.git/objects', '.git/deleted.py'])
def test_git_administration_is_not_an_execution_workspace(repository, relative):
    root, _git = repository
    with pytest.raises(delivery.WorkspaceBindingError):
        delivery._checkout(str(root / relative))


def test_plain_directory_needs_no_git(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('a plain folder must not start Git')

    monkeypatch.setattr(subprocess, 'run', forbidden)
    assert delivery._checkout(str(tmp_path / 'deleted.py')) is None
