"""Storage-independent repository boundary for conversation synchronization."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from lib.log import get_logger
from lib.storage import StorageClient, get_storage_client
from lib.storage.errors import StorageError


logger = get_logger(__name__)


class ConversationSyncRepository(Protocol):
    """All storage access needed by the snapshot/replay application service."""

    def snapshot(
        self,
        conversation_id: str,
        user_id: int,
        *,
        turn_limit: int | None = None,
        include_artifact_hint: bool = False,
        max_projection_bytes: int = 0,
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

    def round_directory(
        self, conversation_id: str, user_id: int, *, lane_id: str,
        expected_sync_sequence: int, offset: int = 0, limit: int = 50,
        anchor_turn_id: str = "",
    ) -> Mapping[str, Any] | None: ...

    def turn_page(
        self,
        conversation_id: str,
        user_id: int,
        *,
        lane_id: str,
        expected_sync_sequence: int,
        before_ordinal: int | None,
        limit: int,
        max_projection_bytes: int = 0,
    ) -> Mapping[str, Any] | None:
        ...

    def turn(self, conversation_id: str, turn_id: str, user_id: int) -> Mapping[str, Any] | None:
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

    def _query_window(
        self, operation: str, payload: dict[str, Any], limit_key: str,
    ) -> Any:
        """Reduce only bounded reads after native resource exhaustion.

        The successful storage transaction owns the page, attempts, replay head,
        and continuation cursor. Never slice a response or assemble several
        snapshots: that would lose history or mix authoritative revisions.
        """
        client = self._client()
        while True:
            try:
                return client.query(operation, payload)
            except StorageError as exc:
                limit = payload.get(limit_key)
                if (exc.code != "database_unavailable"
                        or exc.message != "database_resource_exhausted"
                        or not isinstance(limit, int) or limit <= 1):
                    raise
                # The authority conflates byte-bound exhaustion with pressure.
                # A smaller page reduces both; the geometric descent is finite.
                # Full/branched snapshots remain loud rather than hiding lanes.
                next_limit = max(1, limit // 2)
                logger.warning(
                    "Conversation read reducing window op=%s count=%s next_count=%s",
                    operation, limit, next_limit,
                )
                payload = {**payload, limit_key: next_limit}

    def snapshot(
        self,
        conversation_id: str,
        user_id: int,
        *,
        turn_limit: int | None = None,
        include_artifact_hint: bool = False,
        max_projection_bytes: int = 0,
    ) -> Mapping[str, Any] | None:
        payload: dict[str, Any] = {
            "conversation_id": conversation_id,
            "user_id": user_id,
        }
        if turn_limit is not None:
            payload["turn_limit"] = int(turn_limit)
        if include_artifact_hint:
            payload["include_artifact_hint"] = True
        if max_projection_bytes:
            payload["max_projection_bytes"] = int(max_projection_bytes)
        result = self._query_window(
            "turn.sync.snapshot", payload, "turn_limit",
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

    def round_directory(
        self, conversation_id: str, user_id: int, *, lane_id: str,
        expected_sync_sequence: int, offset: int = 0, limit: int = 50,
        anchor_turn_id: str = "",
    ) -> Mapping[str, Any] | None:
        return self._client().query("turn.sync.round_directory", {
            "conversation_id": conversation_id, "user_id": user_id, "lane_id": lane_id,
            "sync_sequence": expected_sync_sequence, "offset": offset, "limit": limit,
            "anchor_turn_id": anchor_turn_id,
        })

    def turn_page(
        self,
        conversation_id: str,
        user_id: int,
        *,
        lane_id: str,
        expected_sync_sequence: int,
        before_ordinal: int | None,
        limit: int = 64,
        max_projection_bytes: int = 0,
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
        if max_projection_bytes:
            payload["max_projection_bytes"] = int(max_projection_bytes)
        result = self._query_window("turn.sync.page", payload, "limit")
        if result is not None and not isinstance(result, Mapping):
            raise StorageError(
                "database_protocol_error",
                "Invalid conversation sync turn page result",
            )
        return result

    def turn(self, conversation_id: str, turn_id: str, user_id: int) -> Mapping[str, Any] | None:
        result = self._client().query("turn.get", {
            "conversation_id": conversation_id, "turn_id": turn_id, "user_id": user_id,
        })
        if result is not None and not isinstance(result, Mapping):
            raise StorageError("database_protocol_error", "Invalid conversation Turn result")
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
