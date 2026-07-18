"""Hallucinated-tool rejection: repeat-rejection circuit breaker, the
standardized model-facing rejection message, and the audit reporters.

A model that invents a tool with NO similar real tool gets a rejection that
can't point it anywhere — so under autopilot it re-emits the SAME fake call
every round (pure token burn). The breaker tracks how many times a given fake
name has been rejected within ONE conversation and, after a threshold,
ESCALATES the rejection to enumerate the real tools the model may actually
call. The count is keyed ``(convId, tool_name)`` so it spans autopilot
follow-up tasks (separate task ids, same conversation).
"""

from __future__ import annotations

from typing import Any

from lib.log import audit_log, get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════
#  Repeat-rejection circuit breaker
# ══════════════════════════════════════════

# Escalate (inject the live tool list) starting at the Nth consecutive
# rejection of the same name. N=2 → the first repeat already gets the list.
REJECTION_ESCALATE_THRESHOLD = 2
# Autopilot hard-abort threshold — DELIBERATELY HIGHER than the escalate
# threshold. Decoupling matters: if abort fired at the same count as the
# tool-list injection, the autopilot task would die in the SAME round the list
# was injected, so the model would never get a turn to USE the list — the
# graceful-recovery path would be dead code exactly where it's needed most.
# With ABORT=4 vs ESCALATE=2, the model gets ~2 rounds holding the enumerated
# tool list to self-correct before abort kicks in as a true last resort. Worst
# case converges at 4 rounds (still bounded) instead of 7+.
HALLUCINATION_ABORT_THRESHOLD = 4
# Cap the enumerated tool list so a 170-tool MCP session doesn't dump a wall of
# names into the result — list the built-ins/common ones, truncate the rest.
_REJECTION_TOOL_LIST_CAP = 60

# In-memory repeat counter: {(conv_id, tool_name): consecutive_reject_count}.
# Bounded by distinct fake names per conversation (tiny); entries are best-
# effort and never persisted — a process restart resetting them is harmless.
_REJECT_COUNTS: dict[tuple[str, str], int] = {}
_REJECT_COUNTS_MAX = 4096


def record_rejection(conv_id: str, tool_name: str) -> int:
    """Increment and return the consecutive-rejection count for a fake name.

    Keyed ``(conv_id, tool_name)`` so the count survives across autopilot
    follow-up tasks (which share the conversation but get fresh task ids).
    Total / never raises. A soft cap evicts arbitrary entries if the map ever
    grows pathologically (it won't in practice — distinct fake names per conv
    are few).
    """
    if not tool_name:
        return 0
    key = (conv_id or '', tool_name)
    n = _REJECT_COUNTS.get(key, 0) + 1
    _REJECT_COUNTS[key] = n
    if len(_REJECT_COUNTS) > _REJECT_COUNTS_MAX:
        try:
            for _k in list(_REJECT_COUNTS.keys())[:_REJECT_COUNTS_MAX // 2]:
                _REJECT_COUNTS.pop(_k, None)
        except Exception as e:
            logger.debug('[ToolRepair] reject-count eviction skipped: %s', e)
    return n


def clear_rejection(conv_id: str, tool_name: str) -> None:
    """Reset the consecutive-rejection count for one ``(conv_id, tool_name)``.

    Called when the same name is NO LONGER rejected (the model corrected
    itself), so a later unrelated reuse starts the count fresh rather than
    inheriting a stale streak.
    """
    if not tool_name:
        return
    _REJECT_COUNTS.pop((conv_id or '', tool_name), None)


def build_rejection_message(descriptor: dict[str, Any], *,
                            repeat_count: int = 1,
                            known_tools: set[str] | None = None) -> str:
    """Build the standardized model-facing rejection text for a fake tool call.

    One source of truth for the message returned to the LLM (as the tool
    result) so it can self-correct, instead of the ad-hoc per-site strings
    that existed before. Mentions the closest real tools when known.

    Args:
        descriptor: ``classify_tool_call`` output (``attempted`` + ``suggestions``).
        repeat_count: How many times this fake name has been rejected in a row
            within the conversation (1 = first time). At or above
            :data:`REJECTION_ESCALATE_THRESHOLD`, AND only when there are no
            ``suggestions`` (a pure invention with no nearby real tool to point
            at), the message ESCALATES to enumerate ``known_tools`` so the model
            has a concrete, correctable target instead of looping the same name.
        known_tools: The live REAL-tool set for this turn. Used only for the
            escalation path.
    """
    attempted = descriptor.get('attempted') or '?'
    suggestions = descriptor.get('suggestions') or []
    msg = (
        f'Error: `{attempted}` is not a real tool and was NOT executed. '
        f'It is not in the list of tools available to you this turn.'
    )
    if suggestions:
        hint = ', '.join(f'`{s}`' for s in suggestions)
        msg += f' Did you mean one of: {hint}? '
        msg += 'Call only tools from the provided tool list, using their exact names.'
        return msg

    # No suggestion to offer. On repeated invention of the SAME phantom name,
    # stop repeating the useless generic line — enumerate the real tools so the
    # model has a concrete target (the only way to break a no-suggestion loop).
    if repeat_count >= REJECTION_ESCALATE_THRESHOLD and known_tools:
        names = sorted(known_tools)
        shown = names[:_REJECTION_TOOL_LIST_CAP]
        listed = ', '.join(f'`{n}`' for n in shown)
        if len(names) > len(shown):
            listed += f', … (+{len(names) - len(shown)} more)'
        msg += (
            f' You have now called this non-existent tool {repeat_count} times — '
            f'STOP calling `{attempted}`. The ONLY tools you may call this turn are: '
            f'{listed}. Pick one of these exact names, or if none fits, reply to '
            f'the user in plain text WITHOUT a tool call.'
        )
        return msg

    msg += ' '
    msg += 'Call only tools from the provided tool list, using their exact names.'
    return msg


def report_hallucinated(name: str, descriptor: dict[str, Any], *, model: str = '') -> None:
    """Emit a ``tool_hallucinated`` audit event for a rejected fake tool call.

    Lets the nightly optimizer cluster which non-existent tool names a given
    model keeps inventing (e.g. a model that persistently calls ``search_web``)
    so the alias table or system prompt can be tuned.
    """
    audit_log(
        'tool_hallucinated',
        tool=name,
        model=model,
        suggestions=descriptor.get('suggestions') or [],
    )


def report_tool_name_aliased(attempted: str, resolved: str, alias_kind: str,
                             *, model: str = '') -> None:
    """Emit a ``tool_name_aliased`` audit event when a wrong name was rewritten.

    Quantifies which cross-harness tool names (Claude Code's ``Read`` / ``Bash``
    / ``MultiEdit`` / ``AskUserQuestion`` …) models actually emit, broken down
    by model — the data needed to decide whether a presentation-level schema
    rename (per model family) would pay off, vs. keeping the alias layer.
    """
    audit_log(
        'tool_name_aliased',
        attempted=attempted,
        resolved=resolved,
        kind=alias_kind,
        model=model,
    )


def report_invalid(tool_name: str, fn_args: Any, *, reason: str, model: str = '') -> None:
    """Emit ``tool_input_invalid`` audit when arguments couldn't be repaired.

    Called by the dispatcher when validation still fails after repair —
    surfaces the (tool, model, reason) tuple so the optimizer can spot
    regressions after a model swap.
    """
    audit_log(
        'tool_input_invalid',
        tool=tool_name,
        model=model,
        reason=reason,
        keys=list(fn_args.keys()) if isinstance(fn_args, dict) else None,
    )
