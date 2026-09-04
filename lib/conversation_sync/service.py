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
from lib.conversation_sync.turn_images import (
    ConversationTurnImage,
    ConversationTurnImageNotFound,
    ConversationTurnImageStale,
    decode_stored_turn_image,
    turn_image_owner_scope,
)
from lib.conversation_sync.validation import decode
from lib.turn_image_transport import MAX_TURN_IMAGES
from lib.turn_projection_segments import (
    public_value_with_stable_segments,
    snapshot_with_reference_tool_segments,
)


class ConversationSyncNotFound(LookupError):
    pass


class ConversationTurnPageStale(RuntimeError):
    def __init__(self, current_sync_sequence: int) -> None:
        super().__init__("Conversation history page cursor is stale")
        self.current_sync_sequence = current_sync_sequence


class ConversationSyncService:
    """Pure orchestration over an injected, user-scoped repository."""

    def __init__(
        self,
        repository: ConversationSyncRepository,
        *,
        snapshot_query_factory: Callable[
            [Callable[..., dict[str, Any]]], ConversationSnapshotQuery
        ] = ConversationSnapshotQuery,
    ) -> None:
        self._repository = repository
        self._snapshot_query = snapshot_query_factory(
            self._load_authoritative_snapshot
        )

    @property
    def heartbeat_interval_ms(self) -> int:
        return int(STREAM_POLICY["heartbeatIntervalMs"])

    def snapshot(
        self,
        conversation_id: str,
        user_id: int,
        *,
        push_withheld: bool = False,
        segment_payload: str = "full",
        turn_limit: int | None = None,
        include_artifact_hint: bool = False,
    ) -> dict[str, Any]:
        if segment_payload not in {"full", "refs"}:
            raise ValueError("Invalid conversation segment payload")
        if turn_limit is not None and not 1 <= turn_limit <= 256:
            raise ValueError("Invalid conversation snapshot turn limit")
        snapshot = self._snapshot_query.read(
            conversation_id,
            user_id,
            push_withheld=push_withheld,
            representation=segment_payload,
            project_representation=(
                snapshot_with_reference_tool_segments
                if segment_payload == "refs"
                else None
            ),
            project_representation_kwargs=(
                {
                    "owner_cache_scope": turn_image_owner_scope(
                        user_id, conversation_id
                    ),
                }
                if segment_payload == "refs"
                else None
            ),
            turn_limit=turn_limit,
            include_artifact_hint=include_artifact_hint,
        )
        # The shared authority has already passed generated-schema validation.
        # A refs flight only removes optional fields and introduces declared
        # references; its executable contract is pinned in projection tests.
        return snapshot

    def _load_authoritative_snapshot(
        self,
        conversation_id: str,
        user_id: int,
        turn_limit: int | None = None,
        *,
        include_artifact_hint: bool = False,
    ) -> dict[str, Any]:
        snapshot_options: dict[str, Any] = {}
        if turn_limit is not None:
            snapshot_options["turn_limit"] = turn_limit
        if include_artifact_hint:
            snapshot_options["include_artifact_hint"] = True
        stored = self._repository.snapshot(
            conversation_id,
            user_id,
            **snapshot_options,
        )
        if stored is None:
            raise ConversationSyncNotFound("Conversation not found")
        sequence = int(stored.get("syncSequence") or 0)
        # Legacy/read-adapter boundary: pre-presentation Turn rows remain
        # readable without teaching the browser a second identity state
        # machine. New storage rows already carry the explicit value; only an
        # absent/empty legacy field deterministically falls back to turnId.
        stored_turns: list[Any] = []
        for raw_turn in list(stored.get("turns") or []):
            if isinstance(raw_turn, dict) and not raw_turn.get("presentationId"):
                raw_turn = {
                    **raw_turn,
                    "presentationId": str(raw_turn.get("turnId") or ""),
                }
            stored_turns.append(raw_turn)
        response = {
            "ok": True,
            "contract": "tofu.conversation-sync.snapshot/v1",
            "scope": {
                "kind": "conversation",
                "ownerId": user_id,
                "threadId": conversation_id,
            },
            "conversationId": conversation_id,
            "conversationRevision": int(stored.get("conversationRevision") or 0),
            "syncSeq": sequence,
            "cursor": encode_cursor(conversation_id, user_id, sequence),
            "serverBootId": BOOT_ID,
            "heartbeatIntervalMs": self.heartbeat_interval_ms,
            "settings": dict(stored.get("settings") or {}),
            "turns": public_value_with_stable_segments(
                stored_turns
            ),
            "attempts": list(stored.get("attempts") or []),
            "queueItems": list(stored.get("queueItems") or []),
            # Read-side delivery-wedge signal (withheld authoritative pushes);
            # the query layer stamps the current request-local value after any
            # shared authority read.
            "pushWithheld": False,
        }
        if isinstance(stored.get("turnWindow"), dict):
            response["turnWindow"] = dict(stored["turnWindow"])
        if include_artifact_hint and isinstance(stored.get("hasArtifacts"), bool):
            response["hasArtifacts"] = stored["hasArtifacts"]
        return decode("ConversationSyncSnapshot", response)

    def turn_page(
        self,
        conversation_id: str,
        user_id: int,
        *,
        lane_id: str,
        expected_sync_sequence: int,
        before_ordinal: int | None = None,
        limit: int = 64,
        segment_payload: str = "full",
    ) -> dict[str, Any]:
        """Return one bounded historical lane page at an exact replay head."""
        if segment_payload not in {"full", "refs"}:
            raise ValueError("Invalid conversation segment payload")
        stored = self._repository.turn_page(
            conversation_id,
            user_id,
            lane_id=lane_id,
            expected_sync_sequence=expected_sync_sequence,
            before_ordinal=before_ordinal,
            limit=limit,
        )
        if stored is None:
            raise ConversationSyncNotFound("Conversation not found")
        if stored.get("stale") is True:
            raise ConversationTurnPageStale(
                int(stored.get("syncSequence") or 0)
            )
        sequence = int(stored.get("syncSequence") or 0)
        response = {
            "ok": True,
            "contract": "tofu.conversation-sync.turn-page/v1",
            "conversationId": conversation_id,
            "conversationRevision": int(
                stored.get("conversationRevision") or 0
            ),
            "syncSeq": sequence,
            "cursor": encode_cursor(conversation_id, user_id, sequence),
            "laneId": str(stored.get("laneId") or lane_id),
            "beforeOrdinal": int(stored.get("beforeOrdinal") or 0),
            "nextBeforeOrdinal": stored.get("nextBeforeOrdinal"),
            "hasMore": bool(stored.get("hasMore")),
            "totalTurns": int(stored.get("totalTurns") or 0),
            "turns": public_value_with_stable_segments(
                list(stored.get("turns") or [])
            ),
            "attempts": list(stored.get("attempts") or []),
        }
        if segment_payload == "refs":
            response = snapshot_with_reference_tool_segments(
                response,
                owner_cache_scope=turn_image_owner_scope(
                    user_id, conversation_id
                ),
            )
        return decode("ConversationTurnPage", response)

    def turn_image(
        self,
        conversation_id: str,
        turn_id: str,
        user_id: int,
        *,
        projection_revision: int,
        image_index: int,
    ) -> ConversationTurnImage:
        """Load and verify one immutable owner-scoped historical image."""
        if (
            not isinstance(conversation_id, str)
            or not conversation_id
            or len(conversation_id) > 256
            or not isinstance(turn_id, str)
            or not turn_id
            or len(turn_id) > 128
            or not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or user_id < 1
            or not isinstance(projection_revision, int)
            or isinstance(projection_revision, bool)
            or not 1 <= projection_revision <= 2**63 - 1
            or not isinstance(image_index, int)
            or isinstance(image_index, bool)
            or not 0 <= image_index < MAX_TURN_IMAGES
        ):
            raise ValueError("Invalid conversation Turn image request")
        stored = self._repository.turn_image(
            conversation_id,
            turn_id,
            user_id,
            projection_revision=projection_revision,
            image_index=image_index,
        )
        if stored is None:
            raise ConversationTurnImageNotFound("Conversation Turn image not found")
        if stored.get("stale") is True:
            raise ConversationTurnImageStale(
                int(stored.get("projectionRevision") or 0)
            )
        return decode_stored_turn_image(stored.get("base64"))

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
    "ConversationTurnPageStale",
    "ConversationSyncService",
]
