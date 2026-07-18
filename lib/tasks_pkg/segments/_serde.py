"""lib/tasks_pkg/segments/_serde.py — persistable ("thin") form + rehydration.

``segments_to_json`` strips the ``_round`` mirror (a full copy of the origin
round dict, already persisted in the sibling ``tool_rounds`` column) so the
persisted segments don't double the payload or become a second source of truth.
``rehydrate_segments`` is the inverse — re-zips the k-th ``tool_use`` segment
with the k-th co-persisted round so ``derive_tool_rounds`` is byte-identical
again.

Pure functions; no Flask, no DB, no LLM.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

from lib.tasks_pkg.segments._types import SEG_TOOL_USE

logger = get_logger(__name__)


def segments_to_json(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the PERSISTABLE ("thin") form of the segment list.

    Strips the ``_round`` mirror off every ``tool_use`` segment. ``_round``
    embeds the ENTIRE origin round dict (assistantContent / toolArgs / thinking
    / results / …), which is already persisted verbatim in the sibling
    ``task_results.tool_rounds`` column and ``last_msg['toolRounds']``. Keeping
    it inside ``segments`` too would double the largest payload AND create a
    second source of truth that can drift from the ``toolRounds`` column.

    The thin form keeps everything a reader needs WITHOUT ``toolRounds``:
    ``thinking`` / ``text`` (with ``deliverable``) segments are complete, and a
    ``tool_use`` keeps ``id`` / ``name`` / ``input`` / ``llmRound`` / ``result``
    (the nested ``{content,status}``) — enough for the compat surfaces (step 3)
    to render block-by-block. The full round is recoverable via
    ``rehydrate_segments`` when ``derive_tool_rounds`` is needed (step 4).

    Returns NEW segment dicts (shallow copies); the input is not mutated.
    """
    out: list[dict[str, Any]] = []
    for s in segments:
        if s.get('type') == SEG_TOOL_USE and '_round' in s:
            s = {k: v for k, v in s.items() if k != '_round'}
        out.append(s)
    return out


def rehydrate_segments(thin_segments: list[dict[str, Any]],
                       tool_rounds: list) -> list[dict[str, Any]]:
    """Re-attach the ``_round`` mirror to a thin (persisted) segment list.

    The inverse of ``segments_to_json``: walks the ``tool_use`` segments in
    order and re-zips each with the correspondingly-ordered entry of
    ``tool_rounds`` (assembly emits exactly one ``tool_use`` per merged round,
    in merged order — so the k-th ``tool_use`` segment maps to the k-th round).
    After rehydration ``derive_tool_rounds`` is byte-identical to
    ``_merge_tool_rounds`` again, proving the strip is LOSSLESS given
    ``tool_rounds`` was co-persisted.

    Non-``tool_use`` segments pass through unchanged. If the counts disagree
    (should never happen for a co-persisted pair) the surplus ``tool_use``
    segments are left thin — a reader that needs ``_round`` will simply skip
    them in ``derive_tool_rounds`` rather than crash.

    Returns NEW segment dicts; inputs are not mutated.
    """
    out: list[dict[str, Any]] = []
    tu_idx = 0
    for s in thin_segments:
        if s.get('type') == SEG_TOOL_USE:
            if tu_idx < len(tool_rounds):
                s = {**s, '_round': tool_rounds[tu_idx]}
            tu_idx += 1
        out.append(s)
    return out
