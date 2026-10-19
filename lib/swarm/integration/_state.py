"""lib/swarm/integration/_state.py — process-wide swarm session registry.

**#1 shared-state module.** Every module-level session-registry dict/lock lives
HERE and is shared BY REFERENCE (re-exported from ``__init__``) so there is
exactly ONE ``_active_sessions`` in the process — a divergent copy would strand
live swarm sessions. Functions that rebind these module vars via ``global``
(``_cleanup_stale_sessions`` → ``_last_cleanup``; cleanup lifecycle functions
→ ``_cleanup_timer``) MUST live in this same module, so they're here too.

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


from contextlib import contextmanager
import threading
import time

from lib import agent_inbox
from lib.identity import require_user_id
from lib.log import get_logger
from lib.swarm.integration._config import (
    MAX_SESSIONS,
    SESSION_TTL_SECONDS,
    _CLEANUP_INTERVAL,
)
from lib.swarm.master import MasterOrchestrator
from lib.swarm.persistence_contract import SWARM_SESSION_TERMINAL_STATUSES

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

_session_reservations: set[str] = set()
_session_timestamps: dict[str, float] = {}
_key_aliases: dict[str, str] = {}
_sessions_lock = threading.Lock()
_last_cleanup: float = 0.0
_cleanup_timer: threading.Timer | None = None


def task_has_running_agents(*, task_id: str, user_id: int) -> bool:
    """Fence an owning task's worktree until its registered children settle."""
    with _sessions_lock:
        return any(session.task_id == task_id and session.user_id == user_id
                   and not session.is_terminated for session in _active_sessions.values())
_cleanup_timer_starts = 0
_cleanup_timer_retirements = 0
_RETIRED_ALIAS_CAPACITY = max(8, MAX_SESSIONS * 4)


class SwarmSessionConflict(RuntimeError):
    """The same session identity is already active or reserved."""


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
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        # Direct task-id hit.
        t = chat_task_runtime.get(swarm_key)
        if t is not None and t.get('status') not in ('done', 'error', 'aborted'):
            return True
        # Conversation-scoped: any live task in this conversation keeps the
        # swarm alive across turns.
        for t in chat_task_runtime.snapshot():
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
    """Retire inactive memory by TTL; capacity targets never revoke live work."""
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
        agent_inbox.clear(key, modes=['swarm-update'])
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

    excess = len(_active_sessions) - MAX_SESSIONS
    if excess > 0:
        terminal_keys = [key for key, session in _active_sessions.items()
                         if bool(getattr(session, 'is_terminated', False))]
        terminal_keys.sort(key=lambda key: _session_timestamps.get(key, 0))
        for key in terminal_keys[:excess]:
            _retire_terminal_session_locked(key)


def _publish_cleanup_timer(timer: threading.Timer | None) -> None:
    """Keep the facade's scalar timer seam synchronized with this owner."""
    try:
        import lib.swarm.integration as _pkg
        _pkg._cleanup_timer = timer
    except Exception as e:
        logger.debug('[Swarm] facade _cleanup_timer sync skipped: %s', e)


def _retire_cleanup_timer_locked(
    *,
    expected: threading.Timer | None = None,
    cancel: bool = True,
) -> threading.Timer | None:
    """Detach one exact timer generation. Caller holds ``_sessions_lock``."""
    global _cleanup_timer, _cleanup_timer_retirements
    timer = _cleanup_timer
    if timer is None or (expected is not None and timer is not expected):
        return None
    _cleanup_timer = None
    _cleanup_timer_retirements += 1
    _publish_cleanup_timer(None)
    if cancel:
        timer.cancel()
    return timer


def _start_cleanup_timer_locked() -> bool:
    """Start one timer iff a session needs it. Caller holds the registry lock."""
    global _cleanup_timer, _cleanup_timer_starts
    if not _active_sessions:
        return False
    current = _cleanup_timer
    if current is not None and current.is_alive():
        return False
    if current is not None:
        _retire_cleanup_timer_locked(expected=current)

    timer: threading.Timer

    def _fire() -> None:
        _background_cleanup(expected_timer=timer)

    timer = threading.Timer(_CLEANUP_INTERVAL, _fire)
    timer.daemon = True
    timer.name = 'swarm-session-cleanup'
    _cleanup_timer = timer
    _cleanup_timer_starts += 1
    _publish_cleanup_timer(timer)
    try:
        timer.start()
    except BaseException:
        # Even a partially started generation must lose registry/facade ownership.
        _retire_cleanup_timer_locked(expected=timer)
        raise
    return True


def _reconcile_cleanup_timer_locked() -> None:
    """Match timer residency to whether the registry has live value."""
    if _active_sessions:
        _start_cleanup_timer_locked()
    else:
        _retire_cleanup_timer_locked()


def _background_cleanup(
    expected_timer: threading.Timer | None = None,
) -> None:
    """Sweep one exact timer generation and re-arm only for live sessions."""
    global _last_cleanup
    try:
        import lib.swarm.integration as _pkg
        with _sessions_lock:
            if (expected_timer is not None
                    and _cleanup_timer is not expected_timer):
                return
            if expected_timer is not None:
                _retire_cleanup_timer_locked(
                    expected=expected_timer, cancel=False)
            _last_cleanup = 0.0
            _pkg._last_cleanup = 0.0
            try:
                _cleanup_stale_sessions()
            finally:
                _reconcile_cleanup_timer_locked()
    except Exception as e:
        logger.warning('[Swarm] Background cleanup error: %s', e, exc_info=True)


def _start_cleanup_timer() -> bool:
    """Lazily start cleanup when at least one registered session exists."""
    with _sessions_lock:
        return _start_cleanup_timer_locked()


def stop_swarm_cleanup_timer(timeout: float = 2.0) -> bool:
    """Cancel and bounded-join the current timer without deleting sessions."""
    with _sessions_lock:
        timer = _retire_cleanup_timer_locked()
    if timer is None:
        return True
    try:
        wait_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[Swarm] invalid cleanup stop timeout; using 2.0: %s', exc)
        wait_seconds = 2.0
    if timer is not threading.current_thread():
        timer.join(timeout=wait_seconds)
    return not timer.is_alive()


def swarm_cleanup_snapshot() -> dict:
    """Return bounded, non-authoritative timer lifecycle diagnostics."""
    with _sessions_lock:
        timer = _cleanup_timer
        snapshot = {
            'activeSessions': len(_active_sessions),
            'sessionCapacity': MAX_SESSIONS,
            'aliases': len(_key_aliases),
            'retiredAliasCapacity': _RETIRED_ALIAS_CAPACITY,
            'timerAlive': bool(timer is not None and timer.is_alive()),
            'timerStarts': _cleanup_timer_starts,
            'timerRetirements': _cleanup_timer_retirements,
        }
    from lib.swarm.execution_gate import process_swarm_execution_gate
    snapshot['execution'] = process_swarm_execution_gate().snapshot()
    return snapshot


# ── Session getters / setters ────────────────────────────

def _get_session(task_id: str) -> MasterOrchestrator | None:
    with _sessions_lock:
        _cleanup_stale_sessions()
        session = _active_sessions.get(_resolve_key(task_id))
        _reconcile_cleanup_timer_locked()
        return session


def _retire_terminal_session_locked(key: str) -> None:
    """Retire terminal memory only; durable results and inbox remain owned."""
    session = _active_sessions.get(key)
    if session is None or not bool(getattr(session, 'is_terminated', False)):
        return
    _active_sessions.pop(key, None)
    _session_timestamps.pop(key, None)
    # Keep recent task-id aliases pointing at the durable swarm key so a
    # status route that only knows the spawning task id still resolves
    # after terminal memory retires. Bound aliases independently; active
    # session aliases are never candidates for this pruning.
    retired_aliases = [
        alias for alias, target in _key_aliases.items()
        if target not in _active_sessions
    ]
    for alias in retired_aliases[:-_RETIRED_ALIAS_CAPACITY]:
        _key_aliases.pop(alias, None)
    with _autocontinue_lock:
        _autocontinue_chain.pop(key, None)
        _autocontinue_inflight.discard(key)
    logger.info(
        '[Swarm:%s] retired terminal in-memory session at cache target '
        '(durable result preserved)', key)


@contextmanager
def reserve_session(swarm_key: str):
    """Identity reservation for recovery; no registry lock over storage.

    A published key remains reserved until activation exits this context.
    Reservation protects that identity; unrelated sessions can enter freely.
    """
    with _sessions_lock:
        if swarm_key in _active_sessions or swarm_key in _session_reservations:
            raise SwarmSessionConflict('swarm session already admitted')
        _session_reservations.add(swarm_key)
    try:
        yield
    finally:
        with _sessions_lock:
            _session_reservations.discard(swarm_key)


def _set_session(swarm_key: str, session: MasterOrchestrator, *,
                 task_id: str = '', reserved: bool = False):
    """Register *session* under its stable swarm key.

    ``task_id`` (when distinct) is recorded as an alias so route callers and
    the orchestrator teardown — which only know the task id — still resolve
    to this session.
    """
    with _sessions_lock:
        _cleanup_stale_sessions()
        if swarm_key in _session_reservations and not reserved:
            raise SwarmSessionConflict('swarm recovery admission already reserved')
        missing = object()
        previous_session = _active_sessions.get(swarm_key, missing)
        previous_timestamp = _session_timestamps.get(swarm_key, missing)
        has_alias = bool(task_id and task_id != swarm_key)
        previous_alias = _key_aliases.get(task_id, missing)
        timestamp = time.time()
        _active_sessions[swarm_key] = session
        _session_timestamps[swarm_key] = timestamp
        if has_alias:
            _key_aliases[task_id] = swarm_key
        try:
            _reconcile_cleanup_timer_locked()
        except BaseException:
            # Publication is not acknowledged until cleanup residency succeeds.
            # Restore only this registration's writes; never delete durable truth
            # or unrelated aliases, including an alias overwritten by this turn.
            if previous_session is missing:
                _active_sessions.pop(swarm_key, None)
            else:
                _active_sessions[swarm_key] = previous_session
            if previous_timestamp is missing:
                _session_timestamps.pop(swarm_key, None)
            else:
                _session_timestamps[swarm_key] = previous_timestamp
            if has_alias:
                if previous_alias is missing:
                    _key_aliases.pop(task_id, None)
                else:
                    _key_aliases[task_id] = previous_alias
            raise


def _remove_session(task_id: str, *, preserve_durable: bool = False):
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
        _reconcile_cleanup_timer_locked()
    if preserve_durable:
        return
    # Only the swarm lane is being torn down here. The conversation-keyed
    # inbox is shared with background-command / user-steer / peer-msg items
    # whose delivery owners are still live — clearing them mid-turn would
    # strand their durable rows until settlement (the stuck "result
    # delivered-unconfirmed" banner).
    agent_inbox.clear(key, modes=['swarm-update'])
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


def _agent_status_counts(agents: list[dict]) -> dict[str, int]:
    """Fold the returned roster, never independently sampled pool occupancy."""
    return {
        'running': sum(a.get('status') in ('running', 'retrying') for a in agents),
        'pending': sum(a.get('status') == 'pending' for a in agents),
        'completed': sum(a.get('status') in ('completed', 'failed', 'cancelled', 'aborted')
                         for a in agents),
    }


def get_swarm_status(task_id: str, *, user_id: int) -> dict | None:
    """Return swarm status for a task — THREE states, never a bare "no".

    The frontend reconciler settles a stuck panel on this answer, so the
    answer must distinguish:

      * ``active: True``            — live in memory, still working.
      * ``active: False, known: True, terminated: True`` — definitively over;
        ``agents`` carries real per-agent outcomes (in-memory session, or the
        durable row after a restart/eviction). Safe to settle from.
      * ``active: None, known: False`` — no record anywhere (or a persisted
        'running' row the process lost track of: pre-rehydrate window, failed
        rehydrate). NOT a settle signal — the caller must keep probing.

    Returns None only when there is genuinely no trace of a swarm; the routes
    translate that into the ``active: None / known: False`` envelope.
    """
    from lib.identity import require_user_id

    owner_user_id = require_user_id(user_id, context='swarm status')
    session = _get_session(task_id)
    if session is None:
        return _status_from_persistence(task_id, user_id=owner_user_id)
    if int(session.user_id) != owner_user_id:
        return None
    try:
        agents_info = []
        for sid, info in session.get_status(include_tool_timeline=True).items():
            agents_info.append({'id': sid, **info})
        return {
            'active':     not session.is_terminated and not session.is_restart_suspended,
            'known':      True,
            'terminated': session.is_terminated and not session.is_restart_suspended,
            'awaitingContinue': session.is_restart_suspended,
            'task_id':    task_id,
            'agents':     agents_info,
            'agent_count': len(agents_info),
            **_agent_status_counts(agents_info),
            'created_at': _session_timestamps.get(_resolve_key(task_id), 0),
        }
    except Exception as e:
        logger.warning('[swarm] Error getting status for %s: %s',
                       task_id, e, exc_info=True)
        return {'active': True, 'known': True, 'task_id': task_id,
                'error': str(e)}


def _status_from_persistence(task_id: str, *, user_id: int) -> dict | None:
    """Answer a status probe from the durable record when memory lost the session.

    A terminated row is a definitive answer (settle with its agents' real
    statuses). A 'running' row with nothing in memory is AMBIGUOUS — the
    process may be mid-rehydrate after a restart — so it maps to the
    ``known: False`` envelope (keep probing), with the persisted agent rows
    attached for display context.
    """
    key = _resolve_key(task_id)
    from lib.swarm import persistence
    row = persistence.load_session(key)
    if row is None and key != task_id:
        # The probe may carry the ORIGINAL spawning task id whose alias was
        # lost with the process; the durable row is keyed by the swarm key.
        row = persistence.load_session(task_id)
    if row is None:
        return None
    config = row.get('config') or {}
    try:
        persisted_owner_user_id = require_user_id(
            config.get('user_id') if isinstance(config, dict) else None,
            context='persisted swarm status')
    except ValueError:
        return None
    if persisted_owner_user_id != int(user_id):
        return None
    agents: list[dict] = []
    for a in (row.get('agents') or []):
        if not isinstance(a, dict):
            continue
        result = a.get('result') or {}
        status = a.get('status') or 'pending'
        error = ((result.get('error_message') or '')
                 if isinstance(result, dict) else '')
        if status == 'completed' and error:
            status = 'failed'
        agents.append({
            'id':        a.get('agent_id'),
            'role':      a.get('role') or '',
            'objective': (a.get('objective') or '')[:120],
            'status':    status,
            'error':     error,
        })
    if row.get('status') == 'running':
        try:
            from lib.swarm.restart_recovery import restart_source_attempt
            interrupted = restart_source_attempt(row, user_id=persisted_owner_user_id) is not None
        except Exception:
            logger.warning('[Swarm:%s] restart provenance unavailable; retaining unknown status',
                           key, exc_info=True)
            interrupted = False
        if interrupted:
            for agent in agents:
                if agent['status'] in ('pending', 'running', 'retrying'):
                    agent['status'] = 'interrupted'
            return {
                'active': False, 'known': True, 'terminated': False,
                'awaitingContinue': True, 'recoveryReason': 'server_restart',
                'source': 'persisted', 'task_id': task_id,
                'swarm_key': key, 'swarm_run_id': str(row.get('task_id') or ''),
                'agents': agents, 'agent_count': len(agents),
                'created_at': row.get('created_at', 0),
                **_agent_status_counts(agents),
            }
    if row.get('status') in SWARM_SESSION_TERMINAL_STATUSES:
        return {
            'active':     False,
            'known':      True,
            'terminated': True,
            'source':     'persisted',
            'task_id':    task_id,
            'agents':     agents,
            'agent_count': len(agents),
            'created_at': row.get('created_at', 0),
        }
    return {
        'active':           None,
        'known':            False,
        'persisted_status': row.get('status'),
        'task_id':          task_id,
        'agents':           agents,
        'agent_count':      len(agents),
    }


def list_active_swarms(*, user_id: int) -> list[dict]:
    """Bounded summaries of this owner's LIVE (non-terminated) swarm sessions.

    Powers the background-activity surface: the spawning turn has usually
    ended by the time anyone reads this, so nothing here depends on a task
    id — the stable swarm key and the conversation id are what the client
    needs to deep-link. Per-agent status is sampled OUTSIDE the registry
    lock (``get_status`` takes the session's own lock) so a busy session
    never stalls session registry readers.
    """
    owner_user_id = require_user_id(user_id, context='swarm activity list')
    with _sessions_lock:
        live = [
            (key, session, _session_timestamps.get(key, 0))
            for key, session in _active_sessions.items()
            if int(session.user_id) == owner_user_id
            and not getattr(session, 'is_terminated', False)
        ]
    out: list[dict] = []
    for key, session, created_at in live:
        try:
            agents = [
                {'id': sid, **info}
                for sid, info in session.get_status().items()
            ]
            # The dock consumes this same poll for run messages. A session key
            # is reusable; only the original task ID identifies its mailbox.
            from lib.swarm.message_receipts import snapshot_receipts
            with session._lock:
                specs = list(session.specs)
            messages = snapshot_receipts(session, agents, specs)
            out.append({
                'swarm_key':  key,
                'swarm_id':   session.task_id,
                **messages,
                'conv_id':    session.conv_id,
                **_agent_status_counts(agents),
                'agent_count': len(agents),
                'agents':     agents,
                'created_at': created_at,
            })
        except Exception as e:
            logger.warning('[swarm] activity snapshot failed for %s: %s',
                           key, e, exc_info=True)
    return out


def suspend_swarms_for_restart() -> int:
    """Halt unique drivers without tombstoning delivery or Stop authority."""
    with _sessions_lock:
        sessions = list(_active_sessions.values())
    seen = set()
    suspended = 0
    for session in sessions:
        if id(session) in seen or session.is_terminated:
            continue
        seen.add(id(session))
        session.suspend_for_restart()
        suspended += 1
    return suspended


def abort_swarm(task_id: str, *, user_id: int) -> dict:
    """Abort a running swarm session (used by routes/api_v1/swarm)."""
    from lib.identity import require_user_id

    owner_user_id = require_user_id(user_id, context='swarm abort')
    session = _get_session(task_id)
    if session is None or int(session.user_id) != owner_user_id:
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
