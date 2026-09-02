"""lib/llm_dispatch/slot.py — Slot dataclass: one (api_key, model) routing target.

A Slot tracks live statistics (RPM usage, latency EMA, error rate, inflight
count) and computes a composite score used by the dispatcher to pick the
best available target for each request.
"""

import collections
import math
import random
import threading
import time
from dataclasses import dataclass, field

from lib.log import get_logger

logger = get_logger(__name__)

# Single source of truth for the legal ``Slot.thinking_format`` values.
# Every code path that builds, persists, or validates a thinking-format
# string MUST consult this set — preventing silent typos like
# ``chat_template_kwarg`` (missing the trailing ``s``) from degrading
# to auto-detect.
#
# Adding a new dialect = one entry here + one branch in
# ``lib/llm/body.py::build_body`` and ``lib/llm_dispatch/api.py::_readjust_thinking_params``.
#
#   ''                       auto-detect from model name + brand
#   'enable_thinking'        cloud Qwen / LongCat / ERNIE
#   'thinking_type'          Doubao / GLM / Kimi / Claude
#   'reasoning_effort'       Gemini 3.x (OpenAI-style reasoning_effort string)
#   'chat_template_kwargs'   sglang / vLLM self-hosted dual-mode
#   'none'                   send no thinking parameters
THINKING_FORMATS = frozenset({
    '', 'enable_thinking', 'thinking_type', 'reasoning_effort',
    'chat_template_kwargs', 'none',
})

# ── Truncation cooldown window (record_truncation) ──
# ``consecutive_errors`` is zeroed by every interleaved record_success, so an
# INTERMITTENTLY-truncating upstream (success, truncate, success, truncate —
# the measured premature-close pattern: 19 closes on 2026-08-05, every one
# followed by a success that reset the streak) never reached the old
# ``>= 3 consecutive`` cooldown gate and the slot kept being picked. A rolling
# time window survives successes: ≥ _TRUNCATION_WINDOW_MIN truncations inside
# _TRUNCATION_WINDOW_S seconds cools the slot regardless of the streak.
_TRUNCATION_WINDOW_S = 600.0
_TRUNCATION_WINDOW_MIN = 3


def _is_valid_thinking_format(value: str) -> bool:
    """True for a built-in format OR a registered tofu.providers plugin dialect.

    Defers to :func:`lib.llm_dispatch.provider_registry.is_valid_thinking_format`
    so plugin dialects are accepted, but falls back to the built-in set if that
    module can't be imported (keeps Slot construction robust during early
    bootstrap before the registry is wired).
    """
    try:
        from lib.llm_dispatch.provider_registry import is_valid_thinking_format
        return is_valid_thinking_format(value)
    except Exception as e:
        logger.debug('[Slot] provider_registry unavailable, using built-in '
                     'THINKING_FORMATS only: %s', e)
        return value in THINKING_FORMATS


__all__ = [
    'Slot',
    'THINKING_FORMATS',
]


@dataclass
class Slot:
    """A single (api_key, model) routing target with live statistics."""
    key_name: str           # 'key_0', 'key_1', ... (index in LLM_API_KEYS)
    api_key: str
    model: str
    capabilities: set       # {'text', 'vision', 'thinking', 'cheap'}

    def __post_init__(self):
        # Defensive copy — prevent shared-reference bugs when
        # multiple Slots are built from the same caps set.
        self.capabilities = set(self.capabilities)
        if not self.logical_model:
            self.logical_model = self.model
        # Defensive copy — the marker is per-provider config state.
        self.adapter = dict(self.adapter or {})
        # Reject typos at construction. We deliberately do NOT silently
        # coerce — a typo'd dialect would otherwise degrade to
        # auto-detect with zero feedback (the bug that motivated this
        # check; see commit history for the qwen35-0p8b stream_anomaly
        # incident).
        if not _is_valid_thinking_format(self.thinking_format):
            raise ValueError(
                'thinking_format=%r is not one of %s (nor a registered '
                'tofu.providers plugin dialect)' % (
                    self.thinking_format, sorted(THINKING_FORMATS)))
        # Seed the recovery ceiling from the constructor rpm_limit (the
        # dispatcher overrides it via set_rpm_ceiling when it reseeds from
        # benchmark data). 0.0 means "not provided" → use the current limit.
        if self.rpm_limit_max <= 0:
            self.rpm_limit_max = self.rpm_limit

    # ── Provider routing ──
    logical_model: str = ''         # the model entry's LOGICAL model_id this slot
                                    # serves (model = the wire id actually sent).
                                    # Routing (prefer_model) keys on THIS — owner
                                    # directive 2026-08-06: model_id is the only
                                    # routing key; aliases are wire spellings,
                                    # never routing widenings. '' → __post_init__
                                    # defaults it to `model` (env/ephemeral slots).
    base_url: str = ''              # provider-specific base URL (empty = use global default)
    provider_id: str = 'default'    # which provider this slot belongs to
    extra_headers: dict = field(default_factory=dict)  # provider-specific custom HTTP headers
    thinking_format: str = ''       # per-provider thinking param format:
                                    # '' = auto-detect from model name (default)
                                    # 'enable_thinking' = {enable_thinking: bool} (LongCat, Qwen)
                                    # 'thinking_type' = {thinking: {type: enabled/disabled}} (Doubao, Claude)
                                    # 'reasoning_effort' = {reasoning_effort: str} (Gemini 3.x)
                                    # 'none' = no thinking parameters sent
    protocol: str = ''              # per-provider wire protocol:
                                    # '' / 'openai' = OpenAI Chat Completions (default)
                                    # 'anthropic' = Anthropic Messages API (POST /v1/messages)
                                    # 'responses' = OpenAI Responses API (POST …/responses)
    responses_profile: str = ''     # Responses feature profile:
                                    # compatible / openai / codex. Core
                                    # protocol support never implies public
                                    # OpenAI-only request fields.
    oauth: str = ''                 # subscription-OAuth provider for this slot:
                                    # '' = normal api_key auth (default)
                                    # 'claude' / 'codex' = resolve a live token +
                                    # client-identity headers at request time
                                    # (see lib/oauth/outbound.py)
    adapter: dict = field(default_factory=dict)
                                    # subscription-ADAPTER marker for this slot:
                                    # {} = normal provider reached directly (default)
                                    # {'agent_id': …, 'port': …} = a CLIProxyAPI
                                    # sidecar on the user's desktop agent — its
                                    # base_url is loopback-ON-THE-AGENT, so the
                                    # transport must ride lib.desktop.adapter's
                                    # bridge relay (never route_request, never
                                    # direct).
    stream_only: bool = False       # True if model only supports stream=True (e.g. qwq-plus, deepseek-reasoner)

    # ── Rate limiting ──
    rpm_limit: float = 60           # estimated max requests per minute
    # Ceiling for rpm_limit RECOVERY. A 429 decays rpm_limit by 20%
    # (``record_error(is_rate_limit=True)``); sustained success recovers it
    # (``record_success``) but never above this ceiling. Seeded from the
    # constructor value in ``__post_init__`` and re-seeded by the dispatcher
    # whenever it authoritatively (re)sets ``rpm_limit`` from benchmark data
    # (see ``set_rpm_ceiling``). 0.0 = "not yet seeded" → first ``__post_init__``
    # sets it. Without this the floor decayed toward 5 for the whole process
    # lifetime, so a key that took transient 429s became permanently
    # cold-preferred — the slow-bleed form of the per-key cache thrash the
    # conversation-sticky warm-key hold fixes at the cooldown layer.
    rpm_limit_max: float = 0.0
    rpm_window: list = field(default_factory=list)  # timestamps of recent requests
    _5h_window: list = field(default_factory=list, repr=False)  # timestamps for 5-hour quota tracking

    # ── Performance tracking (EMA = exponential moving average) ──
    latency_ema: float = 2000.0     # ms — lower is better, seeded from benchmark
    ttft_ema: float = 1000.0        # ms — time-to-first-token (streaming)
    throughput_ema: float = 0.0     # tokens/sec — output generation speed (0 = unknown)
    ema_alpha: float = 0.3          # EMA smoothing factor (higher = more reactive)
    last_success_time: float = 0.0  # epoch seconds; 0 = never

    # ── Error tracking ──
    consecutive_errors: int = 0
    total_requests: int = 0
    total_errors: int = 0
    # External shared-project contention (429s naming a project-level TPM
    # limit saturated by OTHER tenants). Counted separately so the model
    # success-rate column and the consecutive-429 auto-exhaust streak
    # reflect only genuine key health (2026-07-28 kimi-k3 incident: 782
    # contention 429s crushed the card's success rate to 24% while the
    # genuine failure rate was ~1%).
    contention_errors: int = 0
    # Gateway-class outages (HTTP 502/503/504, upstream-vendor transients
    # wrapped in 4xx, mid-stream SSE errors). Counted separately for the
    # same reason: a gateway storm hits EVERY key on the provider
    # uniformly, so it says nothing about key health (2026-08-03 sankuai
    # 502 storm: 813 such failures crushed the daily key-card success
    # rates to 40%/77%/10% and auto-disabled 2 of 3 healthy keys).
    gateway_errors: int = 0
    last_error_time: float = 0.0
    last_error_msg: str = ''
    # Rolling timestamps of record_truncation events (pruned to
    # _TRUNCATION_WINDOW_S). Survives record_success — the intermittent-rot
    # cooldown evidence the consecutive-error streak cannot hold.
    _truncation_events: 'collections.deque' = field(
        default_factory=collections.deque, repr=False, compare=False)

    # ── Inflight tracking ──
    inflight: int = 0               # currently executing requests

    # ── Availability ──
    is_available: bool = True
    cooldown_until: float = 0.0     # timestamp — slot is cooled down until
    # WHY the current cooldown was imposed — single source of truth for the
    # dispatch wait-loop's HUD label (the old code hardcoded "rate-limited"
    # for EVERY cooldown wait, so a hard-error 300s backoff masqueraded as
    # 限流排队). One of: '' (none) / 'rate_limit' (per-key 429, 0.5s) /
    # 'upstream' (gateway 5xx, upstream-vendor transient, endpoint
    # unreachable) / 'error' (consecutive-error backoff) / 'quota' (billing).
    # Shared-project contention never appears here: its provider/model retry
    # probes are coordinated without parking or teaching individual slot health.
    cooldown_reason: str = ''

    # ── Cost ──
    cost_per_1k_tokens: float = 0.01  # USD — used as a tiebreaker

    # ── Thread safety ──
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def current_rpm_usage(self) -> float:
        """Count requests in the last 60 seconds."""
        now = time.time()
        cutoff = now - 60
        with self._lock:
            # Prune old entries
            self.rpm_window = [t for t in self.rpm_window if t > cutoff]
            return len(self.rpm_window)

    @property
    def rpm_headroom(self) -> float:
        """How much RPM capacity is left (0.0 to 1.0, higher = more room)."""
        usage = self.current_rpm_usage
        if self.rpm_limit <= 0:
            return 0.0
        return max(0.0, 1.0 - usage / self.rpm_limit)

    @property
    def success_rate(self) -> float:
        """Success rate over lifetime (1.0 = perfect)."""
        if self.total_requests < 3:
            return 0.95  # assume good until proven otherwise
        return max(0.0, 1.0 - self.total_errors / self.total_requests)

    @property
    def requests_5h(self) -> int:
        """Count requests in the last 5 hours (rolling window)."""
        now = time.time()
        cutoff = now - 5 * 3600
        with self._lock:
            self._5h_window = [t for t in self._5h_window if t > cutoff]
            return len(self._5h_window)

    def record_request(self):
        """Call before sending a request."""
        now = time.time()
        with self._lock:
            self.rpm_window.append(now)
            self._5h_window.append(now)
            self.inflight += 1
            self.total_requests += 1


    def set_rpm_ceiling(self, rpm: float):
        """Authoritatively (re)set the RPM limit AND its recovery ceiling.

        Called by the dispatcher when seeding ``rpm_limit`` from benchmark data
        (``rpm_effective``). Sets both ``rpm_limit`` and ``rpm_limit_max`` so a
        later benchmarked-DOWN value isn't silently undone by recovery toward a
        stale higher ceiling, and a benchmarked-UP value raises the ceiling the
        recovery path can climb back to. Use this instead of assigning
        ``rpm_limit`` directly whenever the new value is a fresh authoritative
        estimate (not a 429 decay).
        """
        with self._lock:
            self.rpm_limit = max(5, rpm)
            self.rpm_limit_max = self.rpm_limit

    def release(self):
        """Release a reserved inflight slot WITHOUT recording success/failure.

        Used when a request terminates for a payload-level reason (content
        filter, oversized prompt, invalid image, user abort) that reflects
        neither slot health nor a successful completion — so neither
        ``record_success`` nor ``record_error`` is appropriate, but the
        inflight reservation taken by ``record_request`` must still be
        returned or the slot's ``score()`` is permanently inflated.
        """
        with self._lock:
            self.inflight = max(0, self.inflight - 1)

    def record_success(self, latency_ms, ttft_ms=None,
                       output_tokens: int = 0):
        """Call after a successful response.

        Args:
            latency_ms: end-to-end response time (ms).
            ttft_ms: time-to-first-token (ms) for streaming. None for non-streaming.
            output_tokens: completion tokens (used to update throughput EMA).
                Pass 0 (default) when token count isn't available.
        """
        with self._lock:
            self.inflight = max(0, self.inflight - 1)
            self.consecutive_errors = 0
            self.last_success_time = time.time()

            # ── RPM-limit recovery ──
            # A 429 decays rpm_limit by 20% with no built-in way back up, so a
            # key that took transient per-minute throttling would stay
            # permanently deprioritized (its score() RPM-pressure penalty never
            # relaxes) — making a warm/sticky key chronically cold-preferred. A
            # genuine success is evidence the key has real capacity, so recover
            # multiplicatively (mirroring the *0.8 decay) toward the seeded
            # ceiling. Capped at rpm_limit_max so it can never drift above the
            # configured/benchmarked limit. Recovery is gentler than decay
            # (1.1x up vs 0.8x down) so a key that keeps 429ing doesn't ratchet
            # back to full and immediately overload again.
            if self.rpm_limit < self.rpm_limit_max:
                self.rpm_limit = min(self.rpm_limit_max, self.rpm_limit * 1.1)

            # Update EMA
            self.latency_ema = (self.ema_alpha * latency_ms +
                                (1 - self.ema_alpha) * self.latency_ema)
            if ttft_ms is not None:
                self.ttft_ema = (self.ema_alpha * ttft_ms +
                                 (1 - self.ema_alpha) * self.ttft_ema)

            # Update throughput EMA. Use generation duration (post-TTFT) when
            # streaming, else total latency. Skip on tiny / missing data so a
            # single 1-token completion doesn't drag the average to ~0.
            if output_tokens and output_tokens > 1 and latency_ms > 0:
                if ttft_ms is not None and ttft_ms < latency_ms:
                    gen_ms = max(1.0, latency_ms - ttft_ms)
                else:
                    gen_ms = latency_ms
                tps = output_tokens / (gen_ms / 1000.0)
                if 0 < tps < 10000:  # sanity bound
                    if self.throughput_ema <= 0:
                        self.throughput_ema = tps
                    else:
                        self.throughput_ema = (self.ema_alpha * tps +
                                               (1 - self.ema_alpha) * self.throughput_ema)

        # Daily success/failure tracker (outside lock — it has its own lock).
        # Rate-limit failures don't get here, so every record_success is a
        # genuine health signal for this key.
        try:
            from lib.key_stats import record_outcome
            record_outcome(self.provider_id, self.key_name, success=True)
        except Exception as e:
            logger.debug('[Slot] key_stats record_outcome(success) failed: %s', e)

    def record_truncation(
            self, error: str = '', *, release_reservation: bool = False):
        """Call when a response was syntactically successful (HTTP 200, parseable)
        but the body was unusable — truncated mid-output, or empty when content
        was expected. The dispatcher should treat this as a soft failure: bump
        consecutive_errors so ``score()`` deprioritizes the slot for a while,
        but skip the rate-limit / quota-exhaustion / key-stats paths since the
        key itself is healthy.
        """
        with self._lock:
            if release_reservation:
                self.inflight = max(0, self.inflight - 1)
            self.consecutive_errors += 1
            self.total_errors += 1
            self.last_error_time = time.time()
            _now = self.last_error_time
            self._truncation_events.append(_now)
            while (self._truncation_events
                   and _now - self._truncation_events[0] > _TRUNCATION_WINDOW_S):
                self._truncation_events.popleft()
            if error:
                self.last_error_msg = ('[truncation] ' + str(error))[:200]
            # Cool when EITHER the consecutive streak OR the rolling window
            # says this upstream keeps truncating — the window is what catches
            # intermittent rot (streak resets on every success; the window
            # does not). The escalation exponent takes the stronger signal.
            _window_hits = len(self._truncation_events)
            _strikes = max(self.consecutive_errors, _window_hits)
            if _strikes >= 3:
                cooldown = min(300, 5 * (2 ** (_strikes - 3)))
                self.cooldown_until = time.time() + cooldown
                self.cooldown_reason = 'error'
                logger.warning('  ⚠️ Slot %s:%s cooled down %ds '
                               'after %d truncations/empty outputs '
                               '(consecutive=%d, %dmin-window=%d)',
                               self.key_name, self.model, cooldown, _strikes,
                               self.consecutive_errors,
                               int(_TRUNCATION_WINDOW_S // 60), _window_hits)

    def record_error(self, is_rate_limit=False, error: str = '',
                     is_quota_exhausted: bool = False, is_gateway: bool = False,
                     is_shared_contention: bool = False,
                     is_account_quota: bool = False,
                     cooldown_s: float = None):
        """Call after a failed request.

        Args:
            cooldown_s: Explicit cooldown duration (seconds) for rate-limit
                class errors — used when the upstream TOLD us when the
                window resets (OAuth-subscription quota:
                ``RateLimitError.retry_after_s`` parsed from
                ``resets_at``/``resets_in_seconds``). Overrides the generic
                0.5s 429 steering nudge; None keeps the generic policy
                (the 80431312 transient-429 behavior is untouched).
            is_rate_limit: True for HTTP 429/gateway-throttled errors — these
                typically reflect contention, not key health, so they don't
                count as failures in the daily success-rate tracker UNLESS
                is_quota_exhausted is also True (persistent billing error).
            error: short error description (optional, stored for UI display).
            is_quota_exhausted: True when the 429/402 indicates a PERSISTENT
                billing/quota problem (insufficient balance, credits too low).
                Such keys should be marked as exhausted for the rest of the day,
                not briefly cooled down and retried.
            is_account_quota: True when the quota signal is HTTP 402 Payment
                Required — the gateway ACCOUNT's own credit pool is empty
                (sankuai body: ext.error.source=AIGC, stage=validation), so
                EVERY model on this key is dead and the honest stop is the
                key-wide ``exhausted`` flag. Per-model convergence there
                just burns one live 402 per remaining model (2026-07-29
                sankuai_key_2: 12 of 43 model entries stopped, ~30 more
                live 402s queued for real users). A 429+insufficient_quota
                body stays VENDOR-AMBIGUOUS and keeps per-model granularity
                (2026-07-28 aggregating-gateway contract).
            is_gateway: True when the failure is an UPSTREAM outage class
                (gateway 502/503/504, or an upstream-vendor transient wrapped
                in a 4xx) rather than per-key contention. Such errors feed
                NEITHER the consecutive-429 telemetry streak NOR the daily
                failure stats — a gateway storm hits every key uniformly
                and must never trip the auto-disable gate (2026-08-03
                incident); they are counted on the separate
                ``gateway_errors`` channel. The dead-key safety net lives
                on the unchanged genuine-failure classes (401/403 auth
                death, endpoint-unreachable).
            is_shared_contention: True when the 429 names a PROJECT-LEVEL
                limit shared with other tenants of the gateway account
                (RateLimitError.is_shared_contention). Counted into
                ``contention_errors`` instead of ``total_errors`` (the
                success-rate column stays honest) and feeds neither
                key_stats path. Slot-local cooldown still applies so the
                picker rotates away briefly.
        """
        with self._lock:
            self.inflight = max(0, self.inflight - 1)
            self.consecutive_errors += 1
            if is_shared_contention and not is_gateway:
                # External project-level contention: NOT this key's health.
                # Count it separately and pull the attempt back out of
                # total_requests (record_request already added it) so the
                # success-rate column reflects genuine outcomes only.
                self.contention_errors += 1
                self.total_requests = max(0, self.total_requests - 1)
            elif is_gateway and not is_quota_exhausted:
                # Gateway-class outage: the UPSTREAM is sick, not this key.
                # Same accounting as contention — own counter + compensate
                # the attempt out of total_requests — so an upstream storm
                # never crushes the success-rate columns. A quota death
                # keeps counting as a genuine error (billing is key health).
                self.gateway_errors += 1
                self.total_requests = max(0, self.total_requests - 1)
            else:
                self.total_errors += 1
            self.last_error_time = time.time()
            if error:
                self.last_error_msg = str(error)[:200]

            if is_quota_exhausted:
                # Persistent billing/balance problem — long cooldown so this
                # process stops cycling to the dead key for at least an hour
                # (the daily key-stats tracker also disables it).
                self.cooldown_until = time.time() + 3600
                self.cooldown_reason = 'quota'
            elif is_rate_limit:
                # Reduce effective RPM estimate — but NOT for external
                # shared-project contention: the pipe is full of OTHER
                # tenants' tokens, so decaying our own pacing estimate
                # teaches the scorer a false "this key is slow" lesson it
                # then has to recover from at 1.1x.
                if not is_shared_contention:
                    self.rpm_limit = max(5, self.rpm_limit * 0.8)
                if cooldown_s and cooldown_s > 0:
                    # Subscription-quota timed hold — the upstream named
                    #   the reset time, so park the slot for exactly that
                    #   long instead of the generic 0.5s steering nudge.
                    #   Reason 'quota' (long hold — NOT worth a sticky-key
                    #   wait), NOT 'rate_limit'.
                    self.cooldown_until = time.time() + min(float(cooldown_s), 86400.0)
                    self.cooldown_reason = 'quota'
                    logger.warning('  ⏳ Slot %s:%s cooled down %.0fs '
                                   '(subscription quota reset)',
                                   self.key_name, self.model,
                                   min(float(cooldown_s), 86400.0))
                else:
                    # Very brief cooldown — just enough to steer picker to
                    # another slot; the caller will keep cycling rapidly.
                    self.cooldown_until = time.time() + 0.5
                    self.cooldown_reason = 'upstream' if is_gateway else 'rate_limit'
            elif self.consecutive_errors >= 3:
                # Exponential backoff cooldown after repeated failures.
                # Cap at 300s (5min) for sustained failures (e.g. DNS unreachable).
                cooldown = min(300, 5 * (2 ** (self.consecutive_errors - 3)))
                self.cooldown_until = time.time() + cooldown
                self.cooldown_reason = 'error'
                logger.warning('  ⚠️ Slot %s:%s cooled down %ds '
                      'after %d consecutive errors', self.key_name, self.model, cooldown, self.consecutive_errors)

        # Daily key-health tracker.
        #   - Quota-exhausted 429/402 (clear billing signal in body): immediately
        #     mark the key as exhausted so it's disabled for the rest of today.
        #   - Generic 429 rate-limit: telemetry only (rate_limited /
        #     consecutive_429 counters for the UI). A 429 streak NEVER
        #     disables the key (owner policy 2026-07-29) — the brief steering
        #     cooldown above is the whole answer, so the key rejoins on its
        #     own the moment the upstream recovers.
        #   - Other errors: count as a regular failure for the success-rate column.
        if is_quota_exhausted:
            try:
                from lib.key_stats import mark_key_exhausted
                # HTTP 402 = account credit pool dead → key-wide stop;
                # 429-quota = vendor-ambiguous → per-model stop.
                mark_key_exhausted(self.provider_id, self.key_name,
                                   reason=error or 'quota exhausted (HTTP 402/429)',
                                   model='' if is_account_quota else self.model)
            except Exception as e:
                logger.debug('[Slot] key_stats mark_key_exhausted failed: %s', e)
        elif is_shared_contention and not is_gateway:
            # External contention feeds NEITHER the consecutive-429
            # telemetry streak NOR the daily failure stats (nothing failed —
            # someone else's traffic filled the pipe). The slot-local
            # contention_errors counter above keeps the volume visible on
            # the model card.
            pass
        elif is_gateway:
            # Gateway-class outage — own daily counter ONLY. Feeding the
            # failure stats here is what crushed all three sankuai key
            # cards and auto-disabled two of them during the 2026-08-03
            # 502 storm; feeding the 429 streak would equally misattribute
            # an upstream outage to per-key contention.
            try:
                from lib.key_stats import record_gateway_error
                record_gateway_error(self.provider_id, self.key_name,
                                     reason=error or 'gateway 5xx')
            except Exception as e:
                logger.debug('[Slot] key_stats record_gateway_error failed: %s', e)
        elif is_rate_limit and not is_gateway:
            try:
                from lib.key_stats import record_rate_limit
                record_rate_limit(self.provider_id, self.key_name,
                                  reason=error or 'HTTP 429')
            except Exception as e:
                logger.debug('[Slot] key_stats record_rate_limit failed: %s', e)
        else:
            # Generic failure (auth death, endpoint-unreachable, hard 5xx
            # that is not gateway-class). record_outcome feeds the daily
            # success-rate column (no auto-exhaust threshold), so the
            # dead-key safety net keeps working while the consecutive-429
            # auto-exhaust streak is reserved for genuine per-key
            # contention.
            try:
                from lib.key_stats import record_outcome
                record_outcome(self.provider_id, self.key_name,
                               success=False, error=error)
            except Exception as e:
                logger.debug('[Slot] key_stats record_outcome(failure) failed: %s', e)

    def score(self) -> float:
        """Lower score = better slot. Used for picking the best candidate.

        Factors (weighted):
          1. Latency EMA           — dominant factor (fast models preferred)
          2. RPM headroom          — penalize slots near their limit
          3. Inflight count        — penalize busy slots
          4. Error penalty         — penalize unstable slots
          5. Cost                  — slight tiebreaker (prefer cheap)
          6. Cooldown              — hard penalty if in cooldown
        """
        now = time.time()

        # Snapshot mutable fields under lock for a consistent read
        with self._lock:
            cooldown_until = self.cooldown_until
            latency_ema = self.latency_ema
            inflight = self.inflight
            consecutive_errors = self.consecutive_errors
            rpm_limit = self.rpm_limit
            cost = self.cost_per_1k_tokens
            total_requests = self.total_requests
            total_errors = self.total_errors
            # Prune and snapshot RPM window atomically
            cutoff = now - 60
            self.rpm_window = [t for t in self.rpm_window if t > cutoff]
            rpm_usage = len(self.rpm_window)

        # Hard block: cooldown
        if now < cooldown_until:
            return float('inf')

        # Base score = latency EMA (ms)
        base = latency_ema

        # RPM pressure: penalize when > 70% utilized
        rpm_headroom = max(0.0, 1.0 - rpm_usage / rpm_limit) if rpm_limit > 0 else 0.0
        usage_ratio = 1.0 - rpm_headroom
        if usage_ratio > 0.7:
            # Exponential penalty: 1x at 70%, ~3x at 90%, ~10x at 100%
            rpm_penalty = 1.0 + math.exp((usage_ratio - 0.7) * 8) - 1.0
            base *= rpm_penalty

        # Inflight penalty: aggressively spread load across slots
        # Each concurrent request multiplies score substantially,
        # so that even a fast slot with 2 inflight loses to a slower idle slot.
        # Formula: first inflight → 2x, second → 3.5x, third → 5.75x ...
        if inflight > 0:
            base *= (1.0 + inflight * 0.8 + inflight ** 1.5 * 0.2)

        # Error penalty: consecutive errors make the slot much less attractive
        if consecutive_errors > 0:
            base *= (1.0 + consecutive_errors * 3.0)

        # Success rate penalty: long-term unreliability
        # Use snapshotted values for consistency (avoid re-acquiring lock)
        if total_requests < 3:
            sr = 0.95
        else:
            sr = max(0.0, 1.0 - total_errors / total_requests)
        if sr < 0.9:
            base *= (1.0 + (1.0 - sr) * 5.0)

        # Cost tiebreaker (very small influence)
        base += cost * 10

        # Small random jitter to avoid thundering herd
        base *= random.uniform(0.95, 1.05)

        return base

    def __repr__(self):
        return (f'Slot({self.key_name}:{self.model} '
                f'rpm={self.current_rpm_usage:.0f}/{self.rpm_limit:.0f} '
                f'lat={self.latency_ema:.0f}ms '
                f'inflight={self.inflight} '
                f'err={self.consecutive_errors} '
                f'score={self.score():.0f})')
