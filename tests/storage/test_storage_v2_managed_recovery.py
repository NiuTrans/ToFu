"""Supervised native calls wait while their authority lives; time never fences them.

The runtime-bound recovery port is the client's only liveness witness
(docs/STORAGE.md, "Client retry contract"). Between replay attempts the client
pauses on it and resumes with the ready generation; a stopping, degraded,
refused or fenced runtime ends the wait at once. Scope, receipt identity and
the per-attempt wire deadline are unchanged however long the wait lasts.
"""
import asyncio
import threading
import time as real_time
from types import SimpleNamespace

import pytest

from lib.storage import v2_client
from lib.storage.errors import StorageError, StorageStartRefused
from lib.storage.retry import decline_storage_wait, storage_wait_owner
from lib.storage.runtime import StorageRuntime

pytestmark = pytest.mark.unit

# Longer than every retry window the client ever had (the 45 s command floor,
# 3 x 20 s production attempts, the 30 s restart wait).
_LONGER_THAN_ANY_OLD_WINDOW_S = 600.0


def client(owner=1, *, timeout=5.0):
    return v2_client.StorageV2Client('127.0.0.1', 1, b's' * 48, owner_id=owner,
                                     tenant_id=1, timeout=timeout)


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(v2_client, 'time', SimpleNamespace(
        monotonic=lambda: now[0], time=lambda: now[0],
        sleep=lambda delay: now.__setitem__(0, now[0] + delay)))
    monkeypatch.setattr(v2_client.random, 'uniform', lambda *_: 0)
    return now


def _call(native, kind):
    if kind == 'query':
        return native.query('record.get', {'namespace': 'n', 'key': 'k'})
    return native.command('record.put', {'namespace': 'n', 'key': 'k'}, 'one-receipt')


@pytest.mark.parametrize('kind', ['query', 'command'])
@pytest.mark.parametrize('failure', [
    # The daemon sends deadline_elapsed with the wire flag cleared.
    StorageError('database_timeout', 'deadline_elapsed', False),
    StorageError('database_unavailable', 'authority starved', True, 100),
    StorageError('database_busy', 'rotation in flight', True, 1),
], ids=['timeout', 'unavailable', 'busy'])
def test_live_authority_is_waited_out_past_every_old_window(monkeypatch, clock, kind, failure):
    native = client(timeout=20.0)
    identities, deadlines, pauses = [], [], []

    def attempt(operation, payload, **kwargs):
        identities.append(kwargs['command_id'])
        deadlines.append(kwargs['timeout'])
        clock[0] += kwargs['timeout'] if failure.code == 'database_timeout' else 0.01
        if clock[0] - 100.0 < _LONGER_THAN_ANY_OLD_WINDOW_S:
            raise failure
        return {'version': 7}

    def alive(pause):
        pauses.append(pause)
        clock[0] += pause
        return native

    monkeypatch.setattr(native, '_attempt', attempt)
    native.bind_runtime_recovery(alive)
    try:
        assert _call(native, kind) == {'version': 7}
    finally:
        native.close()
    assert clock[0] - 100.0 >= _LONGER_THAN_ANY_OLD_WINDOW_S
    assert len(identities) > v2_client._BUSY_COMMAND_ATTEMPTS
    assert set(identities) == ({None} if kind == 'query' else {'one-receipt'})
    # Every replay keeps its full wire deadline; no aggregate budget shrinks it.
    assert set(deadlines) == {20.0}
    assert len(pauses) == len(identities) - 1
    assert max(pauses) <= v2_client._AVAILABILITY_BACKOFF_CAP_S


class _Supervisor:
    """Supervisor double whose ready generation is one native client."""

    def __init__(self, native, *, replacement=None, refuse_restart=False):
        self.callback = None
        self.ready = False
        self.client = native
        self._replacement = replacement
        self._refuse_restart = refuse_restart
        self.starts = 0

    def add_crash_callback(self, callback):
        self.callback = callback

    def start(self):
        self.starts += 1
        if self.starts > 1:
            if self._refuse_restart:
                raise StorageStartRefused('engine_identity', 'engine changed',
                                          'restart the server')
            if self._replacement is not None:
                self.client = self._replacement
        self.ready = True
        return self.client

    def stop(self, timeout=10.0):
        self.ready = False

    def status(self):
        return {'ready': self.ready, 'state': 'ready' if self.ready else 'stopped',
                'backend': 'tofudb', 'pid': None, 'port': None, 'last_exit_code': None}


def _native(monkeypatch, failure=None, *, attempts=None):
    native = client()
    monkeypatch.setattr(native, 'health', lambda deadline=2.0: {'ready': True})
    seen = [] if attempts is None else attempts

    def attempt(operation, payload, **kwargs):
        seen.append(kwargs['command_id'])
        if failure is not None:
            raise failure
        return {'version': 1}

    monkeypatch.setattr(native, '_attempt', attempt)
    return native, seen


def _eventually(predicate, timeout=10.0):
    deadline = real_time.monotonic() + timeout
    while not predicate():
        if real_time.monotonic() >= deadline:
            return False
        real_time.sleep(0.01)
    return True


@pytest.mark.parametrize('state', ['degraded', 'refused', 'stopped', 'fenced'])
def test_proven_down_authority_fails_promptly(monkeypatch, state):
    failure = StorageError('database_unavailable', 'connection refused', True, 100)
    native, attempts = _native(monkeypatch, failure)
    runtime = StorageRuntime(_Supervisor(native, refuse_restart=state == 'refused'),
                             auto_restart=state != 'fenced')
    try:
        assert runtime.start() is native
        if state == 'degraded':
            runtime._enter_degraded('crash storm')
        elif state == 'refused':
            runtime._crashed(-9)
            assert _eventually(lambda: runtime.status()['state'] == 'refused')
        elif state == 'stopped':
            runtime.stop(timeout=5.0)
        else:
            # Exited with no restart worker: nothing will make it alive again.
            runtime._crashed(-9)
        started = real_time.monotonic()
        with pytest.raises(StorageError) as raised:
            native.command('record.put', {}, 'one-receipt')
        assert real_time.monotonic() - started < 2.0
        assert attempts == ['one-receipt']
        assert raised.value.code == 'database_unavailable'
        assert raised.value.retryable is (state == 'fenced')
    finally:
        runtime.stop(timeout=5.0)
        native.close()


def test_restart_in_flight_is_waited_out_on_the_same_receipt(monkeypatch):
    attempts = []
    original, _ = _native(monkeypatch, StorageError(
        'database_unavailable', 'connection refused', True, 100), attempts=attempts)
    replacement, _ = _native(monkeypatch, attempts=attempts)
    runtime = StorageRuntime(_Supervisor(original, replacement=replacement))
    try:
        assert runtime.start() is original
        runtime._crashed(-9)
        assert original.command('record.put', {}, 'one-receipt') == {'version': 1}
        assert attempts[0] == 'one-receipt' and set(attempts) == {'one-receipt'}
        assert runtime.ready
    finally:
        runtime.stop(timeout=5.0)
        original.close()
        replacement.close()


def test_runtime_stop_interrupts_a_wait_on_a_live_authority(monkeypatch):
    # Every escalated pause outlasts the test; only the stop can end it.
    monkeypatch.setattr(v2_client, '_AVAILABILITY_BACKOFF_BASE_S', 300.0)
    monkeypatch.setattr(v2_client, '_AVAILABILITY_BACKOFF_CAP_S', 300.0)
    native, attempts = _native(
        monkeypatch, StorageError('database_timeout', 'deadline_elapsed', False))
    runtime = StorageRuntime(_Supervisor(native))
    outcome = {}

    def wait_on_live_authority():
        try:
            native.command('record.put', {}, 'one-receipt')
        except StorageError as error:
            outcome['error'] = error

    worker = threading.Thread(target=wait_on_live_authority, daemon=True)
    try:
        assert runtime.start() is native
        worker.start()
        assert _eventually(lambda: len(attempts) >= 2)
        assert runtime.ready and worker.is_alive()
        started = real_time.monotonic()
        runtime.stop(timeout=5.0)
        worker.join(timeout=10.0)
        assert not worker.is_alive()
        assert real_time.monotonic() - started < 5.0
        assert outcome['error'].code == 'database_unavailable'
        assert outcome['error'].retryable is False
        assert set(attempts) == {'one-receipt'} and len(attempts) == 2
    finally:
        runtime.stop(timeout=5.0)
        worker.join(timeout=10.0)
        native.close()


@pytest.mark.parametrize('failure', [
    StorageError('database_conflict', 'version moved'),
    StorageError('database_protocol_error', 'checksum mismatch'),
    StorageError('database_resource_exhausted', 'domain bound', False),
])
def test_non_transient_failure_raises_at_once(monkeypatch, failure):
    native = client()
    attempts, resolutions = [], []

    def attempt(operation, payload, **kwargs):
        attempts.append(kwargs['command_id'])
        raise failure

    monkeypatch.setattr(native, '_attempt', attempt)
    native.bind_runtime_recovery(lambda pause: resolutions.append(pause) or native)
    try:
        with pytest.raises(StorageError) as raised:
            native.command('record.put', {}, 'one-receipt')
        assert raised.value is failure
        assert attempts == ['one-receipt'] and resolutions == []
    finally:
        native.close()


def test_observers_and_unsupervised_clients_keep_attempt_bounds_without_a_window(
        monkeypatch, clock):
    supervised, unsupervised = client(timeout=20.0), client(timeout=20.0)
    resolutions = []
    supervised.bind_runtime_recovery(lambda pause: resolutions.append(pause) or supervised)
    attempts = []

    def failing(code):
        def attempt(operation, payload, **kwargs):
            attempts.append(operation)
            clock[0] += kwargs['timeout']
            raise StorageError(code, 'starved', True, 100)
        return attempt

    try:
        # Metrics observe the authority and must answer while it is slow.
        monkeypatch.setattr(supervised, '_attempt', failing('database_unavailable'))
        assert supervised.metrics()['protocol'] == 'storage.v2'
        assert len(attempts) == supervised._read_attempts and resolutions == []
        # Without a supervising runtime nothing proves liveness: the attempt
        # bound remains, but elapsed time no longer cuts it short.
        attempts.clear()
        monkeypatch.setattr(unsupervised, '_attempt', failing('database_busy'))
        with pytest.raises(StorageError):
            unsupervised.command('record.put', {}, 'one-receipt')
        assert len(attempts) == v2_client._BUSY_COMMAND_ATTEMPTS
        attempts.clear()
        monkeypatch.setattr(unsupervised, '_attempt', failing('database_unavailable'))
        with pytest.raises(StorageError):
            unsupervised.query('record.get', {})
        assert len(attempts) == unsupervised._read_attempts
    finally:
        supervised.close()
        unsupervised.close()


def test_event_loop_thread_never_waits_on_liveness(monkeypatch, clock):
    native = client()
    attempts, resolutions = [], []

    def attempt(operation, payload, **kwargs):
        attempts.append(operation)
        raise StorageError('database_unavailable', 'starved', True, 100)

    monkeypatch.setattr(native, '_attempt', attempt)
    native.bind_runtime_recovery(lambda pause: resolutions.append(pause) or native)

    async def blocking_call_on_the_serving_loop():
        return native.query('record.get', {})

    try:
        with pytest.raises(StorageError):
            asyncio.run(blocking_call_on_the_serving_loop())
        assert len(attempts) == native._read_attempts and resolutions == []
    finally:
        native.close()


def test_long_wait_reaches_the_thread_owner_which_may_end_it(monkeypatch, clock):
    native = client(timeout=20.0)
    failure = StorageError('database_timeout', 'deadline_elapsed', False)
    attempts = []

    def attempt(operation, payload, **kwargs):
        attempts.append(clock[0])
        clock[0] += 0.5
        raise failure

    def alive(pause):
        clock[0] += pause
        return native

    monkeypatch.setattr(native, '_attempt', attempt)
    native.bind_runtime_recovery(alive)
    seen, outer = [], []

    class OwnerStopped(Exception):
        pass

    def owner(error):
        seen.append((clock[0] - 100.0, error))
        if len(seen) == 3:
            raise OwnerStopped()

    try:
        with storage_wait_owner(outer.append), storage_wait_owner(owner):
            with pytest.raises(OwnerStopped):
                native.command('record.put', {}, 'one-receipt')
        # A short blip stays invisible; only a slow call's wait is reported,
        # to every owner on the thread, innermost first.
        assert attempts[0] == 100.0 and len(attempts) > len(seen)
        assert all(waited >= v2_client.SLOW_RPC_WARN_SECONDS for waited, _ in seen)
        assert all(error is failure for _, error in seen)
        assert outer == [failure, failure]
        # A caller that keeps its own bounded loop declines the in-call wait.
        attempts.clear()
        with storage_wait_owner(decline_storage_wait):
            with pytest.raises(StorageError) as raised:
                native.query('record.get', {})
        assert raised.value is failure
        assert len(attempts) > 1
    finally:
        native.close()


@pytest.mark.parametrize('replacement_kind', ['foreign_owner', 'foreign_backend', 'stopped'])
def test_recovery_cannot_cross_scope_or_restart_a_stopped_runtime(monkeypatch, replacement_kind):
    original, replacement = client(), client(2)
    seen = []
    failure = StorageError('database_unavailable', 'disconnected', True, 1)
    def unavailable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(original, '_attempt', unavailable)
    monkeypatch.setattr(replacement, '_attempt', lambda *a, **kw: seen.append('dispatched'))

    def resolve(timeout):
        if replacement_kind == 'stopped':
            raise StorageError('database_unavailable', 'recovery stopped', False)
        return replacement if replacement_kind == 'foreign_owner' else object()

    original.bind_runtime_recovery(resolve)
    try:
        with pytest.raises(StorageError) as error:
            original.command('record.put', {}, 'one-receipt', deadline=1)
        assert not error.value.retryable
        assert error.value.code == ('database_unavailable' if replacement_kind == 'stopped'
                                   else 'database_integrity')
        assert not seen
    finally:
        original.close()
        replacement.close()


def test_checkpoint_control_never_uses_generation_recovery(monkeypatch):
    original = client()
    resolutions = []
    original.bind_runtime_recovery(lambda timeout: resolutions.append(timeout))
    failure = StorageError('database_unavailable', 'bound instance closed', True)
    def unavailable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(original, '_request_once', unavailable)
    try:
        with pytest.raises(StorageError) as error:
            original._checkpoint_request('checkpoint.control', {}, 1, 2**62)
        assert error.value is failure
        assert not resolutions
    finally:
        original.close()
