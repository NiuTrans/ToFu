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
``ConversationRepository.get_complete`` reads one whole transcript at one
revision, in message windows when it exceeds any single storage response.
``list_conversations`` reads a bounded, filtered snapshot set.
``ConversationRepository.list_catalog_page`` reads one metadata page plus its
authoritative total without materializing the owner's complete archive.
``count_conversation_activity_intervals`` counts activity through timestamp-
only authority projections rather than hydrating transcript content.
``scan_conversation_turn_usage`` lists lightweight candidates, then reads each
one's per-Turn usage rows (``conversation.turn_usage``) for a time window;
no transcript content crosses the RPC.
``scan_conversations_bounded`` lists lightweight candidates first, then
hydrates transcripts in small RPC batches so archive scans cannot build one
oversize storage frame. Given a time bound, a transcript too large for any
single response is paged backward in message windows to that bound instead.
``search_conversation_ids`` searches the authority without loading transcripts.

Dependencies
------------
Only ``lib.storage.get_storage_client``, the stable storage error envelope and
``lib.log``.
Callers never receive a database connection, SQL row, archive blob, or
storage-mode switch.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Callable

from lib.agent_core.storage_errors import StorageError, is_resource_bound_refusal
from lib.log import get_logger


logger = get_logger(__name__)
StorageClientFactory = Callable[..., Any]
_TRANSCRIPT_SCAN_BATCH_SIZE = 4
_TRANSCRIPT_SCAN_MAX_BATCH_SIZE = 32
_ACTIVITY_DATE_MAX_INTERVALS = 366
MAX_MESSAGE_WINDOW = 500


def _is_response_too_large(exc: StorageError) -> bool:
    """SQL frame-limit or native response-budget refusal of one read."""
    return is_resource_bound_refusal(exc)


def _timestamped_before(message: Mapping[str, Any], bound_ms: int) -> bool:
    timestamp = message.get("timestamp")
    return (
        not isinstance(timestamp, bool)
        and isinstance(timestamp, (int, float))
        and 0 < timestamp < bound_ms
    )


def _message_page_start(
    document: Mapping[str, Any],
    message_count: int,
    before_sequence: int | None,
) -> int:
    """Validate one window's ``message_page`` and return its first sequence."""
    page = document.get("message_page")
    if not isinstance(page, Mapping):
        raise RuntimeError("conversation message page is malformed")
    start, end, total = page.get("start"), page.get("end"), page.get("total_count")
    if (
        any(isinstance(value, bool) or not isinstance(value, int)
            for value in (start, end, total))
        or not 0 <= start <= end <= total
        or end - start != message_count
    ):
        raise RuntimeError("conversation message page is malformed")
    # Sequences are append-stable, so a window must end exactly at the cursor;
    # a shorter transcript was truncated between windows.
    if end != (total if before_sequence is None else before_sequence):
        raise RuntimeError(
            "conversation transcript changed during a windowed read")
    return start


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
    """Stable domain projection; metadata stays separate from transcript.

    ``message_start`` is the absolute main-lane sequence of ``messages[0]``:
    0 for a complete transcript; the first retained sequence when an
    oversized transcript was read back in windows (``metadata.msg_count``
    still counts the whole transcript). Consumers that place a message by
    its position in the full conversation add it to the local index.
    """

    metadata: dict[str, Any]
    messages: list[dict[str, Any]]
    message_start: int = 0

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
class AnchoredMessageWindow:
    """One bounded transcript window placed around a main-lane Turn.

    ``snapshot.message_start`` is the absolute main-lane sequence of the first
    message; ``anchor_sequence`` is the Turn's sequence, or None when it is
    not a main-lane Turn of the conversation (then the window is empty).
    """

    snapshot: ConversationSnapshot
    total_count: int
    anchor_sequence: int | None


@dataclass(frozen=True, slots=True)
class ConversationTurnUsage:
    """One conversation's per-Turn usage projection; no transcript content.

    ``turns`` holds the usage-bearing main-lane messages whose Turn time is
    in the requested window, plus archive messages whose time is unknown;
    each row carries ``index`` (its main-lane sequence), ``usage``,
    ``timestamp`` when known and the attribution fields it stored (``model``,
    ``preset``, ``effort``, ``provider_id``, ``providerId``). ``message_count``
    counts the whole main lane. ``title_fallback`` is the first message's
    content prefix when the conversation has no title and one was requested.
    """

    metadata: dict[str, Any]
    message_count: int
    title_fallback: str
    turns: list[dict[str, Any]]


def _turn_usage_projection(result: Any) -> ConversationTurnUsage:
    malformed = RuntimeError("conversation turn usage projection is malformed")
    if not isinstance(result, Mapping):
        raise malformed
    metadata = result.get("metadata")
    message_count = result.get("message_count")
    title_fallback = result.get("title_fallback")
    turns = result.get("turns")
    if (
        not isinstance(metadata, Mapping)
        or not isinstance(metadata.get("id"), str)
        or not isinstance(metadata.get("title"), str)
        or not isinstance(metadata.get("settings"), Mapping)
        or any(isinstance(metadata.get(field), bool)
               or not isinstance(metadata.get(field), int)
               for field in ("created_at", "updated_at"))
        or isinstance(message_count, bool)
        or not isinstance(message_count, int)
        or message_count < 0
        or not isinstance(title_fallback, str)
        or not isinstance(turns, list)
    ):
        raise malformed
    for turn in turns:
        index = turn.get("index") if isinstance(turn, Mapping) else None
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < message_count
            or not isinstance(turn.get("usage"), Mapping)
        ):
            raise malformed
    return ConversationTurnUsage(
        metadata=dict(metadata),
        message_count=message_count,
        title_fallback=title_fallback,
        turns=[dict(turn) for turn in turns],
    )


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
        document = self._get_document(
            conversation_id,
            user_id=user_id,
            include_messages=include_messages,
            message_window=message_window,
            before_sequence=before_sequence,
        )
        if document is None:
            return None
        if not include_messages:
            document = dict(document)
            document["messages"] = []
        return self._snapshot(document)

    def get_anchored_window(
        self,
        conversation_id: str,
        *,
        user_id: int,
        turn_id: str,
        message_window: int,
    ) -> AnchoredMessageWindow | None:
        """Read the window around one main-lane Turn (both authorities).

        The authority ends the window at ``S + 1 + message_window // 2`` for
        the Turn's sequence S. A returned anchor is verified against the
        delivered Turn identity, so a drifting projection fails loudly.
        """
        if not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 128:
            raise ValueError("turn_id must be a 1-128 character Turn ID")
        document = self._get_document(
            conversation_id,
            user_id=user_id,
            include_messages=True,
            message_window=message_window,
            before_sequence=None,
            anchor_turn_id=turn_id,
        )
        if document is None:
            return None
        snapshot = self._snapshot(document)
        page = document.get("message_page")
        anchor = page.get("anchor_sequence") if isinstance(page, Mapping) else None
        if (not isinstance(page, Mapping) or "anchor_sequence" not in page
                or (anchor is not None and (isinstance(anchor, bool)
                                            or not isinstance(anchor, int)))):
            raise RuntimeError("anchored conversation window is malformed")
        start, end, total = page.get("start"), page.get("end"), page.get("total_count")
        if (
            any(isinstance(value, bool) or not isinstance(value, int)
                for value in (start, end, total))
            or not 0 <= start <= end <= total
            or end - start != len(snapshot.messages)
            or (anchor is None and end != 0)
            or (anchor is not None and not (
                start <= anchor < end
                and snapshot.messages[anchor - start].get("_turnId") == turn_id))
        ):
            raise RuntimeError("anchored conversation window is malformed")
        return AnchoredMessageWindow(
            snapshot=ConversationSnapshot(
                metadata=snapshot.metadata, messages=snapshot.messages,
                message_start=start),
            total_count=total,
            anchor_sequence=anchor,
        )

    def _get_document(
        self,
        conversation_id: str,
        *,
        user_id: int,
        include_messages: bool,
        message_window: int | None,
        before_sequence: int | None,
        anchor_turn_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        if message_window is not None:
            if (
                isinstance(message_window, bool)
                or not isinstance(message_window, int)
                or not 1 <= message_window <= MAX_MESSAGE_WINDOW
            ):
                raise ValueError(
                    f"message_window must be between 1 and {MAX_MESSAGE_WINDOW}")
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
        if anchor_turn_id is not None:
            if message_window is None or before_sequence is not None:
                raise ValueError("anchor_turn_id requires message_window without before_sequence")
            payload["anchor_turn_id"] = anchor_turn_id
        return self._client().query("conversation.get", payload)

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
        oversized_messages_since_ms: int | None = None,
    ) -> tuple[int, Iterator[ConversationSnapshot]]:
        """Return candidate count plus a lazy, frame-bounded transcript scan.

        The first request is metadata-only. Transcript bodies are then loaded
        by owner-scoped ID batches, so a month/report scan never asks the
        Sidecar to serialize an entire personal archive into one 64 MiB frame.
        A rare oversize batch is split recursively; an individual conversation
        that exceeds the protocol frame still fails loudly instead of yielding
        incomplete analytics.

        ``oversized_messages_since_ms`` is a time-window contract for that
        last case: the single conversation is read backward in bounded
        message windows and its snapshot holds exactly the messages after the
        newest message timestamped before the bound (``msg_count`` still
        counts the whole transcript, and ``message_start`` is the absolute
        sequence of the first retained message). Sequence order is append
        order, so the unread older messages, including untimestamped ones,
        predate the bound. A window that cannot carry even one message fails
        loudly.
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
        if oversized_messages_since_ms is not None and (
            isinstance(oversized_messages_since_ms, bool)
            or not isinstance(oversized_messages_since_ms, int)
        ):
            raise ValueError("oversized_messages_since_ms must be an integer")
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
                    oversized_messages_since_ms=oversized_messages_since_ms,
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

    def turn_usage(
        self,
        conversation_id: str,
        *,
        user_id: int,
        timestamp_gte: int,
        timestamp_lt: int,
        title_fallback_characters: int = 0,
    ) -> ConversationTurnUsage | None:
        """Read one conversation's usage rows in ``[gte, lt)`` Turn time."""
        for value in (timestamp_gte, timestamp_lt, title_fallback_characters):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("turn usage bounds must be integers")
        if timestamp_gte >= timestamp_lt:
            raise ValueError("timestamp_gte must precede timestamp_lt")
        if not 0 <= title_fallback_characters <= 256:
            raise ValueError("title_fallback_characters must be between 0 and 256")
        result = self._client().query("conversation.turn_usage", {
            "user_id": _owner_id(user_id),
            "conv_id": _conversation_id(conversation_id),
            "timestamp_gte": timestamp_gte,
            "timestamp_lt": timestamp_lt,
            "title_fallback_characters": title_fallback_characters,
        })
        return None if result is None else _turn_usage_projection(result)

    def scan_turn_usage(
        self,
        *,
        user_id: int,
        updated_at_gte: int,
        created_at_lt: int,
        timestamp_gte: int,
        timestamp_lt: int,
        limit: int = 1000,
        title_fallback_characters: int = 0,
    ) -> tuple[int, Iterator[ConversationTurnUsage]]:
        """Candidate count plus a lazy per-conversation usage projection.

        Candidates are listed metadata-only; each one's usage rows are then
        read in its own bounded response. A conversation deleted after the
        listing is skipped; any other failure propagates.
        """
        metadata = self.list(
            user_id=user_id,
            updated_at_gte=updated_at_gte,
            created_at_lt=created_at_lt,
            order_by="updated_at_desc",
            limit=limit,
            include_messages=False,
            settings_keys=[],
        )
        conversation_ids = [str(row["id"]) for row in metadata]

        def projections() -> Iterator[ConversationTurnUsage]:
            for conversation_id in conversation_ids:
                usage = self.turn_usage(
                    conversation_id,
                    user_id=user_id,
                    timestamp_gte=timestamp_gte,
                    timestamp_lt=timestamp_lt,
                    title_fallback_characters=title_fallback_characters,
                )
                if usage is not None:
                    yield usage

        return len(conversation_ids), projections()

    def _hydrate_id_batch(
        self,
        conversation_ids: list[str],
        *,
        user_id: int,
        settings_keys: list[str] | tuple[str, ...] | None,
        oversized_messages_since_ms: int | None,
    ) -> Iterator[ConversationSnapshot]:
        """Split rejected batches; window or fail one oversized transcript."""
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
            if not _is_response_too_large(exc):
                raise
            if len(conversation_ids) > 1:
                midpoint = len(conversation_ids) // 2
                for half in (conversation_ids[:midpoint],
                             conversation_ids[midpoint:]):
                    yield from self._hydrate_id_batch(
                        half,
                        user_id=user_id,
                        settings_keys=settings_keys,
                        oversized_messages_since_ms=oversized_messages_since_ms,
                    )
                return
            if oversized_messages_since_ms is None:
                raise
            snapshot = self._read_messages_since(
                conversation_ids[0],
                user_id=user_id,
                since_ms=oversized_messages_since_ms,
                settings_keys=settings_keys,
            )
            if snapshot is not None:
                yield snapshot
            return
        by_id = {str(snapshot["id"]): snapshot for snapshot in snapshots}
        for conversation_id in conversation_ids:
            snapshot = by_id.get(conversation_id)
            if snapshot is not None:
                yield snapshot

    def _message_windows(
        self,
        conversation_id: str,
        *,
        user_id: int,
    ) -> Iterator[tuple[ConversationSnapshot, int, int]]:
        """Yield one oversized transcript's message windows newest-first.

        Each item is ``(window, start, total)``: one storage response, the
        absolute sequence of its first message, and the message count that
        response reported. Both authorities window by main-lane sequence:
        ``message_window=W`` with ``before_sequence=B`` returns sequences
        ``[max(0, B - W), B)`` in order (``B`` defaults to the message count)
        and reports them in ``message_page``, whose ``start`` is the next
        cursor. Neither shrinks a window to its response bound (native byte
        budget, SQL frame); both refuse it with the whole-transcript signal. A
        one-message tail window is read first: its ``message_page.total_count``
        is the authoritative total (a listing ``msg_count`` can be missing or
        stale), and the refused whole read that would exceed it is never
        attempted. Read windows therefore start at half of that total and
        halve on every refusal; a refused one-message window fails loudly.
        An empty transcript yields that probe; a conversation that disappears
        ends the iteration before the window starting at sequence 0.
        """
        document = self._get_document(
            conversation_id,
            user_id=user_id,
            include_messages=True,
            message_window=1,
            before_sequence=None,
        )
        if document is None:
            return
        probe = self._snapshot(document)
        # Validates the tail page and its authoritative total.
        _message_page_start(document, len(probe.messages), None)
        total = document["message_page"]["total_count"]
        if total == 0:
            yield probe, 0, 0
            return
        window = max(1, min(MAX_MESSAGE_WINDOW, total // 2))
        before_sequence = total
        while True:
            try:
                document = self._get_document(
                    conversation_id,
                    user_id=user_id,
                    include_messages=True,
                    message_window=window,
                    before_sequence=before_sequence,
                )
            except StorageError as exc:
                if not _is_response_too_large(exc) or window == 1:
                    raise
                window //= 2
                continue
            if document is None:
                return
            snapshot = self._snapshot(document)
            start = _message_page_start(
                document, len(snapshot.messages), before_sequence)
            yield snapshot, start, document["message_page"]["total_count"]
            if start == 0:
                return
            before_sequence = start

    def get_complete(
        self,
        conversation_id: str,
        *,
        user_id: int,
    ) -> ConversationSnapshot | None:
        """Read one whole transcript as one conversation revision.

        One storage response carries the transcript when it fits. A transcript
        above every single response (``is_resource_bound_refusal``) is read
        back through ``_message_windows`` instead and stitched only when every
        window reports the revision and message count of the first, whose end
        is the transcript's end. Windows are separate reads, and a write
        between them could otherwise combine two versions of the
        conversation; that fails as a ``database_conflict`` the caller retries
        as a whole. Any other failure keeps its original classification.
        """
        try:
            return self.get(conversation_id, user_id=user_id)
        except StorageError as exc:
            if not _is_response_too_large(exc):
                raise
        metadata: dict[str, Any] | None = None
        revision = total = None
        windows: list[list[dict[str, Any]]] = []
        for window, start, window_total in self._message_windows(
                conversation_id, user_id=user_id):
            if metadata is None:
                metadata, revision, total = (
                    window.metadata, window.metadata.get("rev"), window_total)
                if isinstance(revision, bool) or not isinstance(revision, int):
                    raise RuntimeError("conversation revision is malformed")
                changed = start + len(window.messages) != total
            else:
                changed = (window.metadata.get("rev") != revision
                           or window_total != total)
            if changed:
                raise StorageError(
                    "database_conflict",
                    "conversation changed during a windowed read",
                    operation_id="conversation.get",
                )
            windows.append(window.messages)
            if start == 0:
                break
        else:
            return None
        messages = [message for page in reversed(windows) for message in page]
        logger.info(
            "[Conversations] %s exceeds one storage response; read all %d "
            "messages at revision %d in %d windows",
            conversation_id, len(messages), revision, len(windows) + 1,
        )
        return ConversationSnapshot(metadata=metadata, messages=messages)

    def _read_messages_since(
        self,
        conversation_id: str,
        *,
        user_id: int,
        since_ms: int,
        settings_keys: list[str] | tuple[str, ...] | None,
    ) -> ConversationSnapshot | None:
        """Read one oversized transcript back to a time bound.

        Windows come newest-first from ``_message_windows``; reading stops at
        the newest message timestamped before ``since_ms``.
        """
        metadata: dict[str, Any] | None = None
        end = 0
        suffix: list[dict[str, Any]] = []
        windows = 0
        for window, start, _total in self._message_windows(
                conversation_id, user_id=user_id):
            window_messages = window.messages
            windows += 1
            if metadata is None:
                metadata = window.metadata
                end = start + len(window_messages)
            boundary = next(
                (index for index in range(len(window_messages) - 1, -1, -1)
                 if _timestamped_before(window_messages[index], since_ms)),
                None,
            )
            # Cut at the boundary inside the window too, so the suffix
            # never depends on the window size that happened to fit.
            suffix[:0] = (
                window_messages
                if boundary is None
                else window_messages[boundary + 1:]
            )
            if boundary is not None or start == 0:
                break
        else:
            return None
        messages = suffix
        if end:
            logger.info(
                "[Conversations] %s exceeds one storage response; read the last "
                "%d of %s messages back to %d in %d windows",
                conversation_id, len(suffix), metadata.get("msg_count"),
                since_ms, windows + 1,
            )
        if settings_keys is not None:
            settings = metadata.get("settings")
            if not isinstance(settings, Mapping):
                raise RuntimeError("conversation settings projection is malformed")
            keys = [key.strip() for key in settings_keys]
            metadata["settings"] = {
                key: settings[key] for key in keys if key in settings
            }
        return ConversationSnapshot(
            metadata=metadata, messages=messages,
            message_start=end - len(messages),
        )

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
    oversized_messages_since_ms: int | None = None,
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
        oversized_messages_since_ms=oversized_messages_since_ms,
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


def scan_conversation_turn_usage(
    *,
    user_id: int,
    updated_at_gte: int,
    created_at_lt: int,
    timestamp_gte: int,
    timestamp_lt: int,
    limit: int = 1000,
    title_fallback_characters: int = 0,
) -> tuple[int, Iterator[ConversationTurnUsage]]:
    """Scan candidates' per-Turn usage rows without reading transcripts."""
    return ConversationRepository().scan_turn_usage(
        user_id=user_id,
        updated_at_gte=updated_at_gte,
        created_at_lt=created_at_lt,
        timestamp_gte=timestamp_gte,
        timestamp_lt=timestamp_lt,
        limit=limit,
        title_fallback_characters=title_fallback_characters,
    )


def search_conversation_page(query: str, *, user_id: int, limit: int = 200,
                             cursor: str | None = None) -> dict:
    """Owner-scoped v2 search page, including continuation and coverage."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError('search page limit must be between 1 and 200')
    page = ConversationRepository()._client().query('conversation.search', {
        'query': query.strip(), 'user_id': _owner_id(user_id),
        'limit': limit, 'page': True, 'cursor': cursor,
    })
    if (not isinstance(page, dict) or not isinstance(page.get('items'), list)
            or 'next_cursor' not in page or not isinstance(page.get('incomplete'), bool)
            or (page['next_cursor'] is not None and not isinstance(page['next_cursor'], str))):
        raise RuntimeError('conversation search page is malformed')
    for hit in page['items']:
        if not isinstance(hit, Mapping) or not isinstance(hit.get('id'), str) or not hit['id']:
            raise RuntimeError('conversation search hit is malformed')
        if not _search_hit_turn_is_valid(hit.get('turn_id'), hit.get('ordinal')):
            raise RuntimeError('conversation search hit turn is malformed')
    return page


def _search_hit_turn_is_valid(turn_id: Any, ordinal: Any) -> bool:
    """A hit names its live Turn and lane ordinal together, or neither."""
    if turn_id is None:
        return ordinal is None
    return (isinstance(turn_id, str) and bool(turn_id)
            and not isinstance(ordinal, bool) and isinstance(ordinal, int)
            and ordinal >= 0)


def get_anchored_conversation_window(
    conversation_id: str, *, user_id: int, turn_id: str, message_window: int,
) -> AnchoredMessageWindow | None:
    return ConversationRepository().get_anchored_window(
        conversation_id, user_id=user_id, turn_id=turn_id,
        message_window=message_window)


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
    "AnchoredMessageWindow",
    "ConversationCatalogPage",
    "ConversationRepository",
    "ConversationSnapshot",
    "ConversationTurnUsage",
    "get_conversation",
    "list_conversations",
    "scan_conversations_bounded",
    "scan_conversation_turn_usage",
    "count_conversation_activity_intervals",
    "get_anchored_conversation_window",
    "search_conversation_ids",

    "search_conversation_page",
]
