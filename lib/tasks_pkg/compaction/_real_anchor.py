"""lib/tasks_pkg/compaction/_real_anchor.py — REAL (provider-measured)
prompt-size anchor for the compaction gate.

WHY
---
The gate's authoritative count walks tiers: usage_cache (exact, but
prefix-signature-validated) → network count APIs → tiktoken → CJK heuristic
floor. When the exact tier cannot validate — a task cold start, or a message
list just REWRITTEN by a compaction (the signature can never match again) —
the estimate tiers take over, and on CJK-heavy content they can disagree with
reality by an order of magnitude. Measured 2026-08-01, conv=mrxinirv0t6n6v
(app.log 20:10:18): gate counted 2,198,193 via tiktoken+heuristic_floor and
force-compacted 5119 messages down to 33, while the REAL prompt one minute
earlier was 215,552 (CacheStats input=215552, hit=100%) — a 10x over-count
that destroyed the context of a conversation sitting at ~22% of its window.

This module supplies the missing REAL yardstick for exactly that window: the
last provider-MEASURED prompt size of the conversation, from sources that do
not depend on the current message list's shape:

  1. **in-memory usage_cache entry** — the per-round recording of the last
     successful call's FULL prompt (post-2026-08-01 it stores the normalized
     total on both wire conventions). ``_lookup`` enforces the TTL. Using the
     raw entry WITHOUT the counter's prefix-signature validation is
     deliberate: after a rewrite the signature never matches, but the
     recorded total is still a real measurement — stale-HIGH at worst (a
     rewrite only ever shrinks the list), which is the SAFE direction for an
     upper-bound clamp.
  2. **durable ``settings.lastTurnCacheRead``** — the previous turn's final
     cache_read (~99% of total input on warm rounds), surviving restarts and
     replica bounces.

The anchor is only ever an UPPER-BOUND clamp (the gate never raises an
estimate toward it): over-triggering destroys context lossily and
irreversibly, while under-triggering is bounded by the next round's fresh
usage recording and the L3 reactive net.

No DB access of its own — both sources are TTL-cached readers.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def real_prompt_anchor(conv_id: str, task: dict | None = None) -> tuple[int, str]:
    """Return the last REAL measured prompt size for ``conv_id``.

    Args:
        conv_id: Conversation id (empty → ``(0, 'none')``).
        task:    Unused today; kept in the signature so callers can pass the
                 gate's task dict without a second call shape.

    Returns:
        ``(tokens, source)`` where source is ``'usage_cache'`` (in-memory,
        freshest), ``'durable:lastTurnCacheRead'`` (restart-resilient), or
        ``'none'`` with tokens 0 when nothing real is known.
    """
    if not conv_id:
        return 0, 'none'

    # 1. In-memory per-round recording (freshest).
    try:
        from lib.token_counter.usage_cache import _lookup
        entry = _lookup(conv_id)
        if entry is not None and entry.prompt_tokens > 0:
            return int(entry.prompt_tokens), 'usage_cache'
    except Exception as e:
        logger.debug('[RealAnchor] usage_cache lookup failed conv=%s: %s',
                     conv_id[:8], e)

    # 2. Durable per-turn final cache read (survives restarts).
    try:
        from lib.tasks_pkg.cache_tracking._persist import (
            read_last_turn_cache_read)
        durable = read_last_turn_cache_read(conv_id)
        if durable > 0:
            return int(durable), 'durable:lastTurnCacheRead'
    except Exception as e:
        logger.debug('[RealAnchor] durable read failed conv=%s: %s',
                     conv_id[:8], e)

    return 0, 'none'


__all__ = ['real_prompt_anchor']
