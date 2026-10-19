"""Cache-state singleton + lifecycle (SHARED MUTABLE STATE).

This submodule owns the ONE authoritative ``_cache_states`` dict, its
``_cache_lock``, and the ``CacheState`` class. Every other submodule in the
``cache_tracking`` package imports these by reference from here — there is
exactly one copy of the state, so ``from lib.tasks_pkg.cache_tracking import
_cache_states`` returns the SAME object the internal functions mutate.

Also owns the state-eviction helpers (``cleanup_cache_state`` /
``cleanup_stale_cache_states``). The L2 ROI flush on eviction is delegated to
``_roi._emit_l2_roi`` via a LAZY import
inside the functions to keep the dependency direction clean (``_roi`` imports
this module for the state; this module only reaches into ``_roi`` at call
time, never at import time).
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy
from dataclasses import dataclass, field
from functools import wraps
from itertools import count

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Cache state tracking
# ═══════════════════════════════════════════════════════════════════════════════

class CacheState:
    """Tracks the state of the prompt cache for a conversation.

    Stores system/tool hashes and one bounded source-field fallback so we can
    detect what changed between turns. Provider-bound wire evidence remains
    authoritative; compaction explicitly suppresses fallback comparisons so a
    legitimate lifecycle rewrite is never reported as an ordinary mutation.
    A bounded in-place rewrite records ``expected_rewrite_index`` instead of
    raising ``compaction_pending``, so detectors stay live except at/after
    that api-form index.

    Extended in v2:
      - per_tool_hashes: per-tool hash for diffing which tool changed
      - prefix_field_hashes: per-message fallback mutation evidence used only
        when authoritative provider-bound wire capture is unavailable
      - session-level aggregate stats (total reads/writes/breaks)
    """
    __slots__ = (
        'system_hash', 'tools_hash', 'model',
        'message_count', 'last_cache_read_tokens',
        'last_cache_write_tokens',
        'last_update_time', 'call_count',
        'compaction_pending',
        'expected_rewrite_index',
        # v2: detailed diagnostics
        'per_tool_hashes',
        'prefix_field_count',
        'prefix_field_hashes',
        # Authoritative post-translation wire fingerprint (see wire_fingerprint.py)
        'wire_fp', 'wire_static', 'wire_system', 'wire_markers', 'wire_bytes',
        'wire_field_bytes',
        'wire_region',
        'wire_routing',
        'total_cache_read', 'total_cache_write',
        'total_breaks', 'total_input_tokens',
        'first_call_time',
        'pending_l2_roi',
        'cold_streak',
    )

    def __init__(self):
        self.system_hash: str = ''
        self.tools_hash: str = ''
        self.model: str = ''
        self.message_count: int = 0
        self.last_cache_read_tokens: int = 0
        self.last_cache_write_tokens: int = 0
        self.last_update_time: float = 0.0
        self.call_count: int = 0
        self.compaction_pending: bool = False
        # Earliest api-form messages index a bounded compaction intends to
        # rewrite this round. -1 means none. Consumed with compaction_pending
        # by detect_cache_break. A whole-prefix L2 notify clears this back
        # to -1 and raises compaction_pending instead.
        self.expected_rewrite_index: int = -1
        # Phase-C L2 ROI: the 'saved' half of one force-summary event, stashed
        # at compaction time and completed with the FOLLOWING round's re-billed
        # cache_write in detect_cache_break. None when no L2 event is pending.
        self.pending_l2_roi: dict | None = None
        # Consecutive verifiably-cold rounds (read ≤ floor AND write ≤ floor),
        # updated per round in detect_cache_break. Reporting/classification
        # only — the L1 prefix guard no longer derives from this streak
        # (see the constant above for the loop rationale).
        self.cold_streak: int = 0
        # v2 fields
        self.per_tool_hashes: dict[str, str] = {}  # tool_name → hash
        self.prefix_field_count: int = 0
        # One compact, fixed-width process-local fallback baseline;
        # authoritative wire evidence remains primary.
        self.prefix_field_hashes: list[tuple[int | None, ...]] = []
        # Authoritative post-translation wire fingerprint from the PREVIOUS
        # round (list of per-msg process-local canonical entries) + the
        # stable-format static-floor hash.
        # When present, these are the ground truth for prefix-mutation
        # attribution — they reflect the actual bytes sent, not a client-side
        # reconstruction. See lib/tasks_pkg/wire_fingerprint.py.
        self.wire_fp: list | None = None
        self.wire_static: str = ''
        # Fingerprint of the HOISTED system block + tool schemas from the
        # PREVIOUS round ({'system': md5, 'tools': md5}). On the Anthropic path
        # these live OUTSIDE body['messages'], so wire_fp (canonical_messages)
        # is blind to them; folding this into the prefix-mutation verdict is
        # what stops a per-turn system change (digest/charter/board) from being
        # laundered into a false "server-side PROVEN". See wire_fingerprint.system_fingerprint.
        self.wire_system: dict | None = None
        # Marker LAYOUT (cache_control breakpoint positions/counts) from the
        # PREVIOUS round. canonical_messages strips cache_control, so this is
        # the ONLY signal that catches a breakpoint LOST in translation (a
        # byte-identical round whose tail marker vanished) — see
        # wire_fingerprint.markers_regressed. Folded into detect_cache_break so
        # a dropped breakpoint can never be laundered into "server-side PROVEN".
        self.wire_markers: dict | None = None
        # TRUE-byte per-message process-local integer fingerprints from the
        # PREVIOUS round ([{'key','h'}]). canonical_messages (wire_fp) is
        # LOSSY — it strips
        # cache_control, collapses str↔block, and skips reasoning_details — so
        # "wire_fp identical" does NOT prove the SERIALIZED BYTES were identical.
        # This hashes json.dumps(msg) (only cache_control stripped) so the
        # eviction verdict can be GATED on the real bytes matching, refusing to
        # claim "byte-identical" when a canonical-invisible mutation
        # (reasoning_details rebuild / same-role merge / protocol switch)
        # actually changed the wire. See wire_fingerprint.wire_byte_prefix.
        self.wire_bytes: list | None = None
        # FIELD-GRANULAR process-local true-byte fingerprints
        # ([{'key','fields':{field:int}}]) from the PREVIOUS round. wire_bytes
        # names only THAT a message's bytes
        # changed; this names the EXACT top-level field (reasoning_details /
        # tool_calls / content / __order__) so a canonical-invisible <bytes>
        # divergence is logged with a proven field, not a guessed category.
        # See wire_fingerprint.wire_byte_field_prefix / diff_byte_field_prefix.
        self.wire_field_bytes: list | None = None
        # TRUE-byte hash of the hoisted system + tools region from the PREVIOUS
        # round ({'system':md5,'tools':md5}). system_fingerprint (wire_system)
        # is LOSSY — it runs _text_of over the system blocks and sort_keys over
        # tool params — so a system BLOCK REORDER / wrapping flip / per-turn
        # re-serialization (the fresh-injected charter/board/peer/memories) is
        # invisible to it. This hashes the real serialized bytes so the eviction
        # verdict can be gated on the hoisted region too, not just messages.
        # See wire_fingerprint.wire_byte_region.
        self.wire_region: dict | None = None
        # Cache-NAMESPACE routing fingerprint from the PREVIOUS round
        # ({'key','beta','endpoint'} — see wire_fingerprint.routing_fingerprint).
        # Anthropic prompt caching is namespaced by (upstream key + anthropic-beta
        # header + endpoint); the dispatch layer CAN flip the key mid-conversation
        # (cooldown/429/401/timeout → sticky key scored inf → rebind) and the
        # extended-cache-ttl beta is latched per-TASK, so a byte-identical prefix
        # can land on a COLD namespace = a client-caused miss the BODY fingerprints
        # are blind to. detect_cache_break diffs this BEFORE the byte-identical
        # verdict so a namespace flip is NAMED client-side, not laundered upstream.
        self.wire_routing: dict | None = None
        self.total_cache_read: int = 0
        self.total_cache_write: int = 0
        self.total_breaks: int = 0
        self.total_input_tokens: int = 0
        self.first_call_time: float = 0.0


# ── Compaction-guard hysteresis (the 2026-08-01 self-feeding re-bill fix) ──
# How many CONSECUTIVE verifiably-cold rounds (cache_read ≤ 1000 AND
# cache_write ≤ 1000) a thread must see before the streak counts a
# verifiably-cold round. The streak feeds detector/display classification
# only: since 2026-09-15 ``get_cache_prefix_count`` no longer opens the
# micro_compact guard on streaks at all — flickering zero reports
# (write-visibility race, gateway stochastic miss, namespace flip, kimi's
# never-reported cache_write, GLM idle-gap zeros) are indistinguishable from
# a dead cache, and opening the guard on them let L1 strip steps rewrite
# still-cached prefix bytes EVERY round with no recovery path (conv
# ms9ow2tt 2026-08-01; TB2.1 v7 GLM full-domain run 2026-09-08). A truly
# dead cache costs one ordinary re-write of the same bytes; a wrongly
# opened guard costs a re-bill per round. The monotonic boundary therefore
# never reopens once a sent prefix exists.
COLD_STREAK_GUARD_OPEN = 3

# Read/write token floor below which a round counts as "cold" for the
# streak — mirrors the historical ``_boundary`` gate in ``_prefix.py``.
COLD_ROUND_TOKEN_FLOOR = 1000


_cache_states: dict[tuple[int, str, str | int], CacheState] = {}
"""Task-scoped state; integer keys are retained for unscoped legacy callers."""

_cache_lock = threading.Lock()


@dataclass
class _CacheInvocation:
    key: tuple
    lane: str
    sequence: int
    predecessor_read: int
    predecessor_time: float
    completed: bool = False
    rounds_seen: int = 0
    durable_boundary: int = 0
    task_id: str = ''
    baseline_ready: threading.Event = field(default_factory=threading.Event)


_cache_invocation: ContextVar[_CacheInvocation | None] = ContextVar(
    'cache_invocation', default=None)
_cache_invocations: dict[tuple, _CacheInvocation] = {}
_invocation_sequence = count(1)


def _release_state_locked(key: tuple, pending: list) -> None:
    """Detach one lifecycle and its unpaired ROI; caller holds the state lock."""
    _cache_invocations.pop(key, None)
    removed = _cache_states.pop(key, None)
    if removed is not None and removed.pending_l2_roi is not None:
        pending.append((key, removed.pending_l2_roi))
        removed.pending_l2_roi = None


def _flush_released_roi(pending: list) -> None:
    """Emit outside the state lock; a detached ROI can be flushed only once."""
    if not pending:
        return
    from lib.tasks_pkg.cache_tracking._roi import _emit_l2_roi
    for key, roi in pending:
        _emit_l2_roi(key[1], roi, user_id=key[0], cache_write_rebilled=None)


@contextmanager
def cache_tracking_scope(task: dict):
    """Bind all cache consumers to task identity, never a pooled worker.

    Only completed same-owner/conversation/lane invocations can seed a new
    turn. Snapshot selection happens once, before concurrent calls can advance
    a sibling. Child/inline/transient prompts never borrow the root baseline.
    Retain one completed snapshot per lane plus active invocations; ordinary
    cache eviction also drops this metadata. No durable I/O under the lock.
    """
    from lib.tasks_pkg.invocation import is_child_invocation

    conv_id = task.get('convId')
    task_id = task.get('id')
    if not conv_id or not task_id:
        yield
        return
    user_id = task.get('_userId')
    _state_key(conv_id, user_id=user_id)  # validate ownership before binding
    isolated = (is_child_invocation(task) or task.get('_transientRuntime')
                or task.get('_inline_messages'))
    lane = f'invocation:{task_id}' if isolated else 'conversation'
    current = _current_invocation(conv_id, user_id)
    if current and current.task_id == task_id and current.lane == lane:
        # Whole-turn retry recursively invokes run_task(task). The outer frame
        # owns teardown; all nested retries update that same lifecycle.
        yield
        return
    sequence = next(_invocation_sequence)
    key = (user_id, conv_id, f'task:{task_id}:execution:{sequence}')
    with _cache_lock:
        candidates = [entry for entry in _cache_invocations.values()
                      if entry.key[:2] == key[:2] and entry.lane == lane
                      and entry.completed and entry.key in _cache_states]
        predecessor = max(candidates, key=lambda entry: entry.sequence,
                          default=None)
        state = _cache_states.get(predecessor.key) if predecessor else None

        active_ancestor = min(
            (entry for entry in _cache_invocations.values()
             if entry.key[:2] == key[:2] and entry.lane == lane
             and not entry.completed), key=lambda entry: entry.sequence,
            default=None) if state is None else None
        scope = _CacheInvocation(
            key, lane, sequence,
            state.last_cache_read_tokens if state else 0,
            state.last_update_time if state else 0.0, task_id=task_id)
        if state is not None:
            # Detector comparisons need the actual previous wire/source hashes,
            # not merely a cache-read count from a random sibling.
            _cache_states[key] = copy(state)

            # Mutable pending rewrite events belong to their invocation, not
            # the shared completed prefix snapshot.
            _cache_states[key].pending_l2_roi = None
            _cache_states[key].compaction_pending = False
            _cache_states[key].expected_rewrite_index = -1
        _cache_invocations[key] = scope
    try:
        if active_ancestor is not None:
            # Publication reserves the initialization owner but does not make
            # its zero-valued placeholders usable. Never hold the global lock
            # across this handshake or the owner's storage I/O.
            active_ancestor.baseline_ready.wait()
            scope.predecessor_read = active_ancestor.predecessor_read
            scope.predecessor_time = active_ancestor.predecessor_time
            scope.durable_boundary = active_ancestor.durable_boundary
        else:
            if state is None and not isolated:
                try:
                    from lib.tasks_pkg.cache_tracking._persist import read_last_turn_cache_read
                    scope.predecessor_read = int(read_last_turn_cache_read(
                        conv_id, user_id=user_id) or 0)
                except Exception as exc:
                    logger.debug('[CacheTrack] durable predecessor unavailable: %s', exc)
            if not isolated:
                try:
                    from lib.tasks_pkg.cache_tracking._persist import read_persisted_boundary
                    scope.durable_boundary = int(read_persisted_boundary(
                        conv_id, user_id=user_id) or 0)
                except Exception as exc:
                    logger.debug('[CacheTrack] durable boundary unavailable: %s', exc)
    except BaseException:
        pending = []
        with _cache_lock:
            _release_state_locked(key, pending)
        _flush_released_roi(pending)
        raise
    finally:
        scope.baseline_ready.set()
    token = _cache_invocation.set(scope)
    try:
        yield
    finally:
        pending = []
        with _cache_lock:
            scope.completed = True
            # Finish order is the predecessor sequence; active siblings are
            # never candidates, even when their last API call is newer.
            scope.sequence = next(_invocation_sequence)
            for old_key, entry in list(_cache_invocations.items()):
                if (scope.rounds_seen and old_key != key and entry.completed
                        and old_key[:2] == key[:2] and entry.lane == lane):
                    _release_state_locked(old_key, pending)
            final_state = _cache_states.get(key)
            if final_state is None or not scope.rounds_seen or isolated:
                _release_state_locked(key, pending)
        try:
            _flush_released_roi(pending)
        finally:
            _cache_invocation.reset(token)


def with_cache_tracking_scope(function):
    """Keep invocation entry/teardown paired on every return/exception path."""
    @wraps(function)
    def scoped(task, *args, **kwargs):
        with cache_tracking_scope(task):
            return function(task, *args, **kwargs)
    return scoped


def _current_invocation(conv_id: str, user_id: int):
    scope = _cache_invocation.get()
    if scope is None or scope.key[0]!=user_id:
        return None
    # Compaction gives a child its explicit owner/task namespace while model
    # observations retain the authorization conversation. Both name this one
    # invocation; no sibling or conversation baseline may be borrowed.
    isolated_key=f'invocation:{user_id}:{scope.task_id}'
    if scope.key[1]==conv_id or (scope.lane!='conversation' and conv_id==isolated_key):
        return scope
    return None


def _allows_conversation_baseline(conv_id: str, user_id: int) -> bool:
    scope = _current_invocation(conv_id, user_id)
    return scope is None or scope.lane == 'conversation'


def _state_key(conv_id: str, *, user_id: int) -> tuple[int, str, str | int]:
    """Resolve the task identity shared by detection, guards and compaction.

    Production invocations bind an explicit lifecycle; only unscoped legacy
    callers retain a thread key. Worker reuse is not a turn identity.
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError('user_id must be a positive integer')
    scope = _current_invocation(conv_id, user_id)
    return scope.key if scope else (user_id, conv_id, threading.get_ident())


def get_warm_cache_read(conv_id: str, *, user_id: int) -> int:
    """Largest recent cache-read prefix observed for this conversation.

    The proactive L2 gate runs before the next provider call and may execute on
    a different worker thread from the turn that warmed the cache.  Inspect all
    live per-thread states and the durable previous-turn baseline, then return
    the larger value.  The current task's existing three-round verified-cold
    guard is stronger, newer evidence than either fallback and returns zero.
    One or two cold rounds still preserve the warm baseline.  Zero means there
    is no evidence of a warm prefix, so an automatic rewrite has no known cache
    entry to destroy.
    """
    if not conv_id:
        return 0
    with _cache_lock:
        current = _cache_states.get(_state_key(conv_id, user_id=user_id))
        if (current is not None and current.call_count > 0
                and current.cold_streak >= COLD_STREAK_GUARD_OPEN):
            return 0
        live = max(
            (int(st.last_cache_read_tokens) for (uid, cid, _tid), st
             in _cache_states.items()
             if uid == user_id and cid == conv_id and st.call_count > 0
             and (_current_invocation(conv_id, user_id) is None
                  or (uid, cid, _tid) == _state_key(conv_id, user_id=user_id))),
            default=0,
        )
    scope = _current_invocation(conv_id, user_id)
    if scope is not None:
        return max(live, scope.predecessor_read)
    try:
        from lib.tasks_pkg.cache_tracking._persist import (
            read_last_turn_cache_read)
        durable = int(read_last_turn_cache_read(
            conv_id, user_id=user_id) or 0)
    except Exception as e:
        logger.debug('[CacheTrack] durable warm-cache read unavailable '
                     'conv=%s: %s', conv_id[:8], e)
        durable = 0
    return max(live, durable)


def get_prev_turn_cache_read(conv_id: str, *, user_id: int) -> int:
    """Return the invocation's frozen completed, same-lane predecessor read.

    Detection and write allocation share this snapshot even after current
    usage is stamped. Active siblings and child prompts are never predecessors.
    Legacy unscoped callers use the latest state including their own worker;
    their orchestrator must capture it BEFORE detection, not afterward.
    A cold root invocation reads the durable fallback only once at entry.
    """
    if not conv_id:
        return 0
    scope = _current_invocation(conv_id, user_id)
    if scope is not None:
        return scope.predecessor_read
    best = None
    with _cache_lock:
        for (uid, cid, _tid), st in _cache_states.items():
            if (uid != user_id or cid != conv_id
                    or (uid, cid, _tid) in _cache_invocations
                    or st.call_count <= 0):
                continue
            if best is None or st.last_update_time > best.last_update_time:
                best = st
    if best is not None:
        return best.last_cache_read_tokens

    # ── DURABLE cold-state fallback ──────────────────────────────────────────
    # No live sibling: this is a cold process (restart / >1h stale-eviction /
    # multi-replica bounce). The memory-only path returns 0 here → the round-1
    # boundary judgment short-circuits and a genuine collapsed/zero round-1 is
    # laundered into no_break (log-proven: turn_boundary_rebill fired only 3×
    # while dozens of floor-only round-1s went uncounted). Fall back to the
    # DURABLE previous-turn read persisted on conversations.settings so the
    # boundary re-bill buckets honestly even from a cold process — mirroring
    # read_persisted_boundary's role for the compaction floor. Best-effort:
    # a lazy import + guarded read that returns 0 on any miss (today's behavior).
    try:
        from lib.tasks_pkg.cache_tracking._persist import (
            read_last_turn_cache_read)
        return read_last_turn_cache_read(conv_id, user_id=user_id)
    except Exception as e:
        logger.debug('[CacheTrack] durable prev-turn read fallback '
                     'unavailable conv=%s: %s', conv_id[:8], e)
        return 0


def cleanup_cache_state(conv_id: str, *, user_id: int) -> None:
    """Remove cache state for a conversation that's no longer active.

    Call when a conversation is explicitly deleted or after extended
    inactivity to prevent unbounded memory growth.
    """
    pending = []
    with _cache_lock:
        keys = {key for key in (*_cache_states, *_cache_invocations)
                if key[:2] == (user_id, conv_id)}
        for key in keys:
            _release_state_locked(key, pending)
    _flush_released_roi(pending)
    if keys:
        logger.debug('[CacheTrack] Cleaned up %d state(s) for conv=%s',
                     len(keys), conv_id[:8])


def cleanup_stale_cache_states(max_age_s: float = 3600) -> int:
    """Remove cache states for conversations inactive longer than max_age_s.

    Call periodically (e.g., every 10 minutes) to prevent unbounded
    memory growth from long-lived server processes.

    Args:
        max_age_s: Max seconds since last update before eviction.
                   Default 3600 (1 hour).

    Returns:
        Number of stale entries removed.
    """
    pending = []
    cutoff = time.time() - max_age_s
    removed = 0
    with _cache_lock:
        stale_keys = [
            key for key, state in _cache_states.items()
            if state.last_update_time < cutoff
        ]
        for key in stale_keys:
            _release_state_locked(key, pending)
            removed += 1
    _flush_released_roi(pending)
    if removed:
        logger.info('[CacheTrack] Cleaned up %d stale cache states '
                    '(older than %ds, %d remaining)',
                    removed, int(max_age_s), len(_cache_states))
    return removed
