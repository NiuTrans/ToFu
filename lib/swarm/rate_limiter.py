"""Child execution accounting and cancellation, without local rate gates."""

import threading
from collections.abc import Callable, Hashable

from lib.llm.llm_errors import AbortedError
from lib.log import get_logger
from lib.swarm.execution_gate import (
    SwarmExecutionAccounting,
    process_swarm_execution_gate,
)
from lib.swarm.protocol import SubAgentResult

logger = get_logger(__name__)


class RateLimiter:
    """Track child lifetime while leaving provider limits to provider I/O."""

    def __init__(self, max_concurrent: int = 8, *,
                 owner_key: Hashable | None = None,
                 execution_gate: SwarmExecutionAccounting | None = None):
        self._owner_key = owner_key
        # Production masters supply explicit owner identity for shared
        # accounting. Standalone compositions retain local lifecycle counts.
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
        """Enter immediately after the ordinary cancellation check."""
        self._raise_if_aborted(abort_check)
        if self._execution_gate is not None:
            self._execution_gate.acquire(self._owner_key, abort_check=abort_check)
        with self._lock:
            self._active += 1
            logger.debug('[RateLimiter] acquire: execution entered (active=%d)', self._active)

    def release(self):
        """Release one active execution."""
        with self._lock:
            if self._active <= 0:
                raise RuntimeError('child execution released without ownership')
            if self._execution_gate is not None:
                self._execution_gate.release(self._owner_key)
            self._active -= 1
            logger.debug('[RateLimiter] execution released (active=%d)', self._active)

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def run_agent(
        self,
        agent,
        *,
        abort_check: Callable[[], bool] | None = None,
        on_admitted: Callable[[], None] | None = None,
    ) -> 'SubAgentResult':
        """Run an agent with exact lifecycle release and cancellation checks."""
        logger.debug('[RateLimiter] Entering execution for agent=%s (active=%d)',
                     agent.agent_id, self.active)
        self.acquire(abort_check=abort_check)
        logger.debug('[RateLimiter] Execution entered for agent=%s (active=%d)',
                     agent.agent_id, self.active)
        try:
            self._raise_if_aborted(abort_check)
            if on_admitted is not None:
                on_admitted()
            # Observers may persist or send events; Stop can arrive in them.
            self._raise_if_aborted(abort_check)
            return agent.run()
        finally:
            self.release()
            logger.debug('[RateLimiter] Execution released for agent=%s (active=%d)',
                         agent.agent_id, self.active)

