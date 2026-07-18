"""Cache-state singleton + lifecycle (SHARED MUTABLE STATE).

This submodule owns the ONE authoritative ``_cache_states`` dict, its
``_cache_lock``, and the ``CacheState`` class. Every other submodule in the
``cache_tracking`` package imports these by reference from here — there is
exactly one copy of the state, so ``from lib.tasks_pkg.cache_tracking import
_cache_states`` returns the SAME object the internal functions mutate.

Also owns the state-eviction helpers (``cleanup_cache_state`` /
``cleanup_stale_cache_states``) and the tools-registry latch release. The L2
ROI flush on eviction is delegated to ``_roi._emit_l2_roi`` via a LAZY import
inside the functions to keep the dependency direction clean (``_roi`` imports
this module for the state; this module only reaches into ``_roi`` at call
time, never at import time).
"""

from __future__ import annotations

import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Cache state tracking
# ═══════════════════════════════════════════════════════════════════════════════

class CacheState:
    """Tracks the state of the prompt cache for a conversation.

    Stores hashes of system prompt, tools, and message count so we can
    detect what changed between turns.  Does NOT hash message content
    because micro-compact legitimately mutates older messages — hashing
    content would produce false positives on every round.

    Extended in v2:
      - per_tool_hashes: per-tool hash for diffing which tool changed
      - prefix_content_hash: hash of messages in the cache prefix
        (only used for mutation detection, NOT for break detection)
      - session-level aggregate stats (total reads/writes/breaks)
    """
    __slots__ = (
        'system_hash', 'tools_hash', 'model',
        'message_count', 'last_cache_read_tokens',
        'last_cache_write_tokens',
        'last_update_time', 'call_count',
        'compaction_pending',
        'history_rewrite_pending',
        # v2: detailed diagnostics
        'per_tool_hashes',
        'prefix_content_hash',
        'prefix_content_count',
        'prefix_field_hashes',
        # Authoritative post-translation wire fingerprint (see wire_fingerprint.py)
        'wire_fp', 'wire_static', 'wire_system', 'wire_markers', 'wire_bytes',
        'wire_field_bytes',
        'wire_region',
        'total_cache_read', 'total_cache_write',
        'total_breaks', 'total_input_tokens',
        'first_call_time',
        'pending_l2_roi',
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
        # Set by notify_history_rewrite() when a backend reconcile /
        # committed-dict projection edited committed messages. Unlike
        # compaction_pending it feeds NO break gate and does NOT skip the wire
        # diff — it only NAMES the cause (see detect_cache_break).
        self.history_rewrite_pending: bool = False
        # Phase-C L2 ROI: the 'saved' half of one force-summary event, stashed
        # at compaction time and completed with the FOLLOWING round's re-billed
        # cache_write in detect_cache_break. None when no L2 event is pending.
        self.pending_l2_roi: dict | None = None
        # v2 fields
        self.per_tool_hashes: dict[str, str] = {}  # tool_name → hash
        self.prefix_content_hash: str = ''
        self.prefix_content_count: int = 0
        # Per-message, per-field prefix hashes (precise culprit attribution).
        self.prefix_field_hashes: list[dict] = []
        # Authoritative post-translation wire fingerprint from the PREVIOUS
        # round (list of per-msg canonical entries) + the static-floor hash.
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
        # TRUE-byte per-message prefix hashes from the PREVIOUS round
        # ([{'key','h'}]). canonical_messages (wire_fp) is LOSSY — it strips
        # cache_control, collapses str↔block, and skips reasoning_details — so
        # "wire_fp identical" does NOT prove the SERIALIZED BYTES were identical.
        # This hashes json.dumps(msg) (only cache_control stripped) so the
        # eviction verdict can be GATED on the real bytes matching, refusing to
        # claim "byte-identical" when a canonical-invisible mutation
        # (reasoning_details rebuild / same-role merge / protocol switch)
        # actually changed the wire. See wire_fingerprint.wire_byte_prefix.
        self.wire_bytes: list | None = None
        # FIELD-GRANULAR true-byte hashes ([{'key','fields':{field:md5}}]) from
        # the PREVIOUS round. wire_bytes names only THAT a message's bytes
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
        self.total_cache_read: int = 0
        self.total_cache_write: int = 0
        self.total_breaks: int = 0
        self.total_input_tokens: int = 0
        self.first_call_time: float = 0.0


_cache_states: dict[tuple, CacheState] = {}
"""Cache state keyed by ``(conv_id, thread_id)`` — see ``_state_key``."""

_cache_lock = threading.Lock()


def _state_key(conv_id: str) -> tuple:
    """Key ``_cache_states`` per ``(conv_id, thread)``.

    N concurrent agent loops running under ONE conversation (swarm / flow /
    orchestration fan-out) previously shared a single ``conv_id``-keyed
    ``CacheState`` and clobbered each other's prefix baseline every round —
    the root cause of the incoherent ``PREFIX MUTATION DETECTED`` spam
    (call-counts / message-lengths jumping between threads) and the cache
    cost misattribution. A single task runs its whole round loop on one
    worker thread, so the thread id is a stable per-agent discriminator
    across rounds while distinct concurrent agents get distinct threads →
    distinct, non-colliding state. All same-thread callers
    (``detect_cache_break`` post-round, ``notify_compaction`` /
    ``get_cache_prefix_count`` in the pipeline) resolve to the same entry.
    """
    return (conv_id, threading.get_ident())


def get_prev_turn_cache_read(conv_id: str) -> int:
    """Best CROSS-THREAD ``cache_read`` baseline for a turn's round-1 (0 if none).

    ``_cache_states`` is keyed per ``(conv_id, thread)``. A new user turn runs
    on a fresh ``run_task`` thread → a fresh ``CacheState`` with
    ``call_count == 0``, so the within-thread read baseline
    (``prev.last_cache_read_tokens``) is 0 on that turn's round-1. The break
    classifier tolerates that (it gates on ``call_count > 0``), but the
    write-breakdown then had NO read baseline for round-1 and defaulted the
    whole ``cache_write`` to the benign ``contextWrite`` — even when the
    PREVIOUS turn's cached prefix was partly evicted and re-billed this round.
    That is the round-1 mislabel: an evicted-tail re-bill wearing the
    "first-cache warm-up" hat.

    This returns the most-recently-updated SIBLING state's
    ``last_cache_read_tokens`` for the same conversation — the previous turn's
    final cached-prefix read, carried across the thread boundary — so the
    write-breakdown can classify a round-1 read drop honestly.

    CRUCIAL: the CURRENT thread's own entry is EXCLUDED. ``detect_cache_break``
    runs before the write-breakdown each round and has already advanced this
    thread's entry (``call_count`` bumped, ``last_cache_read_tokens`` set to
    THIS round's read). Including it would return this round's own read →
    ``read_drop`` collapses to 0 and the fix is a no-op. So we skip the entry
    whose thread id is the caller's; the remaining ``call_count > 0`` entries
    are prior turns (or, under swarm fan-out, concurrent agents — acceptable
    for a display-only baseline that only feeds a turn's round-1).

    Best-effort: returns 0 on any miss. Reuses the existing state + lock, so no
    new lifecycle to prune (siblings are already evicted by
    ``cleanup_stale_cache_states``).
    """
    if not conv_id:
        return 0
    _self_tid = threading.get_ident()
    best = None
    with _cache_lock:
        for (cid, _tid), st in _cache_states.items():
            if cid != conv_id or _tid == _self_tid or st.call_count <= 0:
                continue
            if best is None or st.last_update_time > best.last_update_time:
                best = st
    return best.last_cache_read_tokens if best is not None else 0


def _release_multiroot_sticky(conv_id: str) -> None:
    """Release the tools-registry latches (multi-root + tool-schema) on evict.

    Imported lazily so this low-level module doesn't pull in the tools
    package at import time (and tolerates the symbol being absent). Both
    latches key on conv_id and share the cache-state lifecycle, so they're
    released together here.
    """
    if not conv_id:
        return
    try:
        from lib.tools import clear_multiroot_sticky, clear_tool_list_latch
        clear_multiroot_sticky(conv_id)
        clear_tool_list_latch(conv_id)
    except Exception as e:
        logger.debug('[CacheTrack] tools-registry latch release unavailable: %s', e)


def cleanup_cache_state(conv_id: str) -> None:
    """Remove cache state for a conversation that's no longer active.

    Call when a conversation is explicitly deleted or after extended
    inactivity to prevent unbounded memory growth.
    """
    # Lazy import keeps the dependency direction clean (_roi imports us).
    from lib.tasks_pkg.cache_tracking._roi import _emit_l2_roi
    with _cache_lock:
        # State is keyed per (conv_id, thread) — a conversation may have
        # several entries when its agent loops ran on different worker
        # threads (swarm / flow fan-out). Drop them all.
        _keys = [k for k in _cache_states if k[0] == conv_id]
        removed = None
        for k in _keys:
            removed = _cache_states.pop(k, None)
            # Flush any L2 ROI event that fired but never got a following round
            # to observe its re-bill — otherwise late/last-round L2 events (the
            # MOST likely ones, since context grows monotonically) are silently
            # dropped, biasing the retune dataset. Marked 'no_following_round'.
            if removed is not None and removed.pending_l2_roi is not None:
                _emit_l2_roi(conv_id, removed.pending_l2_roi,
                             cache_write_rebilled=None)
                removed.pending_l2_roi = None
        if removed:
            logger.debug('[CacheTrack] Cleaned up %d state(s) for conv=%s '
                         '(last calls=%d, total_breaks=%d)',
                         len(_keys), conv_id[:8], removed.call_count,
                         removed.total_breaks)
    _release_multiroot_sticky(conv_id)


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
    # Lazy import keeps the dependency direction clean (_roi imports us).
    from lib.tasks_pkg.cache_tracking._roi import _emit_l2_roi
    cutoff = time.time() - max_age_s
    removed = 0
    with _cache_lock:
        stale_keys = [
            key for key, state in _cache_states.items()
            if state.last_update_time < cutoff
        ]
        for key in stale_keys:
            _stale = _cache_states.pop(key, None)
            # Same anti-bias flush as cleanup_cache_state: a stale-evicted conv
            # whose last cache-relevant act was an L2 fire must still emit its
            # ROI (re-bill unobserved), not drop it.
            if _stale is not None and _stale.pending_l2_roi is not None:
                _emit_l2_roi(key[0], _stale.pending_l2_roi,
                             cache_write_rebilled=None)
                _stale.pending_l2_roi = None
            removed += 1
    for key in stale_keys:
        _release_multiroot_sticky(key[0])
    if removed:
        logger.info('[CacheTrack] Cleaned up %d stale cache states '
                    '(older than %ds, %d remaining)',
                    removed, int(max_age_s), len(_cache_states))
    return removed
