"""lib/swarm/result_format.py — Result compression and formatting utilities.

Extracted from protocol.py for modularity.

Contains:
  • _CODE_BLOCK_RE      — regex for fenced code blocks
  • compress_result()   — fit agent output into a context budget
  • format_sub_results_for_master() — format multiple results for synthesis
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from lib.log import get_logger

if TYPE_CHECKING:
    from lib.swarm.protocol import SubAgentResult, SubTaskSpec

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════

# Import the budget constant from protocol (canonical location)
# We also define it here so result_format can be used standalone
MAX_COMPRESSED_RESULT_CHARS: int = 8000
"""Default character budget for compress_result()."""


# ═══════════════════════════════════════════════════════════
#  Result Compression — fit results into context budget
# ═══════════════════════════════════════════════════════════

# Regex to find fenced code blocks (``` ... ```)
_CODE_BLOCK_RE = re.compile(r'(```[\s\S]*?```)', re.MULTILINE)


def compress_result(text: Any, max_chars: int = MAX_COMPRESSED_RESULT_CHARS,
                    role: str = '') -> str:
    """Compress a sub-agent's result for master consumption.

    Strategy:
      1. If empty/None, return a placeholder.
      2. If short enough, return as-is.
      3. If it contains code blocks, try to preserve them.
      4. Otherwise, truncate with head + tail preservation.

    Args:
        text: The result text (or any stringifiable value).
              Accepts None, str, or any object with __str__.
        max_chars: Maximum character budget for the output.
        role: Optional role hint (unused currently, reserved for future
              role-specific compression strategies).

    Returns:
        A string that fits within ``max_chars`` (approximately).
    """
    # Handle None / empty
    if text is None or (isinstance(text, str) and not text.strip()):
        return '(no result)' if not text else '(no output)'
    if not isinstance(text, str):
        text = str(text)

    # Short enough — return as-is
    if len(text) <= max_chars:
        return text

    logger.debug('[Protocol] compress_result: text_len=%d > max=%d, compressing (role=%s)',
                 len(text), max_chars, role or 'unset')

    # ── Try to preserve code blocks ──
    code_blocks = _CODE_BLOCK_RE.findall(text)
    if code_blocks:
        # Keep the first code block(s) that fit, plus surrounding text
        preserved_code = '\n\n'.join(code_blocks)
        if len(preserved_code) < max_chars * 0.8:
            # Code fits — add as much surrounding text as possible
            remaining = max_chars - len(preserved_code) - 50
            before_code = text[:text.index(code_blocks[0])]
            before_truncated = before_code[:max(remaining // 2, 100)]
            return (
                before_truncated.rstrip()
                + '\n\n'
                + preserved_code
                + '\n\n[… remaining output truncated]'
            )[:max_chars + 20]  # slight grace for the marker

    # ── Head + tail truncation ──
    marker = ' … '
    available = max_chars - len(marker)
    if available < 10:
        return text[:max_chars]
    head_size = int(available * 0.75)
    tail_size = available - head_size
    return text[:head_size] + marker + text[-tail_size:]


def format_sub_results_for_master(
    results: list[tuple[SubTaskSpec, SubAgentResult]],
    max_chars_per_result: int = 4000,
) -> str:
    """Format multiple sub-agent results into a text block for the master.

    Each result is formatted with its spec metadata (role, objective) and
    status, then compressed to fit within the per-result budget.

    Args:
        results: List of (SubTaskSpec, SubAgentResult) tuples.
        max_chars_per_result: Character budget per individual result.

    Returns:
        A formatted string suitable for injection into a synthesis prompt.
    """
    # Import here to avoid circular imports at module level
    from lib.swarm.protocol import SubAgentStatus

    if not results:
        logger.debug('[Protocol] format_sub_results_for_master: no results')
        return '(no sub-agent results)'

    logger.debug('[Protocol] format_sub_results_for_master: %d results, budget=%d chars each',
                len(results), max_chars_per_result)
    parts: list[str] = []
    for i, (spec, result) in enumerate(results):
        status_icon = '✅' if result.status == SubAgentStatus.COMPLETED.value else '❌'
        retried = f' (retried {result.retry_count}x)' if result.retry_count > 0 else ''

        # Use final_answer preferentially, fall back to answer
        answer_text = result.final_answer or getattr(result, 'answer', '') or ''

        header = (
            f'## Agent {i + 1}: [{spec.role}] {spec.objective[:80]}\n'
            f'Status: {status_icon} {result.status}{retried}'
        )
        if result.rounds_used:
            header += f' | Rounds: {result.rounds_used}'
        if result.total_tokens:
            header += f' | Tokens: {result.total_tokens:,}'

        if result.status == SubAgentStatus.FAILED.value:
            body = f'Error: {result.error_message or "(no error message)"}'
            if answer_text:
                body += f'\nPartial output: {compress_result(answer_text, max_chars=max_chars_per_result // 2)}'
        else:
            body = compress_result(answer_text, max_chars=max_chars_per_result)

        parts.append(f'{header}\n\n{body}')

    return '\n\n---\n\n'.join(parts)
