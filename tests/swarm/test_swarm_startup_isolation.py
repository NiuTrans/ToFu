"""Physical worker startup cannot monopolize Swarm state or lose handoffs."""
from types import SimpleNamespace
import threading

import time
import pytest

from lib.swarm.scheduler import StreamingScheduler
from lib.swarm.types import SubAgentResult, SubTaskSpec

pytestmark = pytest.mark.unit


def spec(name, **kwargs):
    return SubTaskSpec(id=name, role='coder', objective=name, **kwargs)


def scheduler(**kwargs):
    return StreamingScheduler(agent_factory=lambda s: SimpleNamespace(
        run=lambda: SubAgentResult(status='completed', final_answer=s.id)),
        default_retries=0, **kwargs)


@pytest.mark.parametrize('sample', range(5))
def test_slow_submission_leaves_status_and_pending_cancellation_responsive(monkeypatch, sample):
    sched = scheduler()
    entered, release, inspected = (threading.Event() for _ in range(3))
    submit = sched._pool.submit
    cancelled = []

    timings = {}
    def slow_submit(*args, **kwargs):

        started = time.perf_counter()
        entered.set()
        assert release.wait(3)

        timings['startup_held_ms'] = (time.perf_counter() - started) * 1000
        return submit(*args, **kwargs)

    monkeypatch.setattr(sched._pool, 'submit', slow_submit)
    launcher = threading.Thread(target=sched.add_specs, args=([
        spec('first'), spec('dependent', depends_on=['first'])],))

    def inspect():

        started = time.perf_counter()
        assert sched.running_count == 1
        assert not sched.is_idle
        cancelled.extend(sched.cancel_pending())

        timings['status_cancel_ms'] = (time.perf_counter() - started) * 1000
        inspected.set()

    observer = threading.Thread(target=inspect)
    launcher.start()
    try:
        assert entered.wait(3)

        # Deliberately retain the physical handoff for a controlled interval;
        # state operations must finish while that handoff is still held.
        time.sleep(0.2)
        observer.start()
        assert inspected.wait(1), 'thread startup held the scheduler state lock'
        assert [s.id for s in cancelled] == ['dependent']
    finally:
        release.set()
        launcher.join(3)
        if observer.ident is not None:
            observer.join(3)
        sched.shutdown()
    assert not launcher.is_alive() and not observer.is_alive()
    assert [s.id for s, _ in sched.all_results] == ['first']

    assert timings['startup_held_ms'] >= 200
    print(f"sample={sample} startup_held_ms={timings['startup_held_ms']:.3f} "
          f"status_cancel_ms={timings['status_cancel_ms']:.3f}")

def test_refused_handoff_settles_all_reserved_siblings_and_dependents(monkeypatch):
    published = []
    sched = scheduler(on_agent_complete=lambda s, r: published.append((s.id, r.status)))

    def refuse(*args, **kwargs):
        raise RuntimeError('controlled thread startup failure')

    monkeypatch.setattr(sched._pool, 'submit', refuse)
    try:
        sched.add_specs([spec('first'), spec('sibling'),
                         spec('dependent', depends_on=['first'])])
        assert sched.is_idle
        results = sched.run_until_idle(timeout=1)
        assert len(results) == 3
        assert sorted(published) == [('dependent', 'failed'), ('first', 'failed'),
                                     ('sibling', 'failed')]
        assert all('controlled thread startup failure' in r.error_message
                   for _, r in results)
        assert sched.started_at_map() == {}
    finally:
        sched.shutdown()


def test_stop_during_handoff_never_enters_agent(monkeypatch):
    aborted = threading.Event()
    ran = []
    sched = StreamingScheduler(agent_factory=lambda s: ran.append(s.id),
                               abort_check=aborted.is_set, default_retries=0)
    submit = sched._pool.submit

    def stop_before_submit(*args, **kwargs):
        aborted.set()
        return submit(*args, **kwargs)

    monkeypatch.setattr(sched._pool, 'submit', stop_before_submit)
    try:
        sched.add_specs([spec('first')])
        results = sched.run_until_idle(timeout=2)
        assert len(results) == 1 and results[0][1].status == 'cancelled'
        assert ran == []
    finally:
        sched.shutdown()
