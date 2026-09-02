"""Turn-native Autopilot continuation at the conversation/task boundary.

The durable baton is an atomic virtual-user/input + assistant/output turn pair.
There is no messages-array append, parent pre-sync, or ``activeTaskId`` mirror.
The created attempt is claimed, bound to one executor, and only then spawned.

Entry points are imported by ``lib.tasks_pkg.autopilot``:
  * ``_has_pending_real_message``
  * ``_successor_already_running``
  * ``_append_conversation_autopilot_turns``
  * ``_start_followup_task``
"""

from __future__ import annotations

import time

from lib.conversation_sync.attempt_identity import is_conversation_attempt
from lib.log import audit_log, get_logger


logger = get_logger(__name__)


def _owner_user_id(task: dict) -> int:
    from lib.tasks_pkg.manager._registry import task_user_id

    owner_user_id = int(task_user_id(task))
    if owner_user_id < 1:
        raise ValueError("Autopilot requires an authenticated owner")
    return owner_user_id


def _has_pending_real_message(conv_id: str, *, user_id: int) -> bool:
    """Return whether a dispatchable human turn is waiting in the queue."""
    if not conv_id:
        return False
    try:
        from lib.message_queue import has_pending_human_turn

        return has_pending_human_turn(conv_id, user_id=int(user_id))
    except Exception:
        logger.warning(
            "[Autopilot] Human-turn queue probe failed conv=%s",
            conv_id[:8],
            exc_info=True,
        )
        return False


def _successor_already_running(task: dict, conv_id: str) -> bool:
    """Return whether a different live executor already owns the conversation.

    The latest-task map is only a routing cache.  A pointer to a terminal or
    already-discarded carrier is not ownership, so both map and live registry
    must agree before Autopilot stands down.
    """
    if not conv_id:
        return False
    from lib.tasks_pkg.manager.runtime import (
        _latest_task_for_conv,
        chat_task_runtime,
    )

    latest_task_id = _latest_task_for_conv(conv_id)
    if not latest_task_id or latest_task_id == task.get("id"):
        return False
    successor = chat_task_runtime.get(latest_task_id)
    if successor is None or successor.get("status") in {
        "done",
        "error",
        "aborted",
    } or int(successor.get('_userId') or 0) != int(task.get('_userId') or 0):
        return False
    logger.info(
        "[Autopilot] conv=%s already has live successor task=%s",
        conv_id[:8],
        latest_task_id[:8],
    )
    return True


def _append_conversation_autopilot_turns(
    task: dict,
    conv_id: str,
    vu_msg_id: str,
    text: str,
    rounds: list | None = None,
    run_id: str = "",
    segments: list | None = None,
) -> dict | None:
    """Atomically create the VU turn and its pending assistant successor."""
    if not is_conversation_attempt(task):
        logger.error(
            "[Autopilot] Refusing continuation for unbound task=%s conv=%s",
            str(task.get("id") or "")[:8],
            conv_id[:8],
        )
        return None
    parent_turn_id = str(task.get("_turnId") or "")
    attempt_id = str(task.get("_attemptId") or "")
    if not parent_turn_id or not attempt_id:
        logger.error("[Autopilot] Parent attempt identity is incomplete")
        return None

    try:
        from lib.turn_lifecycle import (
            announce_related_turns,
            create_turn_pair,
            get_turn,
        )

        owner_user_id = _owner_user_id(task)
        parent_turn = get_turn(
            conv_id, parent_turn_id, user_id=owner_user_id
        )
        config = dict(task.get("config") or {})
        config["userId"] = owner_user_id
        config["_turnOwnerUserId"] = owner_user_id
        submitted_projection = {
            "content": text,
            "thinking": "",
            "toolRounds": rounds or [],
            "segments": segments or [],
            "timestamp": int(time.time() * 1000),
            "_msgId": vu_msg_id,
            "_isVirtualUser": True,
        }
        from lib.turn_initiation import INITIATOR_AUTOPILOT, stamp_initiator

        stamp_initiator(submitted_projection, INITIATOR_AUTOPILOT)
        if run_id:
            submitted_projection["_autopilotRunId"] = run_id
        result = create_turn_pair(
            conv_id,
            command_id=f"autopilot:{attempt_id}:{vu_msg_id}",
            input_projection=submitted_projection,
            config=config,
            lane_id=parent_turn.get("laneId") or "main",
            parent_turn_id=parent_turn["turnId"],
            kind="autopilot_reply",
            output_actor="assistant",
            run_id=run_id,
            user_id=owner_user_id,
            input_actor="virtual_user",
            input_kind="autopilot_virtual_user",
            require_parent_is_lane_tail=True,
        )
        task["_autopilotNextAttempt"] = result
        task["_nextTurnId"] = result["turn"]["turnId"]
        task["_nextAttemptId"] = result["attempt"]["attemptId"]
        task["_turnVisibleRunTurnIds"] = list(
            dict.fromkeys(
                [
                    *(task.get("_turnVisibleRunTurnIds") or []),
                    result["submittedTurn"]["turnId"],
                ]
            )
        )
        announce_related_turns(
            attempt_id,
            [result["submittedTurn"]["turnId"], result["turn"]["turnId"]],
            user_id=owner_user_id,
        )
        return {
            "role": "user",
            **submitted_projection,
            "timestamp": result["submittedTurn"]["createdAt"],
            "_turnId": result["submittedTurn"]["turnId"],
        }
    except Exception as exc:
        conflict_code = getattr(exc, "code", None)
        if conflict_code:
            logger.info(
                "[Autopilot] Continuation stood down conv=%s: %s",
                conv_id[:8],
                conflict_code,
            )
            return None
        logger.error(
            "[Autopilot] Continuation creation failed conv=%s",
            conv_id[:8],
            exc_info=True,
        )
        return None


def _start_followup_task(task: dict, conv_id: str) -> str | None:
    """Claim, bind, and spawn the already-created successor attempt."""
    attempt_result = task.get("_autopilotNextAttempt")
    if not is_conversation_attempt(task) or not attempt_result:
        logger.error(
            "[Autopilot] Missing durable successor attempt task=%s conv=%s",
            str(task.get("id") or "")[:8],
            conv_id[:8],
        )
        return None

    from lib.error_envelope import make_envelope
    from lib.storage import get_storage_client
    from lib.tasks_pkg.manager import create_task, discard_task
    from lib.tasks_pkg.spawn import spawn_task
    from lib.turn_lifecycle import (
        bind_task,
        build_api_messages,
        claim_attempt_start,
        fail_start,
    )

    owner_user_id = _owner_user_id(task)
    attempt = attempt_result["attempt"]
    output_turn = attempt_result["turn"]
    attempt_id = attempt["attemptId"]
    config = dict(task.get("config") or {})
    for stale_key in (
        "excludeLast",
        "toolHistory",
        "contentPrefix",
        "checkpointToolRounds",
        "checkpointUsage",
        "checkpointApiRounds",
        "checkpointModifiedFiles",
        "checkpointModifiedFileList",
        "assistantMsgId",
        "msgId",
    ):
        config.pop(stale_key, None)
    config.update(
        {
            "_turnId": output_turn["turnId"],
            "_attemptId": attempt_id,
            "_turnOwnerUserId": owner_user_id,
            "userId": owner_user_id,
            "excludeLast": True,
        }
    )

    try:
        document = get_storage_client().query(
            "conversation.get",
            {"conv_id": conv_id, "user_id": owner_user_id},
        )
        live_settings = dict(
            ((document or {}).get("metadata") or {}).get("settings") or {}
        )
        if live_settings.get("model"):
            config["model"] = live_settings["model"]
        if live_settings.get("preset"):
            config["preset"] = live_settings["preset"]
    except Exception:
        logger.warning(
            "[Autopilot] Could not refresh conversation settings conv=%s",
            conv_id[:8],
            exc_info=True,
        )

    api_messages = build_api_messages(
        conv_id,
        output_turn["turnId"],
        config,
        user_id=owner_user_id,
    )
    if not api_messages:
        error = make_envelope(
            "internal",
            detail="Autopilot could not build the successor context.",
            model=config.get("model", ""),
            context="autopilot",
            source="autopilot",
        )
        fail_start(attempt_id, error, user_id=owner_user_id)
        return None
    if not claim_attempt_start(attempt_id, user_id=owner_user_id):
        logger.info(
            "[Autopilot] Successor attempt was already claimed attempt=%s",
            attempt_id[:8],
        )
        return None

    new_task = None
    try:
        new_task = create_task(
            conv_id,
            api_messages,
            config,
            user_id=owner_user_id,
            supersede=False,
        )
        new_task["_autopilotParent"] = task.get("id")
        bound = bind_task(
            attempt_id, new_task["id"], user_id=owner_user_id
        )
        if bound is None:
            raise RuntimeError("successor attempt bind was rejected")
        spawn_task(new_task)
    except Exception as exc:
        if new_task is not None:
            discard_task(new_task["id"])
        error = make_envelope(
            "internal",
            detail="Autopilot failed to start the successor executor.",
            model=config.get("model", ""),
            context="autopilot",
            source="autopilot",
            raw=str(exc),
        )
        fail_start(attempt_id, error, user_id=owner_user_id)
        logger.error(
            "[Autopilot] Successor start failed attempt=%s",
            attempt_id[:8],
            exc_info=True,
        )
        return None

    new_task_id = new_task["id"]
    audit_log(
        "autopilot_followup",
        parent_task_id=task.get("id", ""),
        new_task_id=new_task_id,
        conv_id=conv_id,
        user_id=owner_user_id,
    )
    try:
        from lib.conversations import notify_conv_changed

        notify_conv_changed(conv_id, rev=None, user_id=owner_user_id)
    except Exception:
        logger.debug(
            "[Autopilot] Conversation notification failed conv=%s",
            conv_id[:8],
            exc_info=True,
        )
    return new_task_id


__all__ = [
    "_has_pending_real_message",
    "_successor_already_running",
    "_append_conversation_autopilot_turns",
    "_start_followup_task",
]
