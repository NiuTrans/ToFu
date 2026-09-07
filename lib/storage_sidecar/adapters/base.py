"""Backend-neutral session and backend contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol


class Session(Protocol):
    backend: str
    turn_projection_cache: Any | None

    def lock_key(self, namespace: str, key: str) -> None: ...
    def index_exists(self, index_name: str) -> bool: ...
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int: ...
    def execute_many_exact(
        self, sql: str, params: Sequence[tuple[Any, ...]],
    ) -> int: ...
    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Mapping[str, Any] | None: ...
    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[Mapping[str, Any]]: ...
    def fetch_one_for_update_skip_locked(
        self, sql: str, params: tuple[Any, ...] = (),
    ) -> Mapping[str, Any] | None: ...


Operation = Callable[[Session], Any]


def receipt_cacheable(response: Any) -> bool:
    """Whether a command response may be memoized as a receipt.

    Domain refusals (``{'ok': False, ...}``) are pure reads: the op mutated
    nothing, so there is no exactly-once state to protect. Caching them
    would freeze a stale verdict — e.g. a ``board.delete`` refused for
    active dependents must be allowed to succeed once the dependent
    completes; replaying the cached refusal would strand it forever.
    Hard failures already skip the receipt via the StorageError/rollback
    path, so an ``ok=False`` return is precisely "clean refusal, no write".
    """
    if response is None:
        # Lease/claim operations conventionally return ``None`` when no row
        # was eligible.  That is a read-only miss, not a committed effect.
        # Memoizing it both grows the receipt table on every idle poll and can
        # freeze the miss if the same command is retried after work arrives.
        return False
    return not (isinstance(response, Mapping) and response.get('ok') is False)


class Backend(ABC):
    name: str

    def diagnostic_locator(self) -> dict[str, Any]:
        """Credential-free backend identity published in the project lease."""
        return {
            'format': 'tofu.storage-locator/v1',
            'backend': self.name,
        }

    @abstractmethod
    def start(self) -> dict[str, Any]: ...

    @abstractmethod
    def query(
        self, operation_name: str, operation: Operation, deadline_at: float,
    ) -> Any: ...

    @abstractmethod
    def command(
        self,
        operation_name: str,
        payload_digest: str,
        command_id: str | None,
        priority: str,
        operation: Operation,
        deadline_at: float,
        *,
        receipt_required: bool,
        transaction_timeout_s: float | None = None,
    ) -> Any: ...

    @abstractmethod
    def health(self) -> dict[str, Any]: ...

    @abstractmethod
    def metrics(self) -> dict[str, Any]: ...

    @abstractmethod
    def integrity_check(self, deadline_at: float) -> dict[str, Any]: ...

    @abstractmethod
    def backup(self, deadline_at: float) -> dict[str, Any]: ...

    @abstractmethod
    def baseline(self, deadline_at: float) -> dict[str, Any]: ...

    @abstractmethod
    def close(self) -> None: ...


__all__ = ['Backend', 'Operation', 'Session']
