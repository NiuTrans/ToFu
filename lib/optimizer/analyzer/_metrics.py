"""lib/optimizer/analyzer/_metrics.py — prior-action post-apply metrics.

For each still-active applied action lacking a recorded outcome, compute a
simple count-based metric and persist it back to the action log.  The
``storage`` module is accessed through the facade package so that
``monkeypatch.setattr(analyzer.storage, ...)`` in tests is observed here.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from lib.log import get_logger

from lib.optimizer import analyzer as _facade
from ._domains import _count_irrelevant_dropped_for_domain
from ._signals import _count_tool_errors

logger = get_logger(__name__)


def _compute_post_apply_metrics(
    cutoff_local: datetime,
    *,
    owner_user_id: int,
    allow_unowned_observability: bool,
) -> list[dict]:
    """For each still-active applied action without a recorded outcome,
    compute a simple count-based metric and persist it."""
    summaries: list[dict] = []
    try:
        actions = _facade.storage.list_applied_actions(
            owner_user_id=owner_user_id,
            include_reverted=True,
            limit=100,
        )
    except Exception as e:
        logger.warning('[Optimizer.analyzer] could not list prior actions: %s', e)
        return summaries

    for row in actions:
        action_type = row.get('p_action_type') or ''
        args_raw = row.get('p_action_args') or '{}'
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Optimizer.analyzer] bad action_args for %s: %s',
                         row.get('id'), e)
            args = {}

        outcome_raw = row.get('outcome_metric') or ''
        has_outcome = bool(outcome_raw and outcome_raw not in ('{}', 'null'))
        log_id = row['id']
        metric: dict[str, Any] = {}

        if action_type == 'block_search_domain':
            domain = str(args.get('domain') or '').lower()
            if allow_unowned_observability:
                dropped = _count_irrelevant_dropped_for_domain(
                    domain, cutoff_local)
                tool_errs = _count_tool_errors(cutoff_local)
            else:
                dropped = 0
                tool_errs = 0
            metric = {
                'domain': domain,
                'irrelevant_dropped_24h': dropped,
                'total_tool_errors_24h': tool_errs,
                'unscoped_observability_available': (
                    allow_unowned_observability),
                'interpretation': (
                    'near-zero drops → block working; high drops → may no longer'
                    ' be needed or need broader match'),
            }
        else:
            metric = {'note': 'no auto-metric for this action_type'}

        if not has_outcome:
            try:
                _facade.storage.record_outcome_metric(
                    log_id, metric, owner_user_id=owner_user_id)
            except Exception as e:
                logger.warning('[Optimizer.analyzer] record_outcome_metric '
                               'failed for %s: %s', log_id, e)

        summaries.append({
            'id': log_id,
            'proposal_id': row.get('proposal_id'),
            'action_type': action_type,
            'args': args,
            'applied_at': row.get('applied_at'),
            'expires_at': row.get('expires_at'),
            'reverted_at': row.get('reverted_at') or '',
            'proposal_status': row.get('p_status'),
            'outcome_metric': metric,
        })
    return summaries
