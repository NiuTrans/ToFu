"""lib/swarm/liveness.py — the swarm's view of the ONE agent liveness judgement.

Before this module, three independent clocks decided when to stop a swarm, and
none of them looked at whether work was actually being produced:

  * the driver's ``iter_completions(timeout=600)`` — a whole-swarm budget fixed
    at the instant the generator started, so a second-wave agent inherited a
    deadline set before it existed;
  * ``SubTaskSpec.timeout_seconds=1800`` — checked only at the TOP of a round,
    therefore structurally unreachable for an agent blocked inside a tool
    (measured: one agent sat in a ``pytest`` child for >1h and never tripped it);
  * ``_session_timestamps[key]`` — stamped once in ``_set_session`` and never
    refreshed, so ``_cleanup_stale_sessions`` called ``session.abort()`` on a
    swarm that had been busy the entire time (measured: 105 occurrences).

All three answered "how long has this existed?" when the only question that
justifies killing work is "has it stopped producing?". A wall clock cannot tell
a productive 40-minute agent from a wedged one; it only guarantees that the
longer a task legitimately takes, the more certain we are to destroy it right
before it delivers.

This module holds the replacement: a per-agent monotonic record of the last
observed PROGRESS EVENT. Everything that used to own a deadline now asks
:meth:`ProgressBeacon.is_making_progress` instead. A swarm is stopped only when
it has genuinely gone quiet, so a long job that keeps producing is never
interrupted — and a truly wedged one is still caught, by the absence of output
rather than by the passage of time.

Progress is anything that proves forward motion:
  round start · token/thinking delta · tool dispatched · tool returned ·
  tool subprocess output · agent settled.

An agent running an invocation is BOUND to its task and judged by
``lib.tasks_pkg.liveness`` — the dual clocks, explained waits (storage, writer
admission, a stale filesystem mount), silence origin, per-command interrupt
ladder and threshold that judge a root conversation turn. A sub-agent is an
ordinary agent beside its parent: the driver, the TTL sweep and the stuck-task
reaper therefore cannot disagree about it, and tool-heartbeat self-ticks prove
nothing here either. Touches remain the record for agents not yet bound
(launch and admission) and the "quiet for" presentation. The window defaults
to the shared threshold (``stall_threshold_seconds``); the beacon never stops an
agent itself — the reaper, or the master's backstop, delivers the verdict.

Monotonic by construction (``time.monotonic``): this drives control-flow
decisions and must not be perturbed by a wall-clock step. Wall-clock instants
for the UI stay in ``scheduler._started_at``.
"""

from __future__ import annotations

import math
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['ProgressBeacon', 'thread_progress_sink', 'notify_tool_progress']


class ProgressBeacon:
    """Thread-safe record of when each agent last showed a sign of life.

    One instance per swarm session, owned by the ``MasterOrchestrator`` and
    shared by reference with the scheduler, every ``SubAgent``, and the session
    registry — so all of them read the SAME fact. Handing a component its own
    copy would recreate exactly the divergence this module exists to remove.

    Every method is non-raising: liveness accounting must never be the reason a
    swarm dies. A failure here degrades to "assume alive", because wrongly
    killing real work is far more expensive than reaping a wedged agent late.
    """

    __slots__ = ('_lock', '_last', '_notes', '_stall_timeout', '_created',
                 '_persistence_waits', '_tasks')

    def __init__(self, stall_timeout: float | None = None):
        if stall_timeout is None:
            # Silence bound, not a runtime budget: one threshold for every
            # agent, and a disabled judge never stalls one.
            from lib.tasks_pkg.liveness import stall_threshold_seconds
            threshold = stall_threshold_seconds()
            stall_timeout = threshold if threshold > 0 else math.inf
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._notes: dict[str, str] = {}
        self._stall_timeout = float(stall_timeout)
        self._created = time.monotonic()

        self._persistence_waits: dict[str, set[object]] = {}
        self._tasks: dict[str, dict] = {}

    # ── Recording ────────────────────────────────────

    def touch(self, agent_id: str, note: str = '') -> None:
        """Record a sign of life for *agent_id* (idempotent, very cheap).

        Called from hot paths (token deltas), so it does no I/O and no logging:
        just a dict write under a short lock. ``note`` is the most recent
        progress KIND, surfaced in diagnostics so a stall report can say what
        the agent was last seen doing.
        """
        if not agent_id:
            return
        now = time.monotonic()
        with self._lock:
            self._last[agent_id] = now
            if note:
                self._notes[agent_id] = note

    def begin_persistence_wait(self, agent_id: str, token: object) -> None:
        """An owned pending write is explained waiting, not fabricated progress."""
        with self._lock:
            self._last.setdefault(agent_id, time.monotonic())
            self._persistence_waits.setdefault(agent_id, set()).add(token)

    def end_persistence_wait(self, agent_id: str, token: object, *, acknowledged: bool) -> None:
        with self._lock:
            waits = self._persistence_waits.get(agent_id)
            if waits is not None:
                waits.discard(token)
                if not waits:
                    self._persistence_waits.pop(agent_id, None)
            if acknowledged and agent_id in self._last:
                self._last[agent_id] = time.monotonic()
                self._notes[agent_id] = 'storage_ack'

    def bind(self, agent_id: str, task: dict) -> None:
        """Judge *agent_id* by its running invocation *task* (the shared rule)."""
        if not agent_id:
            return
        with self._lock:
            self._last.setdefault(agent_id, time.monotonic())
            self._tasks[agent_id] = task

    def unbind(self, agent_id: str, task: dict) -> None:
        """Return to touch judgement once *task* (and only that task) has ended."""
        with self._lock:
            if self._tasks.get(agent_id) is not task:
                return
            del self._tasks[agent_id]
            if agent_id in self._last:
                # Settlement is progress: the agent's remaining bookkeeping
                # must not inherit the invocation's (possibly explained) silence.
                self._last[agent_id] = time.monotonic()
                self._notes[agent_id] = 'invocation_settled'

    def forget(self, agent_id: str) -> None:
        """Drop *agent_id* once it has settled, so it can't hold a swarm open."""
        if not agent_id:
            return
        with self._lock:
            self._last.pop(agent_id, None)
            self._notes.pop(agent_id, None)
            self._tasks.pop(agent_id, None)

            self._persistence_waits.pop(agent_id, None)

    def tracked_agents(self) -> list[str]:
        """Ids currently being tracked (i.e. started and not yet settled)."""
        with self._lock:
            return sorted(self._last)

    # ── Reading ──────────────────────────────────────

    def _judgements(self, agent_id: str | None = None) -> list[tuple[str, bool, float, str]]:
        """``(agent_id, stalled, silent_seconds, note)`` per tracked agent.

        Snapshot under the lock, judge outside it: the shared judge reads task
        state and other owners' locks, never this beacon's. A bound agent whose
        task is running gets the shared verdict (the reaper's exact domain);
        any other agent its touch silence, which also never counts before the
        last host filesystem recovery. Nothing is stalled while the filesystem
        is unresponsive or the agent's write is pending.
        """
        from lib.fs_keepalive import filesystem_resumed_at, filesystem_unresponsive
        from lib.tasks_pkg.liveness import judge
        with self._lock:
            ids = list(self._last) if agent_id is None else [agent_id]
            rows = [(aid, self._last[aid], self._notes.get(aid, ''),
                     bool(self._persistence_waits.get(aid)), self._tasks.get(aid))
                    for aid in ids if aid in self._last]
        if not rows:
            return []
        frozen = filesystem_unresponsive()
        floor = filesystem_resumed_at()[0]
        now, wall = time.monotonic(), time.time()
        judged = []
        for aid, last, note, waiting, task in rows:
            if task is None or task.get('status') != 'running' or task.get('aborted'):
                silent = max(0.0, now - max(last, floor))
                stalled = silent >= self._stall_timeout
            else:
                try:
                    verdict = judge(task, wall, self._stall_timeout)
                except Exception as error:
                    # Fail OPEN, loudly: while the judge is broken this agent
                    # can never be judged stalled, so the driver, the TTL sweep
                    # and the reaper lose their stopping condition for it.
                    logger.error('[Beacon] LIVENESS PROBE BROKEN for %s (%s) — assuming '
                                 'alive; while this persists it cannot be judged stalled',
                                 aid, error, exc_info=True)
                    verdict = None
                silent = 0.0 if verdict is None else verdict.silent_seconds
                stalled = verdict is not None and verdict.state == 'wedged'
            judged.append((aid, stalled and not waiting and not frozen, silent, note))
        return judged

    def seconds_since_activity(self, agent_id: str | None = None) -> float:
        """Seconds of silence (unexplained silence for a bound agent).

        With *agent_id*: that agent alone. Without: the MOST RECENT activity
        across all tracked agents — the whole-swarm judgement, so one busy
        agent legitimately keeps the swarm alive for its siblings, which is the
        property the driver's fixed 600s budget lacked.

        An untracked agent (never started, or already settled) reports ``0.0``
        — "no evidence of silence", never "silent forever". The default must
        fail toward keeping work alive.
        """
        return min((silent for _aid, _stalled, silent, _note in self._judgements(agent_id)),
                   default=0.0)

    def is_making_progress(self, agent_id: str | None = None) -> bool:
        """True unless every tracked agent asked about is stalled.

        THE predicate. The driver loop, the master's backstop and the session
        TTL sweep all route through this one call, so "still working" has one
        definition in the swarm subsystem — and, for bound agents, the same one
        the stuck-task reaper applies to every root and child task.
        """
        try:
            judged = self._judgements(agent_id)
            return not judged or not all(stalled for _aid, stalled, _silent, _note in judged)
        except Exception as e:
            # Fail OPEN: a broken beacon must not become a new way to kill a
            # healthy swarm — the exact failure mode this module replaces.
            #
            # But fail-open has a cost that MUST be visible. While this
            # predicate is broken every agent reads as alive forever, so the
            # driver never terminates, ``run_until_idle`` never returns and the
            # TTL sweep never reaps — all three consumers silently lose their
            # only stopping condition, which is strictly worse than the
            # premature-abort bug this module fixed. Hence ERROR + traceback
            # rather than a quiet warning, and the message names the
            # CONSEQUENCE instead of just echoing the exception: anything
            # reaching here is a bug in the beacon itself.
            logger.error('[Beacon] LIVENESS PROBE BROKEN (%s) — assuming alive; '
                         'while this persists NO agent can be judged stalled '
                         'and the swarm has NO stopping condition', e,
                         exc_info=True)
            return True

    def stalled_agents(self) -> list[tuple[str, float, str]]:
        """``(agent_id, silent_seconds, last_note)`` for every stalled agent.

        Used for the operator-facing report and the master's stall delivery: a
        stall must name WHO went quiet and what it was last doing, otherwise the
        only symptom is a swarm that ends early for no stated reason.
        """
        out = [(aid, silent, note) for aid, stalled, silent, note in self._judgements()
               if stalled]
        out.sort(key=lambda x: -x[1])
        return out

    @property
    def stall_timeout(self) -> float:
        return self._stall_timeout

    def describe(self) -> str:
        """One-line diagnostic for logs (never raises)."""
        try:
            with self._lock:
                n = len(self._last)
            return (f'agents={n} quiet_for={self.seconds_since_activity():.0f}s '
                    f'stall_at={self._stall_timeout:.0f}s')
        except Exception as e:
            logger.debug('[Beacon] describe failed: %s', e)
            return f'(beacon describe failed: {e})'


# ═══════════════════════════════════════════════════════════
#  Thread-scoped progress sink — the TOOL-level heartbeat
# ═══════════════════════════════════════════════════════════
#
# The stall check above is only as good as the signals feeding it, and the
# worst real hang produces NO agent-level signal at all: ``coder-tests`` called
# ``run_command`` with no timeout, the pytest child never returned, and the
# agent sat inside one tool call for over an hour. Round starts, tokens and
# tool returns all stop at the moment of entry, so a purely agent-level beacon
# would declare that agent stalled while its subprocess was healthily printing
# test results — reintroducing the very "kill the productive one" failure this
# work removes.
#
# So the tool layer must heartbeat too. It is threaded through a CONTEXTVAR
# rather than a parameter on every tool signature: a sub-agent's tool runs on
# its own thread, so the binding is unambiguous, and ``run_command`` (plus any
# future long tool) can report progress WITHOUT importing swarm or knowing a
# beacon exists. Zero coupling in the tool, no plumbing through
# ``_execute_tool_one``'s whole call chain.
#
# Outside a swarm the contextvar is unset and ``notify_tool_progress`` is a
# no-op, so the main chat path is byte-unaffected.

import contextlib  # noqa: E402
import contextvars  # noqa: E402

_PROGRESS_SINK: contextvars.ContextVar = contextvars.ContextVar(
    'tofu_progress_sink', default=None)


@contextlib.contextmanager
def thread_progress_sink(beacon: 'ProgressBeacon | None', agent_id: str):
    """Bind *beacon*/*agent_id* for the duration of a tool call.

    Entered by the sub-agent around each tool dispatch; any code running
    beneath it (however deep) can call :func:`notify_tool_progress` to prove
    the work is alive.
    """
    if beacon is None or not agent_id:
        yield
        return
    token = _PROGRESS_SINK.set((beacon, agent_id))
    try:
        yield
    finally:
        try:
            _PROGRESS_SINK.reset(token)
        except ValueError as e:
            # Reset from a different context (a tool that hopped threads) —
            # harmless, the binding dies with its context.
            logger.debug('[Beacon] sink reset skipped: %s', e)


def notify_tool_progress(note: str = 'tool_output') -> None:
    """Report that the CURRENT tool call is still doing work.

    Safe to call from anywhere, at any rate, in or out of a swarm: it is a
    no-op when no sink is bound. Never raises — a liveness signal must not be
    able to break the tool that emits it.
    """
    try:
        bound = _PROGRESS_SINK.get()
        if not bound:
            return
        beacon, agent_id = bound
        beacon.touch(agent_id, note)
    except Exception as e:
        logger.debug('[Beacon] tool progress notify failed: %s', e)
