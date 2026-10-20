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
from lib.project_mod.workspace_lease import (
    WorkspaceBusyError, workspace_claims_lease, workspace_file_write_lease,
    workspace_git_metadata_lease,
)

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


def test_plain_cleanup_runs_while_main_source_is_being_edited(linked_checkout):
    parent, target, git = linked_checkout
    scope = removal_scope(parent, target)
    assert scope.presence_roots() == (str(target),)
    assert set(scope.writer_claims()) == {('metadata', str(parent)), ('tree', str(target))}
    with held_by_other_thread(workspace_file_write_lease, parent / 'source'):
        with workspace_claims_lease(scope.writer_claims()):
            git('worktree', 'remove', str(target))
    assert not target.exists()
    assert (parent / 'source').read_text() == 'retained\n'


@pytest.mark.parametrize('held_scope', ['target-file', 'common-metadata'])
def test_cleanup_keeps_actual_target_and_shared_metadata_exclusion(linked_checkout, held_scope):
    parent, target, git = linked_checkout
    factory, path = ((workspace_file_write_lease, target / 'source')
                     if held_scope == 'target-file' else (workspace_git_metadata_lease, parent))
    with held_by_other_thread(factory, path):
        with pytest.raises(WorkspaceBusyError):
            with workspace_claims_lease(removal_scope(parent, target).writer_claims()):
                git('worktree', 'remove', str(target))
    assert target.is_dir()
    assert (target / 'source').read_text() == 'retained\n'


@pytest.mark.parametrize('configuration_owner', ['invoker', 'target'])
@pytest.mark.parametrize('key,value', [
    ('core.fsmonitor', '/untrusted/monitor'),
    ('core.hooksPath', '/untrusted/hooks'),
    ('include.path', '/unresolved/config'),
    ('core.worktree', '/redirected/tree'),
])
def test_worktree_specific_helpers_and_redirects_retain_conservative_scope(
        linked_checkout, configuration_owner, key, value):
    parent, target, git = linked_checkout
    git('config', '--worktree', key, value,
        cwd=parent if configuration_owner == 'invoker' else target)
    scope = removal_scope(parent, target)
    assert set(scope.presence_roots()) == {str(parent), str(target)}
    assert ('tree', str(parent)) in scope.writer_claims()


def test_target_status_callback_cannot_write_through_a_main_file_lease(
        linked_checkout, tmp_path):
    parent, target, git = linked_checkout
    marker = parent / 'source'
    monitor = tmp_path / 'status-monitor'
    monitor.write_text('#!/bin/sh\nprintf called > ' + shlex.quote(str(marker))
                       + "\nprintf 'test-token\\0'\n")
    monitor.chmod(0o700)
    git('config', '--worktree', 'core.fsmonitor', str(monitor), cwd=target)
    scope = removal_scope(parent, target)
    with held_by_other_thread(workspace_file_write_lease, marker):
        with pytest.raises(WorkspaceBusyError):
            with workspace_claims_lease(scope.writer_claims()):
                git('worktree', 'remove', str(target))
    assert marker.read_text() == 'retained\n'
    assert target.is_dir()
    with workspace_claims_lease(scope.writer_claims()):
        git('worktree', 'remove', str(target))
    assert marker.read_text() == 'called'  # Native target status invoked the callback.


def test_target_alias_keeps_physical_tree_identity(linked_checkout, tmp_path):
    parent, target, _ = linked_checkout
    alias = tmp_path / 'alias'
    alias.symlink_to(target, target_is_directory=True)
    scope = removal_scope(parent, alias)
    assert scope.presence_roots() == (str(target),)
    assert ('tree', str(target)) in scope.writer_claims()


@pytest.mark.parametrize('existing_directory', [False, True])
def test_move_uses_actual_destination_without_borrowing_parent_activity(
        linked_checkout, existing_directory):
    parent, target, git = linked_checkout
    destination = parent / 'moved'
    if existing_directory:
        destination.mkdir()
    actual = destination / target.name if existing_directory else destination
    command = shlex.join(['git', 'worktree', 'move', str(target), str(destination)])
    scope = git_command_scopes(command, str(parent), environment=os.environ)[0]
    assert set(scope.presence_roots()) == {str(target), str(actual)}
    assert set(scope.writer_claims()) == {
        ('metadata', str(parent)), ('tree', str(target)), ('tree', str(actual))}
    with held_by_other_thread(workspace_file_write_lease, parent / 'source'):
        with workspace_claims_lease(scope.writer_claims()):
            git('worktree', 'move', str(target), str(destination))
    assert not target.exists()
    assert (actual / 'source').read_text() == 'retained\n'


def test_dirty_target_remains_protected_by_git_and_force_needs_review(linked_checkout):
    parent, target, git = linked_checkout
    (target / 'untracked').write_text('must retain\n')
    with workspace_claims_lease(removal_scope(parent, target).writer_claims()):
        outcome = git('worktree', 'remove', str(target), check=False)
    assert outcome.returncode != 0
    assert (target / 'untracked').read_text() == 'must retain\n'
    assert not git_guard.automatic_receipt_eligible(
        shlex.join(['git', 'worktree', 'remove', '--force', str(target)]))


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
