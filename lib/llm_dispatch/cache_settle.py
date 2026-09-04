"""lib/llm_dispatch/cache_settle.py — cache write-visibility settle gate.

Why this exists
===============
Anthropic's prompt cache has a documented write-visibility race: a cache entry
"only becomes available after the first response begins" (official prompt-cache
docs), and Anthropic's own SDK reproducer (``anthropics/anthropic-sdk-python``
issue #1451) shows that two back-to-back requests with an IDENTICAL cached
prefix miss on the second ~40% of the time — the second request fires before
the first request's cache WRITE has become visible upstream, so it re-writes
the same prefix (a full ``cache_creation`` bill + 0% read) instead of reading
it back. The reproducer's fix is a single mitigation: ``sleep 2s`` between the
two calls drops the miss rate to 0/20.

Our live floor-miss signature matches this race exactly: byte-identical prefix,
read collapses to the static floor, next round rebounds — concentrated in
FAST tool-loop / autopilot conversations that fire round N+1 within a second or
two of round N's stream ending, i.e. before the write settled. This is the
dominant residual after the client-side byte-freeze fixes (which zeroed the
prefix-mutation miss class); it is not caused by cross-conversation
working-set pressure.

What this does
==============
Per CONVERSATION, remember when its last (big) request's stream ENDED. Before
the next big request on the SAME conversation, if less than a settle window has
elapsed since that stream end, wait out the remainder so the prior round's
cache write is visible before this round tries to read it back.

Design invariants (each one matters — see the tests):
  * Same-conversation only. Different conversations have different prefixes and
    cannot read each other's cache, so cross-conv timing is irrelevant here.
  * Cacheable prefixes only. A miss on a trivial few-k prefix costs almost
    nothing, and tiny turns must never eat added latency. Gated on a LOW
    threshold (default 30k), far below the retired 150k cross-conversation
    admission threshold. The observed ~120-140k floor-miss class sat below that
    old inherited bar, so the earlier shared threshold produced zero hits.
  * Tool-loop-internal latency only. The wait sits between the PRIOR round's
    stream end and THIS round's send — inside the agent's own tool loop, where
    the user is already waiting on tool execution. It never delays the FIRST
    request of a turn (no prior stream end recorded → zero wait), so the human's
    perceived time-to-first-token is unaffected.
  * Adaptive, not fixed. We wait only the REMAINDER of the settle window since
    the prior stream end. A round that already took >window (a long tool exec,
    a slow model turn) waits zero — the write has already settled.
  * Abort-aware. The wait uses ``abortable_sleep`` so a cancelled task breaks
    out immediately instead of blocking for the full window.
  * Env-gated + reversible. ``TOFU_CACHE_SETTLE=0`` disables it entirely;
    ``conv_id`` empty (headless / no identity) → transparent no-op.

Env knobs
---------
``TOFU_CACHE_SETTLE``            — master switch (default on).
``TOFU_CACHE_SETTLE_MS``         — settle window in ms: the minimum gap between
                                   a conv's prior stream END and its next big
                                   send. Default 1500 (the SDK #1451 mitigation
                                   used 2000; 1500 is the shortest window that
                                   reliably clears the race in our traffic while
                                   minimising added tool-loop latency).
``TOFU_CACHE_SETTLE_MAX_MS``     — hard cap on any single wait, so a clock skew
                                   or a bogus timestamp can never stall a
                                   request longer than this. Default 4000.
``TOFU_CACHE_SETTLE_THRESHOLD_TOKENS`` — prefix size above which settle applies.
                                   Default 30000. The observed floor-miss class
                                   (~120-140k) sailed past the retired shared
                                   150k threshold.
``TOFU_CACHE_SETTLE_WARM_WRITE_TOKENS`` — uncached warm-tail size that arms a
                                   generic unmetered auto-cache write (default
                                   4096). Smaller tails reuse the older cached
                                   prefix instead of paying a 1.5s hold.

Codex subscription overrides (only when ``cache_profile='codex'``):
``TOFU_CACHE_SETTLE_CODEX_VISIBILITY_MS`` — inferred unmetered-write visibility
                                   window (default 5000, live-tested on Luna).
``TOFU_CACHE_SETTLE_CODEX_SEND_INTERVAL_MS`` — minimum start-to-start spacing
                                   for one prompt_cache_key (default 4200,
                                   keeping fast loops below ~15 requests/min).
``TOFU_CACHE_SETTLE_CODEX_MAX_MS`` — hard cap on one Codex hold (default 6000).
``TOFU_CACHE_SETTLE_CODEX_THRESHOLD_TOKENS`` — Codex cacheability threshold
                                   (default 1024, matching automatic caching).
``TOFU_CACHE_SETTLE_CODEX_WARM_WRITE_TOKENS`` — uncached warm-tail size that
                                   arms the visibility hold (default 8192).
                                   Cold cacheable requests always arm it;
                                   smaller warm tails rely on the send interval
                                   and remain bounded by this threshold.
"""

from __future__ import annotations

import os
import threading
import time

from lib.cost import normalize_usage, split_input_tokens
from lib.log import get_logger
from lib.token_counter.evidence import (
    ADMITTED_INPUT_TOKENS_KEY,
    validated_admitted_input_tokens,
)

logger = get_logger(__name__)

__all__ = [
    'settle_enabled',
    'settle_window_ms',
    'settle_max_wait_ms',
    'settle_cold_enabled',
    'settle_cold_window_ms',
    'settle_cold_max_wait_ms',
    'settle_threshold_tokens',
    'estimate_prefix_tokens',
    'settle_before_send',
    'async_settle_before_send',
    'record_stream_end',
    'is_cold_write',
    'generic_cache_write_pending',
    'codex_cache_write_pending',
    'observe_codex_cache',
    '_reset_settle_for_tests',
]


def settle_enabled() -> bool:
    """Whether the cache write-visibility settle gate is active (default on)."""
    val = os.environ.get('TOFU_CACHE_SETTLE', '1')
    return val.strip().lower() not in ('0', 'false', 'no', 'off', '')


def settle_window_ms() -> float:
    """Minimum ms between a conv's prior stream END and its next big send.

    Default 1500. Anthropic's SDK #1451 reproducer cleared the race with a 2000
    ms sleep; 1500 is the shortest window that reliably clears it in our traffic
    while keeping added tool-loop latency minimal."""
    try:
        v = float(os.environ.get('TOFU_CACHE_SETTLE_MS', '1500'))
        return v if v > 0 else 1500.0
    except (ValueError, TypeError) as e:
        logger.debug('[CacheSettle] TOFU_CACHE_SETTLE_MS parse failed, default: %s', e)
        return 1500.0


def settle_max_wait_ms() -> float:
    """Hard ceiling (ms) on any single settle wait. Default 4000.

    Bounds the worst case so a clock skew, a paused/resumed process, or a bogus
    stored timestamp can never stall a request longer than this."""
    try:
        v = float(os.environ.get('TOFU_CACHE_SETTLE_MAX_MS', '4000'))
        return v if v > 0 else 4000.0
    except (ValueError, TypeError) as e:
        logger.debug('[CacheSettle] TOFU_CACHE_SETTLE_MAX_MS parse failed, default: %s', e)
        return 4000.0


def settle_cold_enabled() -> bool:
    """Whether the LONG cold-write blocking window is active. DEFAULT OFF.

    Deliberately opt-in. The cold-write case (large write, ~0 read) is the
    typical shape of a conversation's FIRST round, so a long blocking window
    would add up to ~cold_window of wall-clock latency to the SECOND round of
    almost every conversation — punishing the main path. Measured live: a cold
    tool loop wastes only ~14k rewrite tokens (a fraction of a cent at Opus
    rates) to skip the block, versus ~10s of user wait to enforce it. For a
    cost-indifferent, experience-sensitive deployment that trade is BACKWARDS,
    so the long block is OFF by default: cold writes keep the ordinary short
    window (accept the cheap re-write, never stall the user). The detector
    still names the miss ``cache_write_unsettled`` (non-blocking, honest
    attribution) regardless. Set ``TOFU_CACHE_SETTLE_COLD=1`` to enable the
    blocking cold window where saving the re-write tokens is worth the latency."""
    val = os.environ.get('TOFU_CACHE_SETTLE_COLD', '0')
    return val.strip().lower() in ('1', 'true', 'yes', 'on')


def settle_cold_window_ms() -> float:
    """Minimum ms between a conv's prior COLD-WRITE stream END and its next big
    send. Default 18000. ONLY consulted when settle_cold_enabled() is True.

    A freshly-WRITTEN Anthropic cache entry is not readable for ~15–20s (the
    anthropic-sdk-python #1451 write-visibility race — reproduced live: a cold
    prefix re-sent every 4s missed at t=3s/t=10s and only HIT at t≈16s; a
    settle sweep missed at ≤14s and hit at ≥17s). The ordinary 1.5s window
    (settle_window_ms) is ~10x too short to bridge THAT gap, so after a COLD
    write the next same-conv send must wait this longer window instead. A WARM
    round keeps the short window (see _compute_wait_s) so tool-loop throughput
    is not crippled."""
    try:
        v = float(os.environ.get('TOFU_CACHE_SETTLE_COLD_MS', '18000'))
        return v if v > 0 else 18000.0
    except (ValueError, TypeError) as e:
        logger.debug('[CacheSettle] TOFU_CACHE_SETTLE_COLD_MS parse failed, default: %s', e)
        return 18000.0


def settle_cold_max_wait_ms() -> float:
    """Hard ceiling (ms) on a single COLD-write settle wait. Default 20000.

    Same clock-skew guard as settle_max_wait_ms, but sized for the longer cold
    window. A wait is NEVER longer than this even with a bogus timestamp."""
    try:
        v = float(os.environ.get('TOFU_CACHE_SETTLE_COLD_MAX_MS', '20000'))
        return v if v > 0 else 20000.0
    except (ValueError, TypeError) as e:
        logger.debug('[CacheSettle] TOFU_CACHE_SETTLE_COLD_MAX_MS parse failed, default: %s', e)
        return 20000.0


_DEFAULT_SETTLE_THRESHOLD_TOKENS = 30_000


def estimate_prefix_tokens(body_or_messages) -> int:
    """Estimate input size for the cache-settle threshold without raising.

    A root round's validated full-prompt admission count is authoritative for
    same-model retries. Other callers retain the historical chars/4 estimate,
    including tool schemas and base64 payloads, because this decision only
    separates cacheable prefixes from small requests.
    """
    try:
        if isinstance(body_or_messages, dict):
            admitted_input_tokens = validated_admitted_input_tokens(
                body_or_messages.get(ADMITTED_INPUT_TOKENS_KEY))
            if admitted_input_tokens is not None:
                return admitted_input_tokens
            messages = body_or_messages.get('messages') or []
            tools = body_or_messages.get('tools') or []
        elif isinstance(body_or_messages, list):
            messages = body_or_messages
            tools = []
        else:
            return 0
        characters = 0
        for message in messages:
            content = (
                message.get('content') if isinstance(message, dict) else None)
            if isinstance(content, str):
                characters += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = (
                            block.get('text') or block.get('thinking') or '')
                        if isinstance(text, str):
                            characters += len(text)
                        source = block.get('source')
                        if isinstance(source, dict):
                            data = source.get('data')
                            if isinstance(data, str):
                                characters += len(data)
                    elif isinstance(block, str):
                        characters += len(block)
        if tools:
            try:
                import json

                characters += len(json.dumps(tools, ensure_ascii=False))
            except (TypeError, ValueError):
                pass
        return characters // 4
    except Exception as error:
        logger.debug('[CacheSettle] estimate_prefix_tokens failed: %s', error)
        return 0


def settle_threshold_tokens() -> int:
    """Prefix-size (est. tokens) above which settle applies. Default 30000.

    Settle waits only the remainder of a short window since this conversation's
    own prior stream end, inside the tool loop. The payoff is avoiding a
    full-body cache rewrite on a byte-identical prefix, so it fires on any
    prefix large enough that a rewrite costs something.

    The observed floor-miss class is ~120-140k-token turns — which sailed
    straight past the retired 150k shared bar. 30k gives generous headroom
    below that while still excluding trivial few-k turns where a miss is nearly
    free. Tune with
    ``TOFU_CACHE_SETTLE_THRESHOLD_TOKENS``."""
    raw = os.environ.get('TOFU_CACHE_SETTLE_THRESHOLD_TOKENS')
    if raw is not None:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (ValueError, TypeError) as e:
            logger.debug('[CacheSettle] TOFU_CACHE_SETTLE_THRESHOLD_TOKENS parse '
                         'failed, using default %d: %s',
                         _DEFAULT_SETTLE_THRESHOLD_TOKENS, e)
    return _DEFAULT_SETTLE_THRESHOLD_TOKENS


# ── Process-global recency map: conv_id → (last stream-END timestamp, cold) ──
# One entry per conversation, recording when its most recent big request's
# stream finished AND whether that round was a COLD WRITE (large cache_write,
# ~0 read — the entry that actually needs ~15–20s to become visible). A warm
# round keeps the short settle window. Thread-safe, TTL-pruned, size-capped.
_last_end: dict[str, tuple[float, bool]] = {}
_lock = threading.Lock()

# Entries older than this (seconds) are useless — well past any settle window,
# and past the cache TTL too — so they are pruned lazily on write.
_ENTRY_TTL_S = 3600.0
_MAX_ENTRIES = 4096

# ChatGPT's Codex Responses endpoint doesn't expose cache-write tokens.  Keep
# its send clock and inferred write state separately from Anthropic's metered
# ``_last_end`` tuple so the mature Anthropic path remains byte-for-byte
# unchanged.  Values are deliberately plain dicts: this module is hot-loaded
# during deploys and a schema-less in-process cache is simpler to migrate.
#
#   conv_id -> {last_send, last_end, pending_write}
_codex_timing: dict[str, dict[str, float | bool]] = {}

# Per-conversation cache-read history used only for honest GPT-5.6 diagnostics.
# It proves append-only wire growth before naming an implicit-breakpoint
# fallback; it never affects request contents or billing decisions. Retain only
# the prior message count + process-local digest, never the conversation-sized
# ``_wire_bytes`` list. The next observation hashes its prior-length prefix to
# prove append-only growth, keeping each entry O(1) in conversation length.
_codex_health: dict[str, dict] = {}

_CODEX_AUTO_CACHE_MIN_TOKENS = 1024
_DEFAULT_GENERIC_WARM_WRITE_TOKENS = 4096
_DEFAULT_CODEX_WARM_WRITE_TOKENS = 8192


def _prune_locked(now: float) -> None:
    """Drop stale entries; if still over the cap, drop the oldest. Caller holds lock."""
    stale = [cid for cid, (ts, _c) in _last_end.items() if now - ts > _ENTRY_TTL_S]
    for cid in stale:
        del _last_end[cid]
    if len(_last_end) > _MAX_ENTRIES:
        ordered = sorted(_last_end.items(), key=lambda kv: kv[1][0])
        for cid, _ in ordered[:len(_last_end) - _MAX_ENTRIES]:
            del _last_end[cid]
    for table in (_codex_timing, _codex_health):
        stale = [cid for cid, value in table.items()
                 if now - float(value.get('last_end') or
                                value.get('updated') or 0.0) > _ENTRY_TTL_S]
        for cid in stale:
            del table[cid]
        if len(table) > _MAX_ENTRIES:
            ordered = sorted(
                table.items(),
                key=lambda kv: float(kv[1].get('last_end') or
                                     kv[1].get('updated') or 0.0))
            for cid, _ in ordered[:len(table) - _MAX_ENTRIES]:
                del table[cid]


_COLD_WRITE_MIN_TOKENS = 20_000


def is_cold_write(usage) -> bool:
    """Whether a finished round was a COLD cache WRITE — a freshly-created cache
    entry (large ``cache_write``, negligible ``cache_read``) that needs ~15–20s
    to become visible upstream before the next same-conv round can read it back.

    Single source of the cold-write signal, shared by both dispatch record
    sites. A round that mostly READ its prefix (warm) returns False, so the next
    send keeps the short settle window. Gated on a non-trivial write so a tiny
    prefix (whose miss is nearly free) never arms the long cold hold."""
    if not isinstance(usage, dict):
        return False
    _u = normalize_usage(usage)
    cw, cr = _u['cache_write'], _u['cache_read']
    if cw < _COLD_WRITE_MIN_TOKENS:
        return False
    # Cold = the write dominated; a warm round reads back most of its prefix.
    return cr < cw


def _generic_warm_write_tokens() -> int:
    """Warm unmetered tail that warrants the generic 1.5s hold (4,096)."""
    try:
        value = int(os.environ.get(
            'TOFU_CACHE_SETTLE_WARM_WRITE_TOKENS',
            str(_DEFAULT_GENERIC_WARM_WRITE_TOKENS)))
        if value > 0:
            return value
    except (TypeError, ValueError) as exc:
        logger.debug('[CacheSettle] generic warm-write threshold parse failed: %s',
                     exc)
    return _DEFAULT_GENERIC_WARM_WRITE_TOKENS


def generic_cache_write_pending(usage) -> bool:
    """Whether a non-Codex round needs the generic visibility hold.

    Metered Anthropic writes are authoritative: a positive cache-creation
    count arms the hold and an explicit zero proves that no new entry needs to
    settle. OpenAI-compatible auto-cache providers such as Kimi do not meter
    writes, so a cold/unreported round remains conservative while a warm round
    only arms after its uncached suffix reaches 4,096 tokens. A smaller suffix
    can reuse the already-visible older prefix; skipping the hold can therefore
    add at most the bounded suffix to one request instead of unconditionally
    adding up to 1.5 seconds to every fast tool-loop round.

    Missing or malformed usage remains conservative so a provider telemetry
    regression cannot silently disable the correctness gate.
    """
    if not isinstance(usage, dict):
        return True
    normalized = normalize_usage(usage)
    if max(0, int(normalized['cache_write'])) > 0:
        return True
    if 'cache_creation_input_tokens' in usage:
        raw_creation = usage.get('cache_creation_input_tokens')
        if isinstance(raw_creation, bool) or raw_creation is None:
            return True
        try:
            metered_creation = int(raw_creation)
        except (TypeError, ValueError):
            return True
        if metered_creation < 0:
            return True
        return metered_creation > 0
    uncached_input, total_input = split_input_tokens(usage)
    if total_input <= 0 or max(0, int(normalized['cache_read'])) <= 0:
        return True
    return max(0, int(uncached_input)) >= _generic_warm_write_tokens()


def codex_cache_write_pending(usage) -> bool:
    """Whether a Codex round should arm the costly write-visibility hold.

    The ChatGPT subscription wire reports ``cache_write_tokens=0`` even when a
    later request proves that a write happened.  A cold cacheable request always
    arms the hold.  A warm request arms it only after its uncached suffix reaches
    the configured material-tail threshold.  Smaller warm suffixes can reuse the
    older prefix and process a bounded tail faster than paying a five-second
    visibility hold after every tool-loop round; continued growth eventually
    reaches the threshold and restores the hold.
    """
    if not isinstance(usage, dict):
        return False
    normalized = normalize_usage(usage)
    try:
        input_tokens = int(usage.get('prompt_tokens') or
                           usage.get('input_tokens') or 0)
    except (TypeError, ValueError) as exc:
        logger.debug('[CacheSettle] invalid input-token count: %s', exc)
        return False
    if input_tokens < _CODEX_AUTO_CACHE_MIN_TOKENS:
        return False
    cache_read = max(0, int(normalized['cache_read']))
    if cache_read == 0:
        return True
    uncached_tail = max(0, input_tokens - cache_read)
    return uncached_tail >= _codex_warm_write_tokens()


def _codex_warm_write_tokens() -> int:
    """Warm uncached-tail size that warrants a visibility hold (8,192)."""
    try:
        value = int(os.environ.get(
            'TOFU_CACHE_SETTLE_CODEX_WARM_WRITE_TOKENS',
            str(_DEFAULT_CODEX_WARM_WRITE_TOKENS)))
        if value >= _CODEX_AUTO_CACHE_MIN_TOKENS:
            return value
    except (TypeError, ValueError) as exc:
        logger.debug('[CacheSettle] Codex warm-write threshold parse failed: %s',
                     exc)
    return _DEFAULT_CODEX_WARM_WRITE_TOKENS


def _codex_visibility_window_ms() -> float:
    """Post-response visibility window for inferred Codex writes (5s)."""
    try:
        value = float(os.environ.get(
            'TOFU_CACHE_SETTLE_CODEX_VISIBILITY_MS', '5000'))
        return value if value > 0 else 5000.0
    except (TypeError, ValueError) as exc:
        logger.debug('[CacheSettle] Codex visibility window parse failed: %s',
                     exc)
        return 5000.0


def _codex_send_interval_ms() -> float:
    """Minimum start-to-start interval per prompt_cache_key (4.2s).

    OpenAI documents reduced cache effectiveness above roughly 15 requests per
    minute for one prompt-cache key.  4.2 seconds keeps a fast tool loop below
    that boundary with a little scheduling headroom.
    """
    try:
        value = float(os.environ.get(
            'TOFU_CACHE_SETTLE_CODEX_SEND_INTERVAL_MS', '4200'))
        return value if value > 0 else 4200.0
    except (TypeError, ValueError) as exc:
        logger.debug('[CacheSettle] Codex send interval parse failed: %s', exc)
        return 4200.0


def _codex_max_wait_ms() -> float:
    try:
        value = float(os.environ.get(
            'TOFU_CACHE_SETTLE_CODEX_MAX_MS', '6000'))
        return value if value > 0 else 6000.0
    except (TypeError, ValueError) as exc:
        logger.debug('[CacheSettle] Codex max wait parse failed: %s', exc)
        return 6000.0


def _codex_threshold_tokens() -> int:
    try:
        value = int(os.environ.get(
            'TOFU_CACHE_SETTLE_CODEX_THRESHOLD_TOKENS',
            str(_CODEX_AUTO_CACHE_MIN_TOKENS)))
        return value if value > 0 else _CODEX_AUTO_CACHE_MIN_TOKENS
    except (TypeError, ValueError) as exc:
        logger.debug('[CacheSettle] Codex threshold parse failed: %s', exc)
        return _CODEX_AUTO_CACHE_MIN_TOKENS


def record_stream_end(conv_id: str, *, now: float | None = None,
                      cold_write: bool = False, cache_profile: str = '',
                      pending_write: bool | None = None) -> None:
    """Record that ``conv_id``'s current request stream just ENDED.

    Called after a successful (or terminal) stream so the NEXT request on the
    same conversation can measure the gap and settle if it arrives too soon.
    No-op when the gate is disabled or ``conv_id`` is empty.

    ``cold_write`` marks the finishing round as a COLD cache WRITE (large
    cache_write, ~0 read — a freshly-created entry that needs ~15–20s to become
    visible upstream). When True the NEXT same-conv send waits the LONG cold
    window (settle_cold_window_ms); a warm round keeps the short window. Default
    False keeps existing callers on the short window (back-compat).

    ``pending_write`` lets the successful stream's usage decide whether there
    is actually a new entry to settle. ``False`` clears the generic clock;
    ``None`` preserves conservative compatibility for older/direct callers.
    For the Codex subscription profile, the value is already filtered through
    :func:`codex_cache_write_pending`: every cold cacheable request and only a
    material warm tail arm the five-second visibility window. The independent
    per-key send interval remains active either way."""
    if not conv_id or not settle_enabled():
        return
    ts = now if now is not None else time.time()
    with _lock:
        if pending_write is False:
            _last_end.pop(conv_id, None)
        else:
            _last_end[conv_id] = (ts, bool(cold_write))
        if cache_profile == 'codex':
            state = _codex_timing.setdefault(conv_id, {})
            state['last_end'] = ts
            state['pending_write'] = bool(pending_write)
        if len(_last_end) > _MAX_ENTRIES:
            _prune_locked(ts)


def _codex_compute_wait_s(conv_id: str, est_tokens: int,
                          now: float) -> tuple[float, str]:
    if est_tokens < _codex_threshold_tokens():
        return 0.0, ''
    with _lock:
        state = dict(_codex_timing.get(conv_id) or {})
    if not state:
        return 0.0, ''

    waits: list[tuple[float, str]] = []
    last_send = float(state.get('last_send') or 0.0)
    if last_send > 0:
        waits.append((max(0.0, _codex_send_interval_ms() / 1000.0
                          - max(0.0, now - last_send)),
                      'prompt_cache_key rate'))
    last_end = float(state.get('last_end') or 0.0)
    if bool(state.get('pending_write')) and last_end > 0:
        waits.append((max(0.0, _codex_visibility_window_ms() / 1000.0
                          - max(0.0, now - last_end)),
                      'unmetered cache write visibility'))
    wait_s, reason = max(waits, default=(0.0, ''), key=lambda item: item[0])
    return min(wait_s, _codex_max_wait_ms() / 1000.0), reason


def _record_codex_send(conv_id: str, sent_at: float) -> None:
    with _lock:
        state = _codex_timing.setdefault(conv_id, {})
        state['last_send'] = sent_at
        if len(_codex_timing) > _MAX_ENTRIES:
            _prune_locked(sent_at)


def _compute_wait_s(conv_id: str, est_tokens: int, now: float | None) -> tuple[float, float, float]:
    """Pure: return ``(wait_s, elapsed, window_s)`` for a prospective send.

    ``wait_s`` is 0.0 when no wait is warranted (gate off, empty conv,
    sub-threshold, no prior stream end, or window already elapsed). Shared by
    the sync and async entry points so the timing rules live in ONE place."""
    if not settle_enabled() or not conv_id:
        return 0.0, 0.0, 0.0
    if est_tokens < settle_threshold_tokens():
        return 0.0, 0.0, 0.0

    now = now if now is not None else time.time()
    with _lock:
        last = _last_end.get(conv_id)
    if last is None:
        # First big request of this conversation (this process) → nothing to
        # settle behind. Never delay the opening request of a turn.
        return 0.0, 0.0, 0.0

    last_ts, last_cold = last
    # A COLD write needs the long visibility window, but the long BLOCKING wait
    # is opt-in (settle_cold_enabled, default OFF) because it would stall the
    # second round of nearly every conversation to save a sub-cent re-write.
    # When the cold block is disabled, a cold prior round falls back to the
    # ordinary short window — we accept the cheap re-write instead of blocking
    # the user, and the detector still names it cache_write_unsettled.
    if last_cold and settle_cold_enabled():
        window_s = settle_cold_window_ms() / 1000.0
        cap_s = settle_cold_max_wait_ms() / 1000.0
    else:
        window_s = settle_window_ms() / 1000.0
        cap_s = settle_max_wait_ms() / 1000.0
    elapsed = now - last_ts
    # Guard against a clock going backwards (elapsed < 0) → treat as 0 elapsed.
    if elapsed < 0:
        elapsed = 0.0
    remaining = window_s - elapsed
    if remaining <= 0:
        # The prior write has already had the full window to settle.
        return 0.0, elapsed, window_s
    wait_s = min(remaining, cap_s)
    return wait_s, elapsed, window_s


def _log_hold(wait_s: float, est_tokens: int, conv_id: str, elapsed: float,
              window_s: float, log_prefix: str) -> None:
    logger.info('%s [CacheSettle] holding %.2fs before big prefix (~%dk tok) '
                'conv=%s so prior round cache write settles (%.2fs since prior '
                'stream end, window %.2fs)', log_prefix, wait_s,
                est_tokens // 1000, conv_id[:8], elapsed, window_s)


def settle_before_send(conv_id: str, est_tokens: int, *,
                       abort_check=None, log_prefix: str = '',
                       now: float | None = None,
                       cache_profile: str = '') -> float:
    """Wait so the prior same-conv round's cache write is visible before send.

    Returns the number of seconds actually waited (0.0 when no wait was needed
    or the gate was inactive) — the caller may log/aggregate it.

    No-op (returns 0.0) when: the gate is disabled, ``conv_id`` is empty, the
    prefix is below :func:`settle_threshold_tokens`, no prior stream end is
    recorded for this conv (the FIRST request of a turn — never delayed), or
    enough time has already elapsed since that stream end.

    The wait is the REMAINDER of the settle window since the prior stream end,
    hard-capped by :func:`settle_max_wait_ms`, and is abort-aware."""
    started_at = now if now is not None else time.time()
    if (cache_profile == 'codex' and settle_enabled() and conv_id
            and est_tokens >= _codex_threshold_tokens()):
        wait_s, reason = _codex_compute_wait_s(
            conv_id, est_tokens, started_at)
        if wait_s > 0:
            logger.info('%s [CacheSettle] holding %.2fs for Codex conv=%s '
                        '(~%dk tok; %s)', log_prefix, wait_s, conv_id[:8],
                        est_tokens // 1000, reason)
            try:
                from lib.llm._transport import abortable_sleep
                abortable_sleep(wait_s, abort_check)
            except ImportError as exc:
                logger.debug('[CacheSettle] abortable_sleep unavailable, '
                             'plain sleep: %s', exc)
                time.sleep(wait_s)
        _record_codex_send(conv_id, started_at + wait_s)
        return wait_s

    wait_s, elapsed, window_s = _compute_wait_s(
        conv_id, est_tokens, started_at)
    if wait_s <= 0:
        return 0.0
    _log_hold(wait_s, est_tokens, conv_id, elapsed, window_s, log_prefix)
    try:
        from lib.llm._transport import abortable_sleep
        abortable_sleep(wait_s, abort_check)
    except ImportError as e:
        logger.debug('[CacheSettle] abortable_sleep unavailable, plain sleep: %s', e)
        time.sleep(wait_s)
    return wait_s


async def async_settle_before_send(conv_id: str, est_tokens: int, *,
                                   abort_check=None, log_prefix: str = '',
                                   now: float | None = None,
                                   cache_profile: str = '') -> float:
    """Async counterpart of :func:`settle_before_send` for the async dispatch
    path. Same timing rules (shared :func:`_compute_wait_s`); uses
    ``async_abortable_sleep`` so it never blocks the event loop."""
    started_at = now if now is not None else time.time()
    if (cache_profile == 'codex' and settle_enabled() and conv_id
            and est_tokens >= _codex_threshold_tokens()):
        wait_s, reason = _codex_compute_wait_s(
            conv_id, est_tokens, started_at)
        if wait_s > 0:
            logger.info('%s [CacheSettle] holding %.2fs for Codex conv=%s '
                        '(~%dk tok; %s)', log_prefix, wait_s, conv_id[:8],
                        est_tokens // 1000, reason)
            try:
                from lib.llm._transport import async_abortable_sleep
                await async_abortable_sleep(wait_s, abort_check)
            except ImportError as exc:
                logger.debug('[CacheSettle] async_abortable_sleep unavailable: '
                             '%s', exc)
                import asyncio
                await asyncio.sleep(wait_s)
        _record_codex_send(conv_id, started_at + wait_s)
        return wait_s

    wait_s, elapsed, window_s = _compute_wait_s(
        conv_id, est_tokens, started_at)
    if wait_s <= 0:
        return 0.0
    _log_hold(wait_s, est_tokens, conv_id, elapsed, window_s, log_prefix)
    try:
        from lib.llm._transport import async_abortable_sleep
        await async_abortable_sleep(wait_s, abort_check)
    except ImportError as e:
        logger.debug('[CacheSettle] async_abortable_sleep unavailable: %s', e)
        import asyncio
        await asyncio.sleep(wait_s)
    return wait_s


def _wire_hash_values(wire_bytes) -> tuple | None:
    """Project rich per-message evidence to one immutable hash sequence."""
    if not isinstance(wire_bytes, list) or not wire_bytes:
        return None
    values = []
    for entry in wire_bytes:
        if not isinstance(entry, dict) or 'h' not in entry:
            return None
        value = entry.get('h')
        try:
            hash(value)
        except TypeError:
            return None
        values.append(value)
    return tuple(values)


def _stored_wire_summary(previous: dict) -> tuple[int, int] | None:
    """Read the compact summary, migrating one legacy rich entry if needed."""
    count = previous.get('wire_count')
    digest = previous.get('wire_digest')
    if (type(count) is int and count > 0
            and type(digest) is int):
        return count, digest

    legacy_values = _wire_hash_values(previous.get('wire_bytes'))
    if legacy_values is None:
        return None
    return len(legacy_values), hash(legacy_values)


def _wire_is_append_only(
    previous: dict,
    usage: dict,
    *,
    current_wire_hashes: tuple | None = None,
) -> bool:
    old_summary = _stored_wire_summary(previous)
    new_hashes = (
        current_wire_hashes
        if current_wire_hashes is not None
        else _wire_hash_values(usage.get('_wire_bytes'))
    )
    if old_summary is None or new_hashes is None:
        return False
    old_count, old_digest = old_summary
    if (len(new_hashes) < old_count
            or hash(new_hashes[:old_count]) != old_digest):
        return False
    old_region = previous.get('wire_region')
    new_region = usage.get('_wire_region')
    if old_region is not None and new_region is not None \
            and old_region != new_region:
        return False
    old_routing = previous.get('wire_routing')
    new_routing = usage.get('_wire_routing')
    return not (old_routing is not None and new_routing is not None
                and old_routing != new_routing)


def observe_codex_cache(conv_id: str, usage) -> dict:
    """Classify one Codex cache result with append-only wire evidence.

    GPT-5.6's subscription endpoint may fall back from a newer implicit cache
    breakpoint to an older 1,024-token boundary even though the request only
    appended items.  Generic cache-break detection intentionally ignores a
    1,024-token fluctuation, so it previously vanished from telemetry.  This
    observer names that state without claiming the client invalidated bytes.
    """
    if not conv_id or not isinstance(usage, dict):
        return {}
    normalized = normalize_usage(usage)
    cache_read = max(0, int(normalized['cache_read']))
    try:
        input_tokens = int(usage.get('prompt_tokens') or
                           usage.get('input_tokens') or 0)
    except (TypeError, ValueError) as exc:
        logger.debug('[CacheSettle] invalid observed input-token count: %s', exc)
        input_tokens = 0
    now = time.time()
    current_wire_hashes = _wire_hash_values(usage.get('_wire_bytes'))
    with _lock:
        previous = dict(_codex_health.get(conv_id) or {})
        call = int(previous.get('call') or 0) + 1
        append_only = (
            _wire_is_append_only(
                previous, usage, current_wire_hashes=current_wire_hashes)
            if previous else False
        )
        previous_read = int(previous.get('cache_read') or 0)
        max_read = max(int(previous.get('max_read') or 0), cache_read)

        if input_tokens < _CODEX_AUTO_CACHE_MIN_TOKENS:
            status = 'not_cacheable'
        elif call == 1 and cache_read == 0:
            status = 'cold'
        elif cache_read == 0 and previous_read == 0:
            status = 'write_visibility_miss'
        elif cache_read == 0 and previous_read > 0 and append_only:
            status = 'upstream_cache_miss'
        elif (0 < cache_read < previous_read and append_only):
            status = 'implicit_breakpoint_fallback'
        elif cache_read > previous_read:
            status = 'prefix_extended'
        elif cache_read > 0:
            status = 'warm'
        else:
            status = 'miss_after_wire_change'

        result = {
            'status': status,
            'call': call,
            'input_tokens': input_tokens,
            'cached_tokens': cache_read,
            'previous_cached_tokens': previous_read,
            'max_cached_tokens': max_read,
            'drop_tokens': max(0, previous_read - cache_read),
            'wire_append_only': append_only,
        }
        _codex_health[conv_id] = {
            'call': call,
            'cache_read': cache_read,
            'max_read': max_read,
            'wire_count': (
                len(current_wire_hashes)
                if current_wire_hashes is not None else 0),
            'wire_digest': (
                hash(current_wire_hashes)
                if current_wire_hashes is not None else None),
            'wire_region': usage.get('_wire_region'),
            'wire_routing': usage.get('_wire_routing'),
            'updated': now,
        }
        if len(_codex_health) > _MAX_ENTRIES:
            _prune_locked(now)

    usage['_codex_cache'] = result
    if status in ('implicit_breakpoint_fallback', 'upstream_cache_miss'):
        logger.warning(
            '[CodexCache] conv=%s call=%d status=%s cached=%d->%d '
            'drop=%d input=%d append_only=%s. The client wire and routing '
            'prefix are unchanged; this is an upstream implicit-breakpoint '
            'fallback/miss, not a Tofu prefix rewrite.',
            conv_id[:8], call, status, previous_read, cache_read,
            result['drop_tokens'], input_tokens, append_only)
    elif status == 'write_visibility_miss':
        logger.info('[CodexCache] conv=%s call=%d second cold read; unmetered '
                    'write was not visible yet (input=%d)',
                    conv_id[:8], call, input_tokens)
    return result


def _reset_settle_for_tests() -> None:
    """Test hook: clear the recency map."""
    with _lock:
        _last_end.clear()
        _codex_timing.clear()
        _codex_health.clear()
