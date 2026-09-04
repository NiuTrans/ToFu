"""Storage-independent repository boundary for conversation synchronization."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from lib.storage import StorageClient, get_storage_client
from lib.storage.errors import StorageError


class ConversationSyncRepository(Protocol):
    """All storage access needed by the snapshot/replay application service."""

    def snapshot(
        self,
        conversation_id: str,
        user_id: int,
        *,
        turn_limit: int | None = None,
        include_artifact_hint: bool = False,
    ) -> Mapping[str, Any] | None:
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

    def turn_page(
        self,
        conversation_id: str,
        user_id: int,
        *,
        lane_id: str,
        expected_sync_sequence: int,
        before_ordinal: int | None,
        limit: int,
    ) -> Mapping[str, Any] | None:
        ...

    def turn_image(
        self,
        conversation_id: str,
        turn_id: str,
        user_id: int,
        *,
        projection_revision: int,
        image_index: int,
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

    def snapshot(
        self,
        conversation_id: str,
        user_id: int,
        *,
        turn_limit: int | None = None,
        include_artifact_hint: bool = False,
    ) -> Mapping[str, Any] | None:
        payload: dict[str, Any] = {
            "conversation_id": conversation_id,
            "user_id": user_id,
        }
        if turn_limit is not None:
            payload["turn_limit"] = int(turn_limit)
        if include_artifact_hint:
            payload["include_artifact_hint"] = True
        result = self._client().query(
            "turn.sync.snapshot",
            payload,
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

    def turn_page(
        self,
        conversation_id: str,
        user_id: int,
        *,
        lane_id: str,
        expected_sync_sequence: int,
        before_ordinal: int | None,
        limit: int = 64,
    ) -> Mapping[str, Any] | None:
        payload: dict[str, Any] = {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "lane_id": lane_id,
            "sync_sequence": int(expected_sync_sequence),
            "limit": int(limit),
        }
        if before_ordinal is not None:
            payload["before_ordinal"] = int(before_ordinal)
        result = self._client().query("turn.sync.page", payload)
        if result is not None and not isinstance(result, Mapping):
            raise StorageError(
                "database_protocol_error",
                "Invalid conversation sync turn page result",
            )
        return result

    def turn_image(
        self,
        conversation_id: str,
        turn_id: str,
        user_id: int,
        *,
        projection_revision: int,
        image_index: int,
    ) -> Mapping[str, Any] | None:
        result = self._client().query(
            "turn.image.get",
            {
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "user_id": user_id,
                "projection_revision": int(projection_revision),
                "image_index": int(image_index),
            },
        )
        if result is not None and not isinstance(result, Mapping):
            raise StorageError(
                "database_protocol_error",
                "Invalid conversation Turn image result",
            )
        return result


__all__ = ["ConversationSyncRepository", "SidecarConversationSyncRepository"]
