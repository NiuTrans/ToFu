"""Turn-native continuation after a conversation-owned swarm settles.

This module owns only the unattended-loop policy. Durable conversation and
executor creation are delegated to the shared scheduled-turn command service;
it never edits a transcript document or mirrors ``activeTaskId`` settings.
"""

from __future__ import annotations

import time

from lib import agent_inbox
from lib.log import get_logger
from lib.swarm.integration._state import (
    _autocontinue_chain,
    _autocontinue_inflight,
    _autocontinue_lock,
)


logger = get_logger(__name__)


def reset_autocontinue_chain(swarm_key: str) -> None:
    """Reset the unattended-chain counter when a human starts a new turn."""
    if not swarm_key:
        return
    with _autocontinue_lock:
        _autocontinue_chain.pop(swarm_key, None)


def _maybe_autocontinue(
    swarm_key: str,
    user_id: int,
    *,
    source_id: str,
) -> None:
    """Start one owner-scoped continuation when settled swarm output is unread."""
    import lib.swarm.integration as integration

    if not integration.SWARM_AUTOCONTINUE_ENABLED or not swarm_key:
        return
    owner_id = int(user_id)
    if owner_id < 1:
        raise ValueError("Swarm auto-continuation requires an authenticated owner")
    if not source_id:
        raise ValueError("Swarm auto-continuation requires a stable source id")

    try:
        if integration._key_is_live(swarm_key):
            logger.debug(
                "[Swarm:%s] auto-continuation skipped: conversation is live",
                swarm_key,
            )
            return
        if not agent_inbox.has_pending(swarm_key):
            logger.debug(
                "[Swarm:%s] auto-continuation skipped: inbox is empty",
                swarm_key,
            )
            return

        with _autocontinue_lock:
            if swarm_key in _autocontinue_inflight:
                return
            chain = _autocontinue_chain.get(swarm_key, 0)
            if chain >= integration.SWARM_AUTOCONTINUE_MAX_CHAIN:
                logger.warning(
                    "[Swarm:%s] auto-continuation ceiling reached (%d); "
                    "leaving %d update(s) for the next human turn",
                    swarm_key,
                    integration.SWARM_AUTOCONTINUE_MAX_CHAIN,
                    agent_inbox.peek(swarm_key),
                )
                return
            _autocontinue_inflight.add(swarm_key)
            _autocontinue_chain[swarm_key] = chain + 1

        try:
            started = integration._start_autocontinue_turn(
                swarm_key,
                owner_id,
                command_id=f"swarm-autocontinue:{source_id}",
            )
            if not started:
                with _autocontinue_lock:
                    current = _autocontinue_chain.get(swarm_key, 0)
                    if current > 0:
                        _autocontinue_chain[swarm_key] = current - 1
        finally:
            with _autocontinue_lock:
                _autocontinue_inflight.discard(swarm_key)
    except Exception:
        logger.error(
            "[Swarm:%s] auto-continuation failed", swarm_key, exc_info=True
        )
        with _autocontinue_lock:
            _autocontinue_inflight.discard(swarm_key)


def _start_autocontinue_turn(
    conversation_id: str,
    user_id: int,
    *,
    command_id: str,
) -> bool:
    """Dispatch an honest virtual-user/assistant pair through turn authority."""
    try:
        from lib.turn_initiation import (
            INITIATOR_SWARM,
            stamp_initiator,
        )
        from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn
        from lib.turn_lifecycle import list_turns

        owner_id = int(user_id)
        page = list_turns(
            conversation_id,
            user_id=owner_id,
            lane_id="main",
            limit=2000,
            light=True,
        )
        turns = list(page.get("turns") or [])
        parent_turn_id = turns[-1]["turnId"] if turns else None

        input_projection = {
            "content": "Continue by integrating the completed swarm results.",
            "thinking": "",
            "segments": [],
            "toolRounds": [],
            "timestamp": int(time.time() * 1000),
            "_swarmAutoContinue": True,
        }
        stamp_initiator(input_projection, INITIATOR_SWARM)
        dispatch = dispatch_scheduled_turn(
            conversation_id,
            input_projection,
            {},
            user_id=owner_id,
            command_id=command_id,
            log_prefix=f"[Swarm:{conversation_id[:12]}]",
            input_actor="virtual_user",
            input_kind="swarm_autocontinue_input",
            turn_kind="swarm_autocontinue_reply",
            parent_turn_id=parent_turn_id,
            require_parent_is_lane_tail=parent_turn_id is not None,
            config_overrides={
                "_swarmAutoContinue": True,
                "schedulerEnabled": False,
            },
        )
        if dispatch.disposition != "started":
            logger.info(
                "[Swarm:%s] auto-continuation stood down: %s",
                conversation_id,
                dispatch.disposition,
            )
            return False

        try:
            from lib.agent_core.push import push_event

            push_event(
                "swarm",
                conversation_id,
                {
                    "type": "swarm_autocontinue_started",
                    "convId": conversation_id,
                    "newTaskId": dispatch.task_id,
                },
                user_id=owner_id,
            )
        except Exception:
            logger.debug(
                "[Swarm:%s] start notification failed",
                conversation_id,
                exc_info=True,
            )
        return True
    except Exception:
        logger.error(
            "[Swarm:%s] auto-continuation dispatch failed",
            conversation_id,
            exc_info=True,
        )
        return False


__all__ = [
    "reset_autocontinue_chain",
    "_maybe_autocontinue",
    "_start_autocontinue_turn",
]
