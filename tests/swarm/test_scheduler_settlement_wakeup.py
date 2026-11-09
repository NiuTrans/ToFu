"""StreamingScheduler settlement wakes waiters instead of being polled.

``shutdown(wait=True)`` blocks until in-flight agents settle. It used to poll
``running_count`` every 50 ms, so every swarm teardown paid a fixed sleep
quantum and re-took the scheduler lock 20 times a second per waiting caller.
The last settlement now notifies a condition on the scheduler lock.
"""

import threading
import time

import pytest

pytestmark = pytest.mark.unit


def _spec(sid, depends_on=None):
    from lib.swarm.protocol import SubTaskSpec
    return SubTaskSpec(id=sid, role='coder', objective=f'objective {sid}',
                       depends_on=depends_on or [])


def _scheduler(gate):
    from lib.swarm.scheduler import StreamingScheduler

    class _GatedAgent:
        model = 'test-model'

        def __init__(self, spec):
            self.spec = spec

        def run(self):
            from lib.swarm.protocol import SubAgentResult, SubAgentStatus
            gate.wait(timeout=10)
            return SubAgentResult(status=SubAgentStatus.COMPLETED.value,
                                  final_answer='done')

    return StreamingScheduler(agent_factory=_GatedAgent, max_parallel=4,
                              default_retries=0)


class _NoSleepTime:
    """``time`` for the scheduler module whose ``sleep`` proves polling."""

    def __getattr__(self, name):
        return getattr(time, name)

    def sleep(self, _seconds):
        raise AssertionError('shutdown must wait for settlement, not poll')


def test_shutdown_wakes_on_last_settlement_without_polling(monkeypatch):
    import lib.swarm.scheduler as scheduler_module
    gate = threading.Event()
    sched = _scheduler(gate)
    sched.add_specs([_spec('a1'), _spec('a2')])
    assert sched.running_count == 2
    monkeypatch.setattr(scheduler_module, 'time', _NoSleepTime())
    release = threading.Timer(0.2, gate.set)
    release.start()
    started = time.monotonic()
    sched.shutdown(timeout=10)
    elapsed = time.monotonic() - started
    release.join()
    assert sched.running_count == 0
    assert 0.15 <= elapsed < 5, elapsed


def test_shutdown_timeout_still_releases_a_wedged_pool():
    gate = threading.Event()
    sched = _scheduler(gate)
    sched.add_specs([_spec('stuck')])
    started = time.monotonic()
    try:
        sched.shutdown(timeout=0.2)
        elapsed = time.monotonic() - started
        assert sched.running_count == 1
        assert 0.15 <= elapsed < 5, elapsed
    finally:
        gate.set()


def test_shutdown_with_nothing_running_returns_immediately(monkeypatch):
    import lib.swarm.scheduler as scheduler_module
    sched = _scheduler(threading.Event())
    monkeypatch.setattr(scheduler_module, 'time', _NoSleepTime())
    sched.shutdown(timeout=10)
    assert sched.running_count == 0

