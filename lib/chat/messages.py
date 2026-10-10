"""Resolve conversation references used while building a human input turn."""

from __future__ import annotations

from lib.log import get_logger


logger = get_logger(__name__)


def resolve_conv_refs(conv_refs, *, user_id):
    """Expand ``{id, title}`` references into bounded conversation text."""
    if not conv_refs:
        return []
    from lib.conv_ref import get_conversation
    from lib.identity import require_user_id

    owner_user_id = require_user_id(
        user_id, context='resolve conversation references')

    results = []
    for reference in conv_refs:
        reference_id = reference.get("id", "")
        reference_title = reference.get("title", "")
        if not reference_id:
            continue
        try:
            resolved_text = get_conversation(
                conversation_id=reference_id,
                include_tool_details=False,
                user_id=owner_user_id,
            )
        except Exception as exc:
            logger.warning(
                "[Send] Failed to resolve conv ref %s: %s",
                reference_id[:12],
                exc,
            )
            resolved_text = f"[Error loading conversation: {exc}]"
        results.append({
            "id": reference_id,
            "title": reference_title,
            "text": resolved_text,
        })
    logger.info("[Send] Resolved %d conv refs", len(results))
    return results


__all__ = ["resolve_conv_refs"]
