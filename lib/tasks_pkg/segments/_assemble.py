"""lib/tasks_pkg/segments/_assemble.py — build the ordered typed-segment list.

``assemble_segments`` is populated alongside the three legacy channels
(``content`` / ``thinking`` / ``toolRounds``); those channels are proved to be
loss-less *projections* of the segment list via the ``_derive`` module.

Ordering observer: the interleaving is ALREADY fully captured at finalization
time. Each llmRound batch's pre-tool prose is stamped onto the FIRST entry of
that batch as ``assistantContent`` / ``thinking`` / ``thinkingSignature``
before ``_discard_pretool_prose`` zeroes the accumulators; the terminal round's
deliverable prose survives in ``task['content']`` / ``task['thinking']``. So
the ordered merged list + terminal strings are a complete, lossless record.

``deliverable`` rule (explicit, position-based): a ``text`` segment is
``deliverable=False`` iff it is the ``assistantContent`` of a tool-round batch;
``deliverable=True`` iff it is the terminal ``task['content']``.

Pure functions; no Flask, no DB, no LLM.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

from lib.tasks_pkg.segments._types import (
    SEG_THINKING, SEG_TEXT, SEG_TOOL_USE, RESUMABLE_FINISH_REASONS,
    is_synthetic_inbox_round,
)

logger = get_logger(__name__)


def _merged_rounds(task: dict[str, Any], merged: list | None) -> list:
    """Return the ordered checkpoint+current tool rounds.

    Lazy-imports ``_merge_tool_rounds`` to avoid a module-level import cycle
    (``manager`` imports this module to call ``assemble_segments``). Callers on
    the hot path (``persist_task_result``) pass the already-computed merged
    list so the merge runs once.
    """
    if merged is not None:
        return merged
    from lib.tasks_pkg.manager import _merge_tool_rounds
    return _merge_tool_rounds(task)


def assemble_segments(task: dict[str, Any],
                      merged: list | None = None) -> list[dict[str, Any]]:
    """Build the ordered typed-segment list for a finished assistant turn.

    Args:
        task: the task dict (reads ``toolRounds`` / ``_checkpointToolRounds``
            via the merge, plus terminal ``content`` / ``thinking``).
        merged: optional pre-computed ``_merge_tool_rounds(task)`` output, to
            avoid a redundant merge on the persist hot path.

    Returns:
        An ordered list of segment dicts. Types: ``thinking``, ``text``
        (with ``deliverable`` bool), ``tool_use`` (with a nested ``result``).
        Each ``tool_use`` also carries ``_round`` — a reference to the original
        merged round dict — so ``derive_tool_rounds`` is byte-identical to
        ``_merge_tool_rounds`` BY CONSTRUCTION (the lossless-superset proof).
        The ``_round`` mirror is retired once readers migrate off ``toolRounds``
        (design §5 step 4/6); until then it is what lets step 1 ship dark with a
        provable byte-identity gate rather than a fragile field-by-field rebuild.
    """
    rounds = _merged_rounds(task, merged)
    segments: list[dict[str, Any]] = []
    seen_batches: set = set()

    for idx, r in enumerate(rounds):
        if not isinstance(r, dict):
            continue
        # Wire-purity guard: frontend display-only inbox-inject rows (async
        # <swarm-update> / peer / user-steer chips) carry no tool_call data and
        # must NOT become tool_use segments — that would mint a phantom wire
        # tool and shift the prefix cache. They live in a separate display-only
        # sidecar. See _types.is_synthetic_inbox_round.
        if is_synthetic_inbox_round(r):
            continue
        lr = r.get('llmRound')
        # Batch key: real tool-call rounds carry an integer llmRound
        # (tool_dispatch.py stamps round_entry['llmRound']). Rounds that BYPASS
        # that path — prefetch fetch_url (executor.py:532) and image-gen
        # progress rounds — have NO llmRound (None). Keying the dedup on the
        # raw llmRound would collapse EVERY None round into one phantom batch;
        # today that's harmless (None-llmRound rounds never carry
        # assistantContent/thinking) but it's fragile. Give each None round its
        # own batch identity (by position) so a future prose-bearing shape can
        # never be silently swallowed. Integer llmRounds still dedup correctly
        # (two tool calls in one assistant turn share llmRound → prose once).
        batch_key = lr if lr is not None else ('__no_llmround__', idx)
        # The pre-tool prose + thinking of an llmRound batch is stamped onto the
        # FIRST entry of that batch. Emit those segments once per batch, in
        # order (thinking before the prose it preceded).
        if batch_key not in seen_batches:
            seen_batches.add(batch_key)
            think = r.get('thinking')
            if think:
                seg: dict[str, Any] = {
                    'type': SEG_THINKING, 'text': think,
                    'deliverable': False, 'llmRound': lr,
                }
                sig = r.get('thinkingSignature')
                if sig:
                    seg['signature'] = sig
                segments.append(seg)
            ac = r.get('assistantContent')
            if ac:
                segments.append({
                    'type': SEG_TEXT, 'text': ac,
                    'deliverable': False, 'llmRound': lr,
                })
        # Every round entry becomes a tool_use segment with its result nested,
        # so a tool and its output are one renderable unit.
        segments.append({
            'type': SEG_TOOL_USE,
            'id': r.get('toolCallId', ''),
            'name': r.get('toolName', ''),
            'input': r.get('toolArgs', ''),
            'llmRound': lr,
            'result': {'content': r.get('toolContent'),
                       'status': r.get('status')},
            '_round': r,
        })

    # ── Terminal round: the deliverable prose + its thinking ──
    # task['content'] / task['thinking'] hold the LAST round's output (reset
    # each tool round). Any Sources-footer / content-filter override applied in
    # _finalize_and_emit_done is already folded into task['content'] by the time
    # we assemble, so the deliverable segment captures it verbatim.
    term_think = task.get('thinking') or ''
    if term_think:
        segments.append({
            'type': SEG_THINKING, 'text': term_think,
            'deliverable': False, 'terminal': True,
        })
    term_content = task.get('content') or ''
    if term_content:
        term_seg: dict[str, Any] = {
            'type': SEG_TEXT, 'text': term_content,
            'deliverable': True, 'terminal': True,
        }
        # A turn cut off mid-answer leaves a RESUMABLE deliverable prefix.
        # Marked here (additive, dark) off the finish reason so a persisted
        # final row carries the signal; resume_prefill_from_segments also
        # accepts a finish_reason override for rows assembled at checkpoint
        # time (status='running', no finishReason yet).
        if (task.get('finishReason') or '') in RESUMABLE_FINISH_REASONS:
            term_seg['resumable'] = True
        segments.append(term_seg)

    return segments
