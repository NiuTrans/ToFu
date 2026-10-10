"""Settle invocation-owned compaction costs and reclaim disposable child state."""
from lib.tasks_pkg.compaction._ownership import compaction_owner_key


def settle_compaction_usage(task, *, accumulated_usage=None, api_rounds=None,
                            model=''):
    """Merge once before terminal delivery; failed preparation retains paid usage.

    A drained ledger moves to the task before any fallible projection. Teardown
    may retry it without losing costs or retaining a process-global child scope.
    """
    from lib.tasks_pkg.compaction._compaction_usage import (
        pop_compaction_usage, merge_compaction_usage_into_total,
    )
    from lib.tasks_pkg.llm_fallback._usage import project_usage_for_round_record

    usage = task.get('_pendingCompactionUsage')
    if not usage:
        usage = pop_compaction_usage(compaction_owner_key(task))
        if not usage:
            return
        task['_pendingCompactionUsage'] = usage
    merged = merge_compaction_usage_into_total(task.get('usage') or {}, usage)
    rounds = []
    existing_rounds = ((task.get('apiRounds') or [])
                       if api_rounds is None else api_rounds)
    offset = sum(row.get('kind') == 'compaction' for row in existing_rounds)
    for index, call in enumerate(usage.get('calls') or [], offset + 1):
        if not isinstance(call, dict) or not call.get('usage'):
            continue
        rounds.append({
            'round': f'compaction-{index}',
            'kind': 'compaction',
            'model': call.get('model') or model or task.get('model') or '',
            'provider_id': call.get('provider_id') or task.get('provider_id') or '',
            'usage': project_usage_for_round_record(call['usage']),
            'tag': f'COMPACTION-{str(call.get("kind") or "SUMMARY").upper()}',
        })
    if accumulated_usage is not None:
        accumulated_usage.clear()
        accumulated_usage.update(merged)
        merged = accumulated_usage
    task['usage'] = merged
    task['compactionUsage'] = usage
    if api_rounds is None:
        api_rounds = task.setdefault('apiRounds', [])
    api_rounds.extend(rounds)
    task.pop('_pendingCompactionUsage', None)


def cleanup_child_compaction(task):
    """No-escape child-only reclamation, keeping unreported costs on the task."""
    from lib.log import get_logger
    from lib.tasks_pkg.invocation import is_child_invocation

    if not is_child_invocation(task):
        return
    logger = get_logger(__name__)
    key = compaction_owner_key(task)
    try:
        settle_compaction_usage(task)
    except BaseException as exc:
        logger.warning('Child compaction costs retained for settlement task=%s: %s',
                       task.get('id'), type(exc).__name__)
    try:
        from lib.token_counter import invalidate
        invalidate(key)
    except BaseException as exc:
        logger.warning('Child token cache cleanup failed task=%s: %s',
                       task.get('id'), type(exc).__name__)
    try:
        from lib.tasks_pkg.compaction._constants import (
            _cooldown_lock, _summary_cooldowns,
        )
        with _cooldown_lock:
            _summary_cooldowns.pop(key, None)
    except BaseException as exc:
        logger.warning('Child cooldown cleanup failed task=%s: %s',
                       task.get('id'), type(exc).__name__)
