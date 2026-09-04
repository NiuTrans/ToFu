"""lib/tasks_pkg/segments/_derive.py — lossless projections OFF the segment list.

These functions prove the three legacy channels (``content`` / ``thinking`` /
``toolRounds``) are loss-less *projections* of the ordered segment list:
``derive_content`` / ``derive_thinking`` / ``derive_tool_rounds``. Plus the
``deliverable_text`` compat accessor and the shared ``_rounds_view_from_segments``
rebuild that the projection module ``_project`` consumes.

Pure functions; no Flask, no DB, no LLM.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.log import get_logger
from lib.tool_round_identity import (
    execution_batch_keys,
    execution_identity,
    execution_llm_round,
)
from lib.tool_round_replay import SUPERSEDED_PROVIDER_ATTEMPT_FIELD

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
        s.get('text', '') for s in (segments or [])
        if isinstance(s, dict)
        and s.get('type') == SEG_TEXT
        and s.get('deliverable')
        and isinstance(s.get('text', ''), str)
    )


def derive_thinking(segments: list[dict[str, Any]]) -> str:
    """Project the reasoning string from the segment list.

    Byte-identical to today's ``task['thinking']`` (the terminal round's
    reasoning accumulator — per-round thinking lives on the tool_use rounds and
    is NOT part of this projection, matching the current channel semantics).
    """
    for s in (segments or []):
        if (isinstance(s, dict)
                and s.get('type') == SEG_THINKING and s.get('terminal')):
            return s.get('text', '') if isinstance(s.get('text'), str) else ''
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
    if not isinstance(segments, (list, tuple)):
        return []

    batch_text: dict[Any, str] = {}
    batch_think: dict[Any, str] = {}
    batch_sig: dict[Any, str] = {}
    ordered_batch_keys = execution_batch_keys(segments)
    for position, s in enumerate(segments):
        if not isinstance(s, dict):
            continue
        if s.get('terminal'):
            continue
        batch_key = ordered_batch_keys[position]
        st = s.get('type')
        if st == SEG_TEXT and not s.get('deliverable'):
            text = s.get('text')
            if isinstance(text, str):
                batch_text.setdefault(batch_key, text)
        elif st == SEG_THINKING:
            text = s.get('text')
            if isinstance(text, str):
                batch_think.setdefault(batch_key, text)
            if isinstance(s.get('signature'), str) and s['signature']:
                batch_sig.setdefault(batch_key, s['signature'])

    rounds: list[dict[str, Any]] = []
    seen_prose_batches: set = set()
    for position, s in enumerate(segments):
        if not isinstance(s, dict) or s.get('type') != SEG_TOOL_USE:
            continue
        lr = execution_llm_round(s)
        batch_key = ordered_batch_keys[position]
        raw_result = s.get('result')
        result = raw_result if isinstance(raw_result, dict) else {}
        r: dict[str, Any] = {
            'toolCallId': s.get('id', ''),
            'toolName': s.get('name', ''),
            'toolArgs': s.get('input', ''),
            'toolContent': result.get('content'),
            'status': result.get('status'),
            'llmRound': lr,
        }
        attempt_id, task_id = execution_identity(s)
        if attempt_id:
            r['attemptId'] = attempt_id
        if task_id:
            r['taskId'] = task_id
        # Attach the batch prose/thinking to the FIRST tool_use of the batch.
        if batch_key not in seen_prose_batches:
            seen_prose_batches.add(batch_key)
            if batch_text.get(batch_key):
                r['assistantContent'] = batch_text[batch_key]
            if batch_think.get(batch_key):
                r['thinking'] = batch_think[batch_key]
            if batch_sig.get(batch_key):
                r['thinkingSignature'] = batch_sig[batch_key]
        # extraContent (Gemini thought_signature) is thin-stripped — recover it
        # from the rehydrated origin round if present.
        raw_origin = s.get('_round')
        origin = raw_origin if isinstance(raw_origin, Mapping) else {}
        if (s.get(SUPERSEDED_PROVIDER_ATTEMPT_FIELD) is True
                or origin.get(SUPERSEDED_PROVIDER_ATTEMPT_FIELD) is True):
            r[SUPERSEDED_PROVIDER_ATTEMPT_FIELD] = True
        if origin.get('roundNum') is not None:
            r['roundNum'] = origin['roundNum']
        if isinstance(origin.get('extraContent'), dict) and origin['extraContent']:
            r['extraContent'] = dict(origin['extraContent'])
        # Preserve malformed attribution for the shared replay validator to
        # reject. Omitting it here would silently promote the occurrence to a
        # direct root call.
        if 'caller' in origin and origin.get('caller') is not None:
            raw_caller = origin.get('caller')
            r['caller'] = (dict(raw_caller)
                           if isinstance(raw_caller, Mapping) else raw_caller)
        if isinstance(origin.get('_anthropicContentBlocks'), list):
            r['_anthropicContentBlocks'] = origin['_anthropicContentBlocks']
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
    if not isinstance(task, dict):
        return ''
    segs = task.get('segments')
    if segs:
        return derive_content(segs)
    content = task.get('content')
    return content if isinstance(content, str) else ''


def derive_tool_rounds(segments: list[dict[str, Any]]) -> list:
    """Project the ordered tool-round list from the segment list.

    Byte-identical to ``_merge_tool_rounds(task)`` by construction — each
    ``tool_use`` segment mirrors its origin round under ``_round`` and this
    returns them in segment order (which is merged order).
    """
    return [s['_round'] for s in (segments or [])
            if isinstance(s, dict)
            and s.get('type') == SEG_TOOL_USE and '_round' in s]
