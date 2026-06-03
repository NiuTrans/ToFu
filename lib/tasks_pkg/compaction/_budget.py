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
    MAX_ROUND_TOOL_RESULTS_CHARS,
    TOOL_RESULT_MAX_CHARS,
)
from lib.tasks_pkg.compaction._persist import _human_size, _persist_to_disk

logger = get_logger(__name__)


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
