"""lib/llm_dispatch/slot.py — Slot dataclass: one (api_key, model) routing target.

A Slot tracks live statistics (RPM usage, latency EMA, error rate, inflight
count) and computes a composite score used by the dispatcher to pick the
best available target for each request.
"""

import math
import random
import threading
import time
from dataclasses import dataclass, field

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'Slot',
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

    # ── Provider routing ──
    base_url: str = ''              # provider-specific base URL (empty = use global default)
    provider_id: str = 'default'    # which provider this slot belongs to
    extra_headers: dict = field(default_factory=dict)  # provider-specific custom HTTP headers
    thinking_format: str = ''       # per-provider thinking param format:
                                    # '' = auto-detect from model name (default)
                                    # 'enable_thinking' = {enable_thinking: bool} (LongCat, Qwen, Gemini)
                                    # 'thinking_type' = {thinking: {type: enabled/disabled}} (Doubao, Claude)
                                    # 'none' = no thinking parameters sent
    stream_only: bool = False       # True if model only supports stream=True (e.g. qwq-plus, deepseek-reasoner)

    # ── Rate limiting ──
    rpm_limit: float = 60           # estimated max requests per minute
    rpm_window: list = field(default_factory=list)  # timestamps of recent requests
    _5h_window: list = field(default_factory=list, repr=False)  # timestamps for 5-hour quota tracking

    # ── Performance tracking (EMA = exponential moving average) ──
    latency_ema: float = 2000.0     # ms — lower is better, seeded from benchmark
    ttft_ema: float = 1000.0        # ms — time-to-first-token (streaming)
    ema_alpha: float = 0.3          # EMA smoothing factor (higher = more reactive)

    # ── Error tracking ──
    consecutive_errors: int = 0
    total_requests: int = 0
    total_errors: int = 0
    last_error_time: float = 0.0

    # ── Inflight tracking ──
    inflight: int = 0               # currently executing requests

    # ── Availability ──
    is_available: bool = True
    cooldown_until: float = 0.0     # timestamp — slot is cooled down until

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

    def record_success(self, latency_ms, ttft_ms=None):
        """Call after a successful response."""
        with self._lock:
            self.inflight = max(0, self.inflight - 1)
            self.consecutive_errors = 0

            # Update EMA
            self.latency_ema = (self.ema_alpha * latency_ms +
                                (1 - self.ema_alpha) * self.latency_ema)
            if ttft_ms is not None:
                self.ttft_ema = (self.ema_alpha * ttft_ms +
                                 (1 - self.ema_alpha) * self.ttft_ema)

        # Daily success/failure tracker (outside lock — it has its own lock).
        # Rate-limit failures don't get here, so every record_success is a
        # genuine health signal for this key.
        try:
            from lib.key_stats import record_outcome
            record_outcome(self.provider_id, self.key_name, success=True)
        except Exception as e:
            logger.debug('[Slot] key_stats record_outcome(success) failed: %s', e)

    def record_error(self, is_rate_limit=False, error: str = '',
                     is_quota_exhausted: bool = False):
        """Call after a failed request.

        Args:
            is_rate_limit: True for HTTP 429/gateway-throttled errors — these
                typically reflect contention, not key health, so they don't
                count as failures in the daily success-rate tracker UNLESS
                is_quota_exhausted is also True (persistent billing error).
            error: short error description (optional, stored for UI display).
            is_quota_exhausted: True when the 429/402 indicates a PERSISTENT
                billing/quota problem (insufficient balance, credits too low).
                Such keys should be marked as exhausted for the rest of the day,
                not briefly cooled down and retried.
        """
        with self._lock:
            self.inflight = max(0, self.inflight - 1)
            self.consecutive_errors += 1
            self.total_errors += 1
            self.last_error_time = time.time()

            if is_quota_exhausted:
                # Persistent billing/balance problem — long cooldown so this
                # process stops cycling to the dead key for at least an hour
                # (the daily key-stats tracker also disables it).
                self.cooldown_until = time.time() + 3600
            elif is_rate_limit:
                # Reduce effective RPM estimate
                self.rpm_limit = max(5, self.rpm_limit * 0.8)
                # Very brief cooldown — just enough to steer picker to
                # another slot; the caller will keep cycling rapidly.
                self.cooldown_until = time.time() + 0.5
            elif self.consecutive_errors >= 3:
                # Exponential backoff cooldown after repeated failures.
                # Cap at 300s (5min) for sustained failures (e.g. DNS unreachable).
                cooldown = min(300, 5 * (2 ** (self.consecutive_errors - 3)))
                self.cooldown_until = time.time() + cooldown
                logger.warning('  ⚠️ Slot %s:%s cooled down %ds '
                      'after %d consecutive errors', self.key_name, self.model, cooldown, self.consecutive_errors)

        # Daily key-health tracker.
        #   - Quota-exhausted 429/402 (clear billing signal in body): immediately
        #     mark the key as exhausted so it's disabled for the rest of today.
        #   - Generic 429 rate-limit: feed the consecutive-429 streak counter —
        #     provider error bodies are ambiguous ("达到使用量上限" can mean either
        #     RPM-overrun on a paid key OR a dead key), so we only auto-exhaust
        #     after the streak crosses MAX_CONSECUTIVE_429. Any success or non-
        #     429 error resets the streak.
        #   - Other errors: count as a regular failure for the success-rate column.
        if is_quota_exhausted:
            try:
                from lib.key_stats import mark_key_exhausted
                mark_key_exhausted(self.provider_id, self.key_name,
                                   reason=error or 'quota exhausted (HTTP 402/429)')
            except Exception as e:
                logger.debug('[Slot] key_stats mark_key_exhausted failed: %s', e)
        elif is_rate_limit:
            try:
                from lib.key_stats import record_rate_limit
                just_exhausted = record_rate_limit(
                    self.provider_id, self.key_name,
                    reason=error or 'HTTP 429')
                if just_exhausted:
                    # Streak threshold tripped — stop hammering this key for
                    # an hour (the UI toggle / day rollover will revive it).
                    self.cooldown_until = time.time() + 3600
            except Exception as e:
                logger.debug('[Slot] key_stats record_rate_limit failed: %s', e)
        else:
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
