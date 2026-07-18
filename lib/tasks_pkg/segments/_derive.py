"""lib/tasks_pkg/segments/_derive.py — lossless projections OFF the segment list.

These functions prove the three legacy channels (``content`` / ``thinking`` /
``toolRounds``) are loss-less *projections* of the ordered segment list:
``derive_content`` / ``derive_thinking`` / ``derive_tool_rounds``. Plus the
``deliverable_text`` compat accessor and the shared ``_rounds_view_from_segments``
rebuild that the projection module ``_project`` consumes.

Pure functions; no Flask, no DB, no LLM.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

from lib.tasks_pkg.segments._types import SEG_THINKING, SEG_TEXT, SEG_TOOL_USE

logger = get_logger(__name__)


def derive_content(segments: list[dict[str, Any]]) -> str:
    """Project the deliverable answer string from the segment list.

    Byte-identical to today's ``task['content']``: the concatenation of
    ``text`` segments flagged ``deliverable`` (only the terminal round produces
    one in the current pipeline). Inter-round narration (``deliverable=False``)
    is excluded — this is the boundary the headless narrator fix (step 3) keys
    on.
    """
    return ''.join(
        s.get('text', '') for s in segments
        if s.get('type') == SEG_TEXT and s.get('deliverable')
    )


def derive_thinking(segments: list[dict[str, Any]]) -> str:
    """Project the reasoning string from the segment list.

    Byte-identical to today's ``task['thinking']`` (the terminal round's
    reasoning accumulator — per-round thinking lives on the tool_use rounds and
    is NOT part of this projection, matching the current channel semantics).
    """
    for s in segments:
        if s.get('type') == SEG_THINKING and s.get('terminal'):
            return s.get('text', '')
    return ''


def _rounds_view_from_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild a per-round view (the `toolRounds` shape the reconstructors
    consume) from the SEGMENT structure — sourcing every field from the
    segments, not from a passed-in rounds list.

    This is what makes the reconstruction genuinely *segment-driven* rather
    than a `derive_tool_rounds` tautology: it reads `tool_use.id/name/input/
    result`, pairs each batch's `deliverable:false` text segment as
    `assistantContent` and its `thinking` segment (+signature) — exactly the
    fields `_reconstruct_tool_call_messages` / `inject_tool_history` need. The
    ONLY field not present in a thin (persisted) segment is Gemini's
    `extraContent`; it is pulled from the rehydrated `_round` mirror when
    present (so callers rehydrate first).

    Batch prose is attached to the FIRST tool_use of each llmRound, matching
    the "first-seen assistantContent in batch" rule of the reconstructors.
    """
    # Pre-scan: per-batch prose + thinking (from the non-terminal text/thinking
    # segments assemble_segments emits once per llmRound batch).
    batch_text: dict[Any, str] = {}
    batch_think: dict[Any, str] = {}
    batch_sig: dict[Any, str] = {}
    for s in segments:
        if s.get('terminal'):
            continue
        lr = s.get('llmRound')
        st = s.get('type')
        if st == SEG_TEXT and not s.get('deliverable'):
            batch_text.setdefault(lr, s.get('text', ''))
        elif st == SEG_THINKING:
            batch_think.setdefault(lr, s.get('text', ''))
            if s.get('signature'):
                batch_sig.setdefault(lr, s['signature'])

    rounds: list[dict[str, Any]] = []
    seen_prose_batches: set = set()
    for s in segments:
        if s.get('type') != SEG_TOOL_USE:
            continue
        lr = s.get('llmRound')
        result = s.get('result') or {}
        r: dict[str, Any] = {
            'toolCallId': s.get('id', ''),
            'toolName': s.get('name', ''),
            'toolArgs': s.get('input', ''),
            'toolContent': result.get('content'),
            'status': result.get('status'),
            'llmRound': lr,
        }
        # Attach the batch prose/thinking to the FIRST tool_use of the batch.
        if lr not in seen_prose_batches:
            seen_prose_batches.add(lr)
            if batch_text.get(lr):
                r['assistantContent'] = batch_text[lr]
            if batch_think.get(lr):
                r['thinking'] = batch_think[lr]
            if batch_sig.get(lr):
                r['thinkingSignature'] = batch_sig[lr]
        # extraContent (Gemini thought_signature) is thin-stripped — recover it
        # from the rehydrated origin round if present.
        origin = s.get('_round') or {}
        if origin.get('extraContent'):
            r['extraContent'] = origin['extraContent']
        rounds.append(r)
    return rounds


def deliverable_text(task: dict[str, Any]) -> str:
    """The narration-free deliverable answer for a headless/compat consumer.

    THE single source of truth for "what text is the answer" on the compat
    surfaces (sync + streaming, OpenAI + Anthropic). Prefers the segment model
    (`derive_content` over `task['segments']`, i.e. concat of `deliverable:true`
    text — inter-round narration excluded by construction); falls back to
    `task['content']` when segments are absent (e.g. an in-flight task whose
    segments haven't been assembled yet at persist time). Both yield the same
    clean deliverable — `task['content']` is already narration-free post
    `_discard_pretool_prose` — so the fallback is safe, not lossy.
    """
    segs = task.get('segments')
    if segs:
        return derive_content(segs)
    return task.get('content') or ''


def derive_tool_rounds(segments: list[dict[str, Any]]) -> list:
    """Project the ordered tool-round list from the segment list.

    Byte-identical to ``_merge_tool_rounds(task)`` by construction — each
    ``tool_use`` segment mirrors its origin round under ``_round`` and this
    returns them in segment order (which is merged order).
    """
    return [s['_round'] for s in segments
            if s.get('type') == SEG_TOOL_USE and '_round' in s]
