"""Cache-aware tool-result ordering, prefix-count gate & diagnostics.

``get_cache_prefix_count`` reads the shared ``_cache_states`` singleton and
the single-source ``EDITABLE_TAIL_COUNT`` bound; ``sort_tool_results`` uses it
to avoid re-ordering already-cached bytes; ``get_cache_diagnostics`` snapshots
both ``_cache_states`` and the TTL-latch table.
"""

from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.cache_tracking._state import (
    COLD_STREAK_GUARD_OPEN,
    _cache_lock,
    _cache_states,
    _state_key,
)
from lib.tasks_pkg.cache_tracking._detect import EDITABLE_TAIL_COUNT
from lib.tasks_pkg.cache_tracking._ttl import _ttl_latch

logger = get_logger(__name__)


def sort_tool_results(messages: list, conv_id: str = '') -> None:
    """Sort consecutive tool-result messages by tool_call_id for cache stability.

    When multiple tool results come back from parallel tool execution, their
    order in the messages array may vary between rounds if tools complete in
    different orders.  This causes the prefix to differ even though the
    content is identical, breaking automatic prefix caching (OpenAI/Qwen).

    For Anthropic explicit breakpoints, this is less critical since the
    breakpoints mark exact positions.  But it doesn't hurt and improves
    determinism.

    This function finds consecutive runs of tool-role messages and sorts
    them by tool_call_id.  It's called before build_body to ensure
    deterministic ordering.

    CACHE-CRITICAL: reordering messages inside the prompt-cache PREFIX
    rewrites the cached prefix bytes and forces a full re-cache — the exact
    silent cache-killer this module otherwise hunts. So the sort is gated to
    indices at/after ``get_cache_prefix_count(conv_id)``: a run that begins
    inside the prefix is left untouched (it was already cached in some order;
    re-sorting it now can only HURT). Newly-appended tool results (the tail,
    which is what actually varies round-over-round) are still sorted. Runs
    that straddle the boundary are skipped entirely rather than partially
    sorted, which would itself mutate prefix bytes.

    Args:
        messages: The messages list (mutated in place).
        conv_id:  Conversation ID — used to look up the cache-prefix boundary.
            When empty (no cache tracked), behaves as before (sort everywhere).
    """
    if not messages or len(messages) < 2:
        return

    _prefix_count = 0
    if conv_id:
        try:
            _prefix_count = get_cache_prefix_count(conv_id)
        except Exception as e:
            logger.debug('[CacheTrack] sort_tool_results prefix lookup failed: %s', e)

    i = 0
    n = len(messages)
    while i < n:
        # Find start of a tool-result run
        if messages[i].get('role') == 'tool':
            run_start = i
            while i < n and messages[i].get('role') == 'tool':
                i += 1
            run_end = i
            # Only sort if there are 2+ consecutive tool results AND the whole
            # run lies OUTSIDE the cached prefix (run_start >= prefix_count).
            # A run that begins inside the prefix \u2014 or straddles the boundary
            # \u2014 is skipped: re-ordering already-cached bytes guarantees a miss.
            if run_end - run_start >= 2 and run_start >= _prefix_count:
                # Sort by tool_call_id for deterministic ordering
                tool_run = messages[run_start:run_end]
                tool_run.sort(key=lambda m: m.get('tool_call_id', ''))
                messages[run_start:run_end] = tool_run
        else:
            i += 1


def get_cache_prefix_count(
    conv_id: str, current_msg_count: int | None = None, *, user_id: int,
) -> int:
    """Get the number of messages in the cache prefix for this conversation.

    Microcompact should skip editing messages[0:N] where N is this count,
    to keep cached content byte-identical for automatic prefix caching
    providers (OpenAI, Qwen, etc.).

    Returns the message count from the previous call if cache was active.
    For Anthropic (explicit breakpoints), this is less critical since
    add_cache_breakpoints places markers at the conversation tail.

    CLAMP TO THE CURRENT PREFIX (the history-shrink guard). The boundary
    sources below (in-memory sibling / durable HWM) are MONOTONIC high-water
    marks — they only ever rise. But the conversation history can legitimately
    SHRINK within the same conv_id: an L2/L3 macro-compaction rewrites/truncates
    it, or an edit-and-resend rewinds to an earlier turn. A stale large boundary
    (say 400) against a now-50-message conversation would make
    ``is_in_cache_prefix(idx)=idx<400`` True for EVERY real index → micro_compact
    would be permanently disabled for that conv → unbounded context growth (an
    OOM-context regression, worse than the miss this whole fix targets). So when
    the caller passes ``current_msg_count`` (the live wire message count) the
    returned boundary is CLAMPED to ``current_msg_count - EDITABLE_TAIL_COUNT``:
    the monotonic value acts only as a FLOOR that can never exceed the messages
    that actually exist this round. Freeze holds for the prefix that still
    exists; a legit shrink lets the boundary fall so compaction keeps working.
    When ``current_msg_count`` is None (e.g. sort_tool_results, diagnostics) the
    raw boundary is returned unchanged (back-compat).

    CROSS-THREAD (per-conversation) boundary — the turn-boundary cache-kill
    fix. ``_state_key`` scopes CacheState per ``(conv_id, thread_id)`` so
    concurrent agents under one conv don't clobber each other's baseline. But
    a plain SEQUENTIAL new user turn runs on a NEW ``run_task`` worker thread,
    so the current thread has NO state on that turn's first round → this used
    to return 0 → micro_compact's prefix guard went OFF → it compacted cold
    history the gateway STILL had cached from the previous turn (within TTL) →
    the whole prefix was rewritten → guaranteed miss re-billing ~all of it.
    (Proven: fresh-thread round-1 rewrote 24 prefix msgs / ~72k tokens on a
    real conv; same-thread rounds were byte-stable.) The cached prefix is a
    CONVERSATION fact that outlives any one thread, so when the current
    thread's entry is missing/cold we fall back to the MAX boundary any warm
    sibling-thread entry for the SAME conv holds. Raising the floor is
    cache-SAFE by construction: it only ever PROTECTS more messages from
    compaction (never fewer), so it can never itself cause a miss — it just
    stops the turn-boundary rewrite. Messages are append-only within a conv,
    so the prior turn's boundary is a valid prefix of the current turn.
    """
    def _boundary(st) -> int:
        # Hysteresis (2026-08-01): protect the last-sent prefix whenever this
        # thread HAS a sent prefix and the cache is not VERIFIABLY cold.
        # The old gate (last_cache_read > 1000 or last_cache_write > 1000)
        # collapsed on a SINGLE zero round — but a zero round does not prove
        # the prefix is uncached (Anthropic write-visibility race, gateway
        # stochastic miss, namespace flip, kimi's never-reported cache_write).
        # With the guard down, micro_compact rewrote messages that had just
        # been on the wire → the next round missed → the guard stayed down →
        # a self-feeding re-bill loop (measured on conv ms9ow2tt calls 3→6).
        # Only COLD_STREAK_GUARD_OPEN CONSECUTIVE verifiably-cold rounds open
        # the guard; a single warm-ish round (read or write over the floor)
        # resets the streak in detect_cache_break. Keep the last
        # EDITABLE_TAIL_COUNT messages editable (single-sourced bound).
        if st and st.cold_streak < COLD_STREAK_GUARD_OPEN:
            # (message_count is 0 until this thread's first call completes, so
            # a never-called state yields 0 without a separate call_count gate.)
            return max(0, st.message_count - EDITABLE_TAIL_COUNT)
        return 0

    def _clamp(boundary: int) -> int:
        # History-shrink guard: a monotonic boundary must never exceed the
        # messages that actually exist this round (see docstring). None ⇒
        # no live count available ⇒ return raw (back-compat).
        if current_msg_count is None:
            return boundary
        return min(boundary, max(0, current_msg_count - EDITABLE_TAIL_COUNT))

    with _cache_lock:
        own = _boundary(_cache_states.get(
            _state_key(conv_id, user_id=user_id)))
        if own > 0:
            return _clamp(own)
        # Current thread has no warm state (typically a new user turn on a
        # fresh run_task thread). Fall back to the max boundary any OTHER
        # thread's state for THIS conv still holds — the previous turn's
        # thread whose prefix the gateway is still caching.
        best = 0
        for _key, _st in _cache_states.items():
            if _key[0] != user_id or _key[1] != conv_id:
                continue
            b = _boundary(_st)
            if b > best:
                best = b
    # DURABLE floor. In-memory sibling state does NOT survive a restart or a
    # replica switch, so a new turn after either finds best==0 and the guard
    # would collapse again. The persisted high-water boundary (settings JSON,
    # cross-restart + cross-replica) is the authoritative fallback. Take the
    # MAX: raising the compaction floor only ever PROTECTS more messages, never
    # fewer, so a (possibly slightly stale) larger durable value can never
    # itself cause a miss. Read OUTSIDE the _cache_lock (own DB/TTL lock).
    try:
        from lib.tasks_pkg.cache_tracking._persist import read_persisted_boundary
        persisted = read_persisted_boundary(conv_id, user_id=user_id)
        if persisted > best:
            best = persisted
    except Exception as e:
        logger.debug('[CacheTrack] persisted boundary lookup failed: %s', e)
    return _clamp(best)


def get_cache_diagnostics() -> dict[str, Any]:
    """Return a diagnostic snapshot of all active cache states.

    Useful for admin endpoints, debugging, or periodic health checks.

    Returns:
        Dict with overall stats and per-conversation summaries.
    """
    now = time.time()
    with _cache_lock:
        convs = []
        total_breaks = 0
        total_reads = 0
        total_writes = 0
        for key, state in _cache_states.items():
            cid = key[1]
            age = now - state.last_update_time if state.last_update_time else 0
            convs.append({
                'conv_id': cid[:8],
                'model': state.model,
                'calls': state.call_count,
                'last_cache_read': state.last_cache_read_tokens,
                'last_cache_write': state.last_cache_write_tokens,
                'total_breaks': state.total_breaks,
                'age_s': round(age, 1),
                'compaction_pending': state.compaction_pending,
            })
            total_breaks += state.total_breaks
            total_reads += state.total_cache_read
            total_writes += state.total_cache_write
        return {
            'active_conversations': len(convs),
            'total_breaks': total_breaks,
            'total_cache_read_tokens': total_reads,
            'total_cache_write_tokens': total_writes,
            'ttl_latches_active': len(_ttl_latch),
            'conversations': sorted(
                convs, key=lambda c: c['age_s']),
        }
