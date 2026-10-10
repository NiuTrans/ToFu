"""Capacity cannot delay child work; cancellation remains terminal without retries."""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit


def _spec(sid, **kw):
    from lib.swarm.protocol import SubTaskSpec
    kw.setdefault('role', 'coder')
    kw.setdefault('objective', 'do a thing')
    return SubTaskSpec(id=sid, **kw)


class _FakeAgent:
    model = 'test-model'

    def __init__(self, spec, run_impl):
        self.spec = spec
        self.agent_id = spec.id
        self._run_impl = run_impl

    def run(self):
        return self._run_impl(self)


# ── 1. gate queue wait heartbeats the bound sink ───────────────

def test_another_owner_enters_while_existing_owner_is_still_running():
    from lib.swarm.execution_gate import SwarmExecutionAccounting
    accounting = SwarmExecutionAccounting(capacity=1, waiter_capacity=1)
    accounting.acquire('first')
    accounting.acquire('second', abort_check=lambda: False)
    assert accounting.snapshot()['active'] == 2
    assert accounting.snapshot()['waiting'] == 0
    accounting.release('second')
    accounting.release('first')


# ── 2. AbortedError from admission settles CANCELLED, no retry ──

def test_aborted_error_maps_to_cancelled_without_retry():
    from lib.llm_errors import AbortedError
    from lib.swarm.protocol import SubAgentStatus
    from lib.swarm.scheduler import StreamingScheduler

    class _AbortingLimiter:
        def run_agent(self, agent, *, abort_check=None, on_admitted=None):
            raise AbortedError('swarm execution cancelled before admission')

    retries = []
    sched = StreamingScheduler(
        agent_factory=lambda s: _FakeAgent(s, lambda a: None),
        rate_limiter=_AbortingLimiter(),
        max_parallel=2,
        default_retries=2,
        on_retry=lambda spec, attempt, err: retries.append((spec.id, attempt)),
    )
    try:
        sched.add_specs([_spec('a1', max_retries=2)])
        settled = sched.run_until_idle(timeout=30)
    finally:
        sched.shutdown(wait=True)
    assert len(settled) == 1
    _, result = settled[0]
    assert result.status == SubAgentStatus.CANCELLED.value, (
        f'abort settled as {result.status!r}, not CANCELLED')
    assert 'AbortedError' in (result.error_message or '')
    assert retries == [], f'cancellation was retried: {retries}'


# ── 3. rate-limiter admission wait keeps the beacon fresh ──────

def test_local_limiter_count_cannot_delay_dependency_ready_work():
    from lib.swarm.protocol import SubAgentResult
    from lib.swarm.rate_limiter import RateLimiter
    from lib.swarm.scheduler import StreamingScheduler
    limiter = RateLimiter(max_concurrent=1)
    limiter.acquire()
    scheduler = StreamingScheduler(
        agent_factory=lambda spec: _FakeAgent(spec, lambda agent:
            SubAgentResult(status='completed', final_answer='done')),
        rate_limiter=limiter, max_parallel=1)
    try:
        scheduler.add_specs([_spec('ready')])
        results = scheduler.run_until_idle(timeout=2)
        assert len(results) == 1 and results[0][1].status == 'completed'
        assert limiter.active == 1
    finally:
        limiter.release()
        scheduler.shutdown(wait=True)


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
