"""lib/swarm/integration/_state.py — process-wide swarm session registry.

**#1 shared-state module.** Every module-level session-registry dict/lock lives
HERE and is shared BY REFERENCE (re-exported from ``__init__``) so there is
exactly ONE ``_active_sessions`` in the process — a divergent copy would strand
live swarm sessions. Functions that rebind these module vars via ``global``
(``_cleanup_stale_sessions`` → ``_last_cleanup``; ``_start_cleanup_timer`` →
``_cleanup_timer``) MUST live in this same module, so they're here too.

The two ``global``-rebound SCALARS (``_last_cleanup`` / ``_cleanup_timer``)
cannot be shared with the facade by reference the way the dicts/locks are —
rebinding here would leave the facade's re-exported name pointing at the old
value. So the cleanup functions read/write those scalars THROUGH the facade
package (``lib.swarm.integration``) as well, keeping ``integ._last_cleanup`` (a
seam the swarm tests reset) authoritative.

Also holds the auto-continue state (``_autocontinue_chain`` /
``_autocontinue_inflight`` / ``_autocontinue_lock``) — the ``_autocontinue``
submodule imports these BY REFERENCE and never rebinds the containers.
"""

from __future__ import annotations

import threading
import time

from lib import agent_inbox
from lib.log import get_logger
from lib.swarm.integration._config import (
    MAX_SESSIONS,
    SESSION_TTL_SECONDS,
    _CLEANUP_INTERVAL,
    swarm_key_for,
)
from lib.swarm.master import MasterOrchestrator

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════
#  Auto-continue (Phase 2) shared state
# ═══════════════════════════════════════════════════════════

#: conv key → number of consecutive auto-continuations since the last
#: human turn. Guarded by ``_autocontinue_lock``.
_autocontinue_chain: dict[str, int] = {}
#: conv keys with an auto-continue in flight (latch against double-fire when
#: several agents settle near-simultaneously / from spawn-more waves).
_autocontinue_inflight: set[str] = set()
_autocontinue_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════
#  Session registry
# ═══════════════════════════════════════════════════════════

#: Swarm sessions are keyed by a STABLE *swarm key* — the conversation id
#: when available, else the spawning task id. This is what lets a swarm
#: outlive the single task-turn that spawned it: a later "continue" turn in
#: the same conversation (which has a fresh task_id) still resolves to the
#: same live session. ``_key_aliases`` maps every task_id that has touched a
#: session → its swarm key, so route callers that only know a task_id (the
#: /api/v1/swarm/* endpoints) keep working unchanged.
_active_sessions: dict[str, MasterOrchestrator] = {}
_session_timestamps: dict[str, float] = {}
_key_aliases: dict[str, str] = {}
_sessions_lock = threading.Lock()
_last_cleanup: float = 0.0
_cleanup_timer: threading.Timer | None = None


def _resolve_key(arg: str) -> str:
    """Map a task_id (or already-a-key) to its swarm key via the alias table."""
    return _key_aliases.get(arg, arg)


# ── Cleanup ──────────────────────────────────────────────

def _key_is_live(swarm_key: str) -> bool:
    """True if ANY non-terminal task belongs to this swarm key's conversation.

    A swarm session is now conversation-scoped (see ``swarm_key_for``), so
    its lifetime is bounded by the *conversation*, not a single task-turn.
    TTL eviction exists only to reap sessions whose conversation has gone
    quiet — it must NOT kill a swarm just because the turn that spawned it
    ended (the whole point of Option A). We scan the chat task registry for
    any live task whose ``convId`` (or ``id``) matches the key. The registry
    read is a plain dict iteration (GIL-safe for a best-effort heuristic).
    Import is lazy + guarded so a missing/renamed registry never breaks
    cleanup.
    """
    if not swarm_key:
        return False
    try:
        from lib.tasks_pkg.manager import tasks as _chat_tasks
        # Direct task-id hit (legacy task-keyed sessions).
        t = _chat_tasks.get(swarm_key)
        if t is not None and t.get('status') not in ('done', 'error', 'aborted'):
            return True
        # Conversation-scoped: any live task in this conversation keeps the
        # swarm alive across turns.
        for t in list(_chat_tasks.values()):
            if (t.get('convId') == swarm_key
                    and t.get('status') not in ('done', 'error', 'aborted')):
                return True
        return False
    except Exception as e:
        logger.debug('[Swarm] key liveness check failed for %s: %s', swarm_key, e)
        return False


def _session_is_producing(swarm_key: str) -> bool:
    """True while this session's agents are still emitting progress.

    THE second shield for TTL eviction, and the one that was missing.
    ``_session_timestamps[key]`` is written exactly once — in ``_set_session``
    at spawn — and never refreshed, so ``now - ts`` measures the session's AGE,
    not its idleness. A swarm working hard for 40 minutes therefore looked
    identical to one abandoned 40 minutes ago, and the sweep called
    ``session.abort()`` on it (105 occurrences in this deployment's logs).

    ``_key_is_live`` did not cover the gap: it searches the chat-task registry
    for a non-terminal task in the conversation, which a fire-and-forget swarm
    — whose spawning turn has already finished, the exact case TTL is supposed
    to serve — does not have.

    So consult the same ``ProgressBeacon`` the driver and the per-agent guard
    read (``lib/swarm/liveness.py``): a session whose agents are still
    producing is NOT stale at any age. Failure is treated as "producing"
    because wrongly aborting live work costs far more than reaping late.
    """
    try:
        session = _active_sessions.get(swarm_key)
        if session is None:
            return False
        # A TERMINATED swarm is BY DEFINITION not producing — check this FIRST.
        # Without it, a beacon entry left behind by an agent that never reached
        # the scheduler's ``forget`` (a crash, or a settle racing this sweep)
        # would keep a finished session alive forever: a memory leak traded for
        # the premature-abort bug. Termination is an authoritative fact and
        # outranks the liveness heuristic.
        if getattr(session, 'is_terminated', False):
            return False
        beacon = getattr(session, 'progress_beacon', None)
        if beacon is None:
            return False
        # An EMPTY beacon must NOT read as "producing". ``is_making_progress``
        # deliberately fails OPEN on an unknown/absent agent so the driver is
        # never tricked into quitting during the launch window before the first
        # token — but "no agents are tracked" is a different question, and for
        # TTL it means every agent has settled or died. Answering it with the
        # fail-open default would make a genuinely dead session immortal, i.e.
        # trade a premature-abort bug for a leak. Require real tracked agents.
        tracked = beacon.tracked_agents()
        if not tracked:
            return False
        if beacon.is_making_progress():
            logger.info('[Swarm:%s] past TTL but still producing (%s, agents=%s) '
                        '— not reaping', swarm_key, beacon.describe(), tracked)
            return True
        return False
    except Exception as e:
        logger.warning('[Swarm:%s] liveness probe failed during TTL sweep '
                       '(keeping session): %s', swarm_key, e)
        return True


def _cleanup_stale_sessions():
    """Drop sessions past TTL or above MAX_SESSIONS. Caller must hold lock."""
    global _last_cleanup
    # ``_last_cleanup`` is a scalar rebound via ``global`` — unlike the registry
    # dicts it cannot be shared with the facade by reference. Route the throttle
    # read/write through the facade package so a caller (or test) that resets
    # ``lib.swarm.integration._last_cleanup`` actually affects the throttle this
    # function checks. This module's own binding is kept in sync too.
    import lib.swarm.integration as _pkg
    now = time.time()
    _throttle = getattr(_pkg, '_last_cleanup', _last_cleanup)
    if now - _throttle < 60:
        return
    _last_cleanup = now
    _pkg._last_cleanup = now

    def _purge_aliases(key: str):
        for alias in [a for a, k in _key_aliases.items() if k == key]:
            _key_aliases.pop(alias, None)

    stale_ids = [
        key for key, ts in _session_timestamps.items()
        if now - ts > SESSION_TTL_SECONDS
        and not _key_is_live(key)
        and not _session_is_producing(key)
    ]
    for key in stale_ids:
        session = _active_sessions.pop(key, None)
        _session_timestamps.pop(key, None)
        agent_inbox.clear(key)
        _purge_aliases(key)
        try:
            from lib.swarm import persistence
            persistence.delete_session(key)
        except Exception as e:
            logger.debug('[Swarm:%s] persisted session delete (TTL) failed: %s', key, e)
        # Drop the auto-continue bookkeeping for a reaped conversation so the
        # chain counter / inflight latch don't accumulate stale keys.
        with _autocontinue_lock:
            _autocontinue_chain.pop(key, None)
            _autocontinue_inflight.discard(key)
        if session:
            logger.info('[Swarm:%s] Session expired after %ds TTL — cleaning up',
                        key, SESSION_TTL_SECONDS)
            try:
                session.abort()
            except Exception as e:
                logger.debug('[Swarm:%s] cleanup abort failed: %s', key, e, exc_info=True)

    if len(_active_sessions) > MAX_SESSIONS:
        sorted_ids = sorted(_session_timestamps, key=_session_timestamps.get)
        excess = len(_active_sessions) - MAX_SESSIONS
        for key in sorted_ids[:excess]:
            session = _active_sessions.pop(key, None)
            _session_timestamps.pop(key, None)
            agent_inbox.clear(key)
            _purge_aliases(key)
            try:
                from lib.swarm import persistence
                persistence.delete_session(key)
            except Exception as e:
                logger.debug('[Swarm:%s] persisted session delete (evict) failed: %s', key, e)
            if session:
                logger.warning('[Swarm:%s] Evicted (MAX_SESSIONS=%d exceeded)',
                               key, MAX_SESSIONS)
                try:
                    session.abort()
                except Exception as e:
                    logger.debug('[Swarm:%s] eviction abort failed: %s',
                                 key, e, exc_info=True)


def _background_cleanup():
    global _last_cleanup
    try:
        import lib.swarm.integration as _pkg
        with _sessions_lock:
            _last_cleanup = 0.0
            _pkg._last_cleanup = 0.0
            _cleanup_stale_sessions()
    except Exception as e:
        logger.warning('[Swarm] Background cleanup error: %s', e, exc_info=True)
    finally:
        _start_cleanup_timer()


def _start_cleanup_timer():
    global _cleanup_timer
    _cleanup_timer = threading.Timer(_CLEANUP_INTERVAL, _background_cleanup)
    _cleanup_timer.daemon = True
    _cleanup_timer.start()
    # Keep the facade attribute in sync (scalar rebound via ``global`` — not
    # shareable by reference). Guarded: during the initial module import the
    # facade package isn't fully constructed yet.
    try:
        import lib.swarm.integration as _pkg
        _pkg._cleanup_timer = _cleanup_timer
    except Exception as e:
        logger.debug('[Swarm] facade _cleanup_timer sync skipped (import incomplete): %s', e)


_start_cleanup_timer()  # launch on module import


# ── Session getters / setters ────────────────────────────

def _get_session(task_id: str) -> MasterOrchestrator | None:
    with _sessions_lock:
        _cleanup_stale_sessions()
        return _active_sessions.get(_resolve_key(task_id))


def _set_session(swarm_key: str, session: MasterOrchestrator, *,
                 task_id: str = ''):
    """Register *session* under its stable swarm key.

    ``task_id`` (when distinct) is recorded as an alias so route callers and
    the orchestrator teardown — which only know the task id — still resolve
    to this session.
    """
    with _sessions_lock:
        _cleanup_stale_sessions()
        _active_sessions[swarm_key] = session
        _session_timestamps[swarm_key] = time.time()
        if task_id and task_id != swarm_key:
            _key_aliases[task_id] = swarm_key


def _remove_session(task_id: str):
    """Remove the session resolved from *task_id* (task id or swarm key).

    Also drops the durable DB rows — ``_remove_session`` is only called on
    genuine teardown (explicit abort or the task ended with a terminated
    swarm), never on DETACH, so the persisted state is no longer needed.
    """
    key = _resolve_key(task_id)
    with _sessions_lock:
        _active_sessions.pop(key, None)
        _session_timestamps.pop(key, None)
        for alias in [a for a, k in _key_aliases.items() if k == key]:
            _key_aliases.pop(alias, None)
    agent_inbox.clear(key)
    try:
        from lib.swarm import persistence
        persistence.delete_session(key)
    except Exception as e:
        logger.debug('[Swarm:%s] persisted session delete failed: %s', key, e)


def add_session_alias(task_id: str, swarm_key: str):
    """Map a later turn's task_id onto an existing conv-scoped session.

    Called when a fresh task in the same conversation wants to reach the
    live swarm (e.g. ``await_agents`` from a "continue" turn) but isn't the
    task that spawned it.
    """
    if not task_id or not swarm_key or task_id == swarm_key:
        return
    with _sessions_lock:
        if swarm_key in _active_sessions:
            _key_aliases[task_id] = swarm_key


def get_active_session(task_id: str) -> MasterOrchestrator | None:
    """Public accessor for routes / orchestrator to inspect a live swarm."""
    return _get_session(task_id)


def has_live_or_pending_swarm(task: dict | None) -> bool:
    """True when a swarm is live OR has undrained <swarm-update>s for *task*.

    The orchestrator calls this each turn to decide whether the swarm
    follow-up tools (``await_agents`` / ``get_agent_result`` / ``spawn_agents``)
    MUST be in this turn's schema even when ``swarmEnabled`` is false — so a
    ``<swarm-update>`` (drained UNGATED) that instructs the model to collect
    results can never point at a tool the turn wasn't given (the hallucination-
    rejection desync from conv ``mr2ysg473scxv8``).

    Resolved off the conversation-scoped swarm key (``swarm_key_for``), so a
    later "continue" turn with a fresh task_id still sees its own conversation's
    live session / pending inbox. Best-effort — any lookup error is treated as
    "no swarm" (fail-open to the normal swarmEnabled gate) and logged.
    """
    try:
        key = swarm_key_for(task)
        if not key:
            return False
        if _get_session(key) is not None:
            return True
        return agent_inbox.has_pending(key)
    except Exception as e:  # never let this break tool assembly
        logger.warning('[Swarm] has_live_or_pending_swarm probe failed: %s', e)
        return False


def get_swarm_status(task_id: str) -> dict | None:
    """Return swarm status for a task, or None if no active swarm."""
    session = _get_session(task_id)
    if session is None:
        return None
    try:
        agents_info = []
        for sid, info in session.get_status().items():
            agents_info.append({'id': sid, **info})
        return {
            'active':     not session.is_terminated,
            'task_id':    task_id,
            'agents':     agents_info,
            'agent_count': len(agents_info),
            'pending':    session.pending_count,
            'running':    session.running_count,
            'completed':  session.completed_count,
            'created_at': _session_timestamps.get(_resolve_key(task_id), 0),
        }
    except Exception as e:
        logger.warning('[swarm] Error getting status for %s: %s',
                       task_id, e, exc_info=True)
        return {'active': True, 'task_id': task_id, 'error': str(e)}


def abort_swarm(task_id: str) -> dict:
    """Abort a running swarm session (used by routes/api_v1/swarm)."""
    session = _get_session(task_id)
    if session is None:
        return {'success': False, 'error': 'No active swarm for this task'}
    try:
        session.abort()
        _remove_session(task_id)
        logger.info('[swarm] Aborted swarm for task %s', task_id)
        return {'success': True, 'task_id': task_id}
    except Exception as e:
        logger.error('[swarm] Error aborting %s: %s', task_id, e, exc_info=True)
        _remove_session(task_id)
        return {'success': False, 'error': str(e)}
