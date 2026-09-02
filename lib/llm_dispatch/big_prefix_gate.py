"""lib/llm_dispatch/big_prefix_gate.py — Per-key big-prefix admission control.

Why this exists
===============
⚠️ CORRECTION (2026-07-18): the "per-key finite cache working set → concurrent
big prefixes LRU-evict each other" premise this gate was built on is **FALSE**
and was debunked by owner infra facts + the project's own A/B evidence:
  * The upstream **gateway only FORWARDS requests — it does not evict caches.**
  * The **AWS/Bedrock backing has NO per-key cache capacity limit** — there is
    no bounded "shared pool" for concurrent conversations to contend over.
  * A/B tested (see memory ``anthropic-cache-key-mechanics``): different
    conversations do NOT evict each other (±0.0% per-round cache_read).
  * The 2026-07-18 miss-rate vs concurrency curve is FLAT and non-monotonic
    (a LONE big conv at concurrency=1, with nobody to evict it, still misses
    ~23%) — refuting mutual eviction as the mechanism entirely.

The two REAL cache-miss causes:
  1. CLIENT-side cross-turn re-serialization of already-cached prefix bytes
     (the ``<bytes>`` / prefix-mutation detection in ``lib/tasks_pkg/cache_tracking``).
     This was the dominant FIXABLE leak and is now addressed by the single-source
     ``build_assistant_tool_call_message()`` extraction (342 in-prefix rewrites
     on 07-17 → ~0 on 07-18). When client wire bytes are stable, misses drop to
     the infra floor.
  2. A stochastic ~6-8% gap-independent SERVER-side per-request miss in the
     gateway/Bedrock layer (see ``production-cache-miss-root-cause-analysis``).
     No client change, admission gate, or SECOND KEY removes this — there is no
     capacity limit to relieve.

⚠️ Consequently this gate does NOT improve the cache hit rate. Its
``key_count<=1`` early return is a harmless no-op on the single-key production
setup (it skips a gate that would do nothing useful), but the residency /
"second key = second cache namespace" rationale below is written on the false
capacity-limit premise — do NOT invest in residency tuning or a second key as a
cache-hit fix. The machinery is kept only because it is a safe bounded-wait
no-op; a later cleanup may remove it entirely.

This gate caps the number of concurrently-resident BIG-prefix requests on any
one key.  A request over :func:`threshold_tokens` acquires a per-key slot before
its stream and releases it after; when the key is at capacity a new big request
briefly WAITS instead of piling on and evicting a warm prefix.  Small requests
(translate, cheap models, short turns) never gate.

It is a *soft* guard: bounded wait, then proceed degraded — better to serve a
big prefix slightly late than to block a task forever.  Fully env-gated and
reversible.

Env knobs
---------
``TOFU_BIG_PREFIX_GATE``            — master switch (default on).
``TOFU_BIG_PREFIX_THRESHOLD_TOKENS`` — prefix size (est. tokens) above which a
                                     request is "big" and gated (default 150000).
``TOFU_BIG_PREFIX_MAX_PER_KEY``    — max concurrent big requests per key
                                     (default 2).
``TOFU_BIG_PREFIX_WAIT_MS``        — max time to wait for a slot before
                                     proceeding degraded (default 45000).

Residency-aware admission (2026-07-16)
======================================
The original semaphore held ONLY for the in-flight ``stream_chat`` duration
(seconds). But Anthropic prompt-cache eviction is a *cache-RESIDENCY*
phenomenon: a cached prefix stays in the key's pool for the cache TTL (~5m/1h),
long after the stream returns. Two big prefixes whose STREAMS never overlap
(they run back-to-back) still coexist in the pool for minutes and LRU-evict
each other — a competition the stream-only semaphore is structurally blind to.
Live evidence: 94.8% of opus floor-misses had a prefix ≥150k (the gated size)
yet the stream gate fired only 6× / hit capacity once; the misses were
time-ISOLATED (median 1/minute), NOT concurrent streams.

So admission now counts the DISTINCT big prefixes RESIDENT on a key within a
residency-TTL window (``TOFU_BIG_PREFIX_RESIDENCY_TTL_MS``, default = the
gateway cache TTL, 5min). A request whose conv is ALREADY resident (warm) is
admitted free — re-using a warm prefix is exactly the goal, not a new
competitor. A NEW distinct big prefix waits up to the budget when the resident
working set is full (``TOFU_BIG_PREFIX_RESIDENCY_MAX``, default = max_per_key),
then proceeds degraded.

HONEST LIMIT: on a SINGLE key pool this can only SERIALIZE the active working
set — it cannot spread load. Sustained >capacity distinct-big-conv load still
evicts (that needs route (i): a second key = a second cache namespace). This is
a working-set bound, not a capacity fix.

Residency env knobs
-------------------
``TOFU_BIG_PREFIX_RESIDENCY``       — residency-aware admission on (default on).
``TOFU_BIG_PREFIX_RESIDENCY_MAX``   — max distinct big prefixes resident per key
                                     (default = max_per_key).
``TOFU_BIG_PREFIX_RESIDENCY_TTL_MS``— how long a prefix counts as resident after
                                     its last use (default 300000 = 5min).
``TOFU_BIG_PREFIX_RESIDENCY_WAIT_MS``— SHORT wait (ms) a new distinct prefix
                                     gives a saturated set before degrading
                                     (default 1500; distinct from the 45s
                                     stream budget — see residency_wait_budget_ms).
``TOFU_BIG_PREFIX_RESIDENCY_LRU_BOUND``— keep the resident table ≤ cap by LRU
                                     eviction (default on); models the gateway
                                     pool's finite LRU so a DEGRADED pass-through
                                     evicts the LRU resident instead of inflating
                                     the working set past cap.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'gate_enabled',
    'threshold_tokens',
    'max_per_key',
    'wait_budget_ms',
    'residency_enabled',
    'residency_max',
    'residency_ttl_ms',
    'residency_wait_budget_ms',
    '_residency_lru_bound_enabled',
    'estimate_prefix_tokens',
    'big_prefix_slot',
    '_reset_residency_for_tests',
]


def gate_enabled() -> bool:
    """Whether the per-key big-prefix admission gate is active (default on)."""
    val = os.environ.get('TOFU_BIG_PREFIX_GATE', '1')
    return val.strip().lower() not in ('0', 'false', 'no', 'off', '')


def threshold_tokens() -> int:
    """Estimated-token prefix size above which a request is gated. Default 150000."""
    try:
        v = int(os.environ.get('TOFU_BIG_PREFIX_THRESHOLD_TOKENS', '150000'))
        return v if v > 0 else 150000
    except (ValueError, TypeError) as e:
        logger.debug('[BigPrefixGate] TOFU_BIG_PREFIX_THRESHOLD_TOKENS parse failed, default: %s', e)
        return 150000


def max_per_key() -> int:
    """Max concurrent big-prefix requests allowed on one key. Default 2."""
    try:
        v = int(os.environ.get('TOFU_BIG_PREFIX_MAX_PER_KEY', '2'))
        return v if v >= 1 else 2
    except (ValueError, TypeError) as e:
        logger.debug('[BigPrefixGate] TOFU_BIG_PREFIX_MAX_PER_KEY parse failed, default: %s', e)
        return 2


def wait_budget_ms() -> float:
    """Max time (ms) to wait for a per-key slot before proceeding degraded. Default 45000."""
    try:
        v = float(os.environ.get('TOFU_BIG_PREFIX_WAIT_MS', '45000'))
        return v if v > 0 else 45000.0
    except (ValueError, TypeError) as e:
        logger.debug('[BigPrefixGate] TOFU_BIG_PREFIX_WAIT_MS parse failed, default: %s', e)
        return 45000.0


def residency_enabled() -> bool:
    """Whether residency-aware admission is active (env-gated, default on).

    When OFF the gate reverts to the legacy stream-only semaphore (concurrency
    of in-flight streams), which is blind to the flow-non-overlap-but-residency-
    overlap eviction case — see the module docstring."""
    val = os.environ.get('TOFU_BIG_PREFIX_RESIDENCY', '1')
    return val.strip().lower() not in ('0', 'false', 'no', 'off', '')


def residency_max() -> int:
    """Max DISTINCT big prefixes counted resident per key. Default = max_per_key."""
    raw = os.environ.get('TOFU_BIG_PREFIX_RESIDENCY_MAX')
    if raw is None:
        return max_per_key()
    try:
        v = int(raw)
        return v if v >= 1 else max_per_key()
    except (ValueError, TypeError) as e:
        logger.debug('[BigPrefixGate] TOFU_BIG_PREFIX_RESIDENCY_MAX parse failed, default: %s', e)
        return max_per_key()


def residency_ttl_ms() -> float:
    """How long (ms) a prefix counts as resident after its last use.

    Default 300000 (5min) = the Anthropic default cache TTL: a prefix cached now
    occupies the key's pool for ~this long, so it competes for that window even
    after its stream ends. Tune with ``TOFU_BIG_PREFIX_RESIDENCY_TTL_MS``."""
    try:
        v = float(os.environ.get('TOFU_BIG_PREFIX_RESIDENCY_TTL_MS', '300000'))
        return v if v > 0 else 300000.0
    except (ValueError, TypeError) as e:
        logger.debug('[BigPrefixGate] TOFU_BIG_PREFIX_RESIDENCY_TTL_MS parse failed, default: %s', e)
        return 300000.0


def residency_wait_budget_ms() -> float:
    """Max time (ms) a NEW distinct big prefix waits for a resident slot to free
    before proceeding degraded, in residency mode. Default 1500 (1.5s).

    CRITICAL — this is DELIBERATELY SEPARATE from :func:`wait_budget_ms` (the
    stream-concurrency budget, default 45000). Residency lives on the cache-TTL
    timescale (minutes): a resident REFRESHES its TTL to now+TTL every round, so
    on a SATURATED SINGLE POOL the working set never drains and a 3rd distinct
    conv would wait the FULL stream budget (45s) EVERY round — pure loss,
    because waiting cannot manufacture pool capacity; the prefix will miss
    whether we wait 1.5s or 45s. So the residency waiter waits only a SHORT
    bounded window: long enough to let a resident that is genuinely about to
    EXPIRE (idle conv) free its slot and be picked up (the CV wakes promptly on
    expiry/exit), but short enough that a saturated-and-not-expiring set
    degrades to a fast miss instead of a 45s stall. Tune with
    ``TOFU_BIG_PREFIX_RESIDENCY_WAIT_MS``."""
    try:
        v = float(os.environ.get('TOFU_BIG_PREFIX_RESIDENCY_WAIT_MS', '1500'))
        return v if v > 0 else 1500.0
    except (ValueError, TypeError) as e:
        logger.debug('[BigPrefixGate] TOFU_BIG_PREFIX_RESIDENCY_WAIT_MS parse failed, default: %s', e)
        return 1500.0


def estimate_prefix_tokens(body_or_messages) -> int:
    """Cheap char-based estimate of a request's prompt size in tokens.

    Deliberately approximate (chars / 4) and lock-free — it runs on the dispatch
    hot path once per attempt and only needs to be good enough to separate BIG
    prefixes (>threshold) from ordinary turns.  Handles both a raw messages list
    and a body dict carrying ``messages``.  A tools blob (if present) is counted
    too since it sits in the cached prefix.

    Args:
        body_or_messages: the messages list, or a body dict with 'messages'.

    Returns:
        Estimated token count (>= 0). Returns 0 on any structural surprise so a
        malformed body never falsely trips the gate.
    """
    try:
        if isinstance(body_or_messages, dict):
            messages = body_or_messages.get('messages') or []
            tools = body_or_messages.get('tools') or []
        elif isinstance(body_or_messages, list):
            messages = body_or_messages
            tools = []
        else:
            return 0
        chars = 0
        for m in messages:
            content = m.get('content') if isinstance(m, dict) else None
            if isinstance(content, str):
                chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        # text / thinking blocks carry the bulk; count their text.
                        t = block.get('text') or block.get('thinking') or ''
                        if isinstance(t, str):
                            chars += len(t)
                        # image blocks: count base64 payload length (it's in the prefix).
                        src = block.get('source')
                        if isinstance(src, dict):
                            data = src.get('data')
                            if isinstance(data, str):
                                chars += len(data)
                    elif isinstance(block, str):
                        chars += len(block)
        if tools:
            try:
                import json
                chars += len(json.dumps(tools, ensure_ascii=False))
            except (TypeError, ValueError):
                pass
        return chars // 4
    except Exception as e:
        logger.debug('[BigPrefixGate] estimate_prefix_tokens failed: %s', e)
        return 0


# Per-key bounded gates, created lazily. Keyed by (key_name, capacity) so a
# runtime change to max_per_key rebuilds the semaphore cleanly rather than
# leaking permits on the old one.
_gates: dict[str, threading.BoundedSemaphore] = {}
_gates_cap: dict[str, int] = {}
_gates_lock = threading.Lock()


def _semaphore_for(key_name: str, capacity: int) -> threading.BoundedSemaphore:
    """Return (creating if needed) the per-key gate sized to *capacity*."""
    with _gates_lock:
        if _gates_cap.get(key_name) != capacity:
            _gates[key_name] = threading.BoundedSemaphore(capacity)
            _gates_cap[key_name] = capacity
        return _gates[key_name]


# ── Residency table: key_name → {conv_id → resident_until_ts} ──
# A prefix is "resident" on a key from admission until resident_until (last-use
# + residency TTL). Distinct resident convs = the key's active cache working
# set. One condition variable per key serializes the admit/expire decision and
# lets a waiter wake the instant a resident expires or a distinct conv leaves.
_residency: dict[str, dict[str, float]] = {}
_residency_cv: dict[str, threading.Condition] = {}
_residency_lock = threading.Lock()


def _cv_for(key_name: str) -> threading.Condition:
    with _residency_lock:
        cv = _residency_cv.get(key_name)
        if cv is None:
            cv = threading.Condition()
            _residency_cv[key_name] = cv
            _residency.setdefault(key_name, {})
        return cv


def _prune_expired_locked(table: dict[str, float], now: float) -> None:
    """Drop residents whose TTL has lapsed. Caller holds the key's cv."""
    for cid in [c for c, until in table.items() if until <= now]:
        del table[cid]


def _residency_lru_bound_enabled() -> bool:
    """Whether the residency table is LRU-bounded to ``cap`` (default on).

    OFF (``TOFU_BIG_PREFIX_RESIDENCY_LRU_BOUND=0``) reproduces the pre-fix
    unbounded-growth bug where degraded pass-throughs inflate the table past
    cap — used by the NEUTER to prove the bound is load-bearing."""
    val = os.environ.get('TOFU_BIG_PREFIX_RESIDENCY_LRU_BOUND', '1')
    return val.strip().lower() not in ('0', 'false', 'no', 'off', '')


def _reserve_locked(table: dict[str, float], conv_id: str, until: float,
                    cap: int) -> None:
    """Set/refresh ``conv_id``'s residency to ``until``, then LRU-BOUND the
    table to ``cap`` entries. Caller holds the key's cv.

    THE unbounded-growth fix. A request that proceeds DEGRADED (forced
    through a saturated working set) STILL wrote its prefix to the key's pool,
    so it IS physically resident — but the pool is a finite LRU cache: writing
    a new prefix EVICTS the least-recently-used one. Modelling that here (evict
    the smallest ``resident_until`` = the LRU resident) keeps the table bounded
    to exactly ``cap`` no matter how many convs degrade through. Without it,
    every degraded pass-through appended a fresh 5-min entry and the table grew
    unbounded — the count that gates the NEXT conv stopped meaning "concurrent
    cache residents" and admission was permanently poisoned. We never evict the
    conv we just reserved (it is the most-recently-used by construction)."""
    table[conv_id] = until
    if not _residency_lru_bound_enabled():
        return
    if len(table) > cap:
        # Evict least-recently-used (smallest resident_until) down to cap,
        # never the just-reserved conv (it has the freshest timestamp anyway).
        victims = sorted((c for c in table if c != conv_id),
                         key=lambda c: table[c])
        for c in victims[:len(table) - cap]:
            del table[c]


def _reset_residency_for_tests() -> None:
    """Test hook: clear all residency state + semaphores."""
    with _residency_lock:
        _residency.clear()
        _residency_cv.clear()
    with _gates_lock:
        _gates.clear()
        _gates_cap.clear()


@contextlib.contextmanager
def big_prefix_slot(key_name: str, est_tokens: int, *, conv_id: str = '',
                    log_prefix: str = '', key_count: int | None = None):
    """Admit a big-prefix request, bounding the key's cache WORKING SET.

    Context manager. A no-op (immediate yield) when the gate is disabled, the
    request is below :func:`threshold_tokens`, ``key_name`` is empty, OR the
    model this request runs on is served by a SINGLE key (``key_count <= 1``).

    SINGLE-KEY NO-OP (2026-07-17): admission gating only pays off with ≥2
    keys, where holding a big prefix back lets it route to a DIFFERENT key's
    cache namespace. On a single-key model the gate cannot add capacity — it can
    only SERIALIZE the one shared pool, which just adds latency without
    improving the hit rate (live evidence: a gated round waited 1.5s and STILL
    hit 99%, i.e. the wait was pure loss). So when the caller tells us the model
    has ≤1 key we skip the gate entirely. ``key_count=None`` (the default, and
    what existing callers/tests pass) preserves the original always-gate
    behavior — the no-op only triggers when a caller explicitly reports a
    single-key model.

    Residency-aware mode (default, :func:`residency_enabled`): admission counts
    the DISTINCT big prefixes RESIDENT on ``key_name`` within the residency-TTL
    window (:func:`residency_ttl_ms`), NOT just concurrent streams. The calling
    conv's prefix is marked resident on entry and its residency is REFRESHED on
    exit (the cached prefix outlives the stream — see module docstring), so a
    warm same-conv re-run is admitted free while a NEW distinct conv waits up to
    :func:`wait_budget_ms` when the working set (:func:`residency_max`) is full,
    then proceeds degraded. ``conv_id`` empty falls back to the legacy
    stream-only semaphore (no identity to track residency by).

    Legacy mode (``TOFU_BIG_PREFIX_RESIDENCY=0``): the original per-key
    :func:`max_per_key` :class:`~threading.BoundedSemaphore` over the stream
    duration only.

    Soft guard either way: bounded wait, then proceed degraded — never block a
    task forever. Fully env-gated and reversible.

    Args:
        key_name: the API key this request will run on (from the picked slot).
        est_tokens: estimated prompt size (see :func:`estimate_prefix_tokens`).
        conv_id: conversation id — the residency identity. Empty → legacy mode.
        log_prefix: optional tag for correlating log lines.
    """
    if not gate_enabled() or not key_name or est_tokens < threshold_tokens():
        yield
        return

    # ── Single-key no-op ──
    # Gating a model served by one key only serializes its single shared cache
    # pool (added latency, no capacity gain — see the docstring). Skip entirely.
    if key_count is not None and key_count <= 1:
        logger.debug('%s [BigPrefixGate] single-key model (key_count=%s) — '
                     'skipping gate for big prefix (~%dk tok) conv=%s: gating '
                     'one key cannot add capacity, only latency', log_prefix,
                     key_count, est_tokens // 1000, (conv_id or '')[:8])
        yield
        return

    # ── Legacy stream-only mode (residency off, or no conv identity) ──
    if not residency_enabled() or not conv_id:
        sem = _semaphore_for(key_name, max_per_key())
        budget_s = wait_budget_ms() / 1000.0
        t0 = time.time()
        acquired = sem.acquire(timeout=budget_s)
        waited = time.time() - t0
        if acquired:
            if waited > 0.05:
                logger.info('%s [BigPrefixGate] admitted big prefix (~%dk tok) '
                            'on key=%s after waiting %.1fs (cap=%d, stream-only)',
                            log_prefix, est_tokens // 1000, key_name, waited,
                            max_per_key())
        else:
            logger.info('%s [BigPrefixGate] proceeding DEGRADED for big prefix '
                        '(~%dk tok) on key=%s — %d already inflight, waited '
                        '%.1fs (budget %.0fs, stream-only)', log_prefix,
                        est_tokens // 1000, key_name, max_per_key(), waited,
                        budget_s)
        try:
            yield
        finally:
            if acquired:
                try:
                    sem.release()
                except ValueError as e:
                    logger.warning('%s [BigPrefixGate] semaphore release on '
                                   'key=%s raised: %s', log_prefix, key_name, e)
        return

    # ── Residency-aware mode ──
    # Wait budget is the SHORT residency budget (default 1.5s), NOT the 45s
    # stream budget: on a saturated single pool a resident refreshes its TTL
    # every round so the set never drains — waiting the full stream budget
    # would stall a new distinct conv 45s EVERY round for a prefix that will
    # miss regardless. The short window still lets a genuinely-EXPIRING resident
    # free its slot (the CV wakes on expiry/exit) before degrading to a fast
    # miss. See residency_wait_budget_ms's docstring.
    cap = residency_max()
    ttl_s = residency_ttl_ms() / 1000.0
    budget_s = residency_wait_budget_ms() / 1000.0
    cv = _cv_for(key_name)
    deadline = time.time() + budget_s
    waited_total = 0.0
    with cv:
        table = _residency[key_name]
        while True:
            now = time.time()
            _prune_expired_locked(table, now)
            # Warm reuse: our conv is already resident → admit free (refresh).
            if conv_id in table:
                break
            # New distinct prefix: admit if the working set has room.
            if len(table) < cap:
                break
            # Working set full and we are a NEW competitor → wait for a resident
            # to expire / leave, up to the budget, then degrade through.
            remaining = deadline - now
            if remaining <= 0:
                logger.info('%s [BigPrefixGate] proceeding DEGRADED for big '
                            'prefix (~%dk tok) conv=%s on key=%s — %d distinct '
                            'prefixes resident (cap=%d), waited %.1fs (budget '
                            '%.0fs)', log_prefix, est_tokens // 1000,
                            conv_id[:8], key_name, len(table), cap,
                            waited_total, budget_s)
                break
            cv.wait(timeout=remaining)
            waited_total = budget_s - max(0.0, deadline - time.time())
        # Reserve/refresh our residency slot with a fresh TTL, LRU-bounding the
        # table to cap so a DEGRADED pass-through cannot inflate the working set
        # past cap (the unbounded-growth fix — see _reserve_locked).
        _reserve_locked(table, conv_id, time.time() + ttl_s, cap)
        if waited_total > 0.05:
            logger.info('%s [BigPrefixGate] admitted big prefix (~%dk tok) '
                        'conv=%s on key=%s after waiting %.1fs (working set '
                        '%d/%d resident)', log_prefix, est_tokens // 1000,
                        conv_id[:8], key_name, waited_total, len(table), cap)
    try:
        yield
    finally:
        # Refresh residency to last-use + TTL: the prefix the stream just wrote
        # stays cached in the key's pool for the TTL, so it KEEPS occupying the
        # working set after the stream ends (the whole point). We do NOT delete
        # it — it expires by TTL. Wake any waiter so it can re-evaluate (a
        # refresh doesn't free a slot, but a concurrent expiry might have).
        with cv:
            _reserve_locked(_residency[key_name], conv_id,
                            time.time() + ttl_s, cap)
            cv.notify_all()
