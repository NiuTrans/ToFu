"""lib/optimizer/analyzer/_metrics.py — prior-action post-apply metrics.

For each still-active applied action lacking a recorded outcome, compute a
simple count-based metric and persist it back to the action log.  The
``storage`` module is accessed through the facade package so that
``monkeypatch.setattr(analyzer.storage, ...)`` in tests is observed here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from lib.log import get_logger

from lib.optimizer import analyzer as _facade
from ._logs import _parse_app_log_ts, _safe_tail_lines

logger = get_logger(__name__)


def _summarize_block_search_log_metrics(
    domains: Sequence[str],
    cutoff_local: datetime,
    *,
    log_lines: Sequence[str] | None = None,
) -> tuple[dict[str, int], int]:
    """Count every tracked domain and tool failure in one app-log pass."""
    unique_domains = tuple(dict.fromkeys(domain for domain in domains if domain))
    domain_patterns = {
        domain: re.compile(
            r'\[Search\].*?IRRELEVANT.*?' + re.escape(domain),
            re.IGNORECASE,
        )
        for domain in unique_domains
    }
    domain_counts = dict.fromkeys(unique_domains, 0)
    tool_failure_pattern = re.compile(r'\[Tool:[^\]]+\] failed')
    tool_failure_count = 0
    lines = log_lines
    if lines is None:
        lines = _safe_tail_lines(_facade.APP_LOG)
    for line in lines:
        has_tool_failure = tool_failure_pattern.search(line) is not None
        lower_line = line.lower()
        may_match_domain = (
            '[search]' in lower_line and 'irrelevant' in lower_line)
        if not has_tool_failure and not may_match_domain:
            continue
        timestamp = _parse_app_log_ts(line)
        if timestamp is None or timestamp < cutoff_local:
            continue
        if has_tool_failure:
            tool_failure_count += 1
        if may_match_domain:
            for domain, pattern in domain_patterns.items():
                if pattern.search(line):
                    domain_counts[domain] += 1
    return domain_counts, tool_failure_count


def _compute_post_apply_metrics(
    cutoff_local: datetime,
    *,
    owner_user_id: int,
    allow_unowned_observability: bool,
    app_log_lines: Sequence[str] | None = None,
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

    prepared_actions: list[tuple[dict, dict, bool]] = []
    block_search_domains: list[str] = []
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
        prepared_actions.append((row, args, has_outcome))
        if action_type == 'block_search_domain':
            block_search_domains.append(
                str(args.get('domain') or '').lower())

    dropped_by_domain: dict[str, int] = {}
    tool_error_count = 0
    if allow_unowned_observability and block_search_domains:
        dropped_by_domain, tool_error_count = (
            _summarize_block_search_log_metrics(
                block_search_domains,
                cutoff_local,
                log_lines=app_log_lines,
            )
        )

    for row, args, has_outcome in prepared_actions:
        action_type = row.get('p_action_type') or ''
        log_id = row['id']
        metric: dict[str, Any] = {}

        if action_type == 'block_search_domain':
            domain = str(args.get('domain') or '').lower()
            if allow_unowned_observability:
                dropped = dropped_by_domain.get(domain, 0)
                tool_errs = tool_error_count
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
