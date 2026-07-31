# HOT_PATH — this leaf is called per stream-loop iteration.
"""Per-round prompt cache-round accounting.

Extracted 2026-07-29 (pt_03f4cdf1 slice 13) from
``lib.tasks_pkg.orchestrator._run.run_task``. The cluster runs
RIGHT AFTER the post-LLM ``flush_deferred_peer_and_steer`` call
(slice 12) — the LLM call has returned successfully,
``rs.last_usage`` is set, and the round's entry has been appended
to ``rs.api_rounds`` by ``_llm_call_with_fallback``.

The cluster stamps three telemetry fields onto ``api_rounds[-1]``
so the frontend cost popover can explain WHY cache_read dropped
and WHERE next-round cache `write` will come from, plus logs the
per-round cache stats at INFO for production visibility:

    * ``cacheBreak``       — the verdict from ``detect_cache_break``
                              (system-prompt change, tools change,
                              TTL expiry, …).
    * ``toolCalls``        — the tool function names emitted this
                              round; the causal driver of the NEXT
                              round's cache `write`.
    * ``writeBreakdown``   — exact decomposition of this round's
                              `write` into
                              {toolResults, prevOutput, envelope}
                              via ``_compute_write_breakdown``.
                              Round-1 (len(api_rounds) < 2) fetches
                              the previous turn's cache_read via
                              ``get_prev_turn_cache_read`` to seed
                              the envelope classification — losing
                              this baseline mis-classifies an
                              evicted-tail re-bill as benign
                              contextWrite.

The whole cluster is guarded by
``if task.get('convId') and usage:`` — non-conv turns and turns
with no recorded usage are silent no-ops (byte-identical to the
inline pre-extraction body).

All three ``api_rounds[-1]`` stamps are protected by a round-match
guard: ``api_rounds[-1].get('round') == round_num + 1`` — a bug
that stamps the wrong round's entry is a hard-to-diagnose
telemetry drift.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.cache_tracking import (
    detect_cache_break,
    get_prev_turn_cache_read,
    log_round_cache_stats,
)
from lib.tasks_pkg.orchestrator._finalize import _compute_write_breakdown


logger = get_logger(__name__)


def stamp_round_cache_accounting(
        task: dict[str, Any], *,
        round_num: int,
        tid: str,
        model: str,
        tools: Any,
        usage: dict[str, Any] | None,
        assistant_msg: dict[str, Any] | None,
        api_rounds: list[dict[str, Any]],
        messages: list[dict[str, Any]]) -> None:
    """Detect cache break + stamp per-round telemetry + log stats.

    Byte-identical to the inline pre-extraction body of the
    ``if task.get('convId') and rs.last_usage:`` block in
    ``run_task``. Safe to call unconditionally — the internal
    guards reproduce the outer conditional's short-circuit.

    Parameters
    ----------
    task
        Live task dict. Only ``task['convId']`` is read; no
        mutations on ``task`` itself.
    round_num
        Zero-based stream-loop round number; api_rounds[-1] is
        stamped ONLY when its ``round`` equals ``round_num + 1``.
    tid
        8-char task-id prefix, used for structured log context.
    model
        The resolved model id for THIS round (post-fallback).
    tools
        The tool schema this round used (``_tools_this_round`` in
        the caller — may be ``None`` on the LAST round when the
        model has exhausted tool budget).
    usage
        The LLM's reported usage dict for this round (or None
        when the round produced no usage — early exit).
    assistant_msg
        The assistant reply dict this round produced; its
        ``tool_calls`` list is walked to derive ``toolCalls``.
    api_rounds
        The per-turn accumulated rounds list; its LAST entry
        (when round-matched) receives the three stamps.
    messages
        The full outbound message list — passed to
        ``detect_cache_break`` so it can hash the system prompt +
        prefix for break-cause classification.
    """
    if not (task.get('convId') and usage):
        return

    _cache_break = detect_cache_break(
        task['convId'], messages,
        tools=tools, model=model,
        usage=usage,
    )
    # Stamp the break reason onto the round we just recorded so
    # the frontend cost popover can explain WHY cache_read dropped
    # (system-prompt change, tools change, TTL expiry, …). Guard on
    # the round number so we don't mis-attribute when this round
    # produced no usage and api_rounds[-1] is an earlier round.
    if _cache_break and api_rounds and api_rounds[-1].get('round') == round_num + 1:
        api_rounds[-1]['cacheBreak'] = _cache_break

    # Stamp WHAT the model did this round (the tool calls it
    # emitted). This is the causal driver of the NEXT round's
    # cache `write`: round N's assistant output (text + these
    # tool_calls) PLUS the tool results fed back get appended to
    # the prefix and cached on round N+1. Recording the tool
    # names lets the cost popover explain why a round that
    # "generated" only a few hundred output tokens leads to a
    # multi-thousand-token write next round.
    if api_rounds and api_rounds[-1].get('round') == round_num + 1:
        try:
            _tcs = (assistant_msg or {}).get('tool_calls') or []
            _names = [
                (tc.get('function') or {}).get('name') or '?'
                for tc in _tcs if isinstance(tc, dict)
            ]
            if _names:
                api_rounds[-1]['toolCalls'] = _names
        except Exception as _te:
            logger.debug('[%s] tool-call stamp failed: %s', tid, _te)
        # Stamp the EXACT decomposition of this round's `write`
        # into {toolResults, prevOutput, envelope} computed from
        # real recorded usage (see _compute_write_breakdown). The
        # frontend renders these three sub-items — which sum to
        # exactly `write` — instead of doing the arithmetic (and
        # only proxying it) client-side.
        try:
            # On a turn's round-1 there is no within-turn predecessor
            # (api_rounds has one entry), so the breakdown has no read
            # baseline and would default the whole write to benign
            # contextWrite — even when the PREVIOUS turn's cached
            # prefix was partly evicted and re-billed this round. Feed
            # the cross-turn baseline (prior turn's final cached-prefix
            # read, recovered across the run_task thread boundary) so
            # round-1 classifies an evicted-tail re-bill as recacheBody.
            _prev_turn_read = (
                get_prev_turn_cache_read(task['convId'])
                if len(api_rounds) < 2 else 0)
            _wb = _compute_write_breakdown(
                task, api_rounds, round_num,
                prev_turn_cache_read=_prev_turn_read)
            if _wb:
                api_rounds[-1]['writeBreakdown'] = _wb
        except Exception as _we:
            logger.debug('[%s] write-breakdown stamp failed: %s', tid, _we)

    # Per-round cache stats at INFO level for production visibility
    log_round_cache_stats(
        task['convId'], round_num, usage,
        model=model, tid=task['id'],
    )
