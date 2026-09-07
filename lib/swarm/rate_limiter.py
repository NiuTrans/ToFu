"""lib/swarm/rate_limiter.py — Semaphore-based rate limiter for concurrent LLM calls.

Extracted from master.py for modularity.
"""

import threading
from collections.abc import Callable, Hashable

from lib.llm_errors import AbortedError
from lib.log import get_logger
from lib.swarm.execution_gate import (
    OwnerFairExecutionGate,
    process_swarm_execution_gate,
)
from lib.swarm.protocol import SubAgentResult

logger = get_logger(__name__)


class RateLimiter:
    """Semaphore-based rate limiter for concurrent LLM calls.

    Wraps around sub-agent execution so we don't blow up the API with
    too many concurrent requests when we have many parallel agents.
    """

    def __init__(self, max_concurrent: int = 8, *,
                 owner_key: Hashable | None = None,
                 execution_gate: OwnerFairExecutionGate | None = None):
        self._semaphore = threading.Semaphore(max_concurrent)
        self._owner_key = owner_key
        # Standalone users retain the historical local-only limiter.
        # Production masters always supply their explicit owner and therefore
        # cross the one process-wide expensive-execution gate.
        self._execution_gate = (
            execution_gate
            if execution_gate is not None else
            process_swarm_execution_gate() if owner_key is not None else None
        )
        self._active = 0
        self._lock = threading.Lock()

    @staticmethod
    def _raise_if_aborted(
        abort_check: Callable[[], bool] | None,
    ) -> None:
        if abort_check is not None and abort_check():
            raise AbortedError('swarm execution cancelled before admission')

    def acquire(
        self,
        *,
        abort_check: Callable[[], bool] | None = None,
    ):
        """Acquire a slot, waiting if at capacity."""
        logger.debug('[RateLimiter] acquire: waiting for slot (active=%d)', self._active)
        if abort_check is None:
            self._semaphore.acquire()
        else:
            while not self._semaphore.acquire(timeout=0.25):
                self._raise_if_aborted(abort_check)
            try:
                self._raise_if_aborted(abort_check)
            except BaseException:
                self._semaphore.release()
                raise
        try:
            if self._execution_gate is not None:
                self._execution_gate.acquire(
                    self._owner_key,
                    abort_check=abort_check,
                )
        except BaseException:
            self._semaphore.release()
            raise
        with self._lock:
            self._active += 1
            logger.debug('[RateLimiter] acquire: slot acquired (active=%d)', self._active)

    def release(self):
        """Release a slot."""
        with self._lock:
            self._active -= 1
            logger.debug('[RateLimiter] release: slot released (active=%d)', self._active)
        if self._execution_gate is not None:
            self._execution_gate.release(self._owner_key)
        self._semaphore.release()

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def run_agent(
        self,
        agent,
        *,
        abort_check: Callable[[], bool] | None = None,
    ) -> 'SubAgentResult':
        """Run an agent within the rate limit."""
        logger.debug('[RateLimiter] Acquiring slot for agent=%s (active=%d)',
                     agent.agent_id, self.active)
        self.acquire(abort_check=abort_check)
        logger.debug('[RateLimiter] Slot acquired for agent=%s (active=%d)',
                     agent.agent_id, self.active)
        try:
            return agent.run()
        finally:
            self.release()
            logger.debug('[RateLimiter] Slot released for agent=%s (active=%d)',
                         agent.agent_id, self.active)

