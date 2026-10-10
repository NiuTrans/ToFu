"""lib/swarm/liveness.py — the ONE liveness judgement for the whole swarm.

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

Monotonic by construction (``time.monotonic``): this drives control-flow
decisions and must not be perturbed by a wall-clock step. Wall-clock instants
for the UI stay in ``scheduler._started_at``.
"""

from __future__ import annotations

import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['ProgressBeacon', 'DEFAULT_STALL_TIMEOUT_SEC',
           'thread_progress_sink', 'notify_tool_progress']


#: Seconds of COMPLETE SILENCE from an agent before it is considered stalled.
#:
#: This is NOT a budget for how long work may take — a job that keeps emitting
#: tokens, tool calls or subprocess output runs indefinitely. It bounds only the
#: gap BETWEEN two signs of life, so the number is chosen against the slowest
#: legitimate gap we can measure rather than against total runtime.
#:
#: The widest real gap observed in this deployment is a rate-limited dispatch
#: cycling on cooldown, which still fires ``on_retry`` well inside a minute; a
#: long ``run_command`` heartbeats on subprocess output. 15 minutes of nothing
#: at all is therefore several orders above the worst honest case, while still
#: reaping a genuinely wedged agent in bounded time.
DEFAULT_STALL_TIMEOUT_SEC = 900.0


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

    __slots__ = ('_lock', '_last', '_notes', '_stall_timeout', '_created')

    def __init__(self, stall_timeout: float = DEFAULT_STALL_TIMEOUT_SEC):
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._notes: dict[str, str] = {}
        self._stall_timeout = float(stall_timeout)
        self._created = time.monotonic()

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

    def forget(self, agent_id: str) -> None:
        """Drop *agent_id* once it has settled, so it can't hold a swarm open."""
        if not agent_id:
            return
        with self._lock:
            self._last.pop(agent_id, None)
            self._notes.pop(agent_id, None)

    def tracked_agents(self) -> list[str]:
        """Ids currently being tracked (i.e. started and not yet settled)."""
        with self._lock:
            return sorted(self._last)

    # ── Reading ──────────────────────────────────────

    def seconds_since_activity(self, agent_id: str | None = None) -> float:
        """Seconds since the last progress event.

        With *agent_id*: that agent alone. Without: the MOST RECENT activity
        across all tracked agents — the whole-swarm judgement, so one busy
        agent legitimately keeps the swarm alive for its siblings, which is the
        property the driver's fixed 600s budget lacked.

        An untracked agent (never started, or already settled) reports ``0.0``
        — "no evidence of silence", never "silent forever". The default must
        fail toward keeping work alive.
        """
        now = time.monotonic()
        with self._lock:
            if agent_id is not None:
                ts = self._last.get(agent_id)
                return 0.0 if ts is None else max(0.0, now - ts)
            if not self._last:
                return 0.0
            return max(0.0, now - max(self._last.values()))

    def is_making_progress(self, agent_id: str | None = None) -> bool:
        """True while activity is fresher than the stall timeout.

        THE predicate. The driver loop, the per-agent guard and the session TTL
        sweep all route through this one call, so "still working" has exactly
        one definition in the swarm subsystem and the three can no longer
        disagree about the same running agent.
        """
        try:
            return self.seconds_since_activity(agent_id) < self._stall_timeout
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

        Used for the operator-facing report: a stall must name WHO went quiet
        and what it was last doing, otherwise the only symptom is a swarm that
        ends early for no stated reason.
        """
        now = time.monotonic()
        out: list[tuple[str, float, str]] = []
        with self._lock:
            for aid, ts in self._last.items():
                silent = max(0.0, now - ts)
                if silent >= self._stall_timeout:
                    out.append((aid, silent, self._notes.get(aid, '')))
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
