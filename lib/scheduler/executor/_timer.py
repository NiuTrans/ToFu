"""Timer tool adapters.

``timer_create`` persists a background watcher and returns immediately. The
watcher owns later polling and, when ready, starts a fresh authoritative turn.
No model task is kept alive merely to sleep between polls.
"""

from __future__ import annotations

import time

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.scheduler.executor._common import _coerce_int_arg


logger = get_logger(__name__)


def _tool_owner_id(fn_args: dict) -> int:
    """Return the authenticated task owner carried by tool dispatch."""
    try:
        owner_id = int(fn_args.get("_user_id"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Timer tools require an authenticated owner") from exc
    if owner_id < 1:
        raise ValueError("Timer tools require an authenticated owner")
    return owner_id


def _execute_timer_create(fn_args):
    """Persist one durable watcher and release the current model task."""
    from lib.scheduler.timer import create_timer, start_timer_loop

    check_instruction = str(fn_args.get("check_instruction") or "")
    continuation_message = str(fn_args.get("continuation_message") or "")
    condition_command = str(fn_args.get("condition_command") or "")
    if not continuation_message:
        return "Error: continuation_message is required."
    if not check_instruction and not condition_command:
        return (
            "Error: provide check_instruction (LLM/hybrid) and/or "
            "condition_command (pure-code predicate)."
        )
    conversation_id = str(fn_args.get("_source_conv_id") or "")
    if not conversation_id:
        return (
            "Error: Could not determine conversation ID. Timer must be "
            "created within a conversation."
        )

    parent_task = fn_args.get("_parent_task")
    parent_config = parent_task.get("config", {}) if parent_task else {}
    tools_config = {
        "projectPath": parent_config.get("projectPath", ""),
        "searchMode": parent_config.get("searchMode", "multi"),
        "fetchEnabled": parent_config.get("fetchEnabled", True),
        "codeExecEnabled": parent_config.get("codeExecEnabled", False),
        "browserEnabled": parent_config.get("browserEnabled", False),
        "imageGenEnabled": parent_config.get("imageGenEnabled", False),
    }
    poll_interval = _coerce_int_arg(
        "poll_interval", fn_args.get("poll_interval", 60), 60
    )
    max_polls = _coerce_int_arg(
        "max_polls", fn_args.get("max_polls", 120), 120
    )

    try:
        owner_id = _tool_owner_id(fn_args)
        timer = create_timer(
            user_id=owner_id,
            conv_id=conversation_id,
            check_instruction=check_instruction,
            continuation_message=continuation_message,
            poll_interval=poll_interval,
            max_polls=max_polls,
            check_command=str(fn_args.get("check_command") or ""),
            tools_config=tools_config,
            source_task_id=str(fn_args.get("_source_task_id") or ""),
            condition_command=condition_command,
            condition_regex=str(fn_args.get("condition_regex") or ""),
        )
        start_timer_loop(timer["id"], user_id=owner_id)
    except (TypeError, ValueError) as exc:
        logger.warning("Timer creation rejected: %s", exc)
        return f"Error: {exc}"
    except Exception as exc:
        logger.error("Timer creation failed: %s", exc, exc_info=True)
        return "Error: Failed to create timer. See server diagnostics."

    # The originating turn receives one durable, reconstructible hand-off
    # marker. Subsequent polls belong to the timer projection, not to a task
    # that has already completed.
    round_num = fn_args.get("_tool_round_num")
    tool_call_id = str(fn_args.get("_tool_call_id") or "")
    if parent_task and round_num is not None:
        for tool_round in parent_task.get("toolRounds", []):
            if tool_round.get("roundNum") == round_num:
                tool_round.update(
                    {
                        "_timerTimerId": timer["id"],
                        "_timerBackground": True,
                        "_timerCheckInstruction": check_instruction,
                        "_timerCheckCommand": timer.get("check_command", ""),
                        "_timerConditionKind": timer.get("condition_kind", "llm"),
                        "_timerConditionCommand": timer.get(
                            "condition_command", ""
                        ),
                        "_timerPollInterval": timer["poll_interval"],
                        "_timerMaxPolls": timer["max_polls"],
                    }
                )
                break
        from lib.tasks_pkg.manager import append_event

        append_event(
            parent_task,
            build_event(
                EventType.TIMER_POLL_CHECK,
                roundNum=round_num,
                toolCallId=tool_call_id,
                timerId=timer["id"],
                pollNum=0,
                decision="started",
                reason="Durable background watcher created",
                checkInstruction=check_instruction[:4000],
                checkCommand=str(timer.get("check_command") or "")[:400],
                conditionKind=timer.get("condition_kind", "llm"),
                conditionCommand=str(timer.get("condition_command") or "")[:400],
                pollInterval=timer["poll_interval"],
                maxPolls=timer["max_polls"],
                nextPollTs=int(
                    (time.time() + int(timer["poll_interval"])) * 1000
                ),
                background=True,
            ),
        )

    return (
        f"Timer {timer['id']} is active in the background. "
        f"It will check every {timer['poll_interval']} seconds and start a new "
        "conversation turn when ready. This task does not need to wait."
    )


def _execute_timer_manage(fn_args):
    """Cancel, inspect, list, or audit timer watchers."""
    from lib.scheduler.timer import (
        cancel_timer,
        get_timer,
        get_timer_poll_log,
        list_active_timers,
    )

    try:
        owner_id = _tool_owner_id(fn_args)
    except ValueError as exc:
        return f"Error: {exc}"
    action = fn_args.get("action", "")
    timer_id = fn_args.get("timer_id", "")

    if action == "list":
        timers = list_active_timers(user_id=owner_id)
        if not timers:
            return "No timers found. Use timer_create to create one."
        lines = [f"Timer Watchers ({len(timers)}):", "-" * 50]
        for timer in timers:
            lines.extend(
                [
                    f"[{timer['status']}] [{timer['id']}]",
                    f"    Conv: {timer['conv_id'][:12]}...",
                    f"    Polls: {timer['poll_count']} / {timer['max_polls']}",
                    f"    Interval: {timer['poll_interval']}s",
                    (
                        f"    Last poll: {timer.get('last_poll_decision', '-')} "
                        f"({timer.get('last_poll_reason', '')[:60]})"
                    ),
                    f"    Check: {timer['check_instruction'][:100]}",
                    f"    Created: {timer['created_at']}",
                    "",
                ]
            )
        return "\n".join(lines)

    if not timer_id:
        return "Error: timer_id is required for this action."
    if action == "cancel":
        changed = cancel_timer(timer_id, user_id=owner_id)
        return (
            f"Timer {timer_id} cancelled."
            if changed
            else f"Error: Active timer {timer_id} not found."
        )
    if action == "status":
        timer = get_timer(timer_id, user_id=owner_id)
        if not timer:
            return f"Error: Timer {timer_id} not found."
        result = (
            f"Timer {timer_id}\n"
            f"  Status: {timer['status']}\n"
            f"  Conv: {timer['conv_id'][:12]}\n"
            f"  Polls: {timer['poll_count']} / {timer['max_polls']}\n"
            f"  Interval: {timer['poll_interval']}s\n"
            f"  Last poll: {timer.get('last_poll_at', 'never')} "
            f"({timer.get('last_poll_decision', '-')})\n"
            f"  Reason: {timer.get('last_poll_reason', '')[:100]}\n"
            f"  Check: {timer['check_instruction'][:200]}\n"
            f"  Command: {timer.get('check_command', '(none)')[:100] or '(none)'}\n"
            f"  Continuation: {timer['continuation_message'][:200]}\n"
            f"  Created: {timer['created_at']}"
        )
        if timer.get("triggered_at"):
            result += f"\n  Triggered: {timer['triggered_at']}"
        if timer.get("execution_task_id"):
            result += f"\n  Exec task: {timer['execution_task_id']}"
        return result
    if action == "log":
        entries = get_timer_poll_log(
            timer_id, user_id=owner_id, limit=fn_args.get("limit", 20)
        )
        if not entries:
            return f"No poll log entries for timer {timer_id}."
        lines = [f"Poll Log for {timer_id} (newest first):"]
        for entry in entries:
            lines.append(
                f"  [{entry['decision'].upper()}] {entry['poll_time']} -- "
                f"{entry.get('reason', '')[:80]} "
                f"(tokens: {entry.get('tokens_used', 0)})"
            )
        return "\n".join(lines)
    return (
        f"Error: Unknown timer_manage action: {action}. "
        "Use cancel/status/list/log."
    )
