"""Storage-independent repository boundary for conversation synchronization."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from lib.storage import StorageClient, get_storage_client
from lib.storage.errors import StorageError


class ConversationSyncRepository(Protocol):
    """All storage access needed by the snapshot/replay application service."""

    def snapshot(self, conversation_id: str, user_id: int) -> Mapping[str, Any] | None:
        ...

    def changes(
        self,
        conversation_id: str,
        user_id: int,
        *,
        after_sequence: int,
        limit: int,
    ) -> Mapping[str, Any] | None:
        ...


class SidecarConversationSyncRepository:
    """Semantic-operation adapter; SQL and backend dialect remain in sidecar."""

    def __init__(
        self,
        client_factory: Callable[..., StorageClient] = get_storage_client,
    ) -> None:
        self._client_factory = client_factory

    def _client(self) -> StorageClient:
        return self._client_factory(write=False)

    def snapshot(self, conversation_id: str, user_id: int) -> Mapping[str, Any] | None:
        result = self._client().query(
            "turn.sync.snapshot",
            {"conversation_id": conversation_id, "user_id": user_id},
        )
        if result is not None and not isinstance(result, Mapping):
            raise StorageError(
                "database_protocol_error", "Invalid conversation sync snapshot result"
            )
        return result

    def changes(
        self,
        conversation_id: str,
        user_id: int,
        *,
        after_sequence: int,
        limit: int = 500,
    ) -> Mapping[str, Any] | None:
        result = self._client().query(
            "turn.sync.changes",
            {
                "conversation_id": conversation_id,
                "user_id": user_id,
                "after": int(after_sequence),
                "limit": int(limit),
            },
        )
        if result is not None and not isinstance(result, Mapping):
            raise StorageError(
                "database_protocol_error", "Invalid conversation sync change result"
            )
        return result


__all__ = ["ConversationSyncRepository", "SidecarConversationSyncRepository"]
