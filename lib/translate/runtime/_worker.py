"""Background worker for one asynchronous translation task.

The worker owns one ordering guarantee: for a conversation-bound translation,
the authoritative turn projection commits before the task becomes ``done`` or
a terminal push is emitted.  A closed tab therefore cannot turn a successful
translation into an unpersisted result.
"""

from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger

from ..commit import commit_translation_to_turn
from ..engine import _translate_freetext
from ..notranslate import (
    _extract_notranslate_blocks,
    _has_translatable_text,
    _reattach_notranslate_blocks,
    _reattach_notranslate_blocks_partial,
)
from ..prompt import _build_translate_prompt, _strip_notranslate_tags
from ..status import _format_status_message
from ._segments import _build_segment_translation_map
from ._state import _translate_runtime


logger = get_logger(__name__)


def _enrich_committed_turn_segments(
    task_id: str,
    conversation_id: str,
    turn_id: str,
    system_prompt: str,
    source: str,
    target: str,
    *,
    user_id: Any,
) -> None:
    """Best-effort segment overlay after whole-turn terminal settlement."""
    try:
        segment_translations = _build_segment_translation_map(
            conversation_id,
            turn_id,
            system_prompt,
            source,
            target,
            user_id=user_id,
        )
        if not segment_translations:
            return
        commit_translation_to_turn(
            conversation_id,
            turn_id,
            None,
            "",
            user_id=user_id,
            segment_translations=segment_translations,
        )
        logger.info(
            '[Translate] optional segment enrichment complete task=%s '
            'conv=%s turn=%s segments=%d',
            task_id[:8],
            conversation_id[:8],
            turn_id[:8],
            len(segment_translations),
        )
    except Exception as segment_error:
        logger.warning(
            '[Translate] optional segment enrichment failed after whole-turn '
            'terminal settlement task=%s conv=%s turn=%s: %s',
            task_id[:8],
            conversation_id[:8],
            turn_id[:8],
            segment_error,
        )


def _schedule_segment_enrichment(
    task_id: str,
    conversation_id: str,
    turn_id: str,
    system_prompt: str,
    source: str,
    target: str,
    *,
    user_id: Any,
) -> None:
    """Queue reconstructible enrichment without extending task lifetime."""
    from ..execution import submit_reconstructible_translation

    try:
        submit_reconstructible_translation(
            f'segments:{task_id}',
            owner_user_id=int(user_id),
            function=lambda: _enrich_committed_turn_segments(
                task_id,
                conversation_id,
                turn_id,
                system_prompt,
                source,
                target,
                user_id=user_id,
            ),
        )
    except Exception as admission_error:
        logger.warning(
            '[Translate] optional segment enrichment not admitted task=%s '
            'conv=%s turn=%s: %s',
            task_id[:8],
            conversation_id[:8],
            turn_id[:8],
            admission_error,
        )


def _do_translate(
    task_id: str,
    text: str,
    target: str,
    source: str,
    conversation_id: str,
    turn_id: str,
    field: str,
    *,
    user_id: Any,
    message_id: str = "",
) -> None:
    """Translate text and, when bound, commit it to ``turn_id``."""
    task = _translate_runtime.get(task_id)
    if not task:
        return

    bound_to_turn = bool(conversation_id or turn_id)
    if bound_to_turn and not (conversation_id and turn_id and user_id not in (None, "")):
        _settle_error(
            task,
            task_id,
            ValueError("conversation-bound translation requires conversationId, turnId, and userId"),
            conversation_id,
            turn_id,
            message_id,
            field,
        )
        return

    system_prompt = _build_translate_prompt(target, source)
    original_text = text
    input_length = len(text)

    def push_running(*, partial=None, partial_by_round=None,
                     status_message=None, status_kind=None):
        if not conversation_id:
            return
        frame = _frame_identity(
            conversation_id, turn_id, message_id, field,
            type="running", status="running",
        )
        if partial is not None:
            frame["partial"] = partial
        if partial_by_round:
            frame["partialByRound"] = partial_by_round
        if status_message:
            frame["statusMessage"] = status_message
            frame["statusKind"] = status_kind or ""
        _translate_runtime.append_event(task_id, frame)

    def on_status(event):
        message = _format_status_message(event)
        updated = _translate_runtime.update_fields(
            task_id,
            fields={
                "statusMessage": message,
                "statusKind": event.get("kind", ""),
                "statusUpdatedAt": time.time(),
            },
            only_if_status="running",
        )
        if updated:
            push_running(
                status_message=message,
                status_kind=event.get("kind", ""),
            )

    last_partial_at = 0.0
    protected_blocks = []

    def on_progress(partial):
        nonlocal last_partial_at
        now = time.time()
        if now - last_partial_at < 0.10:
            return
        last_partial_at = now
        visible_partial = _reattach_notranslate_blocks_partial(
            partial, protected_blocks,
        )
        updated = _translate_runtime.update_fields(
            task_id,
            fields={"partial": visible_partial, "partialUpdatedAt": now},
            only_if_status="running",
        )
        if updated:
            push_running(partial=visible_partial)

    push_running(status_kind="started")
    try:
        translatable_text, protected_blocks = _extract_notranslate_blocks(text)
        if protected_blocks and not _has_translatable_text(translatable_text):
            translated = _strip_notranslate_tags(original_text)
            model = "skipped"
        else:
            translated, usage = _translate_freetext(
                translatable_text,
                system_prompt,
                source=source,
                target=target,
                status_cb=on_status,
                progress_cb=on_progress,
                abort_check=task['abort_event'].is_set,
            )
            translated = (translated or "").strip()
            if protected_blocks:
                translated = _reattach_notranslate_blocks(
                    translated, protected_blocks
                ).strip()
            dispatch = usage.get("_dispatch", {}) if isinstance(usage, dict) else {}
            model = dispatch.get("model") or (
                usage.get("model") if isinstance(usage, dict) else None
            ) or "unknown"

        if not translated:
            raise RuntimeError("Translation produced empty content")

        # The whole-turn translation is authoritative output. Commit it before
        # reconstructible per-segment enrichment so an enrichment outage can
        # neither erase a successful result nor delay its durability.
        if bound_to_turn:
            commit_translation_to_turn(
                conversation_id,
                turn_id,
                field,
                translated,
                user_id=user_id,
                model=model,
            )

        frame = _frame_identity(
            conversation_id,
            turn_id,
            message_id,
            field,
            type="done",
            status="done",
            translated=translated,
            model=model,
        )
        _translate_runtime.update_fields(
            task_id,
            fields={"model": model},
            remove_fields=(
                "statusMessage", "statusKind", "statusUpdatedAt",
                "partial", "partialUpdatedAt",
            ),
            only_if_status="running",
        )
        _translate_runtime.finish(
            task_id,
            result=translated,
            terminal_event_fields=frame,
        )
        logger.info(
            "[Translate] task=%s complete %d→%d chars conv=%s turn=%s model=%s",
            task_id[:8],
            input_length,
            len(translated),
            conversation_id[:8] if conversation_id else "-",
            turn_id[:8] if turn_id else "-",
            model,
        )
        if bound_to_turn and field == "translatedContent":
            _schedule_segment_enrichment(
                task_id,
                conversation_id,
                turn_id,
                system_prompt,
                source,
                target,
                user_id=user_id,
            )
    except Exception as exc:
        if task['abort_event'].is_set():
            _translate_runtime.finish(
                task_id,
                terminal_event_fields=_frame_identity(
                    conversation_id,
                    turn_id,
                    message_id,
                    field,
                    type="aborted",
                    status="aborted",
                ),
            )
            logger.info('[Translate] task=%s aborted', task_id[:8])
            return
        _settle_error(
            task,
            task_id,
            exc,
            conversation_id,
            turn_id,
            message_id,
            field,
        )


def _frame_identity(
    conversation_id: str,
    turn_id: str,
    message_id: str,
    field: str,
    **payload,
) -> dict[str, Any]:
    return {
        **payload,
        "convId": conversation_id or "",
        "turnId": turn_id or "",
        # Renderer correlation only. Persistence never resolves by this value.
        "msgId": message_id or "",
        "field": field,
    }


def _settle_error(
    task: dict[str, Any],
    task_id: str,
    exc: Exception,
    conversation_id: str,
    turn_id: str,
    message_id: str,
    field: str,
) -> None:
    from lib.error_envelope import from_exception
    from lib.translate.errors import (
        TranslationNoAdmissibleProvider,
        TranslationProviderQueueFull,
    )

    if isinstance(exc, TranslationProviderQueueFull):
        kind = 'server_busy'
    elif isinstance(exc, TranslationNoAdmissibleProvider):
        kind = 'no_slot'
    else:
        kind = None

    envelope = from_exception(
        exc,
        model=task.get("model", "") or "",
        context="translate",
        source="lib.translate.runtime",
        kind=kind,
    )
    logger.error("[Translate] task=%s failed: %s", task_id[:8], exc, exc_info=True)
    _translate_runtime.finish(
        task_id,
        error=envelope,
        error_context="translate",
        terminal_event_fields=_frame_identity(
            conversation_id,
            turn_id,
            message_id,
            field,
            type="error",
            status="error",
            errorMessage=str(exc)[:300],
        ),
    )


__all__ = ["_do_translate"]
