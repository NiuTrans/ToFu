"""Research progress projection; task events/status remain the only authority.

No storage, transport, timers, percentages, or inferred scientific coverage.
Unknown counts remain null until an owning stage supplies a receipt.
"""
from copy import deepcopy


def project_progress(previous, event, *, run_id='', now_ms=0):
    value = deepcopy(previous) if previous else {
        'schema_version': 1, 'execution_state': 'running',

        'run_id': run_id, 'revision': 0, 'last_business_progress_at': 0,
        'blocked_reason': '',
        'quality_state': 'unknown', 'stage': '', 'activity': '',
        'artifact_ready': False,
        'counts': dict(discovered=0, admitted=0, parsed=0, cache_hits=0,
                       failed=0, excerpt_covered=None, candidates=None,
                       accepted=None, rejected=None),
    }
    if event.get('type') in ('heartbeat', 'ping', 'keepalive'):
        return value
    value['revision'] += 1
    value['run_id'] = str(run_id or value['run_id'])[:160]
    value['last_business_progress_at'] = max(value['last_business_progress_at'], int(now_ms))
    value['blocked_reason'] = str(event.get('blocked_reason') or '')[:500]
    value['stage'] = str(event.get('stage') or value['stage'])[:80]
    value['activity'] = str(event.get('type') or '')[:80]
    counts = value['counts']
    for key, count in (event.get('counts') or {}).items():
        if key in counts and isinstance(count, int) and not isinstance(count, bool):
            counts[key] = max(0, min(200, count))
    if event.get('type') == 'final':
        # Final follows repository acknowledgement, but generic done/error still
        # owns execution settlement (including a racing cancellation).
        for key in ('accepted', 'rejected'):
            counts[key] = max(0, min(200, int(event.get(key) or 0)))
        counts['candidates'] = min(200, counts['accepted'] + counts['rejected'])
        value['quality_state'] = 'degraded' if event.get('degraded') else 'passed'
    if event.get('type') in ('stage_done', 'stage_skipped') and event.get('stage') == 'publish':
        value['artifact_ready'] = True
    return value
