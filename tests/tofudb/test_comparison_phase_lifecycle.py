"""A concurrent phase settles accepted workers before its caller can retire the engine."""
from __future__ import annotations

import threading

import pytest

from scripts import tofudb_perf_compare as bench


pytestmark = pytest.mark.unit
_START = threading.Thread.start
_JOIN = threading.Thread.join
_WAIT = threading.Event.wait


def _phase(name):
    scale = {**bench._SCALES['tiny'], 'seed_records': 2, 'seed_convs': 2,
             'concurrent_writes': 2, 'concurrent_reads': 2,
             'swarm_writers': 2, 'swarm_readers': 2,
             'swarm_writes_each': 2, 'swarm_reads_each': 2}
    return next(row for row in bench._workloads(scale, 37) if row[0] == name)


class _Engine:
    def __init__(self, blocked=None):
        self.blocked = blocked
        self.entered = threading.Event()
        self.release = threading.Event()
        self.sessions = []
        self.events = []
        self.closed = False

    def execute(self, *args, **kwargs):
        return {}

    def pause(self, point):
        if self.blocked == point:
            self.entered.set()
            assert self.release.wait(5), 'test must release the synthetic operation'

    def open_session(self):
        self.pause('open')
        assert not self.closed
        session = _Session(self)
        self.sessions.append(session)
        return session


class _Session:
    def __init__(self, engine):
        self.engine = engine
        self.operations = 0
        self.close_attempts = 0

    def execute(self, *args, **kwargs):
        self.operations += 1
        self.engine.pause('operation')
        assert not self.engine.closed, 'engine retired before its worker settled'
        return {}

    def close(self):
        self.close_attempts += 1
        self.engine.pause('close')
        self.engine.events.append('session_closed')


@pytest.mark.parametrize('phase', ['concurrent_read', 'swarm_write'])
@pytest.mark.parametrize('failure_point', ['before_start', 'after_start', 'late_start'])
def test_failed_start_releases_accepted_workers_without_executing_operations(
        phase, failure_point, monkeypatch):
    engine = _Engine()
    started, gates, delayed = [], [], []
    gated = threading.Event()
    original_error = RuntimeError('physical startup failed')

    def observe_wait(event, timeout=None):
        if threading.current_thread().name.startswith('perf-') and timeout is None:
            gates.append(event)
            gated.set()
        return _WAIT(event, timeout)

    def start(thread):
        if started:
            assert gated.wait(2)
            if failure_point == 'after_start':
                _START(thread)
                started.append(thread)
            elif failure_point == 'late_start':
                delayed.append(thread)
            raise original_error
        _START(thread)
        started.append(thread)

    monkeypatch.setattr(threading.Event, 'wait', observe_wait)
    monkeypatch.setattr(threading.Thread, 'start', start)
    row = _phase(phase)
    try:
        with pytest.raises(RuntimeError) as caught:
            row[4](engine, row[1], row[2], row[3])
        assert caught.value is original_error
        assert all(not thread.is_alive() for thread in started)
        count = len(engine.sessions)
        engine.closed = True
        for thread in delayed:
            _START(thread)
            started.append(thread)
            _JOIN(thread, timeout=2)
            assert not thread.is_alive()
        assert len(engine.sessions) == count, 'late bootstrap acquired a retired engine'
        assert all(session.close_attempts == 1 for session in engine.sessions)
        assert all(session.operations == 0 for session in engine.sessions)
    finally:
        engine.closed = False
        for gate in gates:
            gate.set()
        engine.release.set()
        for thread in started:
            _JOIN(thread, timeout=3)
            assert not thread.is_alive()


@pytest.mark.parametrize('phase', ['concurrent_read', 'swarm_write'])
@pytest.mark.parametrize('blocked,interrupt', [
    ('open', False), ('operation', False), ('close', False), ('operation', True),
])
def test_timeout_or_interrupt_waits_for_worker_cleanup_before_return(
        phase, blocked, interrupt, monkeypatch):
    engine = _Engine(blocked)
    row = _phase(phase)
    expired, returned = threading.Event(), threading.Event()
    outcome = {}
    workers = []
    original_error = KeyboardInterrupt('interrupt while joining the phase')

    def start(thread):
        _START(thread)
        if thread.name.startswith('perf-'):
            workers.append(thread)

    def join(thread, timeout=None):
        if thread.name.startswith('perf-') and timeout is not None:
            assert engine.entered.wait(2)
            expired.set()
            if interrupt:
                raise original_error
            return
        _JOIN(thread, timeout)

    def run():
        try:
            outcome['result'] = row[4](engine, row[1], row[2], row[3])
        except BaseException as error:
            outcome['error'] = error
        finally:
            engine.events.append('phase_returned')
            returned.set()

    monkeypatch.setattr(threading.Thread, 'start', start)
    monkeypatch.setattr(threading.Thread, 'join', join)
    controller = threading.Thread(target=run, name='phase-controller', daemon=True)
    _START(controller)
    try:
        assert expired.wait(2)
        assert not returned.wait(0.1), 'phase returned while its engine was still borrowed'
        engine.release.set()
        _JOIN(controller, timeout=3)
        assert not controller.is_alive()
        assert all(not thread.is_alive() for thread in workers)
        assert engine.events[-1] == 'phase_returned'
        assert all(session.close_attempts == 1 for session in engine.sessions)
        engine.closed = True
        if blocked == 'open':
            assert all(session.operations == 0 for session in engine.sessions)
        elif blocked == 'operation':
            assert all(session.operations <= 1 for session in engine.sessions)
        if interrupt:
            assert outcome['error'] is original_error
        else:
            assert 'error' not in outcome, outcome
            assert 'threads past join deadline' in outcome['result']['error']
            assert 'p50_ms' not in outcome['result']
    finally:
        engine.closed = False
        engine.release.set()
        _JOIN(controller, timeout=3)
        for thread in workers:
            _JOIN(thread, timeout=3)
            assert not thread.is_alive()


@pytest.mark.parametrize('phase', ['concurrent_read', 'swarm_write'])
def test_abrupt_cleanup_failure_cannot_certify_complete_samples(phase, monkeypatch):
    engine = _Engine()
    escaped = []
    original_close = _Session.close

    def close(session):
        original_close(session)
        raise SystemExit('abrupt session cleanup failure')

    monkeypatch.setattr(_Session, 'close', close)
    monkeypatch.setattr(threading, 'excepthook', lambda event: escaped.append(event.exc_value))
    row = _phase(phase)
    result = row[4](engine, row[1], row[2], row[3])
    assert escaped and all(isinstance(error, SystemExit) for error in escaped)
    assert 'error' in result
    assert 'SystemExit' in result['error'] and 'abrupt session cleanup failure' in result['error']
    assert 'p50_ms' not in result
    assert all(session.close_attempts == 1 for session in engine.sessions)
