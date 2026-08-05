"""Section 2.5 tool-history restoration — extracted from ``_run.py`` (pt_03f4cdf1 slice 8).

The block this module replaces was the ~35-line "Section 2.5: Server-side
tool history restoration" region of ``run_task``, sitting between tool
assembly (Section 2) and context injection (Section 3). It is a clean
seam: no closures captured, no recursion, no shared mutable state beyond
the ``messages`` list it may replace.

**What it does** (byte-parity with the inline block):

  1. Read ``cfg.get('keepToolHistory', True)`` + ``task['convId']``.
  2. If BOTH truthy: emit VU phase ``Autopilot：重建工具调用历史…``.
  3. Call ``rebuild_messages_with_history(conv_id, messages)`` and check
     the returned ``used_store`` flag.
  4. On HIT: compute token overhead via ``estimate_token_overhead``, log
     the ``TOOL HISTORY RESTORED`` info line, replace ``messages`` with
     ``rebuilt``, refresh ``original_messages``, emit a diagnostic PHASE
     event ``tool_history_restored`` with stats + overhead. Returns the
     new lists.
  5. On MISS: log at debug ``keepToolHistory enabled but no stored messages``.
     Returns the inputs unchanged.

**Contract**:

  restore_tool_history(*, task, cfg, messages, tid, vu_phase=None)
      -> (messages: list, original_messages: list, used_store: bool)

Caller reassigns its two locals from the tuple: ``(messages, original_messages, _) = restore_tool_history(...)``.
"""

from __future__ import annotations

from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event
from lib.tasks_pkg.server_message_store import (
    estimate_token_overhead,
    rebuild_messages_with_history,
)
from lib.tasks_pkg.message_builder import inject_tool_history

logger = get_logger(__name__)


def restore_tool_history(
    *,
    task: dict[str, Any],
    cfg: dict[str, Any],
    messages: list,
    tid: str,
    vu_phase=None,
) -> tuple[list, list, bool]:
    """Section 2.5 — rebuild messages with tool history if keepToolHistory=True.

    Args:
        task: The live task dict (read-only here except for the diagnostic
            event emitted via ``append_event``).
        cfg: The run's config dict — read for ``keepToolHistory`` flag.
        messages: The current messages list (potentially replaced with a
            rebuilt version).
        tid: The short task id (``task['id'][:8]``) for logging.
        vu_phase: Optional callable ``(detail: str) -> None`` — run_task's
            local VU phase closure adapter. When provided, used to emit the
            ``Autopilot：重建工具调用历史…`` phase before the rebuild.

    Returns:
        (messages, original_messages, used_store):
          * messages — the (possibly replaced) list to hand to the loop.
          * original_messages — a fresh ``list(messages)`` snapshot (matches
            the inline ``original_messages = list(messages)`` re-assign).
          * used_store — True iff the rebuild actually replaced messages
            (returned so the caller can key follow-up decisions off it
            without re-checking the flag).
    """
    _keep_tool_history = cfg.get('keepToolHistory', True)
    _conv_id = task.get('convId', '')
    if not (_keep_tool_history and _conv_id):
        return messages, list(messages), False

    if callable(vu_phase):
        try:
            vu_phase('Autopilot：重建工具调用历史…')
        except Exception as e:
            logger.debug('[orchestrator] tool_history vu_phase failed: %s', e)

    rebuilt, _rebuild_stats = rebuild_messages_with_history(_conv_id, messages)
    if _rebuild_stats['used_store']:
        # Log the overhead for monitoring
        _oh = estimate_token_overhead(messages, rebuilt)
        logger.info(
            '[%s] conv=%s ★ TOOL HISTORY RESTORED: '
            'frontend=%d msgs → rebuilt=%d msgs '
            '(tool_msgs=%d, overhead=+%d est_tokens, ratio=%.1fx)',
            tid, _conv_id[:8],
            _rebuild_stats['frontend_msg_count'], len(rebuilt),
            _rebuild_stats['tool_msgs_restored'],
            _oh['overhead_est_tokens'], _oh['ratio'],
        )
        messages = rebuilt
        original_messages = list(messages)
        # Emit a diagnostic event for the debug panel
        append_event(task, build_event(
            EventType.PHASE,
            phase='tool_history_restored',
            detail=f'Restored {_rebuild_stats["tool_msgs_restored"]} tool messages from server store',
            stats=_rebuild_stats,
            overhead=_oh,
        ))
        return messages, original_messages, True

    logger.debug('[%s] conv=%s keepToolHistory enabled but no stored messages found',
                 tid, _conv_id[:8])
    return messages, list(messages), False


def inject_continue_tool_history(*, task, rs, messages, cfg, model, tid) -> int:
    """Continue-toolHistory injection + memory-prefetch eligibility drift guard.

    Extracted 2026-08-01 (pt_03f4cdf1 slice 36) from ``run_task`` (between
    context injection and resume-state hydration).

    1. ``inject_tool_history`` restores interrupted tool-call context from
       the continue checkpoint; returns the injected count.
    2. On a non-zero count: ``rs.tool_call_happened = True`` AND
       ``rs.tool_round_num = <count>`` — the offset keeps new roundNums
       from conflicting with the restored ones.
    3. Drift guard: the EARLY memory-prefetch spawn used
       ``len(cfg['toolHistory'])`` as its eligibility input (available
       before this call); if the actual injected count disagrees, WARN —
       inject_tool_history no longer derives its count from that key
       alone, so the spawn's skip decision may silently flip.

    Returns:
        The injected tool-call count (0 when nothing was restored).
    """
    _injected_tool_calls = inject_tool_history(messages, cfg, task, model)
    if _injected_tool_calls:
        rs.tool_call_happened = True
        rs.tool_round_num = _injected_tool_calls  # offset so new roundNums don't conflict

    if bool(_injected_tool_calls) != bool(cfg.get('toolHistory') or []):
        logger.warning(
            '[%s] memory-prefetch eligibility drift: injected=%s but '
            'cfg[toolHistory]=%s — the early spawn used the latter; '
            'inject_tool_history no longer derives its count from that '
            'key alone', tid, _injected_tool_calls,
            len(cfg.get('toolHistory') or []))
    return _injected_tool_calls


__all__ = ['restore_tool_history', 'inject_continue_tool_history']
