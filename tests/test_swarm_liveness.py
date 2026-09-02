"""tests/test_swarm_liveness.py — guards for the swarm's THREE independent clocks.

Measured incident (conv ms8c68l0ppwfcw, 2026-07-31) — three separate deadlines,
none of which asked whether work was being produced:

  1. ``master._start_driver`` called ``iter_completions()`` with no argument and
     inherited ``scheduler.iter_completions(timeout=600.0)``. At exactly 600.0s
     the driver abandoned two RUNNING agents, set ``_terminated``, shut the pool
     down and persisted ``settled:true``. ``await_agents`` then told the model
     the ids "will NEVER complete" — ``orphans`` delivered 21 minutes later
     (``elapsed=2023.1s``, 1,091,830 tokens). The abandoned agents rendered as
     "无结果" because ``_build_agent_snapshot`` coerces resultless agents to
     ``unknown`` once ``terminated`` is set. 56 historical occurrences.

  2. ``SubTaskSpec.timeout_seconds=1800`` is evaluated only in ``before_round``,
     so an agent blocked INSIDE a tool never reaches the check. ``coder-tests``
     sat in a ``pytest`` child for over an hour and never tripped it.

  3. ``_session_timestamps[key]`` is written once, in ``_set_session`` at spawn,
     and never refreshed; ``_cleanup_stale_sessions`` calls ``session.abort()``
     at TTL. Its ``_key_is_live`` shield only looks for a non-terminal CHAT
     task, which is precisely what a fire-and-forget swarm does not have.
     105 historical occurrences.

The fix is one shared liveness fact (``lib/swarm/liveness.ProgressBeacon``) that
all three consult, so none of them can independently decide a busy agent is
finished. These tests are BEHAVIOURAL — they drive the real scheduler/master
with fake agents rather than asserting on source text, because the defect being
guarded is "a deadline fires while work is in flight", which only a running
loop can demonstrate.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.swarm.liveness import DEFAULT_STALL_TIMEOUT_SEC, ProgressBeacon  # noqa: E402
from lib.swarm.protocol import SubAgentResult, SubAgentStatus, SubTaskSpec  # noqa: E402
from lib.swarm.scheduler import StreamingScheduler  # noqa: E402

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════
#  Fakes
# ═══════════════════════════════════════════════════════

class _FakeAgent:
    """Agent that stays busy until released, touching the beacon as it works.

    Models the real shape of the incident: an agent that is PRODUCING (so it
    must never be killed) but takes longer than any fixed budget.
    """

    def __init__(self, spec, beacon=None, gate=None, touch_interval=0.01):
        self.spec = spec
        self.agent_id = f'agent-{spec.role}-{spec.id}'
        self._beacon = beacon
        self._gate = gate or threading.Event()
        self._touch_interval = touch_interval
        self.started = threading.Event()

    def run(self):
        self.started.set()
        while not self._gate.is_set():
            if self._beacon is not None:
                self._beacon.touch(self.spec.id, 'tool_output')
            time.sleep(self._touch_interval)
        return SubAgentResult(
            status=SubAgentStatus.COMPLETED.value,
            final_answer='done for real',
            elapsed_seconds=1.0,
        )


class _SilentAgent:
    """Agent that never touches the beacon — the genuinely wedged shape."""

    def __init__(self, spec, gate=None):
        self.spec = spec
        self.agent_id = f'agent-{spec.role}-{spec.id}'
        self._gate = gate or threading.Event()
        self.started = threading.Event()

    def run(self):
        self.started.set()
        self._gate.wait(timeout=30)
        return SubAgentResult(status=SubAgentStatus.COMPLETED.value,
                              final_answer='late')


def _spec(sid, role='coder'):
    return SubTaskSpec(id=sid, role=role, objective=f'objective for {sid}')


def _await_started(agents: dict, key: str, timeout: float = 20.0):
    """Wait for the pool thread to CONSTRUCT and start agent *key*.

    ``add_specs`` only submits to a ThreadPoolExecutor, so under load the
    worker may not have called the factory yet and ``agents[key]`` legitimately
    does not exist. Asserting on it directly raised ``KeyError`` in a full,
    CPU-saturated suite run while passing in isolation. Poll for the entry
    first, then wait on its event — a harness-timing fix that does not weaken
    what the test asserts.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        a = agents.get(key)
        if a is not None and a.started.wait(timeout=0.05):
            return a
        time.sleep(0.01)
    raise AssertionError(f'agent {key!r} never started within {timeout}s')


# ═══════════════════════════════════════════════════════
#  1. The beacon itself
# ═══════════════════════════════════════════════════════

def test_beacon_reports_progress_while_touched():
    b = ProgressBeacon(stall_timeout=0.3)
    b.touch('a1', 'round_start')
    assert b.is_making_progress('a1')
    assert b.is_making_progress()


def test_beacon_detects_a_silent_agent():
    b = ProgressBeacon(stall_timeout=0.05)
    b.touch('a1')
    time.sleep(0.12)
    assert not b.is_making_progress('a1'), \
        'an agent that stopped emitting must be reported as stalled'
    stalled = b.stalled_agents()
    assert [s[0] for s in stalled] == ['a1']


def test_one_busy_agent_keeps_the_swarm_alive():
    """Whole-swarm liveness = MOST RECENT activity across agents.

    The 600s driver budget killed everyone at once; a swarm must stay alive
    while ANY agent is still producing.
    """
    b = ProgressBeacon(stall_timeout=0.2)
    b.touch('quiet')
    time.sleep(0.25)
    b.touch('busy')
    assert b.is_making_progress(), 'swarm-wide liveness must follow the busiest agent'
    assert not b.is_making_progress('quiet')
    assert b.is_making_progress('busy')


def test_untracked_agent_is_not_reported_stalled():
    """Unknown id → assume alive. The default must never destroy work."""
    b = ProgressBeacon(stall_timeout=0.01)
    assert b.is_making_progress('never-started')
    assert b.seconds_since_activity('never-started') == 0.0


def test_settled_agent_is_forgotten_and_cannot_hold_the_swarm_open():
    b = ProgressBeacon(stall_timeout=0.05)
    b.touch('a1')
    b.forget('a1')
    assert b.tracked_agents() == []
    time.sleep(0.1)
    assert b.is_making_progress(), 'no tracked agents → nothing is stalling'


def test_default_stall_timeout_is_not_a_runtime_budget():
    """The constant bounds SILENCE, not total runtime.

    Pinned because the whole regression was budgets masquerading as health
    checks: any value near a plausible total runtime (600/1800) would let this
    module re-become the thing it replaced.
    """
    assert DEFAULT_STALL_TIMEOUT_SEC >= 600, \
        'too tight — a slow-but-working agent would be reaped'
    assert DEFAULT_STALL_TIMEOUT_SEC != 1800, \
        'must not silently re-adopt the old wall-clock budget'


# ═══════════════════════════════════════════════════════
#  2. Driver: no fixed whole-swarm budget  (defect #1)
# ═══════════════════════════════════════════════════════

def test_scheduler_iter_completions_defaults_to_no_fixed_budget():
    """``iter_completions()`` must not carry a finite default deadline.

    ``master._start_driver`` calls it with NO arguments, so whatever default
    lives here IS the whole-swarm budget. It was 600.0 — the exact value in
    ``Driver done — elapsed=600.0s``.
    """
    import inspect
    sig = inspect.signature(StreamingScheduler.iter_completions)
    default = sig.parameters['timeout'].default
    assert default is None or default == float('inf') or default <= 0, (
        f'iter_completions still defaults to a finite {default}s whole-swarm '
        'budget — a second-wave agent would inherit a deadline fixed before '
        'it was even spawned')


def test_driver_does_not_abandon_a_working_agent():
    """END-TO-END reproduction of the reported bug.

    A working agent outlives a short liveness window; the driver must keep
    draining rather than declaring the swarm finished. Pre-fix (600s constant,
    no beacon) the loop returns while the agent is mid-flight.
    """
    beacon = ProgressBeacon(stall_timeout=5.0)
    gate = threading.Event()
    agents = {}

    def factory(spec):
        a = _FakeAgent(spec, beacon=beacon, gate=gate)
        agents[spec.id] = a
        return a

    sched = StreamingScheduler(agent_factory=factory, max_parallel=2,
                               default_retries=0, progress_beacon=beacon)
    try:
        sched.add_specs([_spec('slow')])
        _await_started(agents, 'slow')

        drained = []
        done = threading.Event()

        def _drive():
            for item in sched.iter_completions(poll_interval=0.05):
                drained.append(item)
            done.set()

        t = threading.Thread(target=_drive, daemon=True)
        t.start()

        # Far longer than any per-poll tick: a healthy agent must not be
        # abandoned just because time passed.
        time.sleep(1.0)
        assert not done.is_set(), \
            'driver exited while the agent was still producing — this is the ' \
            '600s abandonment bug'
        assert sched.running_count == 1

        gate.set()
        assert done.wait(timeout=10), 'driver must exit once the agent settles'
        assert len(drained) == 1
        assert drained[0][1].final_answer == 'done for real'
    finally:
        gate.set()
        sched.shutdown()


def test_driver_still_stops_on_a_genuinely_stalled_swarm():
    """The complement: silence must still terminate, or we trade one bug for a hang."""
    beacon = ProgressBeacon(stall_timeout=0.4)
    gate = threading.Event()
    agents = {}

    def factory(spec):
        a = _SilentAgent(spec, gate=gate)
        agents[spec.id] = a
        return a

    sched = StreamingScheduler(agent_factory=factory, max_parallel=2,
                               default_retries=0, progress_beacon=beacon)
    try:
        sched.add_specs([_spec('wedged')])
        _await_started(agents, 'wedged')

        done = threading.Event()

        def _drive():
            for _ in sched.iter_completions(poll_interval=0.05):
                pass
            done.set()

        threading.Thread(target=_drive, daemon=True).start()
        assert done.wait(timeout=10), \
            'a swarm with zero activity past the stall window must terminate'
    finally:
        gate.set()
        sched.shutdown()


# ═══════════════════════════════════════════════════════
#  3. Termination honesty  (defect #1b — the "never complete" lie)
# ═══════════════════════════════════════════════════════

def test_scheduler_shutdown_waits_for_running_agents():
    """``shutdown()`` must not strand in-flight agents.

    Post-shutdown ``_launch_ready_locked`` raises ``RuntimeError: cannot
    schedule new futures after shutdown`` (verified), so any dependent spec
    unblocked by a late completion dies inside ``_run_one``.
    """
    beacon = ProgressBeacon(stall_timeout=5.0)
    gate = threading.Event()
    agents = {}

    def factory(spec):
        a = _FakeAgent(spec, beacon=beacon, gate=gate)
        agents[spec.id] = a
        return a

    sched = StreamingScheduler(agent_factory=factory, max_parallel=2,
                               default_retries=0, progress_beacon=beacon)
    try:
        sched.add_specs([_spec('busy')])
        _await_started(agents, 'busy')
        assert sched.running_count == 1

        gate.set()
        sched.shutdown()
        assert sched.running_count == 0, \
            'shutdown() returned while an agent was still running — its ' \
            'result can never be recorded'
    finally:
        gate.set()


# ═══════════════════════════════════════════════════════
#  4. Session TTL must follow activity, not age  (defect #3)
# ═══════════════════════════════════════════════════════

def test_session_ttl_is_refreshed_by_agent_activity():
    """``_session_timestamps`` must track ACTIVITY, not the spawn instant.

    Written once at spawn, the entry guaranteed ``session.abort()`` at TTL no
    matter how busy the swarm was (105 historical kills). ``_key_is_live`` did
    not save it: it looks for a non-terminal chat task, which a
    fire-and-forget swarm — the exact case — does not have.
    """
    import lib.swarm.integration._state as st

    key = 'test-conv-liveness'
    beacon = ProgressBeacon(stall_timeout=60.0)

    class _FakeSession:
        is_terminated = False
        progress_beacon = beacon

        def abort(self):
            raise AssertionError('a busy swarm must never be TTL-aborted')

    with st._sessions_lock:
        st._active_sessions[key] = _FakeSession()
        st._session_timestamps[key] = time.time() - (st.SESSION_TTL_SECONDS + 600)

    try:
        beacon.touch('a1', 'round_start')
        with st._sessions_lock:
            import lib.swarm.integration as _pkg
            _pkg._last_cleanup = 0.0
            st._last_cleanup = 0.0
            st._cleanup_stale_sessions()
        assert key in st._active_sessions, \
            'a swarm whose agents are actively producing was reaped by age'
    finally:
        with st._sessions_lock:
            st._active_sessions.pop(key, None)
            st._session_timestamps.pop(key, None)


def test_session_ttl_still_reaps_a_dead_session():
    """Complement: a genuinely quiet expired session must still be cleaned up."""
    import lib.swarm.integration._state as st

    key = 'test-conv-dead'
    beacon = ProgressBeacon(stall_timeout=0.01)
    aborted = threading.Event()

    class _FakeSession:
        is_terminated = False
        progress_beacon = beacon

        def abort(self):
            aborted.set()

    with st._sessions_lock:
        st._active_sessions[key] = _FakeSession()
        st._session_timestamps[key] = time.time() - (st.SESSION_TTL_SECONDS + 600)

    try:
        time.sleep(0.05)
        with st._sessions_lock:
            import lib.swarm.integration as _pkg
            _pkg._last_cleanup = 0.0
            st._last_cleanup = 0.0
            st._cleanup_stale_sessions()
        assert key not in st._active_sessions, \
            'an expired session with no activity must still be reaped'
        assert aborted.is_set()
    finally:
        with st._sessions_lock:
            st._active_sessions.pop(key, None)
            st._session_timestamps.pop(key, None)


# ═══════════════════════════════════════════════════════
#  5. Tool-level heartbeat  (defect #2 — the invisible hang)
# ═══════════════════════════════════════════════════════

def test_tool_output_counts_as_progress():
    """A long tool that keeps printing must keep its agent alive.

    This is the ``coder-tests`` shape: the agent blocks inside ONE
    ``run_command`` for over an hour. Round starts / tokens / tool returns all
    stop at the moment of entry, so without a tool-level signal the agent looks
    silent and any stall check would reap a healthy build.
    """
    from lib.swarm.liveness import notify_tool_progress, thread_progress_sink

    beacon = ProgressBeacon(stall_timeout=0.25)
    beacon.touch('a1', 'tool_start')
    with thread_progress_sink(beacon, 'a1'):
        for _ in range(6):
            time.sleep(0.06)
            notify_tool_progress('subprocess_output')
            assert beacon.is_making_progress('a1'), \
                'subprocess output must count as progress'
    assert beacon.is_making_progress('a1')


def test_silent_tool_does_not_fake_progress():
    """Complement: entering a tool must not itself confer immortality."""
    from lib.swarm.liveness import thread_progress_sink

    beacon = ProgressBeacon(stall_timeout=0.05)
    beacon.touch('a1', 'tool_start')
    with thread_progress_sink(beacon, 'a1'):
        time.sleep(0.15)          # tool produced NOTHING
        assert not beacon.is_making_progress('a1'), \
            'a tool that emits nothing at all must still be detectable'


def test_notify_outside_a_swarm_is_a_noop():
    """The main chat path must be unaffected — no sink bound, no error."""
    import lib.swarm.liveness as liveness
    assert liveness._PROGRESS_SINK.get() is None
    result = liveness.notify_tool_progress('subprocess_output')
    assert result is None
    assert liveness._PROGRESS_SINK.get() is None


def test_run_command_emits_the_heartbeat_on_real_output():
    """END-TO-END: the real ``_safe_on_chunk`` funnel must beat the beacon.

    Asserts the WIRING, not a constant: both run loops push every chunk
    through this helper, so if the heartbeat is removed from it a long command
    goes dark again. Note it must fire even with ``on_chunk=None`` — output
    proves life whether or not anyone subscribed to it.
    """
    from lib.project_mod.run_command import _safe_on_chunk
    from lib.swarm.liveness import thread_progress_sink

    beacon = ProgressBeacon(stall_timeout=0.2)
    beacon.touch('a1', 'tool_start')
    time.sleep(0.25)
    assert not beacon.is_making_progress('a1')

    with thread_progress_sink(beacon, 'a1'):
        _safe_on_chunk(None, 'stdout', 'tests/test_foo.py .... [ 42%]\n')
    assert beacon.is_making_progress('a1'), \
        '_safe_on_chunk no longer heartbeats — a long run_command would ' \
        'look wedged to the stall check'

    with thread_progress_sink(beacon, 'a1'):
        _safe_on_chunk(None, 'stdout', '')      # empty chunk = no evidence
    assert beacon.seconds_since_activity('a1') < 0.2


# ═══════════════════════════════════════════════════════
#  6. Parallel-tool result addressing  (the 4th-clock hunt's real find)
# ═══════════════════════════════════════════════════════
#
# `future.result(timeout=300)` in the parallel branch of _execute_tool_calls
# LOOKS like a fourth wall clock. MEASURED: it could never fire — `as_completed`
# yields only futures that are ALREADY done (done=True at every yield, verified
# including a 2.0s task read with timeout=0.01), so the 300 was dead weight, not
# a live cap. It is gone anyway: a number implying a bound that does not exist
# is a trap for the next reader.
#
# The same block held a REAL and worse defect. Results were keyed by
# `tc.get('id', str(uuid.uuid4())[:8])`, and lib/llm/_sse_core.py:854/922
# creates every tool-call slot with `'id': ''` — the KEY ALWAYS EXISTS, so the
# uuid fallback is unreachable and cannot de-collide anything:
#   * two parallel calls that both keep id='' collapse onto ONE dict key, so
#     the first tool is fed the SECOND tool's output — silently, no error;
#   * had the fallback fired it would mint DIFFERENT uuids at execution and at
#     lookup, feeding the model the literal string '(no result)'.
# Both are now impossible: results are keyed by POSITION in the batch.


class _IdCollisionAgent:
    """Minimal harness driving the REAL _execute_tool_calls parallel branch."""

    def __init__(self, tool_calls, outputs):
        from lib.swarm.agent import SubAgent
        spec = SubTaskSpec(id='collide', role='coder', objective='o')
        self.agent = SubAgent(
            spec, parent_task={}, all_tools=[], model='m',
            thinking_enabled=False,
            build_body_fn=lambda **kw: dict(kw),
            dispatch_stream_fn=lambda *a, **k: ({}, 'stop', {}),
        )
        self._outputs = outputs
        self.tool_calls = tool_calls
        self.agent._execute_single_tool = self._exec

    def _exec(self, tc, round_num):
        # Identify by object IDENTITY — the harness must not rely on ids
        # either, since ids are exactly what is under test.
        for i, cand in enumerate(self.tool_calls):
            if cand is tc:
                return self._outputs[i]
        raise AssertionError('unknown tool_call')


def _tc_no_id(cmd):
    """A tool call shaped like the SSE accumulator emits: 'id' present but ''."""
    return {'id': '', 'function': {'name': 'run_command',
                                   'arguments': '{"command":"' + cmd + '"}'}}


def test_parallel_tools_with_blank_ids_do_not_swap_results():
    """Two blank-id parallel calls must each receive THEIR OWN output.

    Pre-fix both collapsed onto the key '' and the first tool was handed the
    second's output — no error, no log, just a wrong tool result fed to the
    model. This is the shape `coder-tests` ran: it dispatched two parallel
    run_commands in round 1.
    """
    calls = [_tc_no_id('pytest'), _tc_no_id('git status')]
    outputs = ['PYTEST-OUTPUT-12610-CHARS', 'GIT-STATUS-OUTPUT']
    h = _IdCollisionAgent(calls, outputs)
    before = len(h.agent.messages)
    h.agent._execute_tool_calls(calls, round_num=1)
    appended = [m for m in h.agent.messages[before:] if m.get('role') == 'tool']

    assert len(appended) == 2, f'expected 2 tool results, got {len(appended)}'
    contents = [m['content'] for m in appended]
    assert contents == outputs, (
        f'parallel tool results are mis-addressed: {contents!r} != {outputs!r} '
        '— a blank/duplicate id collapsed two results onto one key')


def test_parallel_tool_results_are_never_literally_no_result():
    """No tool call may be handed the placeholder '(no result)'.

    With the old uuid fallback the execution-time id and the lookup-time id
    were different strings, so the lookup missed and the model was told
    '(no result)' — the exact words that appeared on screen.
    """
    calls = [_tc_no_id('a'), _tc_no_id('b'), _tc_no_id('c')]
    outputs = ['OUT-A', 'OUT-B', 'OUT-C']
    h = _IdCollisionAgent(calls, outputs)
    h.agent._execute_tool_calls(calls, round_num=1)
    tool_msgs = [m for m in h.agent.messages if m.get('role') == 'tool']
    assert len(tool_msgs) == 3
    for m in tool_msgs:
        assert m['content'] != '(no result)', \
            'a real tool output was replaced by the (no result) placeholder'
    assert [m['content'] for m in tool_msgs] == outputs


def test_parallel_tool_call_ids_stay_unique_on_the_wire():
    """`tool_call_id` must stay unique even when the model sent blank ids.

    The gateway matches assistant tool_calls to tool results by id, so two
    results sharing '' is a malformed turn. Position-keying fixes the internal
    lookup; the WIRE field needs repairing too.
    """
    calls = [_tc_no_id('x'), _tc_no_id('y')]
    h = _IdCollisionAgent(calls, ['OX', 'OY'])
    h.agent._execute_tool_calls(calls, round_num=1)
    ids = [m['tool_call_id'] for m in h.agent.messages if m.get('role') == 'tool']
    assert len(ids) == 2
    assert all(i for i in ids), f'blank tool_call_id on the wire: {ids!r}'
    assert len(set(ids)) == 2, f'duplicate tool_call_id on the wire: {ids!r}'


def test_real_ids_are_preserved_verbatim():
    """Complement: when the model DID send ids they must reach the wire as-is."""
    calls = [
        {'id': 'toolu_01', 'function': {'name': 'run_command', 'arguments': '{}'}},
        {'id': 'toolu_02', 'function': {'name': 'grep_search', 'arguments': '{}'}},
    ]
    h = _IdCollisionAgent(calls, ['R1', 'R2'])
    h.agent._execute_tool_calls(calls, round_num=1)
    tool_msgs = [m for m in h.agent.messages if m.get('role') == 'tool']
    assert [m['tool_call_id'] for m in tool_msgs] == ['toolu_01', 'toolu_02']
    assert [m['content'] for m in tool_msgs] == ['R1', 'R2']


def test_a_raising_tool_does_not_lose_its_siblings_results():
    """One tool raising must not corrupt the addressing of the others."""
    calls = [_tc_no_id('ok1'), _tc_no_id('boom'), _tc_no_id('ok2')]

    class _H(_IdCollisionAgent):
        def _exec(self, tc, round_num):
            for i, cand in enumerate(self.tool_calls):
                if cand is tc:
                    if i == 1:
                        raise RuntimeError('tool exploded')
                    return self._outputs[i]
            raise AssertionError('unknown tool_call')

    h = _H(calls, ['GOOD-1', None, 'GOOD-2'])
    h.agent._execute_tool_calls(calls, round_num=1)
    contents = [m['content'] for m in h.agent.messages if m.get('role') == 'tool']
    assert len(contents) == 3
    assert contents[0] == 'GOOD-1'
    assert 'RuntimeError' in contents[1] and 'tool exploded' in contents[1]
    assert contents[2] == 'GOOD-2'


# ═══════════════════════════════════════════════════════
#  7. No residual whole-swarm budget in sibling entry points
# ═══════════════════════════════════════════════════════

def test_no_scheduler_entry_point_defaults_to_a_finite_budget():
    """Every completion-draining entry point must be liveness-governed.

    `iter_completions` was fixed first, but `run_until_idle` (sync + async) and
    `AsyncStreamingScheduler.iter_completions` still carried the SAME 600.0
    constant. No in-tree caller reaches them today — which is exactly why
    leaving the constant there is how the defect comes back.
    """
    import inspect

    from lib.swarm.scheduler import AsyncStreamingScheduler, StreamingScheduler

    targets = [
        (StreamingScheduler.iter_completions,
         'StreamingScheduler.iter_completions'),
        (StreamingScheduler.run_until_idle,
         'StreamingScheduler.run_until_idle'),
        (AsyncStreamingScheduler.iter_completions,
         'AsyncStreamingScheduler.iter_completions'),
        (AsyncStreamingScheduler.run_until_idle,
         'AsyncStreamingScheduler.run_until_idle'),
    ]
    for fn, name in targets:
        default = inspect.signature(fn).parameters['timeout'].default
        assert default is None or default <= 0 or default == float('inf'), (
            f'{name} still defaults to a finite {default}s whole-swarm budget')


def test_async_scheduler_shares_the_master_beacon():
    """The async wrapper must not create a 4th private liveness record."""
    import inspect

    from lib.swarm.scheduler import AsyncStreamingScheduler
    assert 'progress_beacon' in inspect.signature(
        AsyncStreamingScheduler.__init__).parameters, \
        'AsyncStreamingScheduler cannot be handed the shared beacon'

    beacon = ProgressBeacon(stall_timeout=1.0)
    sched = AsyncStreamingScheduler(agent_factory=lambda spec: None,
                                    progress_beacon=beacon)
    try:
        assert sched._sync_scheduler._beacon is beacon, \
            'async wrapper built its own beacon instead of sharing'
    finally:
        sched.shutdown()


def test_broken_beacon_is_logged_loudly(caplog):
    """A broken liveness probe fails OPEN but must scream.

    Fail-open is correct (never invent a reason to kill work), but while it is
    active nothing can ever be judged stalled — the swarm loses its ONLY
    stopping condition, which is strictly worse than the bug it replaced. That
    must not be a quiet warning.
    """
    import logging

    beacon = ProgressBeacon(stall_timeout=1.0)
    beacon.touch('a1')

    def _boom(*_a, **_k):
        raise RuntimeError('probe exploded')

    original = ProgressBeacon.seconds_since_activity
    try:
        ProgressBeacon.seconds_since_activity = _boom
        with caplog.at_level(logging.ERROR):
            assert beacon.is_making_progress('a1') is True, \
                'must fail OPEN — never kill work because the probe broke'
        assert any(r.levelno >= logging.ERROR for r in caplog.records), \
            'a broken liveness probe was not logged at ERROR'
    finally:
        ProgressBeacon.seconds_since_activity = original


# ═══════════════════════════════════════════════════════
#  8. The blank-id idiom, swept across EVERY swarm minting site
# ═══════════════════════════════════════════════════════
#
# Fixing the parallel branch only fixed ONE of four sites that shared the same
# broken idiom. `d.get('id', <fallback>)` guards against a MISSING KEY, but the
# producers here always SET the key (to ''), so the fallback is unreachable at
# all four. Swept and repaired:
#
#   agent.py single-tool branch  — blank id reached the WIRE verbatim;
#   agent.py _execute_single_tool — blank tc_id drove the SSE callId, so two
#                                   parallel tool rows shared one call id;
#   _tools.py spawn spec id      — WORST: two agents whose model-supplied
#                                   `"id": ""` collapse onto ONE key in
#                                   master._results_by_id, so the first agent's
#                                   entry holds the SECOND's result and its own
#                                   id can never be resolved → await_agents
#                                   reports "never produced a result". That is
#                                   the "no result" card from a SECOND root
#                                   cause, independent of the parallel-tool one.
#
# The main chat path was never vulnerable: lib/tasks_pkg/tool_dispatch/_parse.py
# uses `tc.get('id') or f'call_{uuid4}'` — the `or` form, which handles the
# empty string. Swarm bypasses parse_tool_calls and re-derived it by hand.


def test_single_tool_branch_never_emits_a_blank_wire_id():
    """The 1-tool fast path must repair a blank id too.

    It is a DIFFERENT branch from the parallel one and was missed by the first
    fix: `len(tool_calls) == 1` skips the thread pool entirely.
    """
    calls = [_tc_no_id('pytest')]
    h = _IdCollisionAgent(calls, ['SINGLE-OUTPUT'])
    h.agent._execute_tool_calls(calls, round_num=1)
    tool_msgs = [m for m in h.agent.messages if m.get('role') == 'tool']
    assert len(tool_msgs) == 1
    assert tool_msgs[0]['tool_call_id'], \
        'blank tool_call_id reached the wire from the single-tool branch'
    assert tool_msgs[0]['content'] == 'SINGLE-OUTPUT'


def test_spawn_specs_with_blank_ids_get_distinct_ids():
    """Two agents whose model-supplied id is '' must NOT share one id.

    `master._results_by_id` is keyed on spec.id, so a collision makes one
    agent's result permanently unreachable and `await_agents` declares it
    resultless — a second, independent producer of the "no result" card.
    """
    from lib.swarm.integration._tools import _handle_spawn_agents

    captured = {}

    class _StubSession:
        is_terminated = False

        def __init__(self, **kw):
            captured['specs'] = kw.get('specs') or []
            self.conv_id = kw.get('conv_id', '')
            self.specs = captured['specs']
            self.artifact_store = None

        def run_in_background(self):
            pass

    import lib.swarm.integration._tools as _t
    _orig = _t.MasterOrchestrator
    try:
        _t.MasterOrchestrator = _StubSession
        _handle_spawn_agents(
            {'agents': [
                {'id': '', 'objective': 'audit TEST SUITE health', 'role': 'coder'},
                {'id': '', 'objective': 'triage untracked ORPHANS', 'role': 'coder'},
            ]},
            task_id='t-blank-ids',
            task={'id': 't-blank-ids', '_userId': 1}, cfg={},
            all_tools=[], model='m', thinking_enabled=False,
            project_path='', abort_check=None, on_event=None,
        )
    finally:
        _t.MasterOrchestrator = _orig
        from lib.swarm.integration._state import _remove_session
        _remove_session('t-blank-ids')

    ids = [s.id for s in captured.get('specs', [])]
    assert len(ids) == 2, f'expected 2 specs, got {ids!r}'
    assert all(ids), f'a spec kept a blank id: {ids!r}'
    assert len(set(ids)) == 2, (
        f'two agents share one id {ids!r} — their results collide in '
        '_results_by_id and one is reported as having produced no result')


def test_explicit_spawn_ids_are_preserved():
    """Complement: model-supplied ids must survive untouched.

    The fix must repair only the blank case — rewriting good ids would break
    `depends_on` wiring and every await_agents(ids=[...]) call.
    """
    from lib.swarm.integration._tools import _handle_spawn_agents

    captured = {}

    class _StubSession:
        is_terminated = False

        def __init__(self, **kw):
            captured['specs'] = kw.get('specs') or []
            self.conv_id = ''
            self.specs = captured['specs']
            self.artifact_store = None

        def run_in_background(self):
            pass

    import lib.swarm.integration._tools as _t
    _orig = _t.MasterOrchestrator
    try:
        _t.MasterOrchestrator = _StubSession
        _handle_spawn_agents(
            {'agents': [
                {'id': 'tests', 'objective': 'o1', 'role': 'coder'},
                {'id': 'orphans', 'objective': 'o2', 'role': 'coder'},
            ]},
            task_id='t-real-ids',
            task={'id': 't-real-ids', '_userId': 1}, cfg={},
            all_tools=[], model='m', thinking_enabled=False,
            project_path='', abort_check=None, on_event=None,
        )
    finally:
        _t.MasterOrchestrator = _orig
        from lib.swarm.integration._state import _remove_session
        _remove_session('t-real-ids')

    assert [s.id for s in captured.get('specs', [])] == ['tests', 'orphans']


def test_no_swarm_site_uses_the_unreachable_get_default_idiom():
    """RATCHET: no swarm module may MINT an id via `get('id', <minted fallback>)`.

    Source-level on purpose — this is a class of bug, not one instance, and the
    point is that a NEW site written in the broken form is caught before it can
    silently collide. `get(k, default)` fires only on a MISSING key; every
    producer here sets the key to '', so the correct form is `get(k) or default`.

    Scoped to MINTING (a uuid / random / generated fallback). Reading a task id
    with a plain literal default — `task.get('id', 'unknown')` — is a different
    thing: it labels a log line, it does not become a dict key that two records
    can collide on. Flagging those made the first draft of this test fire on 6
    innocent lines, which would have taught the next reader to silence it.
    """
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # `.get('id', ...)` whose fallback MINTS a value (uuid4 / short_id / token…)
    minting = re.compile(r"\.get\(\s*'id'\s*,[^)]*(uuid|short_id|token_hex|randint|urandom)")
    bad = []
    for dirpath, _dirs, files in os.walk(os.path.join(root, 'lib', 'swarm')):
        if '__pycache__' in dirpath:
            continue
        for fn in files:
            if not fn.endswith('.py'):
                continue
            p = os.path.join(dirpath, fn)
            with open(p, encoding='utf-8') as fh:
                for n, line in enumerate(fh, 1):
                    if line.lstrip().startswith('#'):
                        continue          # a comment quoting the old form
                    if minting.search(line):
                        bad.append(f'{os.path.relpath(p, root)}:{n}: {line.strip()}')
    assert not bad, (
        'an id is MINTED with the unreachable default-arg idiom — the producer '
        "always sets the key, so use `.get('id') or <fallback>`:\n  "
        + '\n  '.join(bad))


def test_the_ratchet_actually_catches_the_broken_form():
    """NEUTER-proof for the ratchet above: it must fire on the real shape.

    A regex guard that matches nothing is worse than no guard, so pin that it
    recognises the exact line the fix removed AND stays silent on the innocent
    task-id reads it must not flag.
    """
    import re
    minting = re.compile(r"\.get\(\s*'id'\s*,[^)]*(uuid|short_id|token_hex|randint|urandom)")

    assert minting.search("id=agent_def.get('id', str(uuid.uuid4())[:8]),"), \
        'ratchet blind to the exact broken form it exists to catch'
    assert minting.search("tc_id = tool_call.get('id', str(uuid.uuid4())[:8])")
    assert not minting.search("task_id = task.get('id', 'unknown')"), \
        'ratchet must not flag a plain-literal task-id read'
    assert not minting.search("id=agent_def.get('id') or str(uuid.uuid4())[:8],"), \
        'ratchet must accept the CORRECT or-form'


# ═══════════════════════════════════════════════════════
#  9. The stall verdict must reach the SNAPSHOT, not just the log
# ═══════════════════════════════════════════════════════
#
# The smoke test (conv ms8c68l0ppwfcw follow-up, swarm 797036b8) proved the
# driver-level stall verdict fires correctly at exactly 900s. But the verdict
# lived ONLY in logs: `_build_agent_snapshot` had no 'stalled' status at all,
# so a terminated + resultless agent still coerced to 'unknown' — the panel's
# "no result" bucket. smoke-silence only escaped because it came back at
# 1010s; had it genuinely died, its card would still read 无结果 and the panel
# could not answer the user's original question "why no result???".
#
# So the verdict must propagate: driver exit on stall → snapshot status
# 'stalled' (+ measured silence + last activity note) → panel shows
# "已停滞 · 静默 Ns". And the self-heal property the smoke proved (a late
# completion flips the card to done) must survive.


def _drive_stalled_master(stall_timeout=0.5, shutdown_timeout=2.0):
    """Run a REAL MasterOrchestrator whose single agent never shows life.

    Returns (master, gate) once the driver has terminated on the stall.
    The gate stays CLOSED so the agent thread remains in flight until the
    caller is done asserting (release it in a finally).
    """
    from unittest.mock import patch

    from lib.swarm.master import MasterOrchestrator

    gate = threading.Event()
    agents = {}

    def factory(spec, **kw):
        a = _SilentAgent(spec, gate=gate)
        agents[spec.id] = a
        return a

    m = MasterOrchestrator(task_id='t-stall-snap', conv_id='c1',
                           user_id=1,
                           specs=[_spec('wedged')])
    # Small stall window BEFORE the scheduler is built (run_in_background reads
    # self._beacon when constructing it).
    m._beacon = ProgressBeacon(stall_timeout=stall_timeout)
    with patch('lib.swarm.master._build_sub_agent', side_effect=factory):
        m.run_in_background()
        # Bound the driver's shutdown wait so a wedged test agent doesn't hold
        # the suite for the production 30s.
        if m._scheduler is not None:
            _orig_sd = m._scheduler.shutdown
            m._scheduler.shutdown = (
                lambda wait=True, timeout=30.0: _orig_sd(wait=wait,
                                                         timeout=shutdown_timeout))
        deadline = time.time() + 20
        while not m.is_terminated and time.time() < deadline:
            time.sleep(0.05)
    assert m.is_terminated, 'driver must terminate on a stalled swarm'
    return m, gate


def test_driver_stall_writes_stalled_into_the_snapshot():
    """Driver exit on stall → snapshot says 'stalled', never 'unknown'.

    'unknown' → 无结果 is reserved for "never started / never produced". An
    agent the beacon JUDGED silent must carry that verdict to the panel.
    """
    m, gate = _drive_stalled_master()
    try:
        snap = m._build_agent_snapshot()
        assert snap['settled'] is True
        a = next(x for x in snap['agents'] if x['id'] == 'wedged')
        assert a['status'] == 'stalled', (
            f"stall verdict did not reach the snapshot — got {a['status']!r} "
            "(the panel would render 无结果 for a judged-stalled agent)")
        assert a['status'] != 'unknown'
        assert snap['doneCount'] == 0, \
            'a stalled agent is NOT done — it must not inflate the count'
    finally:
        gate.set()


def test_stall_verdict_carries_silence_and_last_activity():
    """The panel needs the EVIDENCE, not just the label: silent seconds + note."""
    m, gate = _drive_stalled_master()
    try:
        snap = m._build_agent_snapshot()
        a = next(x for x in snap['agents'] if x['id'] == 'wedged')
        assert a['status'] == 'stalled'
        assert isinstance(a.get('stallSilentSeconds'), (int, float)), \
            'snapshot must carry how LONG the agent was silent'
        assert a['stallSilentSeconds'] >= 0.4, \
            f"silence measured {a.get('stallSilentSeconds')}s — verdict fired early?"
        # scheduler._run_one seeds every agent with note='agent_start'; a
        # never-active agent's last activity is exactly that.
        assert a.get('stallNote'), 'snapshot must carry the last activity kind'
    finally:
        gate.set()


def test_stalled_snapshot_self_heals_on_late_completion():
    """A stalled agent that comes BACK must flip the snapshot to done.

    This is the exact smoke-test shape: driver released the pool at 900s, the
    sleep returned at 1010s, AGENT_COMPLETE landed, and the card became a real
    completion. The stalled status must not be sticky.
    """
    m, gate = _drive_stalled_master()
    try:
        snap1 = m._build_agent_snapshot()
        assert next(x for x in snap1['agents'] if x['id'] == 'wedged')['status'] == 'stalled'
        v1 = snap1['version']

        # The late completion, through the REAL master callback (this is what
        # fired in production at 18:41:45).
        spec = m.specs[0]
        result = SubAgentResult(status=SubAgentStatus.COMPLETED.value,
                                final_answer='late report', elapsed_seconds=1010.7)
        m._on_agent_complete_callback(spec, result)

        assert spec.id not in m._stalled_agents, \
            'a completed agent must be dropped from the stalled registry'
        snap2 = m._build_agent_snapshot()
        a2 = next(x for x in snap2['agents'] if x['id'] == 'wedged')
        assert a2['status'] == 'done', \
            f"late completion must self-heal the card, got {a2['status']!r}"
        assert snap2['doneCount'] == 1
        assert snap2['version'] > v1, \
            'self-heal must yield a NEWER snapshot or the CAS layer rejects it'
    finally:
        gate.set()


def test_aborted_swarm_still_says_aborted_not_stalled():
    """Precedence: an explicit abort must not be relabelled as a stall."""
    m, gate = _drive_stalled_master()
    try:
        m._aborted = True     # abort flag set before the snapshot is built
        snap = m._build_agent_snapshot()
        a = next(x for x in snap['agents'] if x['id'] == 'wedged')
        assert a['status'] == 'aborted', \
            f"abort semantics regressed: got {a['status']!r}"
    finally:
        gate.set()


def test_panel_maps_the_stalled_status_end_to_end():
    """Frontend wiring: status 'stalled' → amber card + i18n label, not 无结果.

    Source-level on purpose (this project's frontend guards are): the phase
    map, the status-override, the CSS class, and BOTH locale entries must all
    exist — a missing any-one silently falls back to the noResult bucket.
    """
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    import json
    from tests._runtime_sections import runtime_section
    panel = runtime_section('ui/streaming_swarm_panel.js')
    locale_dir = os.path.join(root, 'frontend/src/i18n/locales')
    catalogs = {
        lang: json.load(open(os.path.join(locale_dir, f'{lang}.json'),
                             encoding='utf-8'))
        for lang in ('zh', 'en')
    }
    css = open(os.path.join(root, 'static/styles.css'), encoding='utf-8').read()

    assert re.search(r'stalled:\s*t\("swarm\.phase\.stalled"', panel), \
        'phaseMap does not map stalled — falls through to noResult'
    # Anchor the WHOLE branch line, not the substring: a 'false &&' neuter
    # keeps the substring alive while killing the behaviour (measured — the
    # first draft of this guard stayed green under exactly that neuter).
    assert re.search(r'else if \(a\.status === "stalled"\) \{', panel), \
        'the status-override (which wins for terminated agents) lacks stalled'
    assert 'stallSilentSeconds' in panel, \
        'the silent-seconds label is not rendered'
    # Recovery path: a reloaded panel must carry the backend's stall evidence
    # out of the persisted snapshot, or F5 silently loses the 「静默 Ns」.
    recovery = panel[panel.index('function _recoverSwarmAgents'):
                     panel.index('function _buildSwarmPanelHTML')]
    assert re.search(r'\?\s*a\.stallSilentSeconds', recovery), \
        '_recoverSwarmAgents drops the stallSilentSeconds MAPPING — a reload ' \
        'loses the 「静默 Ns」 label (substring checks stay green: the ' \
        'identifier survives even when the mapping is neutered to undefined)'
    assert 'sw-a-stalled' in panel and '.sw-a-stalled' in css, \
        'no visual class for a stalled card'
    assert catalogs['zh'].get('swarm.phase.stalled') == '已停滞', \
        'i18n key swarm.phase.stalled missing (zh)'
    assert '{seconds}' in catalogs['zh'].get('swarm.phase.stalledSilent', ''), \
        'i18n key swarm.phase.stalledSilent missing (carries the seconds)'
    assert re.search(r'[Ss]tall', catalogs['en'].get('swarm.phase.stalled', '')), \
        'i18n key swarm.phase.stalled missing (en)'
