"""Dispatch scheduler-owned conversation work through turn-native commands."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from lib.conversation_sync.command_service import AttemptStartFailure
from lib.conversation_sync.runtime import conversation_turn_commands
from lib.turn_initiation import (
    INITIATOR_PROACTIVE,
    INITIATOR_TIMER,
    stamp_initiator,
)
from lib.log import get_logger
from lib.scheduler._shared import build_task_config, build_turn_ctx_snapshot
from lib.storage import get_storage_client
from lib.turn_lifecycle import LifecycleConflict, LifecycleNotFound


logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScheduledTurnDispatch:
    """Typed scheduler outcome; ``task_id`` exists only for ``started``."""

    disposition: Literal["started", "busy", "target_missing", "start_failed"]
    task_id: str = ""


def _build_swarm_continuation_config(
    settings: dict[str, Any],
    tools_config: dict[str, Any],
    overrides: dict[str, Any],
) -> dict[str, Any] | None:
    """Share chat defaults, not attended authority or scheduler overrides.

    The swarm caller carries no accepted parent attempt/config. Conversation
    settings cannot reconstruct global prompt/tool policy, so nondefault parent
    policy must still fail replay validation rather than be guessed here.
    """
    from lib.conv_config import resolve_conv_config
    from lib.tasks_pkg.chat_mode import apply_chat_mode

    controls = {'_swarmAutoContinue': True, 'schedulerEnabled': False}
    if tools_config or any(
        key not in controls or value is not controls[key]
        for key, value in overrides.items()
    ):
        raise ValueError('Swarm continuation accepts only its fixed control overrides')

    # The old scheduler builder never inherited these attended capabilities or
    # selected a Flow. Canonical defaults must not silently grant them now.
    config = resolve_conv_config(conv_settings={
        **settings,
        'activeFlow': '',
        'autopilotEnabled': False,
        'desktopEnabled': False,
        'humanGuidanceEnabled': False,
        'schedulerEnabled': False,
    }, is_active=False)
    if config['planMode']:
        # Plan normalization at task start enables human guidance again. Keep
        # its read-only restriction by deferring, not by dropping Plan or
        # pretending a guidance=False clamp would survive that boundary.
        return None

    # Chat mode can expand atomic flags later in model_config. Preserve the
    # legacy swarm's narrower tool choices instead of granting tools for parity.
    if any(config.get(key) != value
           for key, value in apply_chat_mode(config).items() if key != 'chatMode'):
        config.pop('chatMode', None)
    config.update(controls)
    return config


def dispatch_scheduled_turn(
    conversation_id: str,
    user_message: dict[str, Any],
    tools_config: str | dict,
    *,
    user_id: int,
    command_id: str,
    log_prefix: str = "",
    input_actor: str = "human",
    input_kind: str = "input",
    turn_kind: str = "",
    parent_turn_id: str | None = None,
    require_parent_is_lane_tail: bool = False,

    require_parent_not_stopped: bool = False,
    config_overrides: dict[str, Any] | None = None,
) -> ScheduledTurnDispatch:
    """Create one input/output pair and start its bound executor atomically."""
    owner_id = int(user_id)
    if owner_id < 1:
        raise ValueError("A positive scheduler owner is required")
    if not command_id:
        raise ValueError("A stable scheduler command id is required")

    snapshot = get_storage_client().query(
        "conversation.get",
        {
            "conv_id": conversation_id,
            "user_id": owner_id,
            "derive_messages": False,
        },
    )
    if snapshot is None:
        logger.error("%s Conversation %s not found", log_prefix, conversation_id)
        return ScheduledTurnDispatch("target_missing")
    settings = dict((snapshot.get("metadata") or {}).get("settings") or {})
    if isinstance(tools_config, str):
        try:
            parsed_tools = json.loads(tools_config or "{}")
        except (json.JSONDecodeError, TypeError):
            logger.warning("%s Invalid scheduler tools_config", log_prefix)
            parsed_tools = {}
    else:
        parsed_tools = dict(tools_config or {})

    projection = dict(user_message)
    projection.pop("role", None)
    if projection.get("_timer"):
        stamp_initiator(projection, INITIATOR_TIMER)
        kind = "timer_reply"
    elif projection.get("_proactive"):
        stamp_initiator(projection, INITIATOR_PROACTIVE)
        kind = "proactive_reply"
    else:
        kind = "scheduled_reply"
    if turn_kind:
        kind = turn_kind
    if kind == 'swarm_autocontinue_reply':
        task_config = _build_swarm_continuation_config(
            settings, parsed_tools, dict(config_overrides or {}))
        if task_config is None:
            logger.info('%s Swarm continuation deferred: Plan requires human guidance',
                        log_prefix)
            return ScheduledTurnDispatch('start_failed')
    else:
        task_config = build_task_config(parsed_tools, settings)
        task_config.update(dict(config_overrides or {}))
    if '_ctx' not in projection and 'contextSnapshot' not in projection:
        ctx_snapshot = build_turn_ctx_snapshot(task_config)
        if ctx_snapshot is not None:
            projection['_ctx'] = ctx_snapshot

    try:
        outcome = conversation_turn_commands.create_turn(
            conversation_id,
            owner_id,
            {
                "commandId": command_id,
                "inputTurn": projection,
                "config": task_config,
                "kind": kind,
                "actor": "assistant",
                "inputActor": input_actor,
                "inputKind": input_kind,
                "parentTurnId": parent_turn_id,
                "requireParentIsLaneTail": require_parent_is_lane_tail,

                "requireParentNotStopped": require_parent_not_stopped,
            },
            request_started_at=time.time(),
        ).value
    except LifecycleConflict as exc:
        if exc.code != "lane_busy":
            raise
        logger.info("%s Conversation lane busy; dispatch deferred", log_prefix)
        return ScheduledTurnDispatch("busy")
    except LifecycleNotFound:
        return ScheduledTurnDispatch("target_missing")
    except AttemptStartFailure:
        logger.error("%s Durable turn exists but executor start failed", log_prefix)
        return ScheduledTurnDispatch("start_failed")
    task_id = str((outcome.get("attempt") or {}).get("taskId") or "")
    if not task_id:
        logger.warning("%s Turn command returned no executor task", log_prefix)
        return ScheduledTurnDispatch("start_failed")
    logger.info(
        "%s Started turn-native task %s in conv=%s",
        log_prefix,
        task_id[:8],
        conversation_id[:12],
    )
    return ScheduledTurnDispatch("started", task_id)


__all__ = ["ScheduledTurnDispatch", "dispatch_scheduled_turn"]
