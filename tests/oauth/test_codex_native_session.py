"""Synthetic native transport lifecycle; never inspect the host Codex login."""
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.oauth import codex_command_login as command
from tests.oauth.test_codex_command_login import native as native, _events, _expire_metadata

pytestmark = [pytest.mark.unit, pytest.mark.skipif(os.name == 'nt', reason='POSIX native-shell adapter')]


def context(native, **kwargs):
    return command.command_context(environment=native['env'], **kwargs)


def test_cold_initialize_is_not_repeated_at_cache_expiry(native, monkeypatch):
    native['mode'].write_text('cold')
    before = time.monotonic()
    assert not context(native)['shared_login_issue']
    cold_elapsed = time.monotonic() - before
    for _ in range(4):
        _expire_metadata(monkeypatch)
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _: context(native, retry_failed=True), range(16)))
        assert all(not value['shared_login_issue'] for value in values)
    events = [item['event'] for item in _events(native)]
    assert events.count('initialize') == events.count('start') == 1
    assert events.count('config/read') == events.count('configRequirements/read') == 5
    assert 'account/read' not in events
    assert cold_elapsed >= 0.25
    assert command._verified[1] == command._session['born'] + command._CONTEXT_MAX_AGE_SECONDS


@pytest.mark.parametrize('method,phase', [('initialize', 'initialize'),
    ('config/read', 'config'), ('configRequirements/read', 'requirements')])
def test_phase_hang_is_bounded_redacted_and_reaped(native, monkeypatch, caplog, method, phase):
    native['mode'].write_text('hang-' + method)
    monkeypatch.setattr(command, '_DEADLINE_SECONDS', 0.3)
    caplog.set_level(logging.DEBUG, logger=command.__name__)
    assert context(native) == {'shared_login_issue': 'codex_command_timeout'}
    assert 'phase=' + phase in caplog.text
    assert 'phase=spawn' in caplog.text and 'phase=initialize_sent' in caplog.text
    if method != 'initialize':
        assert 'phase=initialize_received' in caplog.text
    assert str(native['home']) not in caplog.text
    assert 'never-copy-refresh' not in caplog.text
    assert command._session is None
    assert all(child.poll() is not None and child.stdin.closed and child.stdout.closed
               for child in native['processes'])


def test_shell_hang_is_honestly_pre_initialize_response(native, monkeypatch, caplog):
    with (native['home'] / '.bashrc').open('a') as stream:
        stream.write('\nsleep 60\n')
    monkeypatch.setattr(command, '_DEADLINE_SECONDS', 0.3)
    caplog.set_level(logging.DEBUG, logger=command.__name__)
    assert context(native) == {'shared_login_issue': 'codex_command_timeout'}
    assert 'phase=initialize_sent' in caplog.text
    assert 'phase=initialize_received' not in caplog.text
    # SIGTERM kills the startup sleep; an interactive shell can briefly
    # continue into exec during teardown. No boot-boundary claim relies on
    # whether the synthetic trace was created in that teardown race.
    assert all(child.poll() is not None for child in native['processes'])


def test_idle_reclamation_joins_reader_and_reaper(native, monkeypatch):
    monkeypatch.setattr(command, '_SESSION_IDLE_SECONDS', 0.05)
    assert not context(native)['shared_login_issue']
    reaper = command._session_reaper
    reaper.join(timeout=3)
    assert not reaper.is_alive()
    assert command._session is None and command._session_reaper is None
    assert all(child.poll() is not None and child.stdout.closed for child in native['processes'])


@pytest.mark.parametrize('source', ['environment', 'shell', 'config', 'executable'])
def test_source_change_cold_relaunches_instead_of_reusing_loaded_policy(native, monkeypatch, source):
    assert not context(native)['shared_login_issue']
    first = native['processes'][0]
    if source == 'environment':
        native['env']['SYNTHETIC_CODEX_POLICY_INPUT'] = 'changed'
    else:
        path = {'shell': native['home'] / '.bashrc', 'config': native['root'] / 'config.toml',
                'executable': native['home'].parent / 'bin' / 'codex'}[source]
        with path.open('a') as stream:
            stream.write('\n# source changed\n')
        # The shell-only PATH is not introspected; unknown dependencies are
        # instead bounded by native session rotation.
        if source == 'executable':
            command._session['born'] -= command._SESSION_ROTATE_SECONDS
            _expire_metadata(monkeypatch)
    assert not context(native, retry_failed=True)['shared_login_issue']
    assert len(native['processes']) == 2 and first.poll() is not None


def test_rotation_preserves_original_absolute_policy_bound(native, monkeypatch):
    assert not context(native)['shared_login_issue']
    first = native['processes'][0]
    command._session['born'] -= command._SESSION_ROTATE_SECONDS
    _expire_metadata(monkeypatch)
    assert not context(native, retry_failed=True)['shared_login_issue']
    assert first.poll() is not None
    assert [item['event'] for item in _events(native)].count('initialize') == 2
    command.clear_command_context()
    assert command._session is None and command._session_reaper is None
    assert all(child.poll() is not None for child in native['processes'])


def test_reaper_admission_failure_reaps_transport(native, monkeypatch):
    original = command.threading.Thread
    def admit(**kwargs):
        if kwargs.get('name') == 'codex-metadata-reaper':
            raise RuntimeError('synthetic admission failure')
        return original(**kwargs)
    monkeypatch.setattr(command.threading, 'Thread', admit)
    assert context(native) == {'shared_login_issue': 'codex_command_unavailable'}
    assert command._session is None and command._session_reaper is None
    assert all(child.poll() is not None for child in native['processes'])


def test_absolute_session_age_reaps_even_without_idle_expiry(native, monkeypatch):
    monkeypatch.setattr(command, '_SESSION_IDLE_SECONDS', 10)
    monkeypatch.setattr(command, '_CONTEXT_MAX_AGE_SECONDS', 0.2)
    assert not context(native)['shared_login_issue']
    first = native['processes'][0]
    reaper = command._session_reaper
    reaper.join(timeout=3)
    assert not reaper.is_alive() and first.poll() is not None
    assert command._session is None
    assert not context(native)['shared_login_issue']
    assert len(native['processes']) == 2


def test_policy_mutation_during_read_cannot_publish_verified_metadata(native):
    native['mode'].write_text('mutate-policy')
    assert context(native) == {'shared_login_issue': 'codex_source_changed'}
    assert command._verified is None and command._session is None
    assert all(child.poll() is not None for child in native['processes'])


def test_retired_reaper_cannot_touch_a_new_generation(native):
    assert not context(native)['shared_login_issue']
    old_owner, old_wake = command._session_reaper, command._session_wake
    command.clear_command_context()
    assert not old_owner.is_alive() and old_wake.is_set()
    assert not context(native)['shared_login_issue']
    fresh = command._session
    # Simulate delayed entry of the old target after a new admission.
    command._reap_idle_session(old_owner, old_wake)
    assert command._session is fresh and native['processes'][-1].poll() is None
    assert command._session_wake is not old_wake


def test_native_death_discards_transport_without_extending_policy_grace(native, monkeypatch):
    assert not context(native)['shared_login_issue']
    original_limit = command._verified[1]
    child = native['processes'][0]
    child.terminate()
    child.wait(timeout=2)
    _expire_metadata(monkeypatch)
    assert not context(native, retry_failed=True)['shared_login_issue']
    assert command._session is None
    assert command._cached[2] == {'shared_login_issue': 'codex_command_unavailable'}
    assert command._verified[1] == original_limit
    assert not context(native)['shared_login_issue']
    assert len(native['processes']) == 1
