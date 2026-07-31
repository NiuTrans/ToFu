# HOT_PATH — consulted once per LLM round in stream_llm_response.
"""Floor-collapse identical-resend mitigation for the prompt-cache write-
visibility race.

Background
=========
A fraction of rounds report ``cache_read`` pinned at the static system+tools
FLOOR (the whole conversation body re-billed as ``cache_creation``) even though
the request's wire bytes are byte-IDENTICAL to a previously-cached request and
the block geometry is inside Anthropic's ~20-block lookback. Real-gateway
replay of the SAME bytes four times collapses DIFFERENT rounds at 13-40% — i.e.
it is a SERVER-SIDE stochastic cache-write-visibility race (Anthropic SDK
#1451), not a client layout bug. See docs/CACHE_GATEWAY_STOCHASTIC_REPORT.md.

Because the collapse is independent per request, RESENDING the identical
byte-stable body re-rolls the dice and usually hits the now-visible cache write
— the harness proved this drives the effective floor% toward zero (mrsfs9d6
20%->0%). This module is the production wiring of that mitigation.

Discipline (2026-07-23 flip — DEFAULT OFF; owner-authored reversal)
==================================================================
  * ENV-GATED (``TOFU_CACHE_FLOOR_RETRY``), **DEFAULT OFF** as of 2026-07-23.
    Why the flip: the "0.0% floor" acceptance metric was REPORT-KIND
    ("effective floor%") — computed from the ADOPTED resend's usage only. The
    DISCARDED first attempt was NEVER billed into apiRounds / accumulated_usage
    / compute_cost / the wallet, so the gateway charged us twice (once at the
    full 1.25× cache_write rate, once at ~0.11× read+tail-write) while the
    frontend cost popover displayed only the second. The North-Star is COST
    MINIMISATION, not any single reported metric — and per-request the resend
    is a strict expected-cost LOSS (a collapse costs 1.25× base whether or not
    we resend; a resend adds another 0.11×~1.25× on top with no proven
    downstream cache-price reduction for the same turn, since is_floor_collapse
    is a per-request predicate with no cross-round memory).
  * Kept env-controllable so a future controlled A/B (with the honest
    accounting fix landed) can re-evaluate against real billing data. Set
    ``TOFU_CACHE_FLOOR_RETRY=1`` to opt in.
  * Only fires on a BYTE-STABLE floor-collapse (the wire prefix is proven
    identical to the previous round). A resend on a body the client actually
    changed would be a wasted call, so it is refused.
  * CAPPED resends (``TOFU_CACHE_FLOOR_RETRY_MAX``, default 2, hard-cap 3) —
    cost is bounded; 2 covers a collapse whose first resend itself hit a 503.
  * 503/throttle-AWARE: a resend that raises a rate-limit/throttle error stops
    the loop immediately (do not pile retries onto an already-throttled gateway;
    that is what limited the harness mrt1ijef arm to 11.8%).
  * HONEST ACCOUNTING (companion fix, always-on): whenever a resend fires, the
    DISCARDED attempt's usage is preserved onto ``usage['_extra_billing_rounds']``
    (list of {model, usage, tag}) — the LLM-fallback loop reads it and appends
    to api_rounds + accumulated_usage so the cost popover, per-round breakdown
    and wallet debit match what the gateway actually charged. The mitigation
    can no longer hide cost from the reports.

Public API
==========
  * ``floor_retry_enabled()`` — env gate.
  * ``floor_retry_max()`` — capped resend count.
  * ``is_floor_collapse(usage)`` — the read-at-floor + big-write predicate.
  * ``wire_prefix_stable(usage)`` — True when this round's usage carries the
    proof its wire prefix was byte-identical to the previous round (safe to
    resend). Consults the live cache-tracking state, non-destructively.
"""
from __future__ import annotations

from lib.cost import normalize_usage
from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)

# The read band that counts as "pinned at the system+tools floor". The live
# floor is ~28k-74k depending on system/tool size; a healthy warm round reads
# back the whole body (150k-300k). 90k cleanly separates the two populations.
_FLOOR_READ_HI = 90_000
# A floor-collapse re-bills the body as cache_creation; a benign small
# editable-tail write is a few thousand tokens. 20k separates them.
_FLOOR_WRITE_LO = 20_000


def floor_retry_enabled() -> bool:
    """True when the floor-collapse resend mitigation is enabled.

    **DEFAULT OFF (2026-07-23)**: the mitigation drove the *reported* floor%
    to zero by ADOPTING the resend's usage while silently discarding the first
    attempt's (billed) usage — but the gateway charged for BOTH. With honest
    accounting now landed (``_extra_billing_rounds`` propagated into
    ``api_rounds`` / ``accumulated_usage`` / ``compute_cost``), the per-request
    expected cost of a resend is strictly HIGHER than doing nothing. Cost
    minimisation is the North-Star; report-only wins do not justify default-on.
    Set ``TOFU_CACHE_FLOOR_RETRY=1`` to opt in for future controlled A/B tests.
    """
    raw = (getenv_compat('TOFU_CACHE_FLOOR_RETRY', default='0') or '0').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def floor_retry_max() -> int:
    """Max identical resends per collapsing round (default 2, hard-capped 3).

    DEFAULT 2 (2026-07-20): acceptance showed some collapses recover only on
    the SECOND resend because the first resend itself hit a gateway 503 — a
    cap of 1 would let those rounds slip through. The stop-on-throttle guard +
    the hard cap of 3 keep the cost bounded.
    """
    raw = (getenv_compat('TOFU_CACHE_FLOOR_RETRY_MAX', default='2') or '2').strip()
    try:
        n = int(raw)
    except (ValueError, TypeError) as _e:
        logger.debug('floor retry max: unparseable/unexpected type (%s)', _e)
        n = 2
    return max(0, min(3, n))


def _cache_tokens(usage) -> tuple[int, int]:
    """Return (cache_read, cache_write) from a usage dict, tolerant of aliases."""
    u = normalize_usage(usage)
    return u['cache_read'], u['cache_write']


def is_floor_collapse(usage) -> bool:
    """True when this round's cache_read is pinned at the floor with a big
    body re-bill — the symptom a resend can recover."""
    cr, cw = _cache_tokens(usage)
    return cw > _FLOOR_WRITE_LO and cr <= _FLOOR_READ_HI


def wire_prefix_stable(conv_id, usage) -> bool:
    """True when the round's wire prefix is byte-IDENTICAL to the previous
    round — so a resend of the same body is legitimate (not masking a real
    client-side prefix change).

    The proof lives in ``usage['_wire_fp']`` (the post-translation fingerprint
    captured in prepare_request). We compare it against the conversation's
    PREVIOUS stored fingerprint held by the cache-tracking state — WITHOUT
    mutating that state (``detect_cache_break`` is the sole writer, called
    later in the same round). Absent a fingerprint we conservatively return
    False (never resend on unproven-stable bytes).

    ``wire_fp`` (``canonical_messages``) is deliberately LOSSY — it is the SAME
    signal ``detect_cache_break`` uses to EARN the "server-side stochastic"
    label rather than reach it by elimination, which is exactly the collapse
    class a resend recovers. A false "stable" would at worst waste one capped
    resend; a false "unstable" only forgoes the mitigation — both safe.
    """
    u = usage or {}
    cur_fp = u.get('_wire_fp')
    if cur_fp is None:
        return False
    if not conv_id:
        return False
    try:
        from lib.tasks_pkg.cache_tracking import _cache_lock, _cache_states
        from lib.tasks_pkg.cache_tracking._state import _state_key
        key = _state_key(conv_id)
        with _cache_lock:
            prev = _cache_states.get(key)
            prev_fp = getattr(prev, 'wire_fp', None) if prev else None
    except Exception as e:
        logger.debug('[FloorRetry] wire-stable lookup failed: %s', e)
        return False
    if not prev_fp:
        return False
    # Byte-stable when the OVERLAPPING prefix is identical. The current round
    # appended new tail messages, so cur_fp is a superset; compare the shared
    # prefix by position (the same contract diff_canonical uses).
    n = min(len(prev_fp), len(cur_fp))
    if n == 0:
        return False
    return list(prev_fp[:n]) == list(cur_fp[:n])
