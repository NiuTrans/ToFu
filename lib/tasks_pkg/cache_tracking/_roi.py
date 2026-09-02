"""L2 ROI instrumentation, session stats, and compaction notification.

Everything here reaches into the shared ``_cache_states`` singleton (imported
from ``._state``) — it mutates per-conversation ``CacheState`` fields
(``compaction_pending`` / ``pending_l2_roi``)
and reads aggregate counters. ``_emit_l2_roi`` is the one function ``._state``
calls back into (lazily) when it flushes an unpaired ROI on eviction.
"""

from __future__ import annotations

import time
from typing import Any

from lib.cost import normalize_usage, split_input_tokens
from lib.log import audit_log, get_logger
from lib.tasks_pkg.cache_tracking._state import (
    _cache_lock,
    _cache_states,
    _state_key,
)

logger = get_logger(__name__)

# Largest documented minimum-cacheable prompt length across the Claude tiers
# (Opus 5 = 512, Sonnet/Opus 4.8 = 1024, Opus 4.7/Haiku 3.5 = 2048,
# Opus 4.5/4.6 + Haiku 4.5 = 4096). Below this NO tier can cache, so a miss
# carries no signal and logging it would be pure noise. At or above it, a miss
# is real money and is always logged.
_MIN_CACHEABLE_PROMPT = 4096


def get_session_cache_stats(
    conv_id: str, *, user_id: int,
) -> dict[str, Any] | None:
    """Get aggregate session-level cache stats for a conversation.

    Returns a dict with cumulative cache read/write tokens, break count,
    overall hit percentage, and session duration. Returns None if no
    state exists for this conversation.

    Use this for end-of-task diagnostics to understand overall cache
    effectiveness across the entire conversation session.
    """
    with _cache_lock:
        state = _cache_states.get(_state_key(conv_id, user_id=user_id))
        if not state or state.call_count == 0:
            return None
        total_input = state.total_input_tokens
        return {
            'calls': state.call_count,
            'total_cache_read': state.total_cache_read,
            'total_cache_write': state.total_cache_write,
            'total_input_tokens': total_input,
            'overall_hit_pct': round(
                state.total_cache_read / max(total_input, 1) * 100),
            'total_breaks': state.total_breaks,
            'session_duration_s': round(
                state.last_update_time - state.first_call_time, 1)
                if state.first_call_time else 0,
            'model': state.model,
        }


def notify_compaction(conv_id: str, *, user_id: int) -> None:
    """Notify that compaction occurred — the next cache_read drop is expected.

    Call this after micro-compaction or force-compaction modifies messages
    so that detect_cache_break doesn't false-positive on the resulting
    cache_read token drop.

    Inspired by Claude Code's notifyCompaction() which resets the baseline.
    """
    if not conv_id:
        return
    with _cache_lock:
        state = _cache_states.get(_state_key(conv_id, user_id=user_id))
        if state:
            state.compaction_pending = True


def _emit_l2_roi(conv_id: str, roi: dict, *, user_id: int,
                 cache_write_rebilled: int | None,
                 cache_read_next: int | None = None,
                 now: float | None = None) -> None:
    """Emit ONE ``l2_cache_roi`` audit metric for a stashed L2 event.

    Two outcomes, BOTH signal (only a silent drop is bias):
      * ``cache_write_rebilled`` is an int → OUTCOME 'paired': we observed the
        following round's re-billed write. ``net_tokens`` = dropped − re-billed.
      * ``cache_write_rebilled`` is None → OUTCOME 'no_following_round': the
        session ended (cleanup / task teardown) OR a second L2 event superseded
        this one before a round paired it. The re-billed half is UNOBSERVED;
        ``net_tokens`` is None so the retune analysis can exclude it from the
        net distribution while still counting the fire. This is what keeps the
        dataset unbiased against late/last-round L2 events.

    Pure emit — the caller owns clearing ``pending_l2_roi``. Best-effort:
    instrumentation must never raise into a cache/cleanup path.
    """
    try:
        _t = now if now is not None else time.time()
        _dropped = int(roi.get('tokens_dropped', 0))
        _read_lost = int(roi.get('cache_read_at_event', 0))
        _observed = cache_write_rebilled is not None
        _net = (_dropped - int(cache_write_rebilled)) if _observed else None
        audit_log(
            'l2_cache_roi',
            user_id=user_id,
            conv_id=conv_id[:12],
            outcome='paired' if _observed else 'no_following_round',
            tokens_dropped=_dropped,
            tokens_before=int(roi.get('tokens_before', 0)),
            tokens_after=int(roi.get('tokens_after', 0)),
            msgs_before=int(roi.get('msgs_before', 0)),
            msgs_after=int(roi.get('msgs_after', 0)),
            cache_read_busted=_read_lost,
            cache_write_rebilled=(int(cache_write_rebilled) if _observed else None),
            cache_read_next=(int(cache_read_next) if cache_read_next is not None else None),
            net_tokens=_net,
            gap_s=round(_t - float(roi.get('event_time', _t)), 2),
        )
        logger.info(
            '[CacheTrack] conv=%s L2 ROI (%s): dropped=%d tokens, '
            're-billed=%s + busted read=%d → net=%s',
            conv_id[:8], 'paired' if _observed else 'no_following_round',
            _dropped,
            (str(int(cache_write_rebilled)) if _observed else 'UNOBSERVED'),
            _read_lost, (str(_net) if _observed else 'n/a'))
    except Exception as _roi_e:
        logger.debug('[CacheTrack] L2 ROI emit failed: %s', _roi_e)


def record_l2_compaction(
    conv_id: str, *, user_id: int, tokens_before: int, tokens_after: int,
    msgs_before: int, msgs_after: int,
) -> None:
    """Record the 'saved' half of ONE L2 (force-summary) compaction event.

    Phase-C instrumentation (measure, don't tune). The ROI of an L2 event has
    two halves separated in time:
      * SAVED   — ``tokens_before - tokens_after`` (prefix tokens the summary
                  dropped) + the ``cache_read`` that was in flight when it fired
                  (the cached prefix the bust discarded); captured HERE.
      * REBILLED — the ``cache_write`` on the FOLLOWING round (the fresh prefix
                  the summary forced to be re-written); completed in
                  ``detect_cache_break`` when that round's usage arrives.

    Stashes the saved half on ``CacheState.pending_l2_roi``; the next
    ``detect_cache_break`` pairs it with the re-billed half and emits ONE
    ``audit_log('l2_cache_roi', ...)`` with BOTH sides populated. No-op when no
    cache state exists yet (cold conv — nothing was cached to bust, so ROI is
    trivially the saved tokens with zero re-bill).
    """
    if not conv_id:
        return
    with _cache_lock:
        state = _cache_states.get(_state_key(conv_id, user_id=user_id))
        if state is None:
            return
        # A second L2 event in the same round-gap would clobber the first's
        # unpaired record → the first event's ROI is silently lost. Flush it
        # first (re-billed half unobserved) so it is counted, not dropped.
        if state.pending_l2_roi is not None:
            _emit_l2_roi(
                conv_id, state.pending_l2_roi, user_id=user_id,
                cache_write_rebilled=None)
            state.pending_l2_roi = None
        state.pending_l2_roi = {
            'tokens_dropped': max(0, int(tokens_before) - int(tokens_after)),
            'tokens_before': int(tokens_before),
            'tokens_after': int(tokens_after),
            'msgs_before': int(msgs_before),
            'msgs_after': int(msgs_after),
            # The cached prefix that was in flight and is now busted by the
            # summary — this read will NOT recur next round.
            'cache_read_at_event': int(state.last_cache_read_tokens),
            'event_time': time.time(),
        }


def log_round_cache_stats(
    conv_id: str,
    round_num: int,
    usage: dict | None,
    model: str,
    tid: str = '',
) -> None:
    """Log per-round cache stats at INFO level for visibility.

    Previously cache stats were only logged at DEBUG in stream_chat.
    This gives us production-visible per-round data for diagnosing
    cache behavior without enabling DEBUG logging.

    Args:
        conv_id: Conversation ID.
        round_num: Current round number (0-based).
        usage: API usage dict from the LLM response.
        model: Model name.
        tid: Task ID for log correlation.
    """
    if not usage:
        return

    _nu = normalize_usage(usage)
    cache_write = _nu['cache_write']
    cache_read = _nu['cache_read']

    # A round with NO cache activity used to return here — which silenced
    #   exactly the most expensive rounds in the system (a full miss on a
    #   200k-token prompt logged nothing at all, while a healthy 99% hit
    #   logged happily). Two 20+ round Opus-5 sessions each burned a whole
    #   context with ZERO per-round evidence because of this gate. Now only
    #   TRIVIALLY SMALL prompts stay quiet: below the largest documented
    #   minimum-cacheable length no Claude tier can cache at all, so their
    #   miss is not actionable signal — everything else is logged, and a
    #   total miss is tagged COLD so it can be grepped directly.
    uncached, total_input = split_input_tokens(usage)
    if not cache_write and not cache_read:
        if total_input < _MIN_CACHEABLE_PROMPT:
            return

    # The hit ratio's denominator is the TOTAL prompt the provider
    #   processed, which is convention-dependent: on the OpenAI-compat wire
    #   prompt_tokens ALREADY INCLUDES the cached tokens, so the old
    #   `prompt_tokens + cache_write + cache_read` counted them TWICE and
    #   halved every reported rate — the reason the live logs cluster on a
    #   physically impossible hit=50% (a 99.998% hit reported as 50%).
    #   split_input_tokens is the SAME helper the cost engine uses, so the
    #   reported hit rate and the price charged for a round can never
    #   disagree about how big the prompt was.
    hit_pct = round(cache_read / max(total_input, 1) * 100)
    _cold = '' if (cache_read or cache_write) else ' COLD'

    # Surface the SERVING KEY + retry count so a clean-round full-floor miss
    # (cache_r=0 with no compaction) can be attributed to per-round dispatch
    # ROUTING — a conv whose soft key-affinity broke and landed on a DIFFERENT
    # gateway key hits a different backend cache pool → guaranteed floor miss,
    # then rebounds when routed back to the warm key (the mrne3bqe R4 pattern:
    # R3 hit 96% on one key, R4 floor-missed, R6 read back the full floor).
    # Without the key on this line that hypothesis can't be confirmed
    # retrospectively. _dispatch is injected by dispatch_stream (see
    # lib/llm_dispatch/api.py); absent on non-dispatched paths.
    _disp = usage.get('_dispatch') if isinstance(usage, dict) else None
    _key = (_disp or {}).get('key', '?')
    _retries = (_disp or {}).get('429_retries', 0)
    # trace_id joins this ground-truth cache_r/key line to the TOFU_CACHE_BYTE_PROBE
    # dump (routing.trace_id) so the analyzer can identify which dumped round was
    # the actual miss (cache_r=0) and read its key_hash + affinity_fell_back —
    # binary-adjudicating affinity namespace-flip (key differs) vs same-key LRU
    # eviction (key same). Set in SSEAccumulator.finalize (usage['trace_id']).
    _trace = usage.get('trace_id', '') if isinstance(usage, dict) else ''

    logger.info(
        '[CacheStats] %s conv=%s R%d model=%s '
        'input=%d cache_w=%d cache_r=%d hit=%d%% key=%s retries=%d trace=%s%s',
        tid[:8] if tid else '???',
        conv_id[:8] if conv_id else '???',
        round_num + 1, model,
        total_input, cache_write, cache_read, hit_pct, _key, _retries,
        _trace or '-', _cold,
    )
