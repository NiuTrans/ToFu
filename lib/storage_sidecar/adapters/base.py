"""Backend-neutral session and backend contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any, Protocol


class Session(Protocol):
    backend: str

    def lock_key(self, namespace: str, key: str) -> None: ...
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int: ...
    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Mapping[str, Any] | None: ...
    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[Mapping[str, Any]]: ...


Operation = Callable[[Session], Any]


class Backend(ABC):
    name: str

    @abstractmethod
    def start(self) -> dict[str, Any]: ...

    @abstractmethod
    def query(self, operation: Operation, deadline_at: float) -> Any: ...

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
    def close(self) -> None: ...


__all__ = ['Backend', 'Operation', 'Session']
