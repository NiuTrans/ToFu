"""An explicit native Codex home must survive server relocation without rebinding.

All homes, credentials and executable discovery are synthetic. No host login,
provider request, sign-in or application-storage write is used.
"""
from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.oauth import codex_command_login as command
from lib.oauth.codex_local_login import read_local_login
from tests.oauth.test_codex_command_login import native as native, _events, _token

pytestmark = [pytest.mark.unit, pytest.mark.skipif(os.name == 'nt', reason='POSIX native-shell adapter')]


def _explicit_environment(native, root):
    return {**native['env'], 'CODEX_HOME': str(root),
            'PATH': str(native['home'].parent / 'bin') + os.pathsep + native['env']['PATH']}


def test_missing_explicit_home_fails_before_native_spawn(native, monkeypatch, caplog):
    root = native['home'] / 'operator-home-not-restored'
    environment = _explicit_environment(native, root)
    original_environment = dict(environment)
    assert command.shutil.which('codex', path=environment['PATH'])
    original_auth = native['auth'].read_bytes()
    attempts = []

    def spawn(*args, **kwargs):
        attempts.append((args, kwargs))
        raise OSError(errno.ENOENT, 'native home absent: ' + str(root))

    monkeypatch.setattr(command.subprocess, 'Popen', spawn)
    caplog.set_level(logging.DEBUG, logger=command.__name__)

    snapshot = read_local_login(environment=environment, refresh=True)

    assert snapshot == {'shared_login_issue': 'codex_home_unavailable'}
    assert attempts == []
    assert environment == original_environment
    assert not root.exists()
    assert native['auth'].read_bytes() == original_auth
    assert str(root) not in caplog.text
    assert 'never-copy-refresh' not in caplog.text
    assert command._session is None


@pytest.mark.parametrize('kind', ['file', 'dangling-symlink', 'file-parent'])
def test_explicit_non_directory_is_unavailable_without_spawn(native, kind):
    root = native['home'] / 'operator-home'
    if kind == 'file':
        root.write_text('not a directory')
    elif kind == 'dangling-symlink':
        root.symlink_to(native['home'] / 'not-restored', target_is_directory=True)
    else:
        root.write_text('not a directory')
        root = root / 'nested-home'

    assert read_local_login(environment=_explicit_environment(native, root)) == {
        'shared_login_issue': 'codex_home_unavailable'}
    assert native['processes'] == []


def test_cleared_codex_home_is_native_discovery_not_a_missing_home(native):
    """`.env` `CODEX_HOME=` clears a stale launch value; it names no home to restore."""
    environment = {**_explicit_environment(native, native['home']), 'CODEX_HOME': ''}

    assert not command.explicit_home_is_unavailable(environment=environment)
    snapshot = read_local_login(environment=environment, refresh=True)

    assert snapshot['account_id'] == 'account-a'
    assert snapshot['shared_login_issue'] == ''


@pytest.mark.parametrize('error_number', [errno.EACCES, errno.EPERM, errno.EIO, errno.ELOOP, errno.ESTALE])
def test_permission_and_unknown_home_errors_are_not_absence(native, monkeypatch, caplog, error_number):
    root = native['linked']
    environment = _explicit_environment(native, root)
    original_stat = Path.stat

    def probe(path, *args, **kwargs):
        if path == root:
            raise OSError(error_number, 'private home detail: ' + str(root))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', probe)
    caplog.set_level(logging.DEBUG, logger=command.__name__)

    assert read_local_login(environment=environment, refresh=True) == {
        'shared_login_issue': 'codex_command_unavailable'}
    assert native['processes'] == []
    assert str(root) not in caplog.text


@pytest.mark.parametrize('refresh', [False, True])
def test_restored_directory_retries_same_source_without_failure_ttl(native, refresh):
    root = native['home'] / 'operator-home-not-restored'
    environment = _explicit_environment(native, root)
    original_auth = native['auth'].read_bytes()
    assert read_local_login(environment=environment) == {'shared_login_issue': 'codex_home_unavailable'}
    assert native['processes'] == []

    # The operator restores the existing intended source, not an empty home.
    native['linked'].rename(root)
    snapshot = read_local_login(environment=environment, expected_account_id='account-a', refresh=refresh)

    assert snapshot['account_id'] == 'account-a'
    assert snapshot['access_token']
    assert 'refresh_token' not in snapshot
    assert len(native['processes']) == 1
    assert 'account/read' not in [event['event'] for event in _events(native)]
    assert native['auth'].read_bytes() == original_auth
    assert environment['CODEX_HOME'] == str(root)


@pytest.mark.parametrize('changed_account', [False, True])
def test_changed_environment_retries_but_keeps_account_validation(native, changed_account):
    missing = native['home'] / 'old-server-home'
    environment = _explicit_environment(native, missing)
    assert read_local_login(environment=environment) == {'shared_login_issue': 'codex_home_unavailable'}
    if changed_account:
        native['auth'].write_text(json.dumps(_token('account-b')))
    environment['CODEX_HOME'] = str(native['linked'])

    snapshot = read_local_login(environment=environment, expected_account_id='account-a')

    if changed_account:
        assert snapshot == {'shared_login_issue': 'account_changed'}
    else:
        assert snapshot['account_id'] == 'account-a'
        assert snapshot['access_token']
    assert not missing.exists()
    assert len(native['processes']) == 1
    assert 'account/read' not in [event['event'] for event in _events(native)]


def test_removed_explicit_home_revokes_warm_metadata_and_reaps_child(native):
    root = native['linked']
    environment = _explicit_environment(native, root)
    assert command.command_context(environment=environment)['codex_home'] == str(root)
    child, reaper = native['processes'][0], command._session_reaper
    original_auth = native['auth'].read_bytes()
    displaced = root.with_name('temporarily-unmounted')
    root.rename(displaced)

    assert command.command_context(environment=environment) == {
        'shared_login_issue': 'codex_home_unavailable'}
    assert command._verified is None
    assert command._session is None
    assert child.poll() is not None and child.stdin.closed and child.stdout.closed
    reaper.join(timeout=2)
    assert not reaper.is_alive()
    assert len(native['processes']) == 1
    assert native['auth'].read_bytes() == original_auth

    displaced.rename(root)
    assert read_local_login(environment=environment, expected_account_id='account-a')['account_id'] == 'account-a'
    assert len(native['processes']) == 2
    assert 'account/read' not in [event['event'] for event in _events(native)]
    command.clear_command_context()
    assert command._cached is None and command._verified is None and command._session is None
    assert all(child.poll() is not None and child.stdin.closed and child.stdout.closed
               for child in native['processes'])


def test_unknown_home_fault_cannot_use_verified_metadata(native, monkeypatch):
    environment = _explicit_environment(native, native['linked'])
    assert command.command_context(environment=environment)['codex_home'] == str(native['linked'])
    child = native['processes'][0]
    original_stat = Path.stat

    def probe(path, *args, **kwargs):
        if path == native['linked']:
            raise OSError(errno.EACCES, 'permission denied')
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'stat', probe)
        assert command.command_context(environment=environment) == {
            'shared_login_issue': 'codex_command_unavailable'}
        assert command._verified is None and command._session is None
        assert child.poll() is not None
        assert len(native['processes']) == 1

    assert read_local_login(environment=environment, refresh=True)['account_id'] == 'account-a'
    assert 'account/read' not in [event['event'] for event in _events(native)]


@pytest.mark.parametrize('surface', ['metadata', 'local_read', 'discovery'])
def test_home_disappearing_during_warm_stamp_read_revokes_grace(
        native, monkeypatch, surface):
    from lib.oauth import codex_local_login as local, codex_shared_login as shared

    root = native['linked']
    environment = _explicit_environment(native, root)
    assert command.command_context(environment=environment)['codex_home'] == str(root)
    child = native['processes'][0]
    revision = command._source_revision
    displaced = root.with_name('warm-stamp-unmounted-source')
    removed = [False]

    def change_after_stamp(*args, **kwargs):
        value = revision(*args, **kwargs)
        if not removed[0]:
            root.rename(displaced)
            removed[0] = True
        return value

    monkeypatch.setattr(command, '_source_revision', change_after_stamp)
    if surface == 'metadata':
        result = command.command_context(environment=environment)
    elif surface == 'local_read':
        result = read_local_login(environment=environment)
    else:
        original_read = local.read_local_login
        monkeypatch.setattr(shared, 'local_owner', lambda owner: owner)
        monkeypatch.setenv('CODEX_HOME', str(root))
        monkeypatch.setattr(local, 'read_local_login', lambda **kwargs: original_read(
            environment=environment, **kwargs))

        def forbidden(*args, **kwargs):
            pytest.fail('warm cache loss cannot authorize another identity')

        monkeypatch.setattr(shared, '_agents', forbidden)
        monkeypatch.setattr(shared, '_remote_snapshot', forbidden)
        assert shared.discover_reference(owner_user_id=1) is None
        result = command._cached[2]
    assert result == {'shared_login_issue': 'codex_home_unavailable'}
    assert command._verified is None and command._session is None
    assert child.poll() is not None and child.stdin.closed and child.stdout.closed
    assert displaced.exists() and not root.exists()
    assert len(native['processes']) == 1
    displaced.rename(root)
    assert read_local_login(environment=environment)['account_id'] == 'account-a'
    assert len(native['processes']) == 2


@pytest.mark.parametrize('file_issue,expected', [
    ('', 'codex_home_unavailable'),
    ('codex_not_signed_in', 'codex_home_unavailable'),
    ('codex_home_storage_full', 'codex_home_storage_full'),
    ('account_changed', 'account_changed'),
])
def test_home_disappearing_during_renewal_file_read_never_publishes_healthy_source(
        native, monkeypatch, file_issue, expected):
    from lib.oauth import codex_local_login as local

    root = native['linked']
    environment = _explicit_environment(native, root)
    assert command.command_context(environment=environment)['codex_home'] == str(root)
    child = native['processes'][0]
    displaced = root.with_name('renewal-read-unmounted-source')

    def file_read(**kwargs):
        root.rename(displaced)
        return ({'shared_login_issue': file_issue} if file_issue else
                {'account_id': 'account-a', 'access_token': 'synthetic-never-dispatched'})

    def forbidden(*args, **kwargs):
        pytest.fail('an unavailable source cannot start native renewal')

    monkeypatch.setattr(local, '_read_file_login', file_read)
    monkeypatch.setattr(command, '_exchange', forbidden)
    result = command.renew_command_login(root, expected_account_id='account-a',
        settings={}, environment=environment)
    assert result == {'shared_login_issue': expected}
    assert command._verified is None and command._session is None
    assert child.poll() is not None and child.stdin.closed and child.stdout.closed
    assert not command._lock.locked()
    assert displaced.exists() and not root.exists()
    assert len(native['processes']) == 1


def test_full_explicit_home_keeps_bounded_verified_metadata(native, monkeypatch):
    environment = _explicit_environment(native, native['linked'])
    original = command.command_context(environment=environment)
    original_limit = command._verified[1]
    monkeypatch.setattr(command, '_cached', (command._cached[0], 0, command._cached[2]))
    original_stat = Path.stat

    def probe(path, *args, **kwargs):
        if path == native['linked']:
            raise OSError(errno.ENOSPC, 'No space left on device')
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', probe)
    assert command.command_context(environment=environment, retry_failed=True) == original
    assert command._cached[2] == {'shared_login_issue': 'codex_home_storage_full'}
    assert command._verified[1] == original_limit
    assert command._session is None
    assert len(native['processes']) == 1 and native['processes'][0].poll() is not None


def test_relative_missing_override_is_checked_against_launch_home(native):
    environment = _explicit_environment(native, 'relative-operator-home-not-restored')
    assert command.command_context(environment=environment) == {
        'shared_login_issue': 'codex_home_unavailable'}
    assert native['processes'] == []


def test_existing_explicit_home_still_reports_storage_full(native, monkeypatch):
    environment = _explicit_environment(native, native['linked'])
    monkeypatch.setattr(command.os, 'statvfs', lambda path: SimpleNamespace(
        f_files=1_000_000, f_favail=0, f_blocks=10_000_000, f_bavail=5_000_000, f_frsize=4096))
    assert command.command_context(environment=environment) == {
        'shared_login_issue': 'codex_home_storage_full'}
    assert native['processes'] == []


def test_home_stat_enospc_keeps_storage_full_attribution(native, monkeypatch):
    environment = _explicit_environment(native, native['linked'])
    original_stat = Path.stat

    def probe(path, *args, **kwargs):
        if path == native['linked']:
            raise OSError(errno.ENOSPC, 'No space left on device')
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', probe)
    assert command.command_context(environment=environment) == {
        'shared_login_issue': 'codex_home_storage_full'}
    assert native['processes'] == []


def test_missing_home_is_not_misreported_as_storage_full(native, monkeypatch):
    environment = _explicit_environment(native, native['home'] / 'operator-home-not-restored')
    monkeypatch.setattr(command.os, 'statvfs', lambda path: SimpleNamespace(
        f_files=1_000_000, f_favail=0, f_blocks=10_000_000, f_bavail=0, f_frsize=4096))
    assert command.command_context(environment=environment) == {
        'shared_login_issue': 'codex_home_unavailable'}
    assert native['processes'] == []


def test_bound_access_keeps_home_issue_transient_and_reference_intact(native, monkeypatch, caplog):
    from lib.llm.llm_errors import SubscriptionCredentialUnavailableError
    from lib.oauth import codex_local_login as local, codex_shared_login as shared

    root = native['home'] / 'operator-home-not-restored'
    environment = _explicit_environment(native, root)
    reference = {'kind': 'local', 'owner_user_id': 1, 'account_id': 'account-a'}
    original_reference = dict(reference)
    original_read = local.read_local_login
    monkeypatch.setattr(shared, 'local_owner', lambda owner: owner)
    monkeypatch.setattr(local, 'read_local_login', lambda **kwargs: original_read(environment=environment, **kwargs))

    def refresh(bound):
        assert bound == reference
        return shared.resolve_reference(bound, refresh=True)

    def forbidden(*args, **kwargs):
        pytest.fail('bound local migration failure must not select another source')

    monkeypatch.setattr(shared, 'refresh_bound_reference', refresh)
    monkeypatch.setattr(shared, '_remote_snapshot', forbidden)
    caplog.set_level(logging.WARNING, logger=shared.__name__)
    with pytest.raises(SubscriptionCredentialUnavailableError) as raised:
        shared.recover_bound_access(reference, initial_issue='codex_home_unavailable')
    assert raised.value.reason == 'codex_home_unavailable'
    assert raised.value.transient is True
    assert raised.value.credential_source == 'local'
    assert reference == original_reference
    assert native['processes'] == []
    assert str(root) not in caplog.text
    assert 'never-copy-refresh' not in caplog.text

    native['linked'].rename(root)
    assert shared.recover_bound_access(reference, initial_issue='codex_home_unavailable')
    assert reference == original_reference
    assert 'account/read' not in [event['event'] for event in _events(native)]


def test_missing_explicit_home_does_not_discover_another_device(native, monkeypatch):
    from lib.oauth import codex_local_login as local, codex_shared_login as shared

    root = native['home'] / 'operator-home-not-restored'
    environment = _explicit_environment(native, root)
    original_read = local.read_local_login
    monkeypatch.setattr(shared, 'local_owner', lambda owner: owner)
    monkeypatch.setattr(local, 'read_local_login', lambda **kwargs: original_read(environment=environment, **kwargs))
    calls = []

    def agents(owner):
        calls.append(owner)
        return []

    monkeypatch.setattr(shared, '_agents', agents)

    assert shared.discover_reference(owner_user_id=1, expected_account_id='account-a') is None
    assert calls == []
    assert native['processes'] == []
    assert not root.exists()


def test_home_unavailable_remains_distinct_while_native_lock_is_busy(native, monkeypatch):
    environment = _explicit_environment(native, native['home'] / 'operator-home-not-restored')
    monkeypatch.setattr(command, '_EXCHANGE_SECONDS', 0.02)
    assert command._lock.acquire(timeout=1)
    try:
        assert command.command_context(environment=environment) == {
            'shared_login_issue': 'codex_home_unavailable'}
    finally:
        command._lock.release()
    assert native['processes'] == []


@pytest.mark.parametrize('failure', ['early_exit', 'broken_pipe', 'spawn_failure',
                                     'metadata_complete', 'storage_full'])
@pytest.mark.parametrize('surface', ['metadata', 'discovery'])
def test_home_disappearing_during_native_startup_is_classified_and_recovers_without_ttl(
        native, monkeypatch, failure, surface):
    import io
    import stat

    root = native['home'] / 'synthetic-unmounted-source'
    state = {'exists': True, 'spawns': 0}
    original_stat = Path.stat

    class Input(io.BytesIO):
        def write(self, data):
            state['exists'] = False
            if failure == 'broken_pipe':
                raise BrokenPipeError(errno.EPIPE, 'synthetic pipe failure')
            if failure == 'storage_full':
                raise OSError(errno.ENOSPC, 'synthetic storage failure')
            return super().write(data)

    class Process:
        pid = 42424242
        returncode = 3

        def __init__(self):
            replies = []
            if failure == 'metadata_complete':
                replies = [
                    {'id': 1, 'result': {'codexHome': str(root)}},
                    {'id': 2, 'result': {'config': {}}},
                    {'id': 3, 'result': {'requirements': None}},
                ]
            output = b''.join(json.dumps(reply).encode() + b'\n' for reply in replies)
            self.stdin, self.stdout = Input(), io.BytesIO(output)

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    process = Process()

    def spawn(*args, **kwargs):
        state['spawns'] += 1
        assert state['exists']
        if failure == 'spawn_failure':
            state['exists'] = False
            raise FileNotFoundError(errno.ENOENT, 'synthetic spawn failure')
        return process

    def probe(path, *args, **kwargs):
        if path == root:
            if not state['exists']:
                raise FileNotFoundError(errno.ENOENT, 'synthetic absence')
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700)
        return original_stat(path, *args, **kwargs)

    subprocess = command.subprocess
    monkeypatch.setattr(command, '_native_shell', lambda environment: None)
    monkeypatch.setattr(Path, 'stat', probe)
    monkeypatch.setattr(command, '_home_storage_full', lambda *a: False)
    monkeypatch.setattr(command, '_launch', lambda *a: ['never-executed'])
    monkeypatch.setattr(command, 'subprocess', SimpleNamespace(
        Popen=spawn, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(command.os, 'killpg', lambda *a: None)
    environment = _explicit_environment(native, root)
    expected = ('codex_home_storage_full' if failure == 'storage_full'
                else 'codex_home_unavailable')
    if surface == 'discovery':
        from lib.oauth import codex_local_login as local, codex_shared_login as shared

        original_read = local.read_local_login
        monkeypatch.setattr(shared, 'local_owner', lambda owner: owner)
        monkeypatch.setenv('CODEX_HOME', str(root))
        monkeypatch.setattr(local, 'read_local_login', lambda **kwargs: original_read(
            environment=environment, **kwargs))

        def forbidden(*args, **kwargs):
            pytest.fail('a disappearing explicit source cannot discover another identity')

        monkeypatch.setattr(shared, '_agents', forbidden)
        monkeypatch.setattr(shared, '_remote_snapshot', forbidden)
        assert shared.discover_reference(owner_user_id=1) is None
        assert command._cached[2] == {'shared_login_issue': expected}
    else:
        assert command.command_context(environment=environment) == {
            'shared_login_issue': expected}
    assert command._session is None and command._verified is None
    assert state['spawns'] == 1
    if failure != 'spawn_failure':
        assert process.stdin.closed and process.stdout.closed
    state['exists'] = True
    recovery_calls = []

    def restored(*args, **kwargs):
        recovery_calls.append(1)
        return {'codex_home': str(root), 'settings': {}, 'shared_login_issue': ''}

    monkeypatch.setattr(command, '_exchange', restored)
    monkeypatch.setattr(command, '_source_revision', lambda *a: ())
    assert command.command_context(environment=environment)['codex_home'] == str(root)
    assert recovery_calls == [1]


@pytest.mark.parametrize('surface', ['metadata', 'renewal'])
@pytest.mark.parametrize('failure,expected', [
    ('pipe', 'codex_home_unavailable'),
    ('generic', 'codex_home_unavailable'),
    ('storage_full', 'codex_home_storage_full'),
    ('policy', 'login_method_restricted'),
    ('source_changed', 'codex_source_changed'),
])
def test_late_home_failure_keeps_positive_cause_and_source_boundary(
        native, monkeypatch, surface, failure, expected):
    root = native['linked']
    environment = _explicit_environment(native, root)
    displaced = root.with_name('late-unmounted-source')

    def fail():
        if root.exists():
            root.rename(displaced)
        if failure == 'pipe':
            raise BrokenPipeError(errno.EPIPE, 'synthetic pipe detail')
        if failure == 'storage_full':
            raise OSError(errno.ENOSPC, 'synthetic full filesystem')
        if failure == 'policy':
            raise command._CommandFailure('login_method_restricted')
        if failure == 'source_changed':
            raise command._CommandFailure('codex_source_changed')
        raise command._CommandFailure('codex_command_unavailable')

    def exchange(*args, **kwargs):
        fail()

    monkeypatch.setattr(command, '_exchange', exchange)
    if surface == 'renewal':
        from lib.oauth import codex_local_login as local

        monkeypatch.setattr(local, '_read_file_login', lambda **kwargs: {
            'shared_login_issue': 'codex_access_expired', 'account_id': 'account-a'})
        result = command.renew_command_login(root, expected_account_id='account-a',
            settings={}, environment=environment)
    else:
        result = command.command_context(environment=environment)

    assert result == {'shared_login_issue': expected}
    assert command._session is None and command._verified is None
    assert displaced.exists() and not root.exists()
    assert not command._lock.locked()


@pytest.mark.parametrize('error_number,expected', [
    (errno.ENOENT, 'codex_home_unavailable'),
    (errno.EACCES, 'codex_home_unavailable'),
    (errno.ENOSPC, 'codex_home_storage_full'),
])
def test_home_disappearing_during_metadata_stamp_read_is_classified(
        native, monkeypatch, error_number, expected):
    root = native['linked']
    environment = _explicit_environment(native, root)
    displaced = root.with_name('unmounted-during-stamp')

    def exchange(*args, **kwargs):
        return {'codex_home': str(root), 'settings': {}, 'shared_login_issue': ''}

    def revision(*args, **kwargs):
        root.rename(displaced)
        raise OSError(error_number, 'synthetic metadata stamp failure')

    monkeypatch.setattr(command, '_exchange', exchange)
    monkeypatch.setattr(command, '_source_revision', revision)
    assert command.command_context(environment=environment) == {
        'shared_login_issue': expected}
    assert command._verified is None and command._session is None


def test_late_unknown_home_fault_revokes_verified_grace(native, monkeypatch):
    root = native['linked']
    environment = _explicit_environment(native, root)
    assert command.command_context(environment=environment)['codex_home'] == str(root)
    child = native['processes'][0]
    original_stat = Path.stat
    state = {'failed': False}

    def probe(path, *args, **kwargs):
        if path == root and state['failed']:
            raise PermissionError(errno.EACCES, 'synthetic inaccessible home')
        return original_stat(path, *args, **kwargs)

    def exchange(*args, **kwargs):
        state['failed'] = True
        raise BrokenPipeError(errno.EPIPE, 'synthetic command pipe failure')

    monkeypatch.setattr(command, '_cached', (command._cached[0], 0, command._cached[2]))
    monkeypatch.setattr(Path, 'stat', probe)
    monkeypatch.setattr(command, '_exchange', exchange)
    assert command.command_context(environment=environment, retry_failed=True) == {
        'shared_login_issue': 'codex_command_unavailable'}
    assert command._verified is None and command._session is None
    assert child.poll() is not None and child.stdin.closed and child.stdout.closed
