"""Owner-scoped conversation reads over the storage authority.

Responsibility
--------------
Expose conversation metadata and the main-lane transcript projection to
domain services. The storage sidecar remains the only runtime authority and
chooses SQLite or PostgreSQL behind its semantic protocol.

Entry points
------------
``get_conversation`` reads one owner-scoped full, metadata-only, or bounded-page
snapshot.
``list_conversations`` reads a bounded, filtered snapshot set.
``ConversationRepository.list_catalog_page`` reads one metadata page plus its
authoritative total without materializing the owner's complete archive.
``count_conversation_activity_intervals`` counts activity through timestamp-
only authority projections rather than hydrating transcript content.
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
_ACTIVITY_DATE_MAX_INTERVALS = 366
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


@dataclass(frozen=True, slots=True)
class ConversationCatalogPage:
    """One bounded owner-scoped metadata page from a single read snapshot."""

    items: tuple[ConversationSnapshot, ...]
    total_count: int
    has_more: bool


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
        message_window: int | None = None,
        before_sequence: int | None = None,
    ) -> ConversationSnapshot | None:
        if message_window is not None:
            if (
                isinstance(message_window, bool)
                or not isinstance(message_window, int)
                or not 1 <= message_window <= 500
            ):
                raise ValueError("message_window must be between 1 and 500")
            if not include_messages:
                raise ValueError(
                    "message_window requires include_messages=True"
                )
        if before_sequence is not None:
            if (
                isinstance(before_sequence, bool)
                or not isinstance(before_sequence, int)
                or before_sequence < 0
            ):
                raise ValueError("before_sequence must be a non-negative integer")
            if message_window is None:
                raise ValueError("before_sequence requires message_window")
        payload = {
            "conv_id": _conversation_id(conversation_id),
            "user_id": _owner_id(user_id),
            "derive_messages": bool(include_messages),
        }
        if message_window is not None:
            payload["message_window"] = message_window
        if before_sequence is not None:
            payload["before_sequence"] = before_sequence
        document = self._client().query("conversation.get", payload)
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
        title_contains: str | None = None,
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
        if title_contains is not None:
            if (
                not isinstance(title_contains, str)
                or not title_contains.strip()
                or len(title_contains) > 512
            ):
                raise ValueError(
                    "title_contains must be non-empty bounded text"
                )
            payload["title_contains"] = title_contains.strip()
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

    def list_catalog_page(
        self,
        *,
        user_id: int,
        limit: int,
        folder_id: str | None = None,
        before_updated_at: int | None = None,
        before_id: str = "",
        settings_keys: list[str] | tuple[str, ...] | None = None,
    ) -> ConversationCatalogPage:
        """Read a cursor page and complete matching count in one Sidecar RPC."""
        owner_id = _owner_id(user_id)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("limit must be between 1 and 1000")
        normalized_folder_id = None
        if folder_id is not None:
            if (
                not isinstance(folder_id, str)
                or not folder_id.strip()
                or len(folder_id) > 512
            ):
                raise ValueError("folder_id must be a non-empty bounded string")
            normalized_folder_id = folder_id.strip()
        if before_updated_at is not None and (
            isinstance(before_updated_at, bool)
            or not isinstance(before_updated_at, int)
            or before_updated_at < 0
        ):
            raise ValueError("before_updated_at must be a non-negative integer")
        if not isinstance(before_id, str) or len(before_id) > 256:
            raise ValueError("before_id must be a bounded string")
        if before_updated_at is None and before_id:
            raise ValueError("before_id requires before_updated_at")

        payload: dict[str, Any] = {
            "user_id": owner_id,
            "catalog_page": True,
            "include_messages": False,
            "order_by": "updated_at_desc",
            "limit": limit,
            "before_id": before_id,
        }
        if normalized_folder_id is not None:
            payload["folder_id"] = normalized_folder_id
        if before_updated_at is not None:
            payload["before_updated_at"] = before_updated_at
        if settings_keys is not None:
            if not all(
                isinstance(key, str) and key.strip() for key in settings_keys
            ):
                raise ValueError("settings_keys must contain non-empty strings")
            payload["settings_keys"] = [key.strip() for key in settings_keys]

        result = self._client().query("conversation.list", payload)
        if not isinstance(result, Mapping):
            raise RuntimeError("conversation catalog page is malformed")
        documents = result.get("items")
        total_count = result.get("total_count")
        has_more = result.get("has_more")
        if not isinstance(documents, list) or not all(
            isinstance(document, Mapping) for document in documents
        ):
            raise RuntimeError("conversation catalog items are malformed")
        if (
            isinstance(total_count, bool)
            or not isinstance(total_count, int)
            or total_count < len(documents)
            or len(documents) > limit
        ):
            raise RuntimeError("conversation catalog total is malformed")
        if not isinstance(has_more, bool):
            raise RuntimeError("conversation catalog cursor is malformed")
        return ConversationCatalogPage(
            items=tuple(self._snapshot(document) for document in documents),
            total_count=total_count,
            has_more=has_more,
        )

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

    def activity_counts(
        self,
        *,
        user_id: int,
        updated_at_gte: int,
        day_boundaries_ms: list[int] | tuple[int, ...],
        created_at_lt: int | None = None,
        limit: int = 10_000,
    ) -> tuple[int, list[int]]:
        """Count distinct active conversations in explicit time intervals."""
        if (
            isinstance(updated_at_gte, bool)
            or not isinstance(updated_at_gte, int)
        ):
            raise ValueError("updated_at_gte must be an integer")
        if created_at_lt is not None and (
            isinstance(created_at_lt, bool) or not isinstance(created_at_lt, int)
        ):
            raise ValueError("created_at_lt must be an integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise ValueError("limit must be between 1 and 10000")
        boundaries = list(day_boundaries_ms)
        if (
            not 2 <= len(boundaries) <= _ACTIVITY_DATE_MAX_INTERVALS + 1
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in boundaries
            )
            or any(
                left >= right
                for left, right in zip(boundaries, boundaries[1:])
            )
        ):
            raise ValueError("day_boundaries_ms must be strictly increasing")
        payload: dict[str, Any] = {
            "user_id": _owner_id(user_id),
            "updated_at_gte": updated_at_gte,
            "day_boundaries_ms": boundaries,
            "limit": limit,
        }
        if created_at_lt is not None:
            payload["created_at_lt"] = created_at_lt
        result = self._client().query("conversation.activity_dates", payload)
        if not isinstance(result, Mapping):
            raise RuntimeError("conversation activity projection is malformed")
        candidate_count = result.get("candidate_count")
        counts = result.get("counts")
        if (
            isinstance(candidate_count, bool)
            or not isinstance(candidate_count, int)
            or not 0 <= candidate_count <= limit
            or not isinstance(counts, list)
            or len(counts) != len(boundaries) - 1
            or any(
                isinstance(count, bool)
                or not isinstance(count, int)
                or not 0 <= count <= candidate_count
                for count in counts
            )
        ):
            raise RuntimeError("conversation activity projection is malformed")
        return candidate_count, list(counts)

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
    message_window: int | None = None,
    before_sequence: int | None = None,
) -> ConversationSnapshot | None:
    return ConversationRepository().get(
        conversation_id,
        user_id=user_id,
        include_messages=include_messages,
        message_window=message_window,
        before_sequence=before_sequence,
    )


def list_conversations(
    *,
    user_id: int,
    ids: list[str] | tuple[str, ...] | None = None,
    project_path: str | None = None,
    title_contains: str | None = None,
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
        title_contains=title_contains,
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


def count_conversation_activity_intervals(
    *,
    user_id: int,
    updated_at_gte: int,
    day_boundaries_ms: list[int] | tuple[int, ...],
    created_at_lt: int | None = None,
    limit: int = 10_000,
) -> tuple[int, list[int]]:
    """Count activity without projecting message content across the RPC."""
    return ConversationRepository().activity_counts(
        user_id=user_id,
        updated_at_gte=updated_at_gte,
        day_boundaries_ms=day_boundaries_ms,
        created_at_lt=created_at_lt,
        limit=limit,
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
    "ConversationCatalogPage",
    "ConversationRepository",
    "ConversationSnapshot",
    "get_conversation",
    "list_conversations",
    "scan_conversations_bounded",
    "count_conversation_activity_intervals",
    "search_conversation_ids",
]
