"""Current-attempt cache evidence for economic decisions, never historical UI state."""


def current_cache_read(task):
    if not isinstance(task, dict):
        return 0
    evidence = task.get('_compactionCacheObservation')
    if not isinstance(evidence, dict) or evidence.get('taskId') != task.get('id'):
        return 0
    config = task.get('config') or {}
    model = task.get('model') or config.get('model')
    provider = task.get('provider_id') or config.get('provider_id') or config.get('providerId')
    if not model or evidence.get('model') != model:
        return 0
    if not provider or evidence.get('providerId') != provider:
        return 0
    try:
        return max(0, int(evidence.get('cacheReadTokens') or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
