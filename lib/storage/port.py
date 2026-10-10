"""Backend-neutral application storage port.

Repositories consume semantic operations from the generated operation catalog.
Adapters own transport, error translation and committed-event delivery; runtime
owns readiness and write fencing. No driver, lifecycle or wire imports belong here.
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class StoragePort(Protocol):
    """Shared contract implemented by storage.v1 and native storage.v2 adapters.

    Deadlines are relative seconds. Owners travel in semantic payloads and must
    match authenticated scope. Commands retain their caller's idempotency key;
    ambiguous writes must never be silently replayed or sent to another backend.
    All adapter failures use StorageError. Maintenance may mutate durable state.
    """

    @property
    def endpoint(self) -> tuple[str, int]: ...

    def query(self, operation: str, payload: Mapping[str, Any] | None = None,
              deadline: float | None = None) -> Any: ...

    def command(self, operation: str, payload: Mapping[str, Any] | None,
                command_id: str | None, priority: str = 'user',
                deadline: float | None = None) -> Any: ...

    def health(self, deadline: float | None = 2.0) -> Any: ...

    def metrics(self, deadline: float | None = 2.0) -> Any: ...

    def maintenance(self, operation: str, payload: Mapping[str, Any] | None = None,
                    deadline: float | None = 30.0) -> Any: ...
