"""Real Git callbacks and object writes contend with their physical owners."""
from contextlib import contextmanager
import os
import shlex
import subprocess
import threading

import pytest

from lib.project_mod.git_command_scope import git_command_scopes
from lib.project_mod.workspace_lease import (
    WorkspaceBusyError, workspace_claims_lease, workspace_file_write_lease,
    workspace_git_metadata_lease,
)

pytestmark = pytest.mark.unit


@contextmanager
def _held_elsewhere(api, path):
    """Another thread avoids same-thread lease reentry masking contention."""
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with api(str(path)):
                ready.set()
                assert release.wait(10), 'test did not release independent holder'
        except BaseException as error:
            errors.append(error)
            ready.set()

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert ready.wait(10) and not errors
        yield
    finally:
        release.set()
        worker.join(10)
        assert not worker.is_alive() and not errors


@pytest.fixture
def real_git(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    environment = {
        'PATH': os.environ['PATH'], 'HOME': str(home),
        'XDG_CONFIG_HOME': str(home), 'GIT_CONFIG_NOSYSTEM': '1',
    }
    repo = tmp_path / 'repo'

    def git(*args, cwd=repo, env=environment):
        return subprocess.run(['git', *args], cwd=cwd, env=env, check=True,
                              capture_output=True, text=True, timeout=10)

    git('init', '-q', str(repo), cwd=tmp_path)
    (repo / 'tracked').write_text('original\n')
    git('add', 'tracked')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'initial')
    return repo, environment, git


@pytest.mark.parametrize('style', [
    'config-env-separated', 'config-env-equals', 'inline-section',
    'inline-include', 'global-inline-section', 'default-hook',
])
@pytest.mark.parametrize('operation', ['soft-reset', 'staged-restore'])
def test_git_callback_excludes_file_writer(real_git, tmp_path, monkeypatch, style, operation):
    repo, environment, git = real_git
    if operation == 'staged-restore':
        (repo / 'tracked').write_text('staged\n')
        git('add', 'tracked')
    hooks = repo / '.git' / 'hooks' if style == 'default-hook' else tmp_path / 'hooks'
    hooks.mkdir(exist_ok=True)
    marker = repo / 'protected'
    marker.write_text('keep\n')
    hook = hooks / ('reference-transaction' if operation == 'soft-reset' else 'post-index-change')
    hook.write_text('#!/bin/sh\nprintf called > ' + shlex.quote(str(marker)) + '\n')
    hook.chmod(0o700)
    args = (['reset', '--soft', 'HEAD'] if operation == 'soft-reset'
            else ['restore', '--staged', '--', 'tracked'])
    option = []
    if style.startswith('config-env-'):
        environment['HOOKDIR'] = str(hooks)
        option = ['--config-env', 'core.hooksPath=HOOKDIR'] if style.endswith('separated') else [
            '--config-env=core.hooksPath=HOOKDIR']
        args = option + args
    elif style == 'inline-include':
        included = tmp_path / 'included.config'
        included.write_text('[core] hooksPath = ' + str(hooks) + '\n')
        with (repo / '.git' / 'config').open('a') as stream:
            stream.write('\n[include] path = ' + str(included) + '\n')
    elif style in {'inline-section', 'global-inline-section'}:
        config = (repo / '.git' / 'config' if style == 'inline-section'
                  else tmp_path / 'home' / '.gitconfig')
        with config.open('a') as stream:
            stream.write('\n[core] hooksPath = ' + str(hooks) + '\n')
    if style != 'default-hook':
        assert git(*option, 'config', '--get', 'core.hooksPath').stdout.strip() == str(hooks)

    def no_subprocess(*args, **kwargs):
        pytest.fail('scope resolution must not execute Git or configured helpers')

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, 'run', no_subprocess)
        scope = git_command_scopes(shlex.join(['git', *args]), str(repo),
                                   environment=environment)[0]
    assert ('tree', str(repo)) in scope.writer_claims()
    with _held_elsewhere(workspace_file_write_lease, marker):
        with pytest.raises(WorkspaceBusyError):
            with workspace_claims_lease(scope.writer_claims()):
                git(*args)
        assert marker.read_text() == 'keep\n'
    with workspace_claims_lease(scope.writer_claims()):
        git(*args)
    assert marker.read_text() == 'called'  # Prove the real callback is reachable.


@pytest.mark.parametrize('use_c_option', [False, True])
def test_relative_common_dir_object_target_contends(real_git, tmp_path, use_c_option):
    repo, environment, git = real_git
    target = tmp_path / 'target'
    git('init', '-q', str(target))
    environment = {**environment, 'GIT_COMMON_DIR': '../target/.git'}
    cwd = tmp_path if use_c_option else repo
    args = (['-C', str(repo)] if use_c_option else []) + ['hash-object', '-w', '/dev/null']
    scope = git_command_scopes(shlex.join(['git', *args]), str(cwd),
                               environment=environment)[0]
    assert str(target / '.git') in scope.paths
    assert str(repo / 'target' / '.git') not in scope.paths
    with _held_elsewhere(workspace_git_metadata_lease, target):
        with pytest.raises(WorkspaceBusyError):
            with workspace_claims_lease(scope.writer_claims()):
                git(*args, cwd=cwd, env=environment)
    with workspace_claims_lease(scope.writer_claims()):
        blob = git(*args, cwd=cwd, env=environment).stdout.strip()
    assert (target / '.git' / 'objects' / blob[:2] / blob[2:]).is_file()
    assert not (repo / '.git' / 'objects' / blob[:2] / blob[2:]).exists()


def test_plain_soft_reset_retains_metadata_only_admission(real_git):
    repo, environment, git = real_git
    marker = repo / 'protected'
    marker.write_text('keep\n')
    scope = git_command_scopes('git reset --soft HEAD', str(repo), environment=environment)[0]
    assert scope.writer_claims() == (('metadata', str(repo)),)
    with _held_elsewhere(workspace_file_write_lease, marker):
        with workspace_claims_lease(scope.writer_claims()):
            git('reset', '--soft', 'HEAD')
    assert marker.read_text() == 'keep\n'
