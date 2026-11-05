"""Worktree retirement targets, configuration evidence and native exclusion.

All mutations occur in private repositories. Presence is tested at the dispatch
boundary separately; these cases prove actual Git and lease behavior together.
"""
from contextlib import contextmanager
import os
import shlex
import subprocess
import threading

import pytest

from lib.project_mod.git_command_scope import git_command_scopes
from lib.project_mod import git_guard

pytestmark = pytest.mark.unit


@pytest.fixture
def linked_checkout(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                 'GIT_CONFIG', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS',
                 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_GLOBAL'):
        monkeypatch.delenv(name, raising=False)
    parent, target = tmp_path / 'shared', tmp_path / 'verification'

    def git(*args, cwd=parent, check=True):
        return subprocess.run(['git', *args], cwd=cwd, check=check, text=True,
                              capture_output=True, timeout=10)

    git('init', '-q', str(parent), cwd=tmp_path)
    (parent / 'source').write_text('retained\n')
    git('add', 'source')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'base')
    git('worktree', 'add', '--detach', str(target), 'HEAD')
    git('config', 'extensions.worktreeConfig', 'true')
    git('config', '--worktree', 'core.fileMode', 'false')
    git('config', '--worktree', 'core.fileMode', 'false', cwd=target)
    return parent, target, git


@contextmanager
def held_by_other_thread(factory, path):
    ready, release = threading.Event(), threading.Event()
    failures = []

    def hold():
        try:
            with factory(str(path)):
                ready.set()
                assert release.wait(10)
        except BaseException as error:
            failures.append(error)
            ready.set()

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert ready.wait(10) and not failures
        yield
    finally:
        release.set()
        worker.join(10)
        assert not worker.is_alive() and not failures


def removal_scope(parent, target, force=False):
    command = shlex.join(['git', 'worktree', 'remove',
                         *(['--force'] if force else []), str(target)])
    return git_command_scopes(command, str(parent), environment=os.environ)[0]


@pytest.mark.parametrize('operation', ['remove', 'move'])
@pytest.mark.parametrize('option', ['--force', '-f', '--force --force'])
def test_force_cannot_borrow_solo_presence_as_content_review(operation, option):
    operands = '/tmp/owned-tree' if operation == 'remove' else '/tmp/owned-tree /tmp/new-tree'
    command = f'git worktree {operation} {option} {operands}'
    assert git_guard.classify(command) == 'tree_destroying'
    assert git_guard.automatic_receipt_eligible(command) is False
    evidence = git_guard.command_diagnostics(command)['segments'][0]
    assert evidence['reasonCode'] == 'forced_worktree_retirement'
    assert evidence['effects'] == ['target_worktree', 'worktree_metadata']
    assert evidence['nextAction'] == 'verify_target_ownership_and_discarded_content'


def test_normal_cleanup_remains_eligible_without_forced_cleanup_in_chain():
    assert git_guard.automatic_receipt_eligible('git worktree remove /tmp/owned-tree')
    assert not git_guard.automatic_receipt_eligible(
        'git branch -d topic && git worktree remove --force /tmp/owned-tree && git worktree list')
