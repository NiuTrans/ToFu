"""Validate L2 summary fidelity and fit optional deterministic recovery hints.

Entry point: accept_summary. Critical state comes from the existing task-state
owner; this module records rejection evidence and returns accepted text without
archive I/O or model-view adoption. Bounded live summaries never substitute lossy
fallback prose for missing critical state.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from lib.log import get_logger

# Preserve the entrypoint diagnostic stream across phase extraction.
logger = get_logger('lib.tasks_pkg.compaction._layer2._compact')


@dataclass(frozen=True, slots=True)
class AcceptedSummary:
    text: str
    turn_diff_included: bool


def accept_summary(
    summary_text: str, messages: list, task: dict | None, *, fallback: bool,
    recent_files_block: str, turn_diff_block: str, turn_diff_included: bool,
    result_meta: dict, log_prefix: str, missing_state_fields: Callable,
    recovery_max_chars: int, is_bounded: Callable,
) -> AcceptedSummary | str:
    """Return validated/decorated text or the unchanged-input refusal message."""
    _recovery_base_summary = summary_text if fallback else ''

    # V2 validates critical state before accepting lossy model prose. If the
    # model omitted unfinished work or grounded mutation/test evidence, reject
    # that prose and continue with a deterministic transcript-derived state
    # projection. This evicts cold history without claiming the rejected
    # summary was faithful.
    _comp_cfg = ((task or {}).get('config', {}) or {}).get('compaction') or {}
    if (not fallback
            and str(_comp_cfg.get('strategy') or '').lower() == 'adaptive'):
        try:
            import json as _json
            from lib.tasks_pkg.context_composer.task_state import (
                derive_task_state_snapshot,
            )
            _ledger = (task or {}).get('_contextEvidenceLedger') or {}
            _critical_types = {
                'test_result', 'error', 'mutation_result', 'modified_file',
                'unfinished',
            }
            _critical = [entry for entry in (_ledger.get('entries') or [])
                         if entry.get('type') in _critical_types]
            _missing_ids = [str(entry.get('id') or '') for entry in _critical
                            if str(entry.get('id') or '') not in summary_text]
            _snapshot = derive_task_state_snapshot(messages, task)
            _missing_fields = missing_state_fields(
                summary_text, _snapshot)
            _state_block = (
                '## TaskStateSnapshotV1\n' + _snapshot.to_context_text())
            _evidence_block = (
                '## Critical Evidence\n' + '\n'.join(
                    f"[EVIDENCE {entry.get('id')}] "
                    + _json.dumps(entry, ensure_ascii=False,
                                  sort_keys=True, separators=(',', ':'))
                    for entry in _critical[:32]))
            if _missing_ids or _missing_fields:
                if is_bounded(task):
                    if isinstance(result_meta, dict):
                        result_meta.update(
                            compacted=False, summaryRejected=True,
                            summaryFailureReason='critical_state_missing',
                            summaryRejectionReason='critical_state_missing',
                            missingEvidenceIds=_missing_ids,
                            missingTaskStateFields=_missing_fields)
                    return ('Summary omitted critical state; original context '
                            'preserved without replacement.')
                _rejected_digest = hashlib.sha256(
                    summary_text.encode('utf-8')).hexdigest()[:16]
                summary_text = _state_block + '\n\n' + _evidence_block
                logger.warning(
                    '%s [Compact] rejected lossy summary digest=%s '
                    'missing_critical=%d missing_fields=%s; using '
                    'deterministic state projection', log_prefix, _rejected_digest,
                    len(_missing_ids), ','.join(_missing_fields))
                if isinstance(result_meta, dict):
                    result_meta.update({
                        'summaryRejected': True,
                        'summaryRejectedDigest': _rejected_digest,
                        'summaryRejectionReason': 'critical_state_missing',
                        'missingEvidenceIds': _missing_ids,
                        'missingTaskStateFields': _missing_fields,
                    })
            else:
                # Adaptive L2 always carries the versioned state projection;
                # accepted model prose is only the short narrative layer.
                summary_text = (
                    _state_block + '\n\n## Narrative Summary\n'
                    + summary_text + '\n\n' + _evidence_block)
        except Exception as exc:
            logger.warning('%s [Compact] v2 summary validation failed closed: %s',
                           log_prefix, exc)
            if isinstance(result_meta, dict):
                result_meta.update({
                    'compacted': False,
                    'summaryRejected': True,
                    'summaryRejectionReason': 'validation_failed',
                })
            return ('Context compaction attempted but summary validation '
                    'failed. Messages preserved as-is.')

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
    return AcceptedSummary(summary_text, turn_diff_included)
