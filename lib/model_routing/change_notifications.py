"""Owner-scoped wake hint for model-routing authority commits.

The frame carries owner identity and the committed aggregate revision only.
The revisioned HTTP aggregate (GET /api/v4/model-routing) remains the single
authority: clients treat the hint as a trigger to re-fetch, so a lost or
duplicated notification never loses data.
"""

from __future__ import annotations

from lib.log import get_logger


logger = get_logger(__name__)

MODEL_ROUTING_PUSH_TASK_ID = "__model_routing__"


def notify_model_routing_changed(*, user_id: int, rev=None) -> None:
    """Publish one best-effort, owner-scoped model-routing wake hint."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be a positive integer")
    try:
        from lib.agent_core.events import build_push_frame
        from lib.agent_core.push import push_event

        frame_fields = {"userId": user_id}
        if rev is not None:
            try:
                revision = int(rev)
            except (TypeError, ValueError):
                logger.debug(
                    "[ModelRoutingNotify] non-int rev=%r dropped", rev)
            else:
                if revision > 0:
                    frame_fields["rev"] = revision
        payload = build_push_frame("model_routing.changed", **frame_fields)
        push_event(
            "notify", MODEL_ROUTING_PUSH_TASK_ID, payload, user_id=user_id)
    except Exception as exc:
        logger.debug("[ModelRoutingNotify] push skipped: %s", exc)


__all__ = ["MODEL_ROUTING_PUSH_TASK_ID", "notify_model_routing_changed"]
