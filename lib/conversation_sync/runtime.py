"""Runtime composition for the authoritative conversation command service.

HTTP handlers and background queue dispatch share this composition root.
Message construction, executor startup, and pending-abort storage remain
injected ports; no route module is imported here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from lib.conversation_sync.command_service import ConversationTurnCommandService


def _build_user_message(
    message: Mapping[str, Any], config: Mapping[str, Any], conversation_id: str,
    user_id: Any,
) -> Any:
    from lib.chat.turn_builder import build_user_msg_from_payload

    return build_user_msg_from_payload(
        dict(message), dict(config), conv_id=conversation_id, user_id=user_id
    )


def _was_aborted_after(
    conversation_id: str, user_id: Any, started_at: float
) -> bool:
    from lib.conversation_sync.pending_abort import was_pending_abort_after

    return was_pending_abort_after(conversation_id, user_id, started_at)


def _start_task(
    conversation_id: str,
    config: dict[str, Any],
    request_data: Mapping[str, Any],
    abort_after_ts: float | None,
    on_task_registered: Callable[[str], None],
) -> tuple[str, Any]:
    from lib.conversation_sync.task_start import (
        start_conversation_attempt_executor,
    )

    return start_conversation_attempt_executor(
        conversation_id,
        config,
        abort_after_ts=abort_after_ts,
        on_task_registered=on_task_registered,
    )


def _mutate_file_changes(
    operation: str,
    task_id: str,
    conversation_id: str,
    user_id: Any,
) -> Mapping[str, Any]:
    """Resolve and mutate one executor task's project at the backend edge."""
    from lib.project_mod import (
        redo_task_modifications,
        resolve_base_path,
        undo_task_modifications,
    )

    # TODO(enterprise): project modification journals need user-scoped
    # ownership before multi-user deployment.  Keep identity explicit here so
    # that upgrade is confined to this gateway instead of HTTP/UI callers.
    del user_id
    project_path = (
        resolve_base_path(task_id=task_id)
        or resolve_base_path(conv_id=conversation_id)
    )
    if not project_path:
        raise ValueError("No project is recorded for this turn")
    mutate = (
        undo_task_modifications
        if operation == "undo"
        else redo_task_modifications
    )
    result = mutate(project_path, task_id)
    if not isinstance(result, Mapping):
        raise ValueError("Project file command returned an invalid result")
    outcome = dict(result)
    if outcome.get("ok") is False:
        raise ValueError(str(outcome.get("error") or "Project file command failed"))
    return outcome


def _retain_media_attachments(
    projection: Mapping[str, Any], user_id: Any,
) -> None:
    attachments = projection.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return
    from lib.media_attachments import resolve_client_refs

    resolve_client_refs(attachments, user_id=int(user_id), retain=True)


conversation_turn_commands = ConversationTurnCommandService(
    build_user_message=_build_user_message,
    was_aborted_after=_was_aborted_after,
    start_task=_start_task,
    mutate_file_changes=_mutate_file_changes,
    retain_attachments=_retain_media_attachments,
)


__all__ = ["conversation_turn_commands"]
