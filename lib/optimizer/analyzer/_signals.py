"""lib/optimizer/analyzer/_signals.py — DB-backed signal collectors.

Scheduler failing/idle-proactive scan, cost-outlier surfacing from
daily_cost_cache, recent-conversation tool distribution, and the app.log
tool-error counter used by post-apply metrics.  All DB collectors degrade
gracefully (return empty counters) on any error.  Mutable log paths are
read via the facade package for test monkeypatching.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from lib.log import get_logger

from lib.optimizer import analyzer as _facade
from ._logs import _safe_tail_lines, _parse_app_log_ts
from ._domains import _domain_of

logger = get_logger(__name__)


def _collect_scheduler_signals(*, owner_user_id: int) -> dict:
    """Mine scheduled_tasks rows for failing / idle-proactive tasks."""
    try:
        from lib.storage import get_storage_client
        rows = get_storage_client().query(
            'scheduler.task.list', {
                'user_id': owner_user_id,
                'limit': 1000,
                'enabled_only': False,
            }) or []
    except Exception as e:
        logger.warning('[Optimizer.analyzer] scheduler scan skipped: %s', e)
        return {'failing_scheduled_tasks': [], 'idle_proactive_tasks': []}

    failing: list[dict] = []
    idle_proactive: list[dict] = []
    for r in rows:
        row = dict(r)
        run_count = int(row.get('run_count') or 0)
        fail_count = int(row.get('fail_count') or 0)
        if run_count >= 5 and fail_count >= max(3, run_count // 2):
            failing.append({
                'id': row['id'],
                'name': row.get('name', ''),
                'task_type': row.get('task_type', ''),
                'run_count': run_count,
                'fail_count': fail_count,
                'fail_ratio': round(fail_count / max(1, run_count), 2),
            })
        if row.get('task_type') == 'agent':
            poll = int(row.get('poll_count') or 0)
            execs = int(row.get('execution_count') or 0)
            if poll >= 20 and execs == 0:
                idle_proactive.append({
                    'id': row['id'],
                    'name': row.get('name', ''),
                    'poll_count': poll,
                    'execution_count': execs,
                    'schedule': row.get('schedule', ''),
                })
    return {
        'failing_scheduled_tasks': failing[:10],
        'idle_proactive_tasks': idle_proactive[:10],
    }


def _collect_cost_outliers(*, owner_user_id: int) -> dict:
    """Surface top-cost conversations from daily_cost_cache (no full scan)."""
    try:
        from lib.storage import get_storage_client
        row = get_storage_client().query(
            'daily_cost.latest', {'user_id': owner_user_id})
    except Exception as e:
        logger.debug('[Optimizer.analyzer] cost cache scan skipped: %s', e)
        return {'top_cost_conversations': []}
    if not row:
        return {'top_cost_conversations': []}
    data = row.get('conversations') or {}
    if not isinstance(data, dict):
        return {'top_cost_conversations': []}
    def _cost_of(v):
        # daily_cost_cache stores either a flat number (legacy) or
        # {"cost": <num>, ...} per conv (current).  Tolerate both.
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            for key in ('cost', 'total', 'usd', 'total_cost'):
                if key in v:
                    try:
                        return float(v[key] or 0)
                    except (TypeError, ValueError) as _e_audit:
                        logger.debug('[analyzer] _cost_of caught %s: %s', type(_e_audit).__name__, _e_audit)
                        return 0.0
        return 0.0

    pairs = sorted(
        ((cid, _cost_of(v)) for cid, v in data.items()),
        key=lambda x: x[1], reverse=True)[:10]
    return {
        'top_cost_conversations': [
            {'conv_id': str(cid)[:16], 'cost_usd': round(cost, 4)}
            for cid, cost in pairs if cost > 0
        ],
    }


def _collect_conversation_tool_distribution(
    cutoff_local: datetime,
    *,
    owner_user_id: int,
) -> dict:
    """Scan recent conversation messages for tool usage distribution.

    Best-effort — on any DB error we return empty counters."""
    tool_counts: Counter = Counter()
    search_urls: Counter = Counter()
    fetch_urls: Counter = Counter()
    try:
        updated_ms = int(cutoff_local.timestamp() * 1000)
        from lib.conversations.repository import scan_conversations_bounded
        _candidate_count, rows = scan_conversations_bounded(
            user_id=owner_user_id,
            updated_at_gte=updated_ms,
            limit=200,
            settings_keys=[],
        )
        # Keep iteration inside the best-effort boundary: bounded scans hydrate
        # lazily, so a later Sidecar frame can fail after earlier rows arrived.
        for row in rows:
            if row is None:
                continue
            messages = (row.get('messages') if isinstance(row, dict)
                        else row.messages)
            for m in messages:
                if not isinstance(m, dict):
                    continue
                rounds = m.get('toolRounds') or []
                if not isinstance(rounds, list):
                    continue
                for r in rounds:
                    if not isinstance(r, dict):
                        continue
                    name = r.get('toolName') or ''
                    if name:
                        tool_counts[name] += 1
                    if name == 'web_search':
                        for res in (r.get('results') or [])[:10]:
                            url = ((res or {}).get('url')
                                   if isinstance(res, dict) else '')
                            if url:
                                dom = _domain_of(url)
                                if dom:
                                    search_urls[dom] += 1
                    elif name == 'fetch_url':
                        args = r.get('args') or {}
                        url = args.get('url') if isinstance(args, dict) else ''
                        if url:
                            dom = _domain_of(url)
                            if dom:
                                fetch_urls[dom] += 1
    except Exception as e:
        logger.warning('[Optimizer.analyzer] conversation scan skipped: %s', e)
        return {'tool_counts': {}, 'search_urls': [], 'fetch_urls': []}

    return {
        'tool_counts': dict(tool_counts),
        'search_urls': [{'domain': d, 'count': n} for d, n in search_urls.most_common(10)],
        'fetch_urls': [{'domain': d, 'count': n} for d, n in fetch_urls.most_common(10)],
    }


def _count_tool_errors(cutoff_local: datetime) -> int:
    total = 0
    fail_re = re.compile(r'\[Tool:[^\]]+\] failed')
    for line in _safe_tail_lines(_facade.APP_LOG):
        ts = _parse_app_log_ts(line)
        if ts is None or ts < cutoff_local:
            continue
        if fail_re.search(line):
            total += 1
    return total
