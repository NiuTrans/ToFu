"""Current-attempt cache evidence for economic decisions, never historical UI state.

One completed provider round leaves ``read + write`` tokens of prefix in the
provider cache: what it read back plus what it wrote for the next round. A
cold full write (read 0, write = whole prompt) is therefore the warmest state
a route can be in, not the coldest. Only failed readbacks on the same route
show that the route is not honouring cache reads; after
``COLD_ROUTE_READBACK_FAILURES`` consecutive failures its writes stop counting
as warm, so cost decisions no longer protect a prefix nobody will read.
"""

COLD_ROUTE_READBACK_FAILURES = 2

# A readback below this share of the previous round's warm prefix is a failure.
_READBACK_SUCCESS_SHARE = 0.5

# Largest documented minimum cacheable prompt length across current Anthropic
# models; a smaller prior prefix may never have been written at all.
_READBACK_EVIDENCE_FLOOR_TOKENS = 4096


def _count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _route_observation(task):
    if not isinstance(task, dict):
        return None
    evidence = task.get('_compactionCacheObservation')
    if not isinstance(evidence, dict) or evidence.get('taskId') != task.get('id'):
        return None
    config = task.get('config') or {}
    model = task.get('model') or config.get('model')
    provider = task.get('provider_id') or config.get('provider_id') or config.get('providerId')
    if not model or evidence.get('model') != model:
        return None
    if not provider or evidence.get('providerId') != provider:
        return None
    return evidence


def current_cache_read(task):
    evidence = _route_observation(task)
    return _count(evidence.get('cacheReadTokens')) if evidence else 0


def current_route_cache_cold(task):
    """True once this task's current route failed consecutive readbacks."""
    evidence = _route_observation(task)
    return bool(evidence) and (_count(evidence.get('readbackFailures'))
                               >= COLD_ROUTE_READBACK_FAILURES)


def current_warm_prefix_tokens(task):
    """Tokens the next request on this route can read back from the cache."""
    evidence = _route_observation(task)
    if evidence is None:
        return 0
    read = _count(evidence.get('cacheReadTokens'))
    if current_route_cache_cold(task):
        return read
    return read + _count(evidence.get('cacheWriteTokens'))


def next_cache_observation(previous, *, task_id, model, provider_id,
                           cache_read, cache_write):
    """Return the observation for one completed round on one route."""
    read, write = _count(cache_read), _count(cache_write)
    failures = 0
    if (isinstance(previous, dict)
            and previous.get('taskId') == task_id
            and previous.get('model') == model
            and previous.get('providerId') == provider_id):
        prior_warm = (_count(previous.get('cacheReadTokens'))
                      + _count(previous.get('cacheWriteTokens')))
        if (prior_warm >= _READBACK_EVIDENCE_FLOOR_TOKENS
                and read < prior_warm * _READBACK_SUCCESS_SHARE):
            failures = _count(previous.get('readbackFailures')) + 1
    return {'taskId': task_id, 'model': model, 'providerId': provider_id,
            'cacheReadTokens': read, 'cacheWriteTokens': write,
            'readbackFailures': failures}
