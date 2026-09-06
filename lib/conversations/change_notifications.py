"""Owner-scoped wake hints for durable conversation synchronization.

The notification carries identity and an optional transcript revision only.
Conversation Sync v3 remains authoritative: clients use the hint to hydrate a
snapshot or event delta, so lost/duplicated notifications never lose data.
"""

from __future__ import annotations

from lib.log import get_logger


logger = get_logger(__name__)


def notify_conv_changed(
    conversation_id: str,
    *,
    rev=None,
    deleted: bool = False,
    user_id: int,
) -> None:
    """Publish one best-effort, owner-scoped conversation wake hint."""
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError("conversation_id is required")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be a positive integer")
    try:
        from lib.agent_core.events import build_push_frame
        from lib.agent_core.push import push_event

        frame_fields = {"convId": conversation_id, "userId": user_id}
        if rev is not None:
            try:
                frame_fields["rev"] = int(rev)
            except (TypeError, ValueError):
                logger.debug(
                    "[ConversationNotify] conv=%s non-int rev=%r dropped",
                    conversation_id[:8],
                    rev,
                )
        payload = build_push_frame(
            "conv_deleted" if deleted else "conv_changed", **frame_fields)
        push_event("notify", conversation_id, payload, user_id=user_id)
    except Exception as exc:
        logger.debug(
            "[ConversationNotify] push skipped conv=%s: %s",
            conversation_id[:8],
            exc,
        )


__all__ = ["notify_conv_changed"]
