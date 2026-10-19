"""Offline specifications for nonblocking Codex client-version preflight."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from lib.oauth import codex_version
from lib.oauth.outbound import CODEX_CLIENT_VERSION, _CODEX_USER_AGENT

pytestmark = pytest.mark.unit

LATEST = '0.153.4'
STALE = '0.151.2'

_REAL_FETCH = codex_version._fetch_latest_version

@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    codex_version._reset_cache(shutdown=True)
    cache = tmp_path / 'codex_client_version.json'
    clock = SimpleNamespace(wall=1_800_000_000.0, mono=1000.0)
    monkeypatch.setattr(codex_version, '_cache_path', lambda: str(cache))
    monkeypatch.setattr(codex_version, 'time', SimpleNamespace(
        time=lambda: clock.wall, monotonic=lambda: clock.mono))
    fetch = mock.Mock(return_value=LATEST)
    monkeypatch.setattr(codex_version, '_fetch_latest_version', fetch)
    codex_version._reset_cache()
    yield SimpleNamespace(cache=cache, clock=clock, fetch=fetch)
    # No monkeypatch or temporary cache outlives its worker.
    codex_version._reset_cache()


def _cached(state, version=STALE, *, age=None):
    if age is None:
        age = codex_version._CACHE_TTL_S + 1
    state.cache.write_text(json.dumps({
        'version': version, 'fetched_at_unix': state.clock.wall - age,
    }))


def _join_worker():
    with codex_version._lock:
        worker = codex_version._worker
    if worker is not None:
        worker.join(timeout=5)
        assert not worker.is_alive(), 'version refresh did not drain'
    return worker


@contextmanager
def _blocked_fetch(state, *, error=None):
    entered = threading.Event()
    release = threading.Event()
    callers = []

    def fetch():
        callers.append(threading.current_thread())
        entered.set()
        assert release.wait(5), 'test did not release registry fetch'
        if error is not None:
            raise error
        return LATEST

    state.fetch.side_effect = fetch
    try:
        yield entered, release
    finally:
        release.set()
        _join_worker()
    assert callers and all(caller is not threading.current_thread() for caller in callers)


def test_fetches_latest_version_and_caches_it(state):
    # Even a fast fetch does not determine the version returned to this call.
    assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
    _join_worker()
    assert codex_version.codex_client_version() == LATEST
    assert codex_version.codex_client_version() == LATEST
    state.fetch.assert_called_once_with()
    assert json.loads(state.cache.read_text()) == {
        'version': LATEST, 'fetched_at_unix': state.clock.wall,
    }
    assert state.cache.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('cached', [False, True], ids=['cold', 'stale'])
def test_registry_wait_never_blocks_preflight(state, cached):
    if cached:
        _cached(state)
    expected = STALE if cached else CODEX_CLIENT_VERSION
    with _blocked_fetch(state) as (entered, release):
        # The gate records thread ownership too: success cannot be a swallowed
        # network timeout on the caller, even on a heavily loaded test host.
        assert codex_version.codex_client_version() == expected
        assert entered.wait(5)
        assert not release.is_set()
        for _ in range(20):
            assert codex_version.codex_client_version() == expected
        state.fetch.assert_called_once_with()
    assert codex_version.codex_client_version() == LATEST


@pytest.mark.parametrize('cached', [False, True], ids=['cold', 'stale'])
def test_concurrent_reads_start_only_one_refresh(state, cached):
    if cached:
        _cached(state)
    expected = STALE if cached else CODEX_CLIENT_VERSION
    readers = 24
    barrier = threading.Barrier(readers)

    def read():
        barrier.wait(timeout=5)
        return codex_version.codex_client_version()

    with _blocked_fetch(state) as (entered, release):
        with ThreadPoolExecutor(max_workers=readers) as pool:
            futures = [pool.submit(read) for _ in range(readers)]
            assert [future.result(timeout=5) for future in futures] == [expected] * readers
        assert entered.wait(5)
        assert not release.is_set()
        state.fetch.assert_called_once_with()
    assert codex_version.codex_client_version() == LATEST


def test_fresh_disk_cache_uses_remaining_ttl_without_refetch(state):
    _cached(state, age=codex_version._CACHE_TTL_S - 10)
    assert codex_version.codex_client_version() == STALE
    state.fetch.assert_not_called()
    # Wall-clock jumps do not extend/expire the in-memory freshness deadline.
    state.clock.wall += 100 * codex_version._CACHE_TTL_S
    state.clock.mono += 9
    assert codex_version.codex_client_version() == STALE
    state.fetch.assert_not_called()
    state.clock.mono += 1
    assert codex_version.codex_client_version() == STALE
    _join_worker()
    assert codex_version.codex_client_version() == LATEST
    state.fetch.assert_called_once_with()


@pytest.mark.parametrize('cached', [False, True], ids=['cold', 'stale'])
def test_failed_refresh_keeps_usable_version_and_retries_after_backoff(state, cached):
    if cached:
        _cached(state)
    expected = STALE if cached else CODEX_CLIENT_VERSION
    state.fetch.side_effect = [TimeoutError('offline'), LATEST]
    assert codex_version.codex_client_version() == expected
    _join_worker()
    original = state.cache.read_bytes() if cached else None
    for _ in range(20):
        assert codex_version.codex_client_version() == expected
    state.clock.wall += 100 * codex_version._CACHE_TTL_S
    state.clock.mono += codex_version._RETRY_INTERVAL_S - 1
    assert codex_version.codex_client_version() == expected
    state.fetch.assert_called_once_with()
    assert (state.cache.read_bytes() if cached else None) == original
    if not cached:
        assert not state.cache.exists()
    state.clock.mono += 1
    assert codex_version.codex_client_version() == expected
    _join_worker()
    assert state.fetch.call_count == 2
    assert codex_version.codex_client_version() == LATEST


@pytest.mark.parametrize('failure_at', ['construct', 'start'])
def test_worker_admission_failure_is_optional_and_backs_off(state, monkeypatch, failure_at):
    worker_type = codex_version.Thread
    failed_worker = mock.Mock(ident=None)
    failed_worker.start.side_effect = RuntimeError('worker unavailable')
    constructor = mock.Mock(
        side_effect=OSError('worker unavailable') if failure_at == 'construct' else None,
        return_value=failed_worker)
    monkeypatch.setattr(codex_version, 'Thread', constructor)
    assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
    for _ in range(10):
        assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
    constructor.assert_called_once()
    state.fetch.assert_not_called()
    monkeypatch.setattr(codex_version, 'Thread', worker_type)
    state.clock.mono += codex_version._RETRY_INTERVAL_S
    assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
    _join_worker()
    assert codex_version.codex_client_version() == LATEST


def test_cancelled_worker_start_releases_singleflight_and_propagates(state, monkeypatch):
    class Cancelled(BaseException):
        pass

    worker_type = codex_version.Thread
    failed_worker = mock.Mock(ident=None)
    failed_worker.start.side_effect = Cancelled()
    monkeypatch.setattr(codex_version, 'Thread', mock.Mock(return_value=failed_worker))
    with pytest.raises(Cancelled):
        codex_version.codex_client_version()
    state.fetch.assert_not_called()
    monkeypatch.setattr(codex_version, 'Thread', worker_type)
    assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
    _join_worker()
    assert codex_version.codex_client_version() == LATEST


def test_disk_write_failure_keeps_fetched_version_without_retry_storm(state, monkeypatch):
    write = mock.Mock(side_effect=OSError('cache unavailable'))
    monkeypatch.setattr(codex_version, 'write_json_atomic', write)
    assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
    _join_worker()
    for _ in range(20):
        assert codex_version.codex_client_version() == LATEST
    state.fetch.assert_called_once_with()
    write.assert_called_once()
    assert not state.cache.exists()


def test_slow_disk_publication_does_not_hold_preflight_lock(state, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    write = codex_version.write_json_atomic

    def blocked_write(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        write(*args, **kwargs)

    monkeypatch.setattr(codex_version, 'write_json_atomic', blocked_write)
    try:
        assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
        assert entered.wait(5)
        for _ in range(20):
            assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
        assert not release.is_set()
        state.fetch.assert_called_once_with()
    finally:
        release.set()
        _join_worker()
    assert codex_version.codex_client_version() == LATEST


@pytest.mark.parametrize('shutdown', [False, True], ids=['reset', 'shutdown'])
@pytest.mark.parametrize('error', [None, TimeoutError('offline')], ids=['late-success', 'late-failure'])
def test_reset_drains_worker_and_fences_late_cache_publication(state, monkeypatch, shutdown, error):
    _cached(state)
    original = state.cache.read_bytes()
    replacement = state.cache.with_name('replacement.json')
    with _blocked_fetch(state, error=error) as (entered, release):
        assert codex_version.codex_client_version() == STALE
        assert entered.wait(5)
        old_worker = codex_version._worker
        reset = threading.Thread(target=codex_version._reset_cache,
                                 kwargs={'shutdown': shutdown})
        reset.start()
        try:
            assert codex_version._draining.wait(5)
            assert reset.is_alive()  # Lifecycle joins; preflight does not.
            monkeypatch.setattr(codex_version, '_cache_path', lambda: str(replacement))
            assert codex_version.codex_client_version() == STALE
            state.fetch.assert_called_once_with()
        finally:
            release.set()
            reset.join(timeout=5)
        assert not reset.is_alive()
        assert not old_worker.is_alive()
    assert state.cache.read_bytes() == original
    assert not replacement.exists()
    state.fetch.side_effect = None
    if shutdown:
        assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
        state.fetch.assert_called_once_with()
        codex_version._reset_cache()  # An explicit lifecycle reset reopens admission.
    assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
    _join_worker()
    assert codex_version.codex_client_version() == LATEST
    assert state.fetch.call_count == 2
    assert json.loads(replacement.read_text())['version'] == LATEST


def test_reset_fences_worker_reserved_before_thread_start(state, monkeypatch):
    real_thread = codex_version.Thread
    entered = threading.Event()
    release = threading.Event()

    class DelayedStart(real_thread):
        def start(self):
            entered.set()
            assert release.wait(5)
            super().start()

    monkeypatch.setattr(codex_version, 'Thread', DelayedStart)
    results = []
    caller = real_thread(target=lambda: results.append(codex_version.codex_client_version()))
    caller.start()
    assert entered.wait(5)
    reset = real_thread(target=codex_version._reset_cache)
    reset.start()
    try:
        assert codex_version._draining.wait(5)
        # Another request still returns without joining bootstrap or reset.
        assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
    finally:
        release.set()
        caller.join(timeout=5)
        reset.join(timeout=5)
    assert not caller.is_alive() and not reset.is_alive()
    assert results == [CODEX_CLIENT_VERSION]
    state.fetch.assert_not_called()
    assert not state.cache.exists()
    monkeypatch.setattr(codex_version, 'Thread', real_thread)
    assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
    _join_worker()
    assert codex_version.codex_client_version() == LATEST


@pytest.mark.parametrize('payload', [
    [], {'version': 'not-semver'}, {'version': '0.1.0'},
    {'version': STALE, 'fetched_at_unix': 'invalid'},
    {'version': STALE, 'fetched_at_unix': float('nan')},
    {'version': STALE, 'fetched_at_unix': float('inf')},
    {'version': STALE, 'fetched_at_unix': 9_000_000_000},
])
def test_malformed_cache_metadata_never_prevents_floor_or_refresh(state, payload):
    state.cache.write_text(json.dumps(payload))
    expected = STALE if isinstance(payload, dict) and payload.get('version') == STALE else CODEX_CLIENT_VERSION
    assert codex_version.codex_client_version() == expected
    _join_worker()
    assert codex_version.codex_client_version() == LATEST
    state.fetch.assert_called_once_with()


@pytest.mark.parametrize('age', [0, codex_version._CACHE_TTL_S + 1])
def test_cached_and_fetched_versions_cannot_lower_the_floor(state, age):
    _cached(state, version='0.1.0', age=age)
    state.fetch.return_value = '0.1.0'
    assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
    _join_worker()
    assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
    assert CODEX_CLIENT_VERSION in codex_version.codex_user_agent()


def test_user_agent_carries_cached_then_refreshed_version(state):
    _cached(state)
    with _blocked_fetch(state) as (entered, _):
        assert codex_version.codex_user_agent() == _CODEX_USER_AGENT.replace(CODEX_CLIENT_VERSION, STALE)
        assert entered.wait(5)
    assert codex_version.codex_user_agent() == _CODEX_USER_AGENT.replace(CODEX_CLIENT_VERSION, LATEST)


@pytest.mark.parametrize('route', ['direct', 'desktop-test'])
def test_fetch_uses_existing_egress_and_timeout(state, route):
    # Exercise the real fetch owner without any live upstream or credentials.
    from lib.desktop import egress
    from lib.http_transport import client

    response = SimpleNamespace(status_code=200, json=lambda: {'version': LATEST})
    with mock.patch.object(egress, 'route_request', return_value=route) as routing, \
         mock.patch.object(client, 'http_get', return_value=response) as direct, \
         mock.patch.object(egress, 'egress_http', return_value=response) as remote:
        assert _REAL_FETCH(user_id='test-owner') == LATEST
        routing.assert_called_once_with(codex_version._NPM_LATEST_URL, user_id='test-owner')
        if route == 'direct':
            direct.assert_called_once_with(codex_version._NPM_LATEST_URL,
                                           timeout=codex_version._FETCH_TIMEOUT_S)
            remote.assert_not_called()
        else:
            remote.assert_called_once_with(codex_version._NPM_LATEST_URL,
                                           method='GET', timeout=codex_version._FETCH_TIMEOUT_S,
                                           user_id='test-owner', agent_id=route)
            direct.assert_not_called()


@pytest.mark.parametrize('cached', [False, True], ids=['cold', 'stale'])
def test_subscription_preflight_returns_identity_headers_while_registry_is_blocked(state, cached):
    from lib.oauth import outbound

    if cached:
        _cached(state)
    expected = STALE if cached else CODEX_CLIENT_VERSION
    with mock.patch('lib.oauth.codex.codex_get_valid_token',
                    return_value='synthetic-access') as token, \
         mock.patch('lib.oauth.token_store.load_token',
                    return_value={'account_id': 'synthetic-account'}), \
         _blocked_fetch(state) as (entered, release):
        for _ in range(10):
            key, headers, _ = outbound.resolve_oauth_request(
                'codex', {'messages': [], '_conv_id': 'test-conversation'},
                None, user_id='test-owner')
            assert key == 'synthetic-access'
            assert headers['chatgpt-account-id'] == 'synthetic-account'
            assert headers['User-Agent'] == _CODEX_USER_AGENT.replace(CODEX_CLIENT_VERSION, expected)
        assert entered.wait(5)
        assert not release.is_set()
        state.fetch.assert_called_once_with()
        assert token.call_count == 10  # No token snapshot/cache introduced.


@pytest.mark.parametrize('interruption', [RuntimeError, KeyboardInterrupt])
def test_post_start_interruption_preserves_worker_ownership(state, monkeypatch, interruption):
    real_thread = codex_version.Thread

    class InterruptedStart(real_thread):
        def start(self):
            super().start()
            raise interruption('injected after OS thread start')

    monkeypatch.setattr(codex_version, 'Thread', InterruptedStart)
    with _blocked_fetch(state) as (entered, release):
        if interruption is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt):
                codex_version.codex_client_version()
        else:
            assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
        assert entered.wait(5)
        worker = codex_version._worker
        assert worker is not None and worker.is_alive()
        assert codex_version._refresh_lock.locked()
        state.clock.mono += 120
        assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
        state.fetch.assert_called_once_with()
        reset = real_thread(target=codex_version._reset_cache)
        reset.start()
        try:
            assert codex_version._draining.wait(5)
            assert reset.is_alive()
        finally:
            release.set()
            reset.join(timeout=5)
        assert not reset.is_alive()
        assert not worker.is_alive()
    assert not state.cache.exists()  # Reset fences publication, not just the handle.
