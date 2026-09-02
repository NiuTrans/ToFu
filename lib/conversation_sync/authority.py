"""Read the conversation transcript authority through the storage boundary.

Routes, queue dispatch, translation, and task workers use this module instead
of inferring authority from settings or archive contents.  User identity is an
explicit argument so the predicate remains valid when authentication expands.
"""

from __future__ import annotations

from typing import Any


def conversation_has_turns(conversation_id: str, *, user_id: Any) -> bool:
    """Return whether the conversation has any authoritative turn rows."""
    from lib.storage import get_storage_client

    return bool(get_storage_client().query(
        "turn.exists",
        {"conversation_id": conversation_id, "user_id": user_id},
    ))


__all__ = ["conversation_has_turns"]
