"""Swarm per-agent start clock survives a reload.

Worse symptom than the paper-media panels: after a refresh the per-agent
stopwatch does not show a wrong number, it **disappears entirely** while the
agent is still running. `streaming_swarm_panel.js` renders the timer only when
`aRunning && a._startedAt`, and `_startedAt` was minted frontend-side from
`Date.now()` and never persisted — so `_recoverSwarmAgents`, rebuilding from the
durable snapshot, produced stubs with no start and the timer node vanished. The
`else if (a.elapsed)` fallback cannot cover it either: `elapsed` only exists once
the agent has FINISHED.

Root cause is a missing DATA SOURCE, not missing wiring: nothing in lib/swarm/
ever recorded when an agent started. `_run_one`'s `t0` is `time.monotonic()`
(not an epoch, and function-local) and the scheduler's `_running` map holds only
the spec.

Pinned here:
  1. the scheduler records a wall-clock start when an agent is LAUNCHED, and
     drops it when the agent finishes;
  2. the durable snapshot carries `startedAt` for running agents, in epoch
     MILLISECONDS through the same `_epoch_ms` seam the rest of this batch uses
     (a seconds value would silently render a ~50-year elapsed);
  3. `filter_snapshot` preserves it through the multi-wave scoping path — that
     rewrite is where a new per-agent field would most plausibly get dropped.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

_MS_FLOOR = 1e12  # epoch-ms ≈ 1.78e12; epoch-SECONDS ≈ 1.78e9


def _spec(sid, objective='do a thing'):
    from lib.swarm.protocol import SubTaskSpec
    return SubTaskSpec(id=sid, role='coder', objective=objective)


def _scheduler(**kw):
    """A scheduler whose agents block until released, so they stay 'running'."""
    from lib.swarm.scheduler import StreamingScheduler

    class _BlockingAgent:
        model = 'test-model'

        def __init__(self, spec, gate):
            self.spec = spec
            self._gate = gate

        def run(self):
            from lib.swarm.protocol import SubAgentResult, SubAgentStatus
            self._gate.wait(timeout=10)
            return SubAgentResult(status=SubAgentStatus.COMPLETED.value,
                                  final_answer='done')

    import threading
    gate = threading.Event()
    sched = StreamingScheduler(
        agent_factory=lambda s: _BlockingAgent(s, gate),
        max_parallel=4, **kw)
    return sched, gate


# ── 1. the scheduler records a launch wall-clock ───────────────

def test_scheduler_records_started_at_on_launch():
    sched, gate = _scheduler()
    try:
        before = time.time()
        sched.add_specs([_spec('a1')])
        # Give the pool a moment to pick the spec up.
        for _ in range(50):
            if sched.started_at_map():
                break
            time.sleep(0.01)
        m = sched.started_at_map()
        assert 'a1' in m, \
            'the scheduler must record when an agent was launched — nothing ' \
            'in lib/swarm/ recorded a start, so the panel had no source'
        assert m['a1'] >= before
        assert m['a1'] <= time.time()
    finally:
        gate.set()
        sched.shutdown()


def test_scheduler_start_is_wall_clock_not_monotonic():
    """It must be an epoch instant — monotonic() cannot be sent to a browser."""
    sched, gate = _scheduler()
    try:
        sched.add_specs([_spec('a1')])
        for _ in range(50):
            if sched.started_at_map():
                break
            time.sleep(0.01)
        val = sched.started_at_map()['a1']
        # A real epoch-seconds value is ~1.7e9; monotonic() on a booted host is
        # typically < 1e7. This asserts the QUANTITY, not the call used.
        assert val > 1.0e9, f'{val} is not an epoch timestamp'
    finally:
        gate.set()
        sched.shutdown()


def test_scheduler_drops_start_when_agent_finishes():
    """No unbounded growth: a settled agent's entry is released."""
    sched, gate = _scheduler()
    try:
        sched.add_specs([_spec('a1')])
        for _ in range(50):
            if sched.started_at_map():
                break
            time.sleep(0.01)
        gate.set()
        sched.run_until_idle(timeout=10)
        assert 'a1' not in sched.started_at_map(), \
            'a finished agent must not linger in the start map'
    finally:
        gate.set()
        sched.shutdown()


# ── 2. the durable snapshot carries startedAt (epoch ms) ───────

def _snapshot_with_running_agent():
    """Build a snapshot from a master whose one agent is mid-flight."""
    from lib.swarm.master import MasterOrchestrator
    m = MasterOrchestrator.__new__(MasterOrchestrator)
    import threading
    m._lock = threading.RLock()
    m._results_by_id = {}
    m._agents = {}
    m.specs = [_spec('a1')]
    m._terminated = False
    m._aborted = False
    m.task_id = 't1'

    started = time.time() - 300.0  # running for 5 minutes

    class _FakeSched:
        _running = {'a1': None}
        _pending = []

        def started_at_map(self):
            return {'a1': started}

    m._scheduler = _FakeSched()
    m._resolve_spec_model = lambda spec: 'test-model'
    return m._build_agent_snapshot(), started


def test_snapshot_running_agent_carries_started_at():
    snap, started = _snapshot_with_running_agent()
    agent = snap['agents'][0]
    assert agent['status'] == 'running'
    assert agent.get('startedAt') is not None, \
        'a running agent must carry startedAt, or the reloaded panel drops ' \
        'its timer node entirely (aRunning && a._startedAt fails)'
    assert agent['startedAt'] == pytest.approx(int(started * 1000))


def test_snapshot_started_at_is_milliseconds():
    """UNIT GUARD — same contract as the rest of this batch."""
    snap, _started = _snapshot_with_running_agent()
    val = snap['agents'][0]['startedAt']
    assert val > _MS_FLOOR, (
        f'startedAt={val} looks like epoch SECONDS; the wire contract is '
        'epoch MILLISECONDS. A seconds value renders a ~50-year elapsed '
        'instead of failing loudly.')
    assert isinstance(val, int)


# ── 3. multi-wave filtering preserves the field ────────────────

def test_filter_snapshot_preserves_started_at():
    """`filter_snapshot` rebuilds the agent list — it must not drop the clock."""
    from lib.swarm.snapshot import filter_snapshot
    started_ms = int((time.time() - 120) * 1000)
    snap = {
        'agents': [
            {'id': 'a1', 'status': 'running', 'startedAt': started_ms},
            {'id': 'a2', 'status': 'done', 'tokens': 10},
        ],
        'settled': False, 'totalTokens': 10, 'agentCount': 2, 'doneCount': 1,
        'version': 1,
    }
    out = filter_snapshot(snap, {'a1'})
    assert len(out['agents']) == 1
    assert out['agents'][0].get('startedAt') == started_ms, \
        'the multi-wave scoping rewrite must carry startedAt through'


def test_snapshot_terminal_agent_has_no_started_at_but_keeps_elapsed():
    """A finished agent renders from `elapsed`; it needs no live start clock."""
    from lib.swarm.master import MasterOrchestrator
    from lib.swarm.protocol import SubAgentResult, SubAgentStatus
    import threading
    m = MasterOrchestrator.__new__(MasterOrchestrator)
    m._lock = threading.RLock()
    spec = _spec('a1')
    res = SubAgentResult(status=SubAgentStatus.COMPLETED.value,
                         final_answer='ok')
    res.elapsed_seconds = 12.3
    res.total_tokens = 42
    res.tool_log = []
    m._results_by_id = {'a1': (spec, res)}
    m._agents = {}
    m.specs = [spec]
    m._terminated = True
    m._aborted = False
    m.task_id = 't1'
    m._scheduler = None
    m._resolve_spec_model = lambda s: 'test-model'
    snap = m._build_agent_snapshot()
    agent = snap['agents'][0]
    assert agent['status'] == 'done'
    assert agent['elapsed'] == 12.3


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
