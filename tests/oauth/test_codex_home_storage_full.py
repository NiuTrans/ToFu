"""A full Codex home must read as storage-full, never as a broken command.

Incident (2026-10-12): the TofuDB search projection filled /home inodes, so
the Codex app-server child died at startup (sqlite/thread state lives under
the same home) and the TUI could not create thread-writer locks. Tofu
reported ``codex_command_unavailable`` ("check Settings → Subscription"),
which invites a pointless re-login: no credential is wrong, the disk is
full. ``codex_home_storage_full`` keeps that failure transient, retryable
and actionable instead.
"""
from __future__ import annotations

import errno
import os
import subprocess
from types import SimpleNamespace

import pytest

from lib.oauth import codex_command_login as command

pytestmark = [pytest.mark.unit, pytest.mark.skipif(os.name == 'nt', reason='POSIX native-shell adapter')]


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    env = {'HOME': str(home), 'SHELL': '/bin/bash', 'PATH': '/usr/bin:/bin'}
    command.clear_command_context()
    yield {'home': home, 'env': env}
    command.clear_command_context()


def _statvfs(*, files=1_000_000, favail=500_000, blocks=10_000_000,
             bavail=5_000_000, frsize=4096):
    return SimpleNamespace(f_files=files, f_favail=favail, f_blocks=blocks,
                           f_bavail=bavail, f_frsize=frsize)


def test_preflight_reports_storage_full_before_spawning(isolated_home, monkeypatch):
    monkeypatch.setattr(command.os, 'statvfs', lambda path: _statvfs(favail=0))
    context = command.command_context(environment=isolated_home['env'])
    assert context == {'shared_login_issue': 'codex_home_storage_full'}


def test_enospc_spawn_failure_maps_to_storage_full(isolated_home, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError(errno.ENOSPC, 'No space left on device')

    monkeypatch.setattr(command, 'subprocess', SimpleNamespace(
        Popen=boom, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired))
    context = command.command_context(environment=isolated_home['env'])
    assert context == {'shared_login_issue': 'codex_home_storage_full'}


def test_other_spawn_failures_stay_generic(isolated_home, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError(errno.EACCES, 'Permission denied')

    monkeypatch.setattr(command, 'subprocess', SimpleNamespace(
        Popen=boom, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired))
    context = command.command_context(environment=isolated_home['env'])
    assert context == {'shared_login_issue': 'codex_command_unavailable'}


def test_early_child_exit_consults_home_capacity(isolated_home, monkeypatch):
    monkeypatch.setattr(command, '_launch',
                        lambda environment, home: ['/bin/sh', '-c', 'exit 3'])
    spawned = False
    original_popen = command.subprocess.Popen
    def spawn(*args, **kwargs):
        nonlocal spawned
        process = original_popen(*args, **kwargs)
        spawned = True
        return process
    monkeypatch.setattr(command.subprocess, 'Popen', spawn)
    monkeypatch.setattr(command.os, 'statvfs',
                        lambda path: _statvfs(favail=0 if spawned else 500_000))
    assert command.command_context(
        environment=isolated_home['env']) == {'shared_login_issue': 'codex_home_storage_full'}
    assert spawned


def test_unknown_capacity_filesystem_is_not_full(isolated_home, monkeypatch):
    monkeypatch.setattr(command, '_launch',
                        lambda environment, home: ['/bin/sh', '-c', 'exit 3'])
    monkeypatch.setattr(command.os, 'statvfs',
                        lambda path: _statvfs(files=0, blocks=0))
    assert command.command_context(
        environment=isolated_home['env']) == {'shared_login_issue': 'codex_command_unavailable'}


def test_storage_full_is_transient_and_retryable():
    assert 'codex_home_storage_full' in command._TRANSIENT_ISSUES
    from lib.oauth import codex_shared_login
    assert 'codex_home_storage_full' in codex_shared_login._TRANSIENT_READ_ISSUES
    from lib.llm.llm_errors import (LocalRequestPreparationError,
                                SubscriptionCredentialUnavailableError)
    cause = SubscriptionCredentialUnavailableError(
        reason='codex_home_storage_full', credential_source='local', transient=True)
    error = LocalRequestPreparationError(f'local request preparation failed: {cause}')
    error.__cause__ = cause
    from lib.error_envelope import from_exception
    envelope = from_exception(error, model='gpt-6.1-sol')
    assert 'storage is full' in envelope['message']
    assert envelope['retryable'] is True
    assert envelope['severity'] == 'warning'
    assert 're-login' in envelope['hint']


def test_known_full_bytes_are_detected_without_inode_accounting(isolated_home, monkeypatch):
    monkeypatch.setattr(command.os, 'statvfs', lambda path: _statvfs(files=0, bavail=0))
    assert command.command_context(environment=isolated_home['env']) == {
        'shared_login_issue': 'codex_home_storage_full'}


def test_unknown_bytes_do_not_hide_known_full_inodes(isolated_home, monkeypatch):
    monkeypatch.setattr(command.os, 'statvfs', lambda path: _statvfs(favail=0, blocks=0))
    assert command.command_context(environment=isolated_home['env']) == {
        'shared_login_issue': 'codex_home_storage_full'}
