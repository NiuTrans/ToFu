"""Dispatch-stats aggregation engines.

Pure data-transforms moved out of ``routes/common.py`` (2026-06). Each takes
raw per-slot / per-key dispatcher data and folds it into the shape the
Settings UI renders. No Flask dependency — the route handlers in
``routes/common.py`` fetch the raw data, call these, and ``jsonify`` the
result.
"""

import time

from lib.log import get_logger

logger = get_logger(__name__)


def aggregate_quota_by_model(slots: list) -> dict:
    """Fold per-slot 5-hour request stats into per-model totals.

    Returns ``{models: {model: {...}}, total_requests_5h, total_requests_all}``.
    """
    models = {}
    total_5h = 0
    total_all = 0
    for s in slots:
        m = s['model']
        r5h = s.get('requests_5h', 0)
        r_all = s.get('total_requests', 0)
        total_5h += r5h
        total_all += r_all
        if m not in models:
            models[m] = {
                'requests_5h': 0,
                'total_requests': 0,
                'slots': 0,
                'rpm_current': 0,
                'rpm_limit': 0,
                'avg_latency_ms': 0,
                'inflight': 0,
                'provider_id': s.get('provider_id', ''),
            }
        entry = models[m]
        entry['requests_5h'] += r5h
        entry['total_requests'] += r_all
        entry['slots'] += 1
        entry['rpm_current'] += s.get('rpm_current', 0)
        entry['rpm_limit'] += s.get('rpm_limit', 0)
        entry['inflight'] += s.get('inflight', 0)
        entry['avg_latency_ms'] = round(
            (entry['avg_latency_ms'] * (entry['slots'] - 1) + s.get('latency_ema_ms', 0))
            / entry['slots'], 1)

    return {
        'models': models,
        'total_requests_5h': total_5h,
        'total_requests_all': total_all,
    }


def aggregate_endpoint_metrics(slots: list) -> dict:
    """Bucket per-slot metrics by base_url into per-endpoint live performance.

    Aggregates over all slots sharing the same base_url (one or many models
    on a self-hosted box). EMAs (ttft / latency / throughput) are weighted by
    each slot's request count so heavily-used slots dominate. Returns
    ``{endpoints: {<base_url>: {...}}, ts}``.
    """
    buckets = {}
    for s in slots:
        url = (s.get('base_url') or '').rstrip('/')
        if not url:
            continue
        b = buckets.setdefault(url, {
            'slots': 0, 'models': set(), 'providers': set(),
            'rpm_current': 0, 'rpm_limit': 0, 'inflight': 0,
            'total_requests': 0, 'total_errors': 0,
            '_ttft_num': 0.0, '_ttft_den': 0,
            '_lat_num': 0.0, '_lat_den': 0,
            '_tp_num': 0.0, '_tp_den': 0,
            'last_success_ts': 0.0,
            'last_error_ts': 0.0,
            'last_error_msg': '',
            'available': False,
            'consecutive_errors': 0,
        })
        b['slots'] += 1
        if s.get('model'): b['models'].add(s['model'])
        if s.get('provider_id'): b['providers'].add(s['provider_id'])
        b['rpm_current'] += s.get('rpm_current', 0) or 0
        b['rpm_limit'] += s.get('rpm_limit', 0) or 0
        b['inflight'] += s.get('inflight', 0) or 0
        b['total_requests'] += s.get('total_requests', 0) or 0
        b['total_errors'] += s.get('total_errors', 0) or 0
        # Weight per-slot EMAs by request count so heavily-used slots dominate
        n = s.get('total_requests', 0) or 0
        w = max(1, n)  # at least 1 so cold slots still contribute
        ttft = s.get('ttft_ema_ms') or 0
        lat = s.get('latency_ema_ms') or 0
        tps = s.get('throughput_ema_tps') or 0
        if ttft > 0 and n > 0:
            b['_ttft_num'] += ttft * w; b['_ttft_den'] += w
        if lat > 0 and n > 0:
            b['_lat_num'] += lat * w; b['_lat_den'] += w
        if tps > 0 and n > 0:
            b['_tp_num'] += tps * w; b['_tp_den'] += w
        ts_s = s.get('last_success_time') or 0
        if ts_s > b['last_success_ts']: b['last_success_ts'] = ts_s
        ts_e = s.get('last_error_time') or 0
        if ts_e > b['last_error_ts']:
            b['last_error_ts'] = ts_e
            b['last_error_msg'] = s.get('last_error_msg') or ''
        if s.get('available'):
            b['available'] = True
        ce = s.get('consecutive_errors', 0) or 0
        if ce > b['consecutive_errors']:
            b['consecutive_errors'] = ce

    # Finalize: serialize sets, compute success rate + averages
    out = {}
    for url, b in buckets.items():
        sr = None
        if b['total_requests'] >= 3:
            sr = max(0.0, 1.0 - b['total_errors'] / b['total_requests'])
        out[url] = {
            'slots': b['slots'],
            'models': sorted(b['models']),
            'providers': sorted(b['providers']),
            'rpm_current': b['rpm_current'],
            'rpm_limit': b['rpm_limit'],
            'inflight': b['inflight'],
            'total_requests': b['total_requests'],
            'total_errors': b['total_errors'],
            'success_rate': round(sr, 3) if sr is not None else None,
            'ttft_ms': round(b['_ttft_num'] / b['_ttft_den'], 1)
                       if b['_ttft_den'] else None,
            'latency_ms': round(b['_lat_num'] / b['_lat_den'], 1)
                          if b['_lat_den'] else None,
            'throughput_tps': round(b['_tp_num'] / b['_tp_den'], 1)
                              if b['_tp_den'] else None,
            'last_success_ts': b['last_success_ts'],
            'last_error_ts': b['last_error_ts'],
            'last_error_msg': b['last_error_msg'],
            'available': b['available'],
            'consecutive_errors': b['consecutive_errors'],
        }
    return {'endpoints': out, 'ts': time.time()}


def aggregate_model_health(slots: list) -> dict:
    """Fold per-slot runtime state into per-(provider, wire-model) health rows.

    Powers the Settings model-card health strip: success rate, error counts,
    consecutive-error streaks, and ACTIVE cooldowns — the "error-rate
    throttling" the dispatcher imposes after repeated failures. Cooldown
    remaining is computed against ``now`` so the payload is self-contained.
    Keyed by (provider_id, wire model id) because slots are per wire id; the
    frontend folds a card's logical model over its request-id pool.

    Returns ``{providers: {provider_id: {model: {...}}}, ts}``.
    """
    now = time.time()
    providers = {}
    for s in slots:
        pid = s.get('provider_id') or 'default'
        mid = s.get('model') or ''
        if not mid:
            continue
        pm = providers.setdefault(pid, {})
        e = pm.setdefault(mid, {
            'slots': 0, 'available_slots': 0,
            'total_requests': 0, 'total_errors': 0,
            'contention_errors': 0, 'gateway_errors': 0,
            'consecutive_errors': 0, 'inflight': 0,
            'cooldown_remaining_s': 0.0, 'cooldown_reason': '',
            'last_error_ts': 0.0, 'last_error_msg': '',
        })
        e['slots'] += 1
        remaining = max(0.0, (s.get('cooldown_until') or 0) - now)
        if remaining <= 0 and s.get('available', True):
            e['available_slots'] += 1
        if remaining > e['cooldown_remaining_s']:
            e['cooldown_remaining_s'] = round(remaining, 1)
            e['cooldown_reason'] = s.get('cooldown_reason') or ''
        e['total_requests'] += s.get('total_requests', 0) or 0
        e['total_errors'] += s.get('total_errors', 0) or 0
        e['contention_errors'] += s.get('contention_errors', 0) or 0
        e['gateway_errors'] += s.get('gateway_errors', 0) or 0
        e['inflight'] += s.get('inflight', 0) or 0
        ce = s.get('consecutive_errors', 0) or 0
        if ce > e['consecutive_errors']:
            e['consecutive_errors'] = ce
        ts_e = s.get('last_error_time') or 0
        if ts_e > e['last_error_ts']:
            e['last_error_ts'] = ts_e
            e['last_error_msg'] = s.get('last_error_msg') or ''

    # Success rate only once there's enough signal (mirrors the endpoint
    # metrics convention): < 3 lifetime requests → None (rendered as '—').
    for pm in providers.values():
        for e in pm.values():
            tr, te = e['total_requests'], e['total_errors']
            e['success_rate'] = (round(max(0.0, 1.0 - te / tr), 3)
                                 if tr >= 3 else None)
    return {'providers': providers, 'ts': now}


def group_key_stats_by_provider(snapshot: dict) -> dict:
    """Regroup a flat ``key_stats`` snapshot into ``{provider_id: {key: row}}``.

    The snapshot's ``keys`` map is keyed by ``"<provider>::<key_name>"`` (or a
    bare key name for the default provider). Returns the response dict with a
    nested ``providers`` map plus the threshold metadata.
    """
    providers = {}
    for pk, row in (snapshot.get('keys') or {}).items():
        if '::' in pk:
            prov_id, key_name = pk.split('::', 1)
        else:
            prov_id, key_name = 'default', pk
        providers.setdefault(prov_id, {})[key_name] = row

    return {
        'day': snapshot.get('day', ''),
        'min_attempts': snapshot.get('min_attempts', 5),
        'min_success_rate': snapshot.get('min_success_rate', 0.5),
        'providers': providers,
    }


__all__ = [
    'aggregate_quota_by_model',
    'aggregate_endpoint_metrics',
    'aggregate_model_health',
    'group_key_stats_by_provider',
]
