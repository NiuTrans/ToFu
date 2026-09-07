"""Per-Feishu-user state and owner-scoped conversation turn persistence.

The bot keeps a bounded prompt cache in memory, but durable web history is
append-only turn authority. It never mirrors or replaces a conversation-sized
message array. External identities are mapped to application owners explicitly
at the integration boundary.
"""

from __future__ import annotations

import json
import os

from lib.feishu import _state as feishu_state
from lib.feishu.user_state import feishu_user_sessions
from lib.log import get_logger


logger = get_logger(__name__)


def get_history(user_id: str) -> list:
    """Return a copy of the bounded model-context cache."""
    return feishu_user_sessions.history(user_id)


def append_message(user_id: str, role: str, content: str) -> None:
    """Append one prompt-cache message and enforce its explicit bound."""
    feishu_user_sessions.append_message(user_id, role, content)


def clear_history(user_id: str) -> None:
    feishu_user_sessions.clear_history(user_id)


def new_conv_id(user_id: str) -> str:
    """Start a new durable conversation identity for one Feishu user."""
    return feishu_user_sessions.new_conversation_id(user_id)


def get_conv_id(user_id: str) -> str:
    return feishu_user_sessions.conversation_id(user_id)


def resolve_owner_user_id(feishu_user_id: str) -> int | None:
    """Resolve an external identity to an authenticated application owner.

    ``FEISHU_USER_OWNER_MAP`` is a JSON object of ``open_id`` to positive
    integer owner id. ``FEISHU_DEFAULT_OWNER_USER_ID`` may be used only when a
    deployment intentionally routes every allowed bot user to one owner.
    Missing/invalid mappings return ``None``; callers must never guess owner 1.
    """
    raw_map = os.getenv("FEISHU_USER_OWNER_MAP", "").strip()
    if raw_map:
        try:
            mapping = json.loads(raw_map)
            value = mapping.get(feishu_user_id) if isinstance(mapping, dict) else None
            owner_id = int(value) if value is not None else 0
            if owner_id > 0:
                return owner_id
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.error("[Feishu] FEISHU_USER_OWNER_MAP is invalid")
            return None
    default_owner = os.getenv("FEISHU_DEFAULT_OWNER_USER_ID", "").strip()
    if not default_owner:
        return None
    try:
        owner_id = int(default_owner)
    except ValueError:
        logger.error("[Feishu] FEISHU_DEFAULT_OWNER_USER_ID must be an integer")
        return None
    return owner_id if owner_id > 0 else None


def persist_exchange(
    feishu_user_id: str,
    user_message: dict,
    assistant_message: dict,
    *,
    owner_user_id: int,
) -> bool:
    """Append one completed Feishu exchange through canonical turn lifecycle."""
    from lib.turn_lifecycle import (
        LifecycleConflict,
        attempt_dispatch_lock,
        bind_task,
        claim_attempt_start,
        create_turn_pair,
        get_attempt,
        get_conversation_revision,
        record_task_event,
    )

    owner_id = int(owner_user_id)
    if owner_id < 1:
        raise ValueError("Feishu persistence requires a positive owner id")
    conversation_id = get_conv_id(feishu_user_id)
    submitted = dict(user_message)
    submitted.pop("role", None)
    submitted["_channel"] = "feishu"
    answer = dict(assistant_message)
    answer.pop("role", None)
    answer["_channel"] = "feishu"
    source_message_id = str(submitted.get("id") or submitted.get("_msgId") or "")
    if not source_message_id:
        raise ValueError("Feishu persistence requires a stable source message id")
    model = str(answer.get("model") or get_model(feishu_user_id) or "")
    config = {
        "model": model,
        "preset": model,
        "userId": owner_id,
        "_turnOwnerUserId": owner_id,
        "_externalChannel": "feishu",
    }

    try:
        result = create_turn_pair(
            conversation_id,
            command_id=f"feishu:{source_message_id}",
            input_projection=submitted,
            config=config,
            kind="feishu_reply",
            user_id=owner_id,
            input_actor="human",
            input_kind="feishu_input",
            conversation_defaults={
                "allowCreate": True,
                "title": str(submitted.get("content") or "Feishu")[:80],
                "settings": {"model": model, "source": "feishu"},
            },
        )
    except LifecycleConflict as exc:
        logger.warning(
            "[Feishu] conversation %s rejected exchange: %s",
            conversation_id[:12],
            exc.code,
        )
        return False

    attempt_id = result["attempt"]["attemptId"]
    with attempt_dispatch_lock(attempt_id):
        attempt = get_attempt(attempt_id, user_id=owner_id)
        if attempt.get("status") == "completed":
            return True
        if not claim_attempt_start(attempt_id, user_id=owner_id):
            logger.warning(
                "[Feishu] exchange attempt is not claimable "
                "attempt=%s status=%s",
                attempt_id[:12],
                attempt.get("status"),
            )
            return False

        synthetic_task_id = f"feishu:{attempt_id}"
        if bind_task(attempt_id, synthetic_task_id, user_id=owner_id) is None:
            logger.error(
                "[Feishu] could not bind exchange attempt=%s", attempt_id[:12]
            )
            return False
        task = {
            "id": synthetic_task_id,
            "convId": conversation_id,
            "_turnId": result["turn"]["turnId"],
            "_attemptId": attempt_id,
            "_userId": owner_id,
            "config": config,
            "content": str(answer.get("content") or ""),
            "thinking": str(answer.get("thinking") or ""),
            "segments": list(answer.get("segments") or []),
            "toolRounds": list(answer.get("toolRounds") or []),
            "model": model,
            "status": "done",
            "finishReason": "stop",
        }
        applied = record_task_event(
            task, {"type": "done", "finishReason": "stop"}
        )
        if not applied:
            logger.error(
                "[Feishu] terminal exchange write was rejected attempt=%s",
                attempt_id[:12],
            )
            return False
    try:
        from lib.conversations import notify_conv_changed

        notify_conv_changed(
            conversation_id,
            rev=get_conversation_revision(conversation_id, user_id=owner_id),
            user_id=owner_id,
        )
    except Exception:
        logger.debug("[Feishu] conversation notification failed", exc_info=True)
    return True


def get_model(user_id: str) -> str:
    from lib import LLM_MODEL
    return feishu_user_sessions.model(user_id, LLM_MODEL)


def set_model(user_id: str, model: str) -> None:
    feishu_user_sessions.set_model(user_id, model)


def get_mode(user_id: str) -> str:
    return feishu_user_sessions.mode(user_id)


def set_mode(user_id: str, mode: str) -> None:
    feishu_user_sessions.set_mode(user_id, mode)


def get_project(user_id: str) -> str:
    # Runtime config can replace the default without restarting the bot; read
    # the scalar from its owner rather than retaining an import-time copy.
    return feishu_user_sessions.project(
        user_id, feishu_state.DEFAULT_PROJECT_PATH)


def set_project(user_id: str, path: str) -> None:
    feishu_user_sessions.set_project(user_id, path)


def get_pending(user_id: str):
    return feishu_user_sessions.pending(user_id)


def set_pending(user_id: str, value) -> None:
    feishu_user_sessions.set_pending(user_id, value)


def clear_pending(user_id: str) -> None:
    feishu_user_sessions.clear_pending(user_id)


__all__ = [
    "append_message",
    "clear_history",
    "clear_pending",
    "get_conv_id",
    "get_history",
    "get_mode",
    "get_model",
    "get_pending",
    "get_project",
    "new_conv_id",
    "persist_exchange",
    "resolve_owner_user_id",
    "set_mode",
    "set_model",
    "set_pending",
    "set_project",
]
