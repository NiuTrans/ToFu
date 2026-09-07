"""Detect a tool name that is the literal concatenation of REAL tool names.

Why this exists (2026-07-27, conv ms2vpi7jned92h): the SSE accumulator used to
append tool names with ``+=``, so two tool calls landing in one slot fused into
one undispatchable name (``read_filesrun_command``). That root cause is fixed in
``lib/llm/_sse_core.py`` — a name is now assigned once and a conflicting name
opens a new slot.

This module is the DEFENSIVE ASSERTION behind that fix, not the fix itself. If a
concatenated name ever reaches ingestion again — via a protocol line we have not
exercised, or a future regression — we want it named as OUR defect, loudly, in
one place, instead of being silently misfiled as a model hallucination. It never
repairs the call: the arguments of the fused calls are already interleaved, so
re-splitting them would be guessing at model intent.

The judgement is deliberately strict: a name counts as our concatenation only
when it segments into known tool names in EXACTLY ONE way. Ambiguity means the
evidence is inconclusive, and an inconclusive verdict must not be used to blame
either side.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# Segmentation guards. A fused name in practice is 2 calls (occasionally 3 when
# a burst collided); anything longer is not worth exploring and the recursion is
# capped so a pathological name can never turn into a combinatorial walk.
_MAX_PARTS = 4
_MAX_SOLUTIONS = 2  # we only need to know "exactly one" vs "more than one"


def split_concatenated_tool_name(name: str,
                                 known: set[str] | None) -> list[str] | None:
    """Return the unique segmentation of ``name`` into ``known`` tool names.

    Args:
        name: The tool name exactly as it arrived (post-alias).
        known: The live REAL-tool set for this turn. The membership oracle —
            pass the same set used for hallucination classification so the
            verdict is made against the tools actually dispatched.

    Returns:
        The list of real tool names (length >= 2) when ``name`` is their
        lossless concatenation AND that segmentation is unique. ``None`` when
        the name does not segment at all (a genuine invention), when it IS a
        real tool, or when more than one segmentation exists (inconclusive).
    """
    if not name or not known or name in known:
        return None

    solutions: list[list[str]] = []
    # Only names that actually prefix-match are worth trying, and the empty
    # string would recurse forever.
    candidates = [k for k in known if k and k in name]
    if not candidates:
        return None

    def _walk(rest: str, acc: list[str]) -> None:
        if len(solutions) >= _MAX_SOLUTIONS:
            return
        if not rest:
            if len(acc) >= 2:
                solutions.append(list(acc))
            return
        if len(acc) >= _MAX_PARTS:
            return
        for part in candidates:
            if rest.startswith(part):
                acc.append(part)
                _walk(rest[len(part):], acc)
                acc.pop()
                if len(solutions) >= _MAX_SOLUTIONS:
                    return

    try:
        _walk(name, [])
    except RecursionError as e:
        logger.warning('[ToolRepair] concat-split aborted for %.80r: %s', name, e)
        return None

    if len(solutions) == 1:
        return solutions[0]
    return None
