"""Concurrent frontend preparation stays bounded and reuses publication."""
import fcntl
import threading
import time

import pytest

import serverctl

pytestmark = pytest.mark.unit


def test_busy_frontend_lock_refuses_before_shutdown(tmp_path, monkeypatch):
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    monkeypatch.setattr(serverctl, '_lifecycle_owns_frontend', lambda: True)
    monkeypatch.setattr(serverctl, '_source_frontend_build_command', lambda: ['unused'])
    monkeypatch.setattr(serverctl, '_FRONTEND_BUILD_LOCK_TIMEOUT_SECONDS', 0.05)
    def stale():
        raise ValueError('stale frontend')
    monkeypatch.setattr(serverctl, '_validate_frontend_artifact', stale)
    monkeypatch.setattr(serverctl.subprocess, 'run', lambda *_a, **_k: pytest.fail('busy lock must not start a build'))
    lock = tmp_path / 'data/.frontend-build.lock'
    lock.parent.mkdir()
    with lock.open('a+b') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        error = serverctl.prepare_source_frontend_artifact('test restart')
        assert time.monotonic() - started < 1
    assert 'another frontend build is still running' in error
    assert 'current service remains online' in error


def test_waiter_reuses_graph_published_by_lock_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    monkeypatch.setattr(serverctl, '_lifecycle_owns_frontend', lambda: True)
    monkeypatch.setattr(serverctl, '_source_frontend_build_command', lambda: ['unused'])
    monkeypatch.setattr(serverctl, '_FRONTEND_BUILD_LOCK_TIMEOUT_SECONDS', 2)
    published = threading.Event()
    validation_started = threading.Event()
    def validate():
        if not published.is_set():
            validation_started.set()
            raise ValueError('stale frontend')
    monkeypatch.setattr(serverctl, '_validate_frontend_artifact', validate)
    monkeypatch.setattr(serverctl.subprocess, 'run', lambda *_a, **_k: pytest.fail('published graph must be reused'))
    lock = tmp_path / 'data/.frontend-build.lock'
    lock.parent.mkdir()
    with lock.open('a+b') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        results = []
        waiter = threading.Thread(target=lambda: results.append(serverctl.prepare_source_frontend_artifact('test restart')))
        waiter.start()
        assert validation_started.wait(timeout=1)
        published.set()
        fcntl.flock(held, fcntl.LOCK_UN)
        waiter.join(timeout=3)
        assert not waiter.is_alive()
    assert results == ['']
