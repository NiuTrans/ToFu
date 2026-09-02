"""Schedule automatic translation from a committed terminal turn event.

This is the single server-side auto-translation entry point.  It runs only
after turn authority has accepted the terminal projection, then addresses all
user-visible generated turns by stable IDs.  The coordinator is detached so
language detection and model work never delay the chat terminal frame.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from lib.log import get_logger


logger = get_logger(__name__)


def schedule_terminal_turn_translations(task: dict[str, Any]) -> bool:
    """Start one detached coordinator for a newly settled executor task."""
    if not task.get("convId") or not task.get("_turnId"):
        return False
    if task.get("_terminalTranslationScheduled"):
        return False
    task["_terminalTranslationScheduled"] = True

    from lib.conv_config import resolve_auto_translate

    if not resolve_auto_translate(task.get("config") or {}):
        _cancel_incremental(task)
        return False

    threading.Thread(
        target=_translate_settled_turns,
        args=(task,),
        daemon=True,
        name=f"terminal-translate-{str(task['_turnId'])[:8]}",
    ).start()
    return True


def _translate_settled_turns(task: dict[str, Any]) -> None:
    from lib.conv_config import resolve_translate_target, target_lang_code
    from lib.tasks_pkg.manager._registry import task_user_id
    from lib.turn_lifecycle import get_turn

    conversation_id = str(task["convId"])
    root_turn_id = str(task["_turnId"])
    user_id = task_user_id(task)
    target = resolve_translate_target(task.get("config") or {})
    target_code = target_lang_code(target)

    candidate_ids = [root_turn_id]
    candidate_ids.extend(task.get("_turnVisibleRunTurnIds") or [])
    candidate_ids = list(dict.fromkeys(str(value) for value in candidate_ids if value))
    incremental_handoff = False

    # The terminal round's reasoning closed with the turn: queue it BEFORE
    # the finalize/stamp handoff so the accumulator drains it first and the
    # terminal commit pins it onto the ``thinking:terminal`` segment —
    # reasoning is immutable history the reader should not have to reopen
    # the conversation to see in the UI language. (``task['thinking']`` is
    # the same source ``assemble_segments`` stamps as that segment.)
    terminal_thinking = str(task.get("thinking") or "").strip()
    if terminal_thinking:
        try:
            from lib.translate.incremental import submit_thinking_segment

            submit_thinking_segment(task, "thinking:terminal", terminal_thinking)
        except Exception as exc:
            logger.debug(
                "[AutoTranslate] terminal thinking submit failed: %s", exc)

    try:
        for turn_id in candidate_ids:
            try:
                turn = get_turn(conversation_id, turn_id, user_id=user_id)
            except Exception as exc:
                logger.warning(
                    "[AutoTranslate] cannot read conv=%s turn=%s: %s",
                    conversation_id[:8], turn_id[:8], exc,
                )
                continue
            if turn.get("status") in {"pending", "running", "waiting_for_user"}:
                continue
            projection = turn.get("projection") or {}
            content = str(projection.get("content") or "").strip()
            if not content or projection.get("translatedContent"):
                continue

            from lib.text_lang import detect_language

            if detect_language(content, force_fasttext=True).code == target_code:
                from lib.translate.commit import mark_turn_translation_complete

                mark_turn_translation_complete(
                    conversation_id, turn_id, user_id=user_id)
                if turn_id == root_turn_id:
                    from lib.translate.incremental import (
                        finalize_incremental_stamp_only,
                    )

                    incremental_handoff = finalize_incremental_stamp_only(task)
                _push_noop(
                    conversation_id, turn_id,
                    str(task.get("_assistantMsgId") or "")
                    if turn_id == root_turn_id else "",
                    user_id=user_id,
                )
                continue

            if turn_id == root_turn_id:
                from lib.translate.incremental import finalize_incremental

                if finalize_incremental(task, content):
                    incremental_handoff = True
                    continue
            _spawn_whole_turn_translation(
                conversation_id=conversation_id,
                turn_id=turn_id,
                content=content,
                target=target,
                user_id=user_id,
                message_id=(str(task.get("_assistantMsgId") or "")
                            if turn_id == root_turn_id else ""),
            )
    except Exception as exc:
        logger.error(
            "[AutoTranslate] coordinator failed conv=%s: %s",
            conversation_id[:8], exc, exc_info=True,
        )
    finally:
        if not incremental_handoff:
            _cancel_incremental(task)


def _spawn_whole_turn_translation(
    *,
    conversation_id: str,
    turn_id: str,
    content: str,
    target: str,
    user_id: Any,
    message_id: str,
) -> None:
    from lib.translate import (
        _do_translate,
        _translate_runtime,
    )

    task_id = uuid.uuid4().hex[:12]
    task = _translate_runtime.create(
        user_id=int(user_id),
        task_id=task_id,
        meta={
            "convId": conversation_id,
            "turnId": turn_id,
            "msgId": message_id,
            "userId": user_id,
            "field": "translatedContent",
            "targetLang": target,
            "textLen": len(content),
        },
    )
    task.update({
        "status": "running",
        "result": None,
        "error": None,
        "model": None,
        "progress": None,
        "convId": conversation_id,
        "turnId": turn_id,
        "msgId": message_id,
        "userId": user_id,
        "field": "translatedContent",
        "targetLang": target,
        "textLen": len(content),
        "created_at": time.time(),
        "completed_at": None,
    })
    _translate_runtime.spawn(
        task_id,
        _do_translate,
        task_id,
        content,
        target,
        "English",
        conversation_id,
        turn_id,
        "translatedContent",
        user_id=user_id,
        message_id=message_id,
    )


def _push_noop(
    conversation_id: str,
    turn_id: str,
    message_id: str,
    *,
    user_id: Any,
) -> None:
    try:
        from lib.agent_core.push import push_event

        push_event("translate", f"noop-{uuid.uuid4().hex[:12]}", {
            "type": "done",
            "status": "done",
            "noop": True,
            "reason": "already_target",
            "model": "skipped",
            "convId": conversation_id,
            "turnId": turn_id,
            "msgId": message_id,
            "field": "translatedContent",
        }, user_id=user_id)
    except Exception as exc:
        logger.debug("[AutoTranslate] no-op push failed: %s", exc)


def _cancel_incremental(task: dict[str, Any]) -> None:
    try:
        from lib.translate.incremental import cancel_incremental

        cancel_incremental(task)
    except Exception as exc:
        logger.debug("[AutoTranslate] incremental cancel failed: %s", exc)


__all__ = ["schedule_terminal_turn_translations"]
