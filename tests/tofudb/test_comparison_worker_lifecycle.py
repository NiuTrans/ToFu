"""Concurrent measurements require complete work and settled session lifetimes."""
from __future__ import annotations

import threading

import pytest

from scripts import tofudb_perf_compare as bench


pytestmark = pytest.mark.unit


def _phase(name):
    scale = {
        **bench._SCALES['tiny'], 'seed_records': 2, 'seed_convs': 2,
        'concurrent_writes': 2, 'concurrent_reads': 2,
        'swarm_writers': 2, 'swarm_readers': 2,
        'swarm_writes_each': 2, 'swarm_reads_each': 2,
    }
    return next(row for row in bench._workloads(scale, 37, namespace='worker-fixture')
                if row[0] == name)


def _worker_name(phase, role):
    if phase == 'swarm_write':
        return f'perf-swarm-{role}-0'
    return 'perf-writer' if role == 'writer' else 'perf-reader-0'


class _Engine:
    def __init__(self, target, fault):
        self.target, self.fault = target, fault
        self.sessions = []

    def execute(self, operation, payload, command):
        return {}

    def open_session(self):
        target = threading.current_thread().name == self.target
        if target and self.fault == 'open':
            raise RuntimeError('session acquisition failed')
        session = _Session(self, target)
        self.sessions.append(session)
        return session


class _Session:
    def __init__(self, engine, target):
        self.engine, self.target = engine, target
        self.close_attempts = 0

    def execute(self, operation, payload, command):
        if self.target:
            if self.engine.fault in {'operation', 'operation_and_close'}:
                raise RuntimeError('operation failed')
            if self.engine.fault == 'abrupt':
                raise SystemExit('worker exited before completing its reads')
        return {}

    def close(self):
        self.close_attempts += 1
        if self.target and self.engine.fault in {'close', 'operation_and_close'}:
            raise RuntimeError('session close failed')


@pytest.mark.parametrize('phase', ['concurrent_read', 'swarm_write'])
@pytest.mark.parametrize('role', ['reader', 'writer'])
@pytest.mark.parametrize('fault', ['open', 'operation', 'close', 'operation_and_close'])
def test_any_session_failure_invalidates_measurement_and_settles_owned_sessions(
        phase, role, fault, monkeypatch):
    escaped = []
    monkeypatch.setattr(threading, 'excepthook', lambda event: escaped.append(event.exc_value))
    engine = _Engine(_worker_name(phase, role), fault)
    row = _phase(phase)

    result = row[4](engine, row[1], row[2], row[3])

    assert 'error' in result, result
    for expected in {
        'open': ['session acquisition failed'], 'operation': ['operation failed'],
        'close': ['session close failed'],
        'operation_and_close': ['operation failed', 'session close failed'],
    }[fault]:
        assert expected in result['error']
    assert 'p50_ms' not in result
    assert not escaped, escaped
    assert engine.sessions and all(session.close_attempts == 1 for session in engine.sessions)


@pytest.mark.parametrize('phase', ['concurrent_read', 'swarm_write'])
def test_borrowed_engine_is_not_closed_by_individual_workers(phase):
    class BorrowedEngine:
        close_attempts = 0

        def execute(self, operation, payload, command):
            return {}

        def close(self):
            self.close_attempts += 1

    engine = BorrowedEngine()
    row = _phase(phase)
    result = row[4](engine, row[1], row[2], row[3])
    assert 'error' not in result
    assert engine.close_attempts == 0


@pytest.mark.parametrize('phase', ['concurrent_read', 'swarm_write'])
def test_partial_reader_population_cannot_be_reported_as_success(phase, monkeypatch):
    # Even an exit outside the ordinary Exception family cannot certify a
    # smaller reader population than the phase declared.
    monkeypatch.setattr(threading, 'excepthook', lambda event: None)
    engine = _Engine(_worker_name(phase, 'reader'), 'abrupt')
    row = _phase(phase)
    result = row[4](engine, row[1], row[2], row[3])
    assert 'error' in result, result
    assert 'reader' in result['error']
    assert 'p50_ms' not in result
    assert all(session.close_attempts == 1 for session in engine.sessions)
