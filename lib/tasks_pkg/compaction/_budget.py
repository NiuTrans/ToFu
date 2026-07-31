"""Layer 0 — per-tool budgeting for tool results.

Three public entry points:

  * ``budget_tool_result`` — single-result wrapper called from
    ``lib/tasks_pkg/tool_dispatch.py`` whenever a tool result enters the
    context.  Persists oversized results to disk via ``_persist_to_disk``
    instead of irreversibly truncating.
  * ``enforce_round_aggregate_budget`` — round-level guard against
    parallel tool-call explosion (10 × 30 KB grep results would still
    swallow the context window even though each one is under its
    per-tool cap).
  * ``mark_empty_result`` — empty-string protector that prevents the
    model from misreading a blank tool result as conversation end.

Imports nothing from sibling sub-modules except ``_constants`` and
``_persist``.
"""

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import (
    _BUDGET_EXEMPT_TOOLS,
    _DEFAULT_TOOL_RESULT_MAX,
    _SINGLE_RESULT_HARD_CEILING_CHARS,
    MAX_ROUND_TOOL_RESULTS_CHARS,
    TOOL_RESULT_MAX_CHARS,
)
from lib.tasks_pkg.compaction._persist import _human_size, _persist_to_disk

logger = get_logger(__name__)


#: Longest run of uninterrupted non-whitespace that still looks like prose or
#: source. Real text wraps: even minified JS and long log lines carry spaces or
#: newlines every few hundred chars. A base64 payload or a mis-decoded binary
#: has no such structure, so a single enormous unbroken run is the signal that
#: separates "opaque blob leaked into the text stream" from "the model just
#: read a lot of files".
_OPAQUE_RUN_CHARS = 4_000

#: Sampled window (head) used for the shape test — the decision must not cost
#: a full scan of a multi-megabyte string on the clamp hot path.
_OPAQUE_SAMPLE_CHARS = 200_000


def _looks_like_opaque_blob(content: str) -> bool:
    """True when an oversized result looks like binary/base64, not text.

    Decides WHICH hard-ceiling message the model receives. Deliberately
    shape-based rather than tool-based: a blob can leak through any tool, and
    any tool can legitimately return a lot of text, so the tool NAME cannot
    tell the two apart.

    Judged on the head sample only (bounded work), by the longest run of
    non-whitespace: text of any kind — source, logs, markdown, CJK prose —
    breaks up long before :data:`_OPAQUE_RUN_CHARS`, while base64 and decoded
    binary do not.
    """
    sample = content[:_OPAQUE_SAMPLE_CHARS]
    longest = 0
    run = 0
    for ch in sample:
        if ch.isspace():
            run = 0
            continue
        run += 1
        if run > longest:
            longest = run
            if longest >= _OPAQUE_RUN_CHARS:
                return True
    return False


def clamp_tool_result_text(tool_name: str, content: str,
                           tc_id: str = '', conv_id: str = '') -> str:
    """Tool-agnostic hard ceiling on a single tool-result text (Layer 2).

    The LAST line of defence before a tool result is committed to the
    message stream.  Unlike :func:`budget_tool_result` (Layer 0), this has
    NO per-tool exemptions and NO disk-persist escape hatch — it simply
    refuses to let any single result exceed
    ``_SINGLE_RESULT_HARD_CEILING_CHARS`` chars of text, full stop.

    Its job is to make the "opaque blob floods the context" bug CLASS
    unrepresentable: even if a future ingress point (a new tool, a
    mis-routed binary read, a str()'d image dict) sneaks an oversized blob
    past every earlier layer, it gets head+tail-clamped here into a
    degraded-but-survivable result instead of a fatal context overflow.

    ``__screenshot__`` dicts and other non-str content are passed through
    untouched — images ride the native ``image_url`` protocol and never
    enter the text stream this guards.

    Args:
        tool_name:  Tool that produced the result (for the log + marker).
        content:    Candidate tool-result text.
        tc_id:      Tool-call id (for the log line).
        conv_id:    Conversation id (for the log line).

    Returns:
        ``content`` unchanged if within the ceiling, else a head+tail
        clamp with an explanatory middle marker.
    """
    if not isinstance(content, str):
        return content
    if len(content) <= _SINGLE_RESULT_HARD_CEILING_CHARS:
        return content

    original_len = len(content)
    ceiling = _SINGLE_RESULT_HARD_CEILING_CHARS
    head_budget = int(ceiling * 0.70)
    tail_budget = int(ceiling * 0.25)
    head = content[:head_budget]
    tail = content[-tail_budget:]
    elided = original_len - head_budget - tail_budget

    # WHY two messages: the ceiling fires for two UNRELATED causes, and telling
    # the model the wrong one is worse than saying nothing. A binary/base64
    # leak is a defect the model should report; a 20-file batch read is a
    # perfectly legal request that merely asked for too much at once. The old
    # single message accused every oversized result of leaking binary data, so
    # a legitimate batch read got a false diagnosis of its own behaviour.
    if _looks_like_opaque_blob(content):
        marker = (
            f'\n\n... [⚠ {elided:,} chars elided by hard ceiling — this single '
            f'"{tool_name}" result was {original_len:,} chars (> {ceiling:,} cap). '
            f'This usually means binary/base64 data leaked into a text result. '
            f'Re-read a specific line range or file instead.] ...\n\n'
        )
        logger.error('[HardCeiling] %s result %s exceeded single-result ceiling '
                     '%s — clamped (tc=%s conv=%s). Investigate: opaque blob in '
                     'text stream.',
                     tool_name, _human_size(original_len), _human_size(ceiling),
                     tc_id[:8] if tc_id else '?', conv_id[:8] if conv_id else '?')
    else:
        marker = (
            f'\n\n... [{elided:,} chars elided — this "{tool_name}" call '
            f'returned {original_len:,} chars, over the {ceiling:,}-char '
            f'single-result limit. The content above and below is intact; the '
            f'middle was dropped. Request less at once: fewer paths per call, '
            f'or a specific range via start_line/end_line.] ...\n\n'
        )
        logger.info('[HardCeiling] %s returned %s (> %s ceiling) — clamped '
                    '(tc=%s conv=%s). Text-shaped, so this is an oversized '
                    'legitimate read, not a blob leak.',
                    tool_name, _human_size(original_len), _human_size(ceiling),
                    tc_id[:8] if tc_id else '?', conv_id[:8] if conv_id else '?')
    return head + marker + tail


def budget_tool_result(tool_name: str, content: str,
                       tool_use_id: str = '', conv_id: str = '') -> str:
    """Budget a tool result — persist to disk or pass through.

    For exempt tools (read_files): always pass through unchanged.
    These tools have their own internal limits and truncating them is
    counterproductive (the model would just re-call).

    For other tools: if the content exceeds the per-tool budget, persist
    the full content to disk and return a preview + file path.  The model
    can later use read_files to access the full content.

    Args:
        tool_name:   Name of the tool that produced the result.
        content:     Raw result string.
        tool_use_id: Tool call ID (for persistence filename).
        conv_id:     Conversation ID (for persistence directory).

    Returns:
        Original content if within budget or exempt, or persisted
        preview+path string.
    """
    if not isinstance(content, str):
        return content

    if tool_name in _BUDGET_EXEMPT_TOOLS:
        return content

    max_chars = TOOL_RESULT_MAX_CHARS.get(tool_name, _DEFAULT_TOOL_RESULT_MAX)
    if len(content) <= max_chars:
        return content

    return _persist_to_disk(content, tool_name, tool_use_id, conv_id)


def enforce_round_aggregate_budget(
    tool_results: dict[str, tuple[str, str, str]],
    conv_id: str = '',
) -> dict[str, tuple[str, str, str]]:
    """Enforce per-round aggregate budget on tool results.

    If the total chars of all tool results in one round exceed
    MAX_ROUND_TOOL_RESULTS_CHARS, persist the largest non-exempt results
    to disk until under budget.

    Args:
        tool_results: dict of tc_id → (content, tool_name, tool_use_id)
        conv_id:      Conversation ID for persistence directory.

    Returns:
        Updated tool_results dict (modified in place and returned).
    """
    total_chars = sum(
        len(content) for content, _, _ in tool_results.values()
        if isinstance(content, str)
    )

    if total_chars <= MAX_ROUND_TOOL_RESULTS_CHARS:
        return tool_results

    logger.info('[AggregateBudget] Round total %s exceeds budget %s, '
                'persisting largest results',
                _human_size(total_chars),
                _human_size(MAX_ROUND_TOOL_RESULTS_CHARS))

    candidates = [
        (tc_id, content, tool_name, tool_use_id)
        for tc_id, (content, tool_name, tool_use_id) in tool_results.items()
        if isinstance(content, str)
        and tool_name not in _BUDGET_EXEMPT_TOOLS
        and not content.startswith('[Persisted to:')
    ]
    candidates.sort(key=lambda x: len(x[1]), reverse=True)

    for tc_id, content, tool_name, tool_use_id in candidates:
        if total_chars <= MAX_ROUND_TOOL_RESULTS_CHARS:
            break
        persisted = _persist_to_disk(content, tool_name, tool_use_id, conv_id)
        saved = len(content) - len(persisted)
        total_chars -= saved
        tool_results[tc_id] = (persisted, tool_name, tool_use_id)
        logger.info('[AggregateBudget] Persisted %s result (%s saved), '
                    'new total %s',
                    tool_name, _human_size(saved), _human_size(total_chars))

    return tool_results


def mark_empty_result(tool_name: str, content: str) -> str:
    """Replace empty/whitespace-only tool results with a descriptive marker.

    Inspired by Claude Code's empty result handling which prevents models
    from misinterpreting empty results as conversation end.
    """
    if isinstance(content, str) and not content.strip():
        return f'({tool_name} completed with no output)'
    return content
