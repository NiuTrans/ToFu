"""Sidecar-backed persistence adapter used by the reusable agent core.

Transcript reads return a derived, v1-shaped view of normalized turns. Every
transcript mutation remains semantic and narrow: projection patch or atomic
turn compaction. This module intentionally exposes no whole-conversation
replace API.

Entry point: :class:`DefaultConversationStore`, resolved through
``lib.agent_core.store.get_conversation_store``.
Dependencies: semantic operations from ``lib.storage`` and the turn lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from lib.log import get_logger


logger = get_logger(__name__)

__all__ = ["DefaultConversationStore"]


def _storage_client(*, write: bool = False):
    from lib.storage import get_storage_client

    return get_storage_client(write=write)


def _turn_snapshot_is_native(messages) -> bool:
    """Return whether every visible transcript row has a stable turn id."""
    if not messages:
        return False
    identities = [
        isinstance(message, dict) and bool(message.get("_turnId"))
        for message in messages
    ]
    if any(identities) and not all(identities):
        raise ValueError("Hybrid turn/blob transcript cannot be compacted")
    return all(identities)


def _build_turn_compaction_payload(
    conv_id,
    current_messages,
    compacted_messages,
    expected_revision,
    *,
    command_id,
    user_id,
):
    """Map two derived snapshots to one narrow ``turn.compact`` command."""
    if not _turn_snapshot_is_native(current_messages):
        return None
    from lib.turn_projection_patch import normalize_projection_document

    current_by_id = {
        str(message["_turnId"]): message for message in current_messages
    }
    if len(current_by_id) != len(current_messages):
        raise ValueError("Duplicate turn identity in current transcript")

    desired_ids: list[str] = []
    summary_message = None
    summary_index = -1
    for index, message in enumerate(compacted_messages):
        if not isinstance(message, dict):
            raise ValueError("Compacted transcript messages must be objects")
        turn_id = str(message.get("_turnId") or "")
        if turn_id:
            if turn_id not in current_by_id or turn_id in desired_ids:
                raise ValueError("Compaction changed or duplicated a turn identity")
            desired_ids.append(turn_id)
            continue
        if (
            summary_message is not None
            or message.get("_isCompactionSummary") is not True
            or message.get("role") != "assistant"
        ):
            raise ValueError(
                "Compaction may insert exactly one synthetic assistant summary"
            )
        summary_message = message
        summary_index = index
    if summary_message is None:
        raise ValueError("Compacted transcript has no summary message")

    current_order = [str(message["_turnId"]) for message in current_messages]
    desired_id_set = set(desired_ids)
    if desired_ids != [
        turn_id for turn_id in current_order if turn_id in desired_id_set
    ]:
        raise ValueError("Compaction reordered retained turn identities")

    previous_turn_id = ""
    next_turn_id = ""
    if summary_index > 0:
        previous_turn_id = str(
            compacted_messages[summary_index - 1].get("_turnId") or ""
        )
    if summary_index + 1 < len(compacted_messages):
        next_turn_id = str(
            compacted_messages[summary_index + 1].get("_turnId") or ""
        )
    if not previous_turn_id and not next_turn_id:
        raise ValueError("Compaction summary has no retained insertion anchor")

    projection_updates = []
    for message in compacted_messages:
        turn_id = str(message.get("_turnId") or "")
        if not turn_id:
            continue
        before = current_by_id[turn_id]
        before_projection = normalize_projection_document(before)
        after_projection = normalize_projection_document(message)
        if before_projection == after_projection:
            continue
        projection_revision = before.get("_projectionRevision")
        if (
            not isinstance(projection_revision, int)
            or isinstance(projection_revision, bool)
            or projection_revision < 0
        ):
            raise ValueError("Turn projection revision is missing or invalid")
        projection_updates.append(
            {
                "turn_id": turn_id,
                "expected_projection_revision": projection_revision,
                "projection": after_projection,
            }
        )

    summary_turn_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"tofu:manual-compaction:{command_id}")
    )
    summary_projection = normalize_projection_document(summary_message)
    # Projection normalization deliberately strips runtime-only legacy flags.
    # ``turn.compact`` still requires this operation marker as proof that the
    # caller intentionally minted a synthetic summary, so restore it only on
    # the narrow compaction command wire.
    summary_projection["_isCompactionSummary"] = True
    return {
        "conversation_id": conv_id,
        "user_id": int(user_id),
        "expected_conversation_revision": int(expected_revision),
        "summary_turn_id": summary_turn_id,
        "summary_projection": summary_projection,
        "delete_turn_ids": [
            turn_id for turn_id in current_order if turn_id not in desired_id_set
        ],
        "projection_updates": projection_updates,
        "insert_after_turn_id": previous_turn_id,
        "insert_before_turn_id": next_turn_id,
    }


class DefaultConversationStore:
    """Stateless owner-scoped adapter over the storage sidecar."""

    def next_task_event_id(self, task_id, *, floor=0):
        """Allocate after the durable task-event tail."""
        latest = _storage_client().query("event.latest", {"task_id": str(task_id)})
        return max(int(floor), int((latest or {}).get("sequence", -1)) + 1)

    def load_transcript(self, conv_id, *, user_id):
        """Return ``(derived_messages, updated_at_ms, revision)`` or ``None``."""
        document = _storage_client().query(
            "conversation.get",
            {
                "conv_id": conv_id,
                "user_id": user_id,
                "derive_messages": True,
            },
        )
        if document is None:
            return None
        metadata = document.get("metadata") or {}
        return (
            list(document.get("messages") or []),
            int(metadata.get("updated_at") or 0),
            int(metadata.get("rev") or 0),
        )

    def compact_turn_transcript(
        self,
        conv_id,
        current_messages,
        compacted_messages,
        expected_revision,
        *,
        command_id,
        user_id,
    ):
        """Atomically apply a semantic compaction to normalized turns."""
        payload = _build_turn_compaction_payload(
            conv_id,
            current_messages,
            compacted_messages,
            expected_revision,
            command_id=command_id,
            user_id=user_id,
        )
        if payload is None:
            raise ValueError("Transcript has no normalized turn authority")
        result = _storage_client(write=True).command(
            "turn.compact", payload, command_id
        )
        return 1 if result.get("applied") else 0

    def archive_transcript(
        self,
        conv_id,
        messages,
        *,
        user_id,
        summary="",
        trigger="force",
        task_id="",
        round_num=0,
        model="",
        tokens_before=0,
        tokens_after=0,
        msgs_before=0,
        msgs_after=0,
        reason="",
        receipt=None,
    ):
        """Create one owner-scoped pre-compaction transcript archive."""
        # Time-prefixed opaque id gives archives a deterministic total order
        # even when several are created inside the same millisecond.
        archive_id = f"{time.time_ns():020d}_{uuid.uuid4().hex}"
        try:
            payload = {
                "archive_id": archive_id,
                "conversation_id": conv_id,
                "user_id": user_id,
                "messages": messages,
                "summary": summary or "",
                "receipt": receipt or {},
                "trigger": trigger,
                "task_id": task_id,
                "round_num": int(round_num or 0),
                "model": model,
                "tokens_before": int(tokens_before or 0),
                "tokens_after": int(tokens_after or 0),
                "msgs_before": int(msgs_before or len(messages)),
                "msgs_after": int(msgs_after or 0),
                "reason": (reason or "")[:500],
                "created_at_ms": int(time.time() * 1000),
            }
            _storage_client(write=True).command(
                "compaction_archive.create",
                payload,
                f"compaction-archive:create:{user_id}:{archive_id}",
            )
            return archive_id
        except Exception as exc:
            logger.warning(
                "[Store] archive create failed conv=%s: %s",
                conv_id[:8] if conv_id else "?",
                exc,
            )
            return None

    def list_compaction_archives(self, conv_id, *, user_id, limit=200):
        return _storage_client().query(
            "compaction_archive.list",
            {
                "conversation_id": conv_id,
                "user_id": user_id,
                "limit": int(limit),
            },
        )["archives"]

    def get_compaction_archive(
        self, conv_id, archive_id, *, user_id, include_messages=True,
    ):
        return _storage_client().query(
            "compaction_archive.get",
            {
                "conversation_id": conv_id,
                "archive_id": str(archive_id),
                "user_id": user_id,
                "include_messages": bool(include_messages),
            },
        )

    def update_archive_summary(
        self, archive_id, summary, tokens_after, msgs_after, *, user_id,
        receipt=None,
    ):
        receipt_json = (
            json.dumps(
                receipt, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            )
            if receipt is not None else ""
        )
        digest = hashlib.sha256(
            ((summary or "") + "\0" + receipt_json).encode("utf-8")
        ).hexdigest()[:16]
        payload = {
            "archive_id": str(archive_id),
            "user_id": user_id,
            "summary": summary or "",
            "tokens_after": int(tokens_after),
            "msgs_after": int(msgs_after),
        }
        if receipt is not None:
            payload["receipt"] = receipt
        return _storage_client(write=True).command(
            "compaction_archive.update_summary",
            payload,
            (
                f"compaction-archive:update:{user_id}:{archive_id}:"
                f"{tokens_after}:{msgs_after}:{digest}"
            ),
        )

    def delete_archives(self, conv_id, *, user_id):
        return _storage_client(write=True).command(
            "compaction_archive.delete_conversation",
            {"conversation_id": conv_id, "user_id": user_id},
            f"compaction-archive:delete:{user_id}:{conv_id}:{uuid.uuid4().hex}",
        )

    def prune_archives(self, conv_id, keep, *, user_id):
        if not conv_id or int(keep) <= 0:
            return 0
        result = _storage_client(write=True).command(
            "compaction_archive.prune",
            {
                "conversation_id": conv_id,
                "user_id": user_id,
                "keep": int(keep),
            },
            (
                f"compaction-archive:prune:{user_id}:{conv_id}:{int(keep)}:"
                f"{uuid.uuid4().hex}"
            ),
        )
        return int(result.get("deleted") or 0)

    def notify_conversation_changed(self, conv_id, *, user_id):
        """Publish an owner-scoped wake hint carrying the current revision."""
        from lib.conversations import notify_conv_changed

        document = _storage_client().query(
            "conversation.get", {"conv_id": conv_id, "user_id": user_id}
        )
        notify_conv_changed(
            conv_id,
            rev=((document or {}).get("metadata") or {}).get("rev"),
            user_id=user_id,
        )
