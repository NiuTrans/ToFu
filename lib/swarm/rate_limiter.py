"""lib/swarm/rate_limiter.py — Semaphore-based rate limiter for concurrent LLM calls.

Extracted from master.py for modularity.
"""

import random
import threading
import time
from typing import Optional

from lib.log import get_logger
from lib.swarm.protocol import SubAgentResult

logger = get_logger(__name__)


class RateLimiter:
    """Semaphore-based rate limiter for concurrent LLM calls.

    Wraps around sub-agent execution so we don't blow up the API with
    too many concurrent requests when we have many parallel agents.

    Enhanced with exponential back-off on 429 / rate-limit errors.
    """

    def __init__(self, max_concurrent: int = 8,
                 backoff_base: float = 1.0,
                 backoff_max: float = 30.0):
        self._semaphore = threading.Semaphore(max_concurrent)
        self._active = 0
        self._lock = threading.Lock()
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        # Shared backoff state for rate-limit events
        self._rate_limit_until = 0.0  # monotonic timestamp

    def acquire(self):
        """Acquire a slot, waiting if at capacity."""
        logger.debug('[RateLimiter] acquire: waiting for slot (active=%d)', self._active)
        self._semaphore.acquire()
        # Respect rate-limit backoff
        wait_until = self._rate_limit_until
        now = time.monotonic()
        if wait_until > now:
            wait_dur = wait_until - now
            logger.debug('[RateLimiter] acquire: rate-limit backoff %.1fs before proceeding', wait_dur)
            time.sleep(wait_dur)
        with self._lock:
            self._active += 1
            logger.debug('[RateLimiter] acquire: slot acquired (active=%d)', self._active)

    def release(self):
        """Release a slot."""
        with self._lock:
            self._active -= 1
            logger.debug('[RateLimiter] release: slot released (active=%d)', self._active)
        self._semaphore.release()

    def report_rate_limit(self):
        """Called when a 429/rate-limit error is received.

        Sets a shared backoff timestamp with exponential increase + jitter.
        """
        now = time.monotonic()
        current_wait = max(0, self._rate_limit_until - now)
        if current_wait < self._backoff_base:
            next_wait = self._backoff_base
        else:
            next_wait = min(current_wait * 2, self._backoff_max)
        next_wait += random.uniform(0, 1)
        self._rate_limit_until = now + next_wait
        logger.warning('[RateLimiter] Rate limit reported, backing off %.1fs', next_wait)

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def run_agent(self, agent) -> 'SubAgentResult':
        """Run an agent within the rate limit."""
        logger.debug('[RateLimiter] Acquiring slot for agent=%s (active=%d)',
                     agent.agent_id, self.active)
        self.acquire()
        logger.debug('[RateLimiter] Slot acquired for agent=%s (active=%d)',
                     agent.agent_id, self.active)
        try:
            return agent.run()
        finally:
            self.release()
            logger.debug('[RateLimiter] Slot released for agent=%s (active=%d)',
                         agent.agent_id, self.active)

    def run_agent_with_backoff(self, agent,
                               max_wait: float = 60.0,
                               max_attempts: int = 4) -> 'SubAgentResult':
        """Run an agent with exponential back-off on 429 / rate-limit errors.

        If ``agent.run()`` raises and the error message contains '429' or
        'rate' (case-insensitive), we back off and retry up to
        *max_attempts* times.  The back-off schedule is
        ``2^attempt + jitter`` seconds, capped at *max_wait*.
        """
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            self.acquire()
            try:
                return agent.run()
            except Exception as exc:
                err_str = str(exc).lower()
                is_rate = '429' in err_str or 'rate' in err_str
                if is_rate and attempt < max_attempts - 1:
                    self.report_rate_limit()
                    delay = min(2 ** attempt + random.uniform(0, 1), max_wait)
                    logger.warning(
                        '[RateLimiter] Rate-limited (attempt %d/%d), backing off %.1fs: %s',
                        attempt + 1, max_attempts, delay, exc,
                        exc_info=True)

                    time.sleep(delay)
                    last_exc = exc
                    continue
                raise
            finally:
                self.release()
        raise last_exc  # type: ignore[misc]
