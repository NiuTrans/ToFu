"""routes/metrics.py — Prometheus exposition.

Single endpoint at ``/metrics`` returning Prometheus-text-format
counters/gauges. We deliberately don't introduce ``prometheus_client``
as a dependency — the format is line-oriented and trivial to emit by
hand. Keeps the dependency footprint flat.

Metrics:

  * ``tofu_usage_requests_total{key_id="…"}``
  * ``tofu_usage_tokens_total{key_id="…",window="7d"}``
  * ``tofu_tasks_inflight{kind="…"}``
  * ``tofu_tasks_total{kind="…",status="…"}``
  * ``tofu_push_subscribers``                      — open WS subscribers
  * ``tofu_idempotency_cache_size``
  * ``tofu_rate_limit_buckets``                    — number of
                                                     in-memory rate buckets

Auth: requires ``admin`` scope OR ``TUNNEL_TOKEN``. Without auth the
endpoint 401s — Prometheus scrapers configure a Bearer header.
"""

from __future__ import annotations

from flask import Blueprint, Response

from lib.log import get_logger
from lib.openapi import api_meta

from routes.api_v1.auth import require_scope

logger = get_logger(__name__)

metrics_bp = Blueprint('metrics', __name__)


def _escape_label(s: str) -> str:
    return (str(s).replace('\\', '\\\\')
                  .replace('"', '\\"')
                  .replace('\n', ' '))


def _emit_counter(out: list, name: str, help_text: str,
                   samples: list[tuple[dict, float]]) -> None:
    out.append(f'# HELP {name} {help_text}')
    out.append(f'# TYPE {name} counter')
    for labels, value in samples:
        if labels:
            label_str = ','.join(f'{k}="{_escape_label(v)}"'
                                  for k, v in labels.items())
            out.append(f'{name}{{{label_str}}} {value}')
        else:
            out.append(f'{name} {value}')


def _emit_gauge(out: list, name: str, help_text: str,
                 samples: list[tuple[dict, float]]) -> None:
    out.append(f'# HELP {name} {help_text}')
    out.append(f'# TYPE {name} gauge')
    for labels, value in samples:
        if labels:
            label_str = ','.join(f'{k}="{_escape_label(v)}"'
                                  for k, v in labels.items())
            out.append(f'{name}{{{label_str}}} {value}')
        else:
            out.append(f'{name} {value}')


def _collect_usage_metrics(out: list) -> None:
    try:
        from lib.usage_tracker import usage_summary, all_keys_with_activity
        for window, days in (('1d', 1), ('7d', 7), ('30d', 30)):
            summary = usage_summary(days=days)
            req_samples = []
            tok_samples = []
            for kid, totals in summary['per_key'].items():
                req_samples.append(({'key_id': kid, 'window': window},
                                     totals['requests']))
                tok_samples.append(({'key_id': kid, 'window': window},
                                     totals['tokens']))
            _emit_counter(out, 'tofu_usage_requests_total',
                           'API requests by key, windowed',
                           req_samples)
            _emit_counter(out, 'tofu_usage_tokens_total',
                           'LLM tokens consumed by key, windowed',
                           tok_samples)
        _emit_gauge(out, 'tofu_active_keys',
                     'Distinct API keys with recorded activity',
                     [({}, len(all_keys_with_activity()))])
    except Exception as e:
        logger.debug('[Metrics] usage block failed: %s', e)


def _collect_task_metrics(out: list) -> None:
    try:
        from routes.api_v1.tasks import _registries
        inflight = []
        totals: dict[tuple[str, str], int] = {}
        for kind, rt in _registries().items():
            try:
                with rt._lock:  # type: ignore[attr-defined]
                    snapshot = list(rt._tasks.values())  # type: ignore[attr-defined]
            except Exception as e:
                logger.debug('[Metrics] task snapshot for kind=%s failed: %s', kind, e)
                continue
            running = sum(1 for t in snapshot
                          if t.get('status') == 'running')
            inflight.append(({'kind': kind}, running))
            for t in snapshot:
                k = (kind, t.get('status') or 'unknown')
                totals[k] = totals.get(k, 0) + 1
        _emit_gauge(out, 'tofu_tasks_inflight',
                     'Tasks currently running, by kind',
                     inflight)
        total_samples = [({'kind': k, 'status': s}, v)
                          for (k, s), v in sorted(totals.items())]
        _emit_gauge(out, 'tofu_tasks_total',
                     'Tasks in registry by kind+status (snapshot)',
                     total_samples)
    except Exception as e:
        logger.debug('[Metrics] task block failed: %s', e)


def _collect_infra_metrics(out: list) -> None:
    try:
        from lib.idempotency import cache_stats
        s = cache_stats() or {}
        _emit_gauge(out, 'tofu_idempotency_cache_size',
                     'Cached idempotency replays in memory',
                     [({}, s.get('size', 0))])
    except Exception as e:
        logger.debug('[Metrics] idempotency block failed: %s', e)
    try:
        from lib import rate_limit_api
        _emit_gauge(out, 'tofu_rate_limit_buckets',
                     'In-memory rate-limit buckets',
                     [({}, len(rate_limit_api._state))])
    except Exception as e:
        logger.debug('[Metrics] rate-limit block failed: %s', e)
    try:
        from lib.agent_core.admission import controller
        st = controller.stats()
        _emit_gauge(out, 'tofu_agent_inflight',
                     'Agent tasks admitted and in-flight (admission gate)',
                     [({}, st['in_flight'])])
        _emit_gauge(out, 'tofu_agent_capacity',
                     'Max concurrent agent tasks (0 = unbounded)',
                     [({}, st['capacity'])])
        if st['capacity'] > 0:
            _emit_gauge(out, 'tofu_agent_available',
                         'Free admission slots for new agent tasks',
                         [({}, st['available'])])
    except Exception as e:
        logger.debug('[Metrics] admission block failed: %s', e)
    try:
        from lib.push import hub
        # PushHub may not have a public .stats() — best-effort.
        size = 0
        for attr in ('_subscribers', '_subs', 'subscribers'):
            v = getattr(hub, attr, None)
            if v is not None:
                try:
                    size = len(v)
                    break
                except Exception as e:
                    logger.debug('[Metrics] hub.%s len() failed: %s', attr, e)
                    continue
        _emit_gauge(out, 'tofu_push_subscribers',
                     'Open /api/push WebSocket subscribers',
                     [({}, size)])
    except Exception as e:
        logger.debug('[Metrics] push block failed: %s', e)


@metrics_bp.route('/metrics', methods=['GET'])
@require_scope('admin')
@api_meta(summary='Prometheus metrics exposition (admin)',
          description='Standard Prometheus text format. Configure your '
                       'scraper with `Authorization: Bearer tofu_admin_…` '
                       'or `X-Tunnel-Token`.',
          tags=['admin'], scope='admin',
          responses={
              '200': {'description': 'Prometheus text format',
                       'content': {'text/plain': {
                           'schema': {'type': 'string'}}}},
          })
def metrics():
    out: list[str] = []
    _collect_usage_metrics(out)
    _collect_task_metrics(out)
    _collect_infra_metrics(out)
    body = '\n'.join(out) + '\n'
    return Response(body, mimetype='text/plain; version=0.0.4; charset=utf-8')


__all__ = ['metrics_bp']
