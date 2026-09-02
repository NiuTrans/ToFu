"""Owner-scoped conversation reads over the storage authority.

Responsibility
--------------
Expose conversation metadata and the main-lane transcript projection to
domain services. The storage sidecar remains the only runtime authority and
chooses SQLite or PostgreSQL behind its semantic protocol.

Entry points
------------
``get_conversation`` reads one owner-scoped snapshot.
``list_conversations`` reads a bounded, filtered snapshot set.
``scan_conversations_bounded`` lists lightweight candidates first, then
hydrates transcripts in small RPC batches so archive scans cannot build one
oversize storage frame.
``search_conversation_ids`` searches the authority without loading transcripts.

Dependencies
------------
Only ``lib.storage.get_storage_client`` and the stable storage error envelope.
Callers never receive a database connection, SQL row, archive blob, or
storage-mode switch.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Callable

from lib.storage.errors import StorageError


StorageClientFactory = Callable[..., Any]
_TRANSCRIPT_SCAN_BATCH_SIZE = 4
_TRANSCRIPT_SCAN_MAX_BATCH_SIZE = 32
_FRAME_TOO_LARGE_MESSAGE = "Storage frame exceeds the size limit"


def _owner_id(user_id: int) -> int:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be a positive integer")
    return user_id


def _conversation_id(conversation_id: str) -> str:
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError("conversation_id is required")
    return conversation_id.strip()


@dataclass(frozen=True, slots=True)
class ConversationSnapshot(Mapping[str, Any]):
    """Stable domain projection; metadata stays separate from transcript."""

    metadata: dict[str, Any]
    messages: list[dict[str, Any]]

    def __getitem__(self, key: str) -> Any:
        if key == "messages":
            return self.messages
        return self.metadata[key]

    def __iter__(self) -> Iterator[str]:
        yield from self.metadata
        yield "messages"

    def __len__(self) -> int:
        return len(self.metadata) + 1


class ConversationRepository:
    """Stateless adapter around the semantic storage protocol."""

    def __init__(self, client_factory: StorageClientFactory | None = None):
        self._client_factory = client_factory

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory(write=False)
        from lib.storage import get_storage_client
        return get_storage_client(write=False)

    @staticmethod
    def _snapshot(document: Mapping[str, Any]) -> ConversationSnapshot:
        metadata = document.get("metadata")
        messages = document.get("messages")
        if not isinstance(metadata, Mapping):
            raise RuntimeError("conversation metadata projection is malformed")
        if not isinstance(messages, list) or not all(
            isinstance(item, Mapping) for item in messages
        ):
            raise RuntimeError("conversation transcript projection is malformed")
        return ConversationSnapshot(
            metadata=dict(metadata),
            messages=[dict(item) for item in messages],
        )

    def get(
        self,
        conversation_id: str,
        *,
        user_id: int,
        include_messages: bool = True,
    ) -> ConversationSnapshot | None:
        document = self._client().query(
            "conversation.get",
            {
                "conv_id": _conversation_id(conversation_id),
                "user_id": _owner_id(user_id),
                "derive_messages": bool(include_messages),
            },
        )
        if document is None:
            return None
        if not include_messages:
            document = dict(document)
            document["messages"] = []
        return self._snapshot(document)

    def list(
        self,
        *,
        user_id: int,
        ids: list[str] | tuple[str, ...] | None = None,
        project_path: str | None = None,
        updated_at_gte: int | None = None,
        updated_at_gt: int | None = None,
        created_at_lt: int | None = None,
        order_by: str = "updated_at_desc",
        limit: int = 1000,
        include_messages: bool = True,
        settings_keys: list[str] | tuple[str, ...] | None = None,
    ) -> list[ConversationSnapshot]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if limit < 0 or limit > 10_000:
            raise ValueError("limit must be between 0 and 10000")
        payload: dict[str, Any] = {
            "user_id": _owner_id(user_id),
            "order_by": order_by,
            "limit": limit,
            "include_messages": bool(include_messages),
            "derive_messages": bool(include_messages),
        }
        if ids is not None:
            payload["ids"] = [
                _conversation_id(conversation_id) for conversation_id in ids
            ]
        if project_path is not None:
            if (
                not isinstance(project_path, str)
                or not project_path
                or len(project_path) > 4096
            ):
                raise ValueError("project_path must be a non-empty bounded string")
            payload["project_path"] = project_path
        if settings_keys is not None:
            if not all(
                isinstance(key, str) and key.strip() for key in settings_keys
            ):
                raise ValueError("settings_keys must contain non-empty strings")
            payload["settings_keys"] = [key.strip() for key in settings_keys]
        if updated_at_gte is not None:
            payload["updated_at_gte"] = int(updated_at_gte)
        if updated_at_gt is not None:
            payload["updated_at_gt"] = int(updated_at_gt)
        if created_at_lt is not None:
            payload["created_at_lt"] = int(created_at_lt)
        documents = self._client().query("conversation.list", payload) or []
        if not isinstance(documents, list):
            raise RuntimeError("conversation list projection is malformed")
        return [self._snapshot(document) for document in documents]

    def scan_bounded(
        self,
        *,
        user_id: int,
        updated_at_gte: int | None = None,
        updated_at_gt: int | None = None,
        created_at_lt: int | None = None,
        order_by: str = "updated_at_desc",
        limit: int = 1000,
        settings_keys: list[str] | tuple[str, ...] | None = None,
        batch_size: int = _TRANSCRIPT_SCAN_BATCH_SIZE,
    ) -> tuple[int, Iterator[ConversationSnapshot]]:
        """Return candidate count plus a lazy, frame-bounded transcript scan.

        The first request is metadata-only. Transcript bodies are then loaded
        by owner-scoped ID batches, so a month/report scan never asks the
        Sidecar to serialize an entire personal archive into one 64 MiB frame.
        A rare oversize batch is split recursively; an individual conversation
        that exceeds the protocol frame still fails loudly instead of yielding
        incomplete analytics.
        """
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= _TRANSCRIPT_SCAN_MAX_BATCH_SIZE
        ):
            raise ValueError(
                "batch_size must be an integer between 1 and "
                f"{_TRANSCRIPT_SCAN_MAX_BATCH_SIZE}"
            )
        metadata = self.list(
            user_id=user_id,
            updated_at_gte=updated_at_gte,
            updated_at_gt=updated_at_gt,
            created_at_lt=created_at_lt,
            order_by=order_by,
            limit=limit,
            include_messages=False,
            settings_keys=[],
        )
        conversation_ids = [str(row["id"]) for row in metadata]

        def snapshots() -> Iterator[ConversationSnapshot]:
            for start in range(0, len(conversation_ids), batch_size):
                batch = conversation_ids[start:start + batch_size]
                yield from self._hydrate_id_batch(
                    batch,
                    user_id=user_id,
                    settings_keys=settings_keys,
                )

        return len(conversation_ids), snapshots()

    def _hydrate_id_batch(
        self,
        conversation_ids: list[str],
        *,
        user_id: int,
        settings_keys: list[str] | tuple[str, ...] | None,
    ) -> Iterator[ConversationSnapshot]:
        """Hydrate one bounded ID batch, splitting only frame-size failures."""
        if not conversation_ids:
            return
        try:
            snapshots = self.list(
                user_id=user_id,
                ids=conversation_ids,
                limit=len(conversation_ids),
                include_messages=True,
                settings_keys=settings_keys,
            )
        except StorageError as exc:
            is_frame_limit = (
                exc.code == "database_protocol_error"
                and _FRAME_TOO_LARGE_MESSAGE in exc.message
            )
            if not is_frame_limit or len(conversation_ids) == 1:
                raise
            midpoint = len(conversation_ids) // 2
            yield from self._hydrate_id_batch(
                conversation_ids[:midpoint],
                user_id=user_id,
                settings_keys=settings_keys,
            )
            yield from self._hydrate_id_batch(
                conversation_ids[midpoint:],
                user_id=user_id,
                settings_keys=settings_keys,
            )
            return
        by_id = {str(snapshot["id"]): snapshot for snapshot in snapshots}
        for conversation_id in conversation_ids:
            snapshot = by_id.get(conversation_id)
            if snapshot is not None:
                yield snapshot

    def search_ids(
        self,
        query: str,
        *,
        user_id: int,
        limit: int = 200,
    ) -> list[str]:
        """Return owner-scoped matching ids without transcript payloads."""
        if not isinstance(query, str) or not query.strip():
            return []
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        hits = self._client().query(
            "conversation.search",
            {
                "query": query.strip(),
                "user_id": _owner_id(user_id),
                "limit": limit,
            },
        ) or []
        if not isinstance(hits, list):
            raise RuntimeError("conversation search projection is malformed")
        result: list[str] = []
        for hit in hits:
            if not isinstance(hit, Mapping):
                raise RuntimeError("conversation search hit is malformed")
            conversation_id = hit.get("id")
            if not isinstance(conversation_id, str) or not conversation_id:
                raise RuntimeError("conversation search hit has no id")
            result.append(conversation_id)
        return result


def get_conversation(
    conversation_id: str,
    *,
    user_id: int,
    include_messages: bool = True,
) -> ConversationSnapshot | None:
    return ConversationRepository().get(
        conversation_id,
        user_id=user_id,
        include_messages=include_messages,
    )


def list_conversations(
    *,
    user_id: int,
    ids: list[str] | tuple[str, ...] | None = None,
    project_path: str | None = None,
    updated_at_gte: int | None = None,
    updated_at_gt: int | None = None,
    created_at_lt: int | None = None,
    order_by: str = "updated_at_desc",
    limit: int = 1000,
    include_messages: bool = True,
    settings_keys: list[str] | tuple[str, ...] | None = None,
) -> list[ConversationSnapshot]:
    return ConversationRepository().list(
        user_id=user_id,
        ids=ids,
        project_path=project_path,
        updated_at_gte=updated_at_gte,
        updated_at_gt=updated_at_gt,
        created_at_lt=created_at_lt,
        order_by=order_by,
        limit=limit,
        include_messages=include_messages,
        settings_keys=settings_keys,
    )


def scan_conversations_bounded(
    *,
    user_id: int,
    updated_at_gte: int | None = None,
    updated_at_gt: int | None = None,
    created_at_lt: int | None = None,
    order_by: str = "updated_at_desc",
    limit: int = 1000,
    settings_keys: list[str] | tuple[str, ...] | None = None,
    batch_size: int = _TRANSCRIPT_SCAN_BATCH_SIZE,
) -> tuple[int, Iterator[ConversationSnapshot]]:
    """Scan filtered transcripts without constructing one archive-sized RPC."""
    return ConversationRepository().scan_bounded(
        user_id=user_id,
        updated_at_gte=updated_at_gte,
        updated_at_gt=updated_at_gt,
        created_at_lt=created_at_lt,
        order_by=order_by,
        limit=limit,
        settings_keys=settings_keys,
        batch_size=batch_size,
    )


def search_conversation_ids(
    query: str,
    *,
    user_id: int,
    limit: int = 200,
) -> list[str]:
    return ConversationRepository().search_ids(
        query,
        user_id=user_id,
        limit=limit,
    )


__all__ = [
    "ConversationRepository",
    "ConversationSnapshot",
    "get_conversation",
    "list_conversations",
    "scan_conversations_bounded",
    "search_conversation_ids",
]
