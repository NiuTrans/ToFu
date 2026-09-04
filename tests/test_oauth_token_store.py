"""Security and durability contracts for OAuth credential persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest

import lib.oauth.token_store as token_store

pytestmark = pytest.mark.unit


@pytest.fixture
def private_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        token_store, '_config_path', lambda relative: str(tmp_path / relative))
    return tmp_path


@pytest.mark.parametrize('provider', [
    '../codex', 'codex/../../outside', '/tmp/outside', 'unknown', '', None,
])
def test_token_path_rejects_unsupported_or_traversing_provider(
        private_store, provider):
    with pytest.raises(ValueError):
        token_store.token_path(provider)


def test_save_is_atomic_private_and_does_not_mutate_input(private_store):
    original = {'access_token': 'access', 'refresh_token': 'refresh'}

    assert token_store.save_token('codex', original) is True

    path = token_store.token_path('codex')
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert '_saved_at' not in original
    assert token_store.load_token('codex')['refresh_token'] == 'refresh'


def test_failed_replace_keeps_last_good_credentials(private_store):
    assert token_store.save_token(
        'claude', {'access_token': 'old', 'refresh_token': 'old-refresh'})
    path = token_store.token_path('claude')
    before = open(path, 'rb').read()

    with mock.patch('lib.json_store.os.replace',
                    side_effect=OSError('injected disk failure')):
        assert token_store.save_token(
            'claude', {'access_token': 'new', 'refresh_token': 'new-refresh'}) is False

    assert open(path, 'rb').read() == before
    assert token_store.load_token('claude')['access_token'] == 'old'


def test_concurrent_reads_never_observe_partial_token(private_store):
    assert token_store.save_token(
        'codex', {'access_token': 'seed', 'refresh_token': 'seed'})

    def write(index):
        assert token_store.save_token('codex', {
            'access_token': f'access-{index}',
            'refresh_token': f'refresh-{index}',
        })

    def read(_index):
        token = token_store.load_token('codex')
        assert isinstance(token, dict)
        assert token['access_token']
        assert token['refresh_token']

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for index in range(24):
            futures.append(pool.submit(write, index))
            futures.append(pool.submit(read, index))
        for future in futures:
            future.result()

    # The final file itself must remain complete JSON as well.
    with open(token_store.token_path('codex'), encoding='utf-8') as handle:
        assert isinstance(json.load(handle), dict)


_REFRESH_WORKER = r'''
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, sys.argv[1])
import lib.oauth.token_store as token_store

store_root, ready_path, start_path, result_path, counter_path = sys.argv[2:]
token_store._config_path = lambda relative: os.path.join(store_root, relative)
Path(ready_path).touch()
deadline = time.monotonic() + 20
while not Path(start_path).exists():
    if time.monotonic() > deadline:
        raise RuntimeError('timed out waiting for refresh barrier')
    time.sleep(0.01)

def refresh(_refresh_token):
    fd = os.open(counter_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, b'upstream-call\n')
    finally:
        os.close(fd)
    time.sleep(0.2)
    fresh = {
        'access_token': 'new-access',
        'refresh_token': 'new-refresh',
        'expire': time.time() + 3600,
    }
    if not token_store.save_token('codex', fresh):
        raise RuntimeError('could not persist refreshed token')
    return fresh

result = token_store.refresh_singleflight(
    'codex', 'old-refresh', refresh,
    load=lambda: token_store.load_token('codex'),
    lock_path=token_store.token_path('codex') + '.refresh')
Path(result_path).write_text(json.dumps(result), encoding='utf-8')
'''


def test_refresh_singleflight_is_cross_process(private_store):
    pytest.importorskip('fcntl')
    assert token_store.save_token('codex', {
        'access_token': 'old-access',
        'refresh_token': 'old-refresh',
        'expire': 0,
    })

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    start_path = private_store / 'refresh-start'
    counter_path = private_store / 'upstream-calls'
    processes = []
    ready_paths = []
    result_paths = []
    for index in range(2):
        ready_path = private_store / f'refresh-ready-{index}'
        result_path = private_store / f'refresh-result-{index}.json'
        ready_paths.append(ready_path)
        result_paths.append(result_path)
        processes.append(subprocess.Popen(
            [sys.executable, '-c', _REFRESH_WORKER, root, str(private_store),
             str(ready_path), str(start_path), str(result_path),
             str(counter_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ))

    deadline = time.monotonic() + 30
    while not all(path.exists() for path in ready_paths):
        if time.monotonic() > deadline:
            for process in processes:
                process.kill()
            pytest.fail('refresh workers did not reach barrier')
        time.sleep(0.01)
    start_path.touch()
    outputs = [process.communicate(timeout=30) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs

    assert counter_path.read_text(encoding='utf-8').splitlines() == [
        'upstream-call']
    results = [json.loads(Path(path).read_text(encoding='utf-8'))
               for path in result_paths]
    assert all(result['access_token'] == 'new-access' for result in results)
    assert all(result['refresh_token'] == 'new-refresh' for result in results)


def test_refresh_singleflight_does_not_retain_rotated_token_locks():
    lock = token_store._sf_lock('codex', 'one-time-refresh-generation')
    reference = weakref.ref(lock)
    assert len(token_store._sf_locks) >= 1

    del lock
    gc.collect()

    assert reference() is None


def test_logout_waits_for_inflight_refresh_and_wins(private_store):
    assert token_store.save_token('codex', {
        'access_token': 'old-access',
        'refresh_token': 'old-refresh',
        'expire': 0,
    })
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    logout_finished = threading.Event()
    outcomes = {}

    def refresh(_refresh_token):
        refresh_started.set()
        assert release_refresh.wait(timeout=10)
        fresh = {
            'access_token': 'new-access',
            'refresh_token': 'new-refresh',
            'expire': time.time() + 3600,
        }
        assert token_store.save_token('codex', fresh)
        return fresh

    def run_refresh():
        outcomes['refresh'] = token_store.refresh_singleflight(
            'codex', 'old-refresh', refresh,
            load=lambda: token_store.load_token('codex'),
            lock_path=token_store.token_path('codex') + '.refresh')

    def logout():
        outcomes['logout'] = token_store.delete_token('codex')
        logout_finished.set()

    refresh_thread = threading.Thread(target=run_refresh)
    logout_thread = threading.Thread(target=logout)
    refresh_thread.start()
    assert refresh_started.wait(timeout=10)
    logout_thread.start()
    assert not logout_finished.wait(timeout=0.1)
    release_refresh.set()
    refresh_thread.join(timeout=10)
    logout_thread.join(timeout=10)

    assert not refresh_thread.is_alive() and not logout_thread.is_alive()
    assert outcomes['refresh']['access_token'] == 'new-access'
    assert outcomes['logout'] is True
    assert token_store.load_token('codex') is None


def test_refresh_started_after_logout_cannot_resurrect_token(private_store):
    assert token_store.save_token('codex', {
        'access_token': 'old-access',
        'refresh_token': 'old-refresh',
        'expire': 0,
    })
    assert token_store.delete_token('codex')
    calls = []

    result = token_store.refresh_singleflight(
        'codex', 'old-refresh', lambda token: calls.append(token),
        load=lambda: token_store.load_token('codex'),
        lock_path=token_store.token_path('codex') + '.refresh')

    assert result is None
    assert calls == []
    assert token_store.load_token('codex') is None


def test_codex_terminal_refresh_rejection_is_persisted_once(
        private_store, monkeypatch):
    """A revoked refresh token is terminal, not a three-retry transient."""
    import lib.oauth.codex as codex

    assert token_store.save_token('codex', {
        'access_token': 'still-valid-access',
        'refresh_token': 'revoked-refresh',
        'expire': time.time() + 120,
    })
    calls = []

    class _Response:
        status_code = 401
        text = '{"error":{"code":"refresh_token_invalidated"}}'

        @staticmethod
        def json():
            return {'error': {'code': 'refresh_token_invalidated'}}

    monkeypatch.setattr(
        codex, '_oauth_http_post',
        lambda *args, **kwargs: calls.append((args, kwargs)) or _Response())
    monkeypatch.setattr(codex.time, 'sleep',
                        lambda *_args: pytest.fail('terminal error retried'))

    assert codex.codex_refresh_token() is None
    stored = token_store.load_token('codex')
    assert len(calls) == 1
    assert stored['access_token'] == 'still-valid-access'
    assert stored['refresh_token'] == ''
    assert stored['refresh_invalidated_reason'] == 'refresh_token_invalidated'

    # Cross-request replay is stopped by the persisted terminal state.
    assert codex.codex_refresh_token() is None
    assert len(calls) == 1


def test_codex_never_returns_access_token_past_recorded_expiry(
        private_store, monkeypatch):
    import lib.oauth.codex as codex

    assert token_store.save_token('codex', {
        'access_token': 'old-access',
        'refresh_token': '',
        'refresh_invalidated_at': time.time(),
        'refresh_invalidated_reason': 'refresh_token_invalidated',
        'expire': time.time() - 1,
    })
    monkeypatch.setattr(
        codex, 'codex_refresh_token',
        lambda *args, **kwargs: pytest.fail('terminal refresh was replayed'))

    assert codex.codex_get_valid_token() is None


def test_codex_retains_access_token_until_recorded_expiry(
        private_store, monkeypatch):
    import lib.oauth.codex as codex

    assert token_store.save_token('codex', {
        'access_token': 'short-lived-access',
        'refresh_token': '',
        'refresh_invalidated_at': time.time(),
        'refresh_invalidated_reason': 'refresh_token_invalidated',
        'expire': time.time() + 60,
    })
    monkeypatch.setattr(
        codex, 'codex_refresh_token',
        lambda *args, **kwargs: pytest.fail('terminal refresh was replayed'))

    assert codex.codex_get_valid_token() == 'short-lived-access'


def test_browser_exchange_reports_persistence_failure(private_store):
    from lib.oauth.claude import claude_store_token
    from lib.oauth.token_store import OAuthExchangeError

    with mock.patch('lib.oauth.claude.save_token', return_value=False):
        with pytest.raises(OAuthExchangeError, match='could not be saved') as exc:
            claude_store_token({
                'access_token': 'access', 'refresh_token': 'refresh',
                'expires_in': 3600,
            })

    assert exc.value.status_code == 500


def test_logout_does_not_claim_success_when_token_delete_fails():
    from lib.oauth.manager import _exchange

    with mock.patch('lib.oauth.token_store.load_token', return_value={}), \
            mock.patch('lib.oauth.token_store.delete_token', return_value=False), \
            mock.patch(
                'lib.oauth.outbound.deprovision_oauth_provider'), \
            mock.patch(
                'lib.subscription_quota.clear_subscription_quota'), \
            mock.patch(
                'lib.oauth.codex_usage.clear_codex_usage_reset_cache'), \
            mock.patch.object(_exchange, 'audit_log') as audit:
        result = _exchange.logout_oauth('codex')

    assert result == {
        'ok': False, 'provider': 'codex',
        'error': 'credential_delete_failed',
    }
    audit.assert_called_with(
        'oauth_logout_failed', provider='codex',
        reason='credential_delete_failed')


def test_logout_signals_device_worker_before_removing_flow():
    from lib.oauth.manager import _exchange
    from lib.oauth.manager._state import _active_flows, _flows_lock

    cancel_event = threading.Event()
    with _flows_lock:
        _active_flows['codex'] = {
            'flow_type': 'device',
            'cancel_event': cancel_event,
        }
    with mock.patch('lib.oauth.token_store.load_token', return_value={}), \
            mock.patch('lib.oauth.token_store.delete_token', return_value=True), \
            mock.patch('lib.oauth.outbound.deprovision_oauth_provider'), \
            mock.patch('lib.subscription_quota.clear_subscription_quota'), \
            mock.patch.object(_exchange, 'audit_log'):
        result = _exchange.logout_oauth('codex')

    assert result['ok'] is True
    assert cancel_event.is_set()
    with _flows_lock:
        assert 'codex' not in _active_flows
