"""Attach verified source continuity to complete L2 model prose.

Entry point: accept_summary. The narrative never selects or certifies critical
execution state. This module decorates the candidate without archive I/O or
adoption; the caller admits the complete packet under its final request budget.
"""
from __future__ import annotations

from dataclasses import dataclass

from lib.log import get_logger

# Preserve the entrypoint diagnostic stream across phase extraction.
logger = get_logger('lib.tasks_pkg.compaction._layer2._compact')


@dataclass(frozen=True, slots=True)
class AcceptedSummary:
    """Full recoverable handoff plus its separately persisted narrative."""
    text: str
    turn_diff_included: bool
    continuity_digest: str = ''
    archive_text: str = ''


def accept_summary(
    summary_text: str, messages: list, task: dict | None, *, fallback: bool,
    recent_files_block: str, turn_diff_block: str, turn_diff_included: bool,
    result_meta: dict, log_prefix: str,
    recovery_max_chars: int,
) -> AcceptedSummary | str:
    """Return validated/decorated text or the unchanged-input refusal message."""
    _recovery_base_summary = summary_text if fallback else ''

    summary_text += recent_files_block
    if turn_diff_block:
        summary_text += f'\n\n{turn_diff_block}'

    if (fallback
            and len(summary_text) > recovery_max_chars):
        # Optional freshness hints must not break the emergency receipt's one
        # global budget. Prefer the bounded recovery state, then add recent
        # file handles only if the complete block still fits; drop the turn
        # diff whole rather than cutting any record mid-entry.
        summary_text = _recovery_base_summary
        turn_diff_included = False
        if (recent_files_block
                and len(summary_text + recent_files_block)
                <= recovery_max_chars):
            summary_text += recent_files_block
        logger.warning(
            '%s [Compact] Recovery receipt extras exceeded %d chars; '
            'dropped optional blocks to preserve the global budget',
            log_prefix, recovery_max_chars)
    from lib.tasks_pkg.compaction._continuity import attach_continuity
    narrative = summary_text
    try:
        summary_text, digest = attach_continuity(summary_text, messages, task)
    except (ValueError, TypeError) as exc:
        logger.warning('%s [Compact] continuity validation refused: %s', log_prefix, exc)
        result_meta.update(compacted=False, summaryFailureReason='continuity_invalid')
        return 'Compaction continuity could not be verified; original context preserved.'
    result_meta['continuityDigest'] = digest
    result_meta['archive_summary_text'] = narrative
    return AcceptedSummary(summary_text, turn_diff_included, digest, narrative)
