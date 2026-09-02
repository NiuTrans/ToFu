"""Application service for authoritative snapshots and ordered replay."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lib.boot_identity import BOOT_ID
from lib.conversation_sync.cursor import (
    ConversationCursorError,
    decode_cursor,
    encode_cursor,
)
from lib.conversation_sync.generated_contract import STREAM_POLICY
from lib.conversation_sync.repository import ConversationSyncRepository
from lib.conversation_sync.snapshot_query import ConversationSnapshotQuery
from lib.conversation_sync.validation import decode
from lib.turn_projection_segments import public_value_with_stable_segments


class ConversationSyncNotFound(LookupError):
    pass


class ConversationSyncService:
    """Pure orchestration over an injected, user-scoped repository."""

    def __init__(
        self,
        repository: ConversationSyncRepository,
        *,
        snapshot_query_factory: Callable[
            [Callable[[str, int], dict[str, Any]]], ConversationSnapshotQuery
        ] = ConversationSnapshotQuery,
    ) -> None:
        self._repository = repository
        self._snapshot_query = snapshot_query_factory(
            self._load_authoritative_snapshot
        )

    @property
    def heartbeat_interval_ms(self) -> int:
        return int(STREAM_POLICY["heartbeatIntervalMs"])

    def snapshot(self, conversation_id: str, user_id: int,
                 *, push_withheld: bool = False) -> dict[str, Any]:
        return self._snapshot_query.read(
            conversation_id,
            user_id,
            push_withheld=push_withheld,
        )

    def _load_authoritative_snapshot(
        self, conversation_id: str, user_id: int
    ) -> dict[str, Any]:
        stored = self._repository.snapshot(conversation_id, user_id)
        if stored is None:
            raise ConversationSyncNotFound("Conversation not found")
        sequence = int(stored.get("syncSequence") or 0)
        response = {
            "ok": True,
            "contract": "tofu.conversation-sync.snapshot/v1",
            "conversationId": conversation_id,
            "conversationRevision": int(stored.get("conversationRevision") or 0),
            "syncSeq": sequence,
            "cursor": encode_cursor(conversation_id, user_id, sequence),
            "serverBootId": BOOT_ID,
            "heartbeatIntervalMs": self.heartbeat_interval_ms,
            "settings": dict(stored.get("settings") or {}),
            "turns": public_value_with_stable_segments(
                list(stored.get("turns") or [])
            ),
            "attempts": list(stored.get("attempts") or []),
            "queueItems": list(stored.get("queueItems") or []),
            # Read-side delivery-wedge signal (withheld authoritative pushes);
            # the query layer stamps the current request-local value after any
            # shared authority read.
            "pushWithheld": False,
        }
        return decode("ConversationSyncSnapshot", response)

    def sequence_from_cursor(
        self, conversation_id: str, user_id: int, cursor: str | None
    ) -> int:
        return decode_cursor(conversation_id, user_id, cursor)

    def cursor_for_sequence(
        self, conversation_id: str, user_id: int, sequence: int
    ) -> str:
        """Encode the public replay cursor for one authoritative sequence."""
        return encode_cursor(conversation_id, user_id, sequence)

    def changes(
        self,
        conversation_id: str,
        user_id: int,
        *,
        after_sequence: int,
        limit: int = 500,
    ) -> dict[str, Any]:
        stored = self._repository.changes(
            conversation_id,
            user_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        if stored is None:
            raise ConversationSyncNotFound("Conversation not found")
        head = int(stored.get("head") or 0)
        if stored.get("resetRequired"):
            reset = {
                "contract": "tofu.conversation-sync.event/v1",
                "type": "sync.reset_required",
                "conversationId": conversation_id,
                "cursor": encode_cursor(conversation_id, user_id, head),
                "reason": str(stored.get("resetReason") or "cursor_invalid"),
            }
            decode("SyncResetRequired", reset)
            return {"head": head, "events": [], "reset": reset, "hasMore": False}
        events = public_value_with_stable_segments(
            list(stored.get("events") or [])
        )
        for event in events:
            decode("ConversationChange", event)
        return {
            "head": head,
            "events": events,
            "reset": None,
            "hasMore": bool(stored.get("hasMore")),
        }

    def heartbeat(
        self,
        conversation_id: str,
        user_id: int,
        sequence: int,
        *,
        degraded: bool,
        push_withheld: bool = False,
    ) -> dict[str, Any]:
        heartbeat = {
            "contract": "tofu.conversation-sync.event/v1",
            "type": "sync.heartbeat",
            "conversationId": conversation_id,
            "cursor": encode_cursor(conversation_id, user_id, sequence),
            "serverBootId": BOOT_ID,
            "degraded": bool(degraded),
            # Always explicit: the post-wedge heartbeat is what clears the
            # client's wedge status back to the normal phase row.
            "pushWithheld": bool(push_withheld),
        }
        return decode("SyncHeartbeat", heartbeat)


__all__ = [
    "ConversationCursorError",
    "ConversationSyncNotFound",
    "ConversationSyncService",
]
