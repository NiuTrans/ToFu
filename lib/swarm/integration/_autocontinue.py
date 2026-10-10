"""Turn-native continuation after a conversation-owned swarm settles.

This module owns only the unattended-loop policy. Durable conversation and
executor creation are delegated to the shared scheduled-turn command service;
it never edits a transcript document or mirrors ``activeTaskId`` settings.
"""

from __future__ import annotations

import hashlib
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
        session = integration.get_active_session(swarm_key)
        if session is not None and session.user_id == owner_id:
            specs = getattr(session, 'specs', ())
            if any(not spec.required_for_completion for spec in specs):
                # Optional work can outlive multiple required delivery waves.
                # Bind deduplication to the durable required spec frontier;
                # rehydration reconstructs the same identity without a counter.
                required_ids = sorted(spec.id for spec in specs if spec.required_for_completion)
                digest = hashlib.sha256('\0'.join(required_ids).encode()).hexdigest()[:16]
                source_id = f'{source_id}:required:{digest}'
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

        # Only stamped success/failure evidence authorizes a wake. Cancellation
        # and legacy unstamped updates alone never grant recovery permission.
        updates = agent_inbox.pending_items(swarm_key, modes=['swarm-update'])
        if not any(item.get('status') in ('completed', 'failed')
                   for item in updates):
            logger.info(
                "[Swarm:%s] auto-continuation skipped: no stamped success/failure "
                "evidence (%d cancelled/unknown update(s) left for "
                "the next human turn)",
                swarm_key,
                len(updates),
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


        try:
            started = integration._start_autocontinue_turn(
                swarm_key,
                owner_id,
                command_id=f"swarm-autocontinue:{source_id}",
            )
            if started:
                with _autocontinue_lock:
                    _autocontinue_chain[swarm_key] = chain + 1
        finally:
            with _autocontinue_lock:
                _autocontinue_inflight.discard(swarm_key)
    except Exception:
        logger.error(
            "[Swarm:%s] auto-continuation failed", swarm_key, exc_info=True
        )
        with _autocontinue_lock:
            _autocontinue_inflight.discard(swarm_key)


def _durable_chain_allows(turns: list[dict], command_id: str, *,
                          failure: bool, ceiling: int) -> bool:
    """One failure-bearing wake per actual human chain, inside the total cap.

    Input receipts survive restart. A failed executor admission is not an
    executed recovery; its paired output's typed error excludes that receipt.
    Unknown old swarm receipts conservatively spend the failure allowance.
    """
    human_index = next((i for i in range(len(turns) - 1, -1, -1)
                        if turns[i].get('actor') == 'human'
                        and not (turns[i].get('projection') or {}).get('_initiator')), None)
    if human_index is None:
        return False
    chain = turns[human_index + 1:]
    if any(turn.get('status') in ('cancelled', 'aborted', 'interrupted')
           for turn in chain):
        return False
    if turns[-1].get('status') in ('pending', 'running'):
        return False
    from lib.turn_lifecycle import automatic_recovery_blocker
    from lib.tasks_pkg.stream_handler._intent_stall import known_external_blocker

    # Consult the latest main-assistant outcome, not child success. A later
    # actual recovery supersedes an earlier failure; a virtual input alone
    # does not. Retain the inbox evidence for the next authorized human turn.
    for parent in reversed(chain):
        if parent.get('actor') != 'assistant':
            continue
        projection = parent.get('projection') or {}
        settlement = parent.get('settlement') or {}
        error = settlement.get('error') or projection.get('error')
        if isinstance(error, dict) and error.get('code') == 'task_start_failed':
            continue  # No execution occurred to supersede the main failure.
        if (automatic_recovery_blocker(error) is not None
                or known_external_blocker(projection) is not None):
            return False
        break
    receipts = []
    for turn in chain:
        if turn.get('kind') != 'swarm_autocontinue_input':
            continue
        projection = turn.get('projection') or {}
        output = next((row for row in chain
                       if row.get('parentTurnId') == turn.get('turnId')
                       and row.get('kind') == 'swarm_autocontinue_reply'), None)
        error = ((output or {}).get('projection') or {}).get('error') or {}
        if isinstance(error, dict) and error.get('code') == 'task_start_failed':
            continue
        # Missing paired evidence is not proof of failed dispatch.
        receipts.append(projection)
    if any(row.get('_swarmAutoContinueCommandId') == command_id for row in receipts):
        return False
    if len(receipts) >= ceiling:
        return False
    return not failure or not any(row.get('_swarmFailureRecovery', True)
                                 for row in receipts)


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
            light=False,
        )
        turns = list(page.get("turns") or [])
        # The bounded listing is oldest-first. A full page cannot prove the
        # tail or the last human authorization: fail closed, never reset a cap.
        if not turns or len(turns) >= 2000:
            return False
        import lib.swarm.integration as integration
        updates = agent_inbox.pending_items(conversation_id, modes=['swarm-update'])
        statuses = [item.get('status') for item in updates]
        failure = 'failed' in statuses
        if not failure and 'completed' not in statuses:
            return False
        if not _durable_chain_allows(turns, command_id, failure=failure,
                                     ceiling=integration.SWARM_AUTOCONTINUE_MAX_CHAIN):
            logger.info('[Swarm:%s] continuation blocked by durable Stop, duplicate '
                        'or non-automatic main-task recovery/budget; updates retained',
                        conversation_id)
            return False
        parent_turn_id = turns[-1]['turnId']
        completed = statuses.count('completed')
        failed = statuses.count('failed')
        cancelled = sum(status in ('cancelled', 'aborted') for status in statuses)
        content = (f'Settled swarm updates: {completed} completed, {failed} failed, '
                   f'{cancelled} cancelled; {len(statuses) - completed - failed - cancelled} '
                   'unknown. Cancelled or unknown work is not successful.')
        if failure:
            content += (' Use this single bounded diagnostic/recovery/closure turn to '
                        'inspect failure evidence, integrate any actual successes, and '
                        'either take a justified corrective action or report the blocker '
                        'and stop. Never blindly repeat failed work or retry cancelled '
                        'work. Further failure recovery requires human authorization.')
        else:
            content += ' Integrate the completed results and report remaining blockers.'

        input_projection = {
            "content": content,
            "thinking": "",
            "segments": [],
            "toolRounds": [],
            "timestamp": int(time.time() * 1000),
            "_swarmAutoContinue": True,
        }

        input_projection['_swarmAutoContinueCommandId'] = command_id
        input_projection['_swarmFailureRecovery'] = failure
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
            require_parent_not_stopped=True,
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
