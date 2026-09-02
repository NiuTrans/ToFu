"""routes/metrics.py — Prometheus exposition.

Single endpoint at ``/metrics`` returning Prometheus-text-format
counters/gauges. We deliberately don't introduce ``prometheus_client``
as a dependency — the format is line-oriented and trivial to emit by
hand. Keeps the dependency footprint flat.

Metrics:

  * ``tofu_usage_requests_total{window="7d"}``
  * ``tofu_usage_tokens_total{window="7d"}``
  * ``tofu_tasks_inflight{kind="…"}``
  * ``tofu_tasks_total{kind="…",status="…"}``
  * ``tofu_push_subscribers``                      — open WS subscribers
  * ``tofu_idempotency_cache_size``
  * ``tofu_rate_limit_buckets``                    — number of
                                                     in-memory rate buckets
  * ``tofu_storage_queries_total{backend="…"}``
  * ``tofu_storage_commands_total{backend="…"}``
  * ``tofu_storage_writer_queue_depth{priority="…"}``

Auth: requires ``admin`` scope. Without auth the
endpoint 401s — Prometheus scrapers configure a Bearer header.
"""

from __future__ import annotations

import threading
import time

from quart import Blueprint, Response

from lib.log import get_logger
from lib.openapi import api_meta

from routes.api_v1.auth import require_scope

logger = get_logger(__name__)

metrics_bp = Blueprint('metrics', __name__)

# Collection touches the usage store and snapshots every task runtime.  A
# Prometheus server can scrape more frequently than either source changes, so
# never repeat that synchronous work on every request.  The body is immutable
# text and the lock is held only while one collector rebuilds it.
_METRICS_CACHE_TTL_SECONDS = 5.0
_metrics_cache_lock = threading.Lock()
_metrics_cache = {'at': 0.0, 'body': ''}


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
            # API-key ids are user-created and unbounded.  They belong in the
            # authenticated usage report, not in Prometheus labels.  Aggregate
            # here so one key rotation cannot create a permanent time series.
            per_key = summary.get('per_key') or {}
            requests = sum(float(row.get('requests') or 0)
                           for row in per_key.values())
            tokens = sum(float(row.get('tokens') or 0)
                         for row in per_key.values())
            _emit_counter(out, 'tofu_usage_requests_total',
                           'API requests across all keys, windowed',
                           [({'window': window}, requests)])
            _emit_counter(out, 'tofu_usage_tokens_total',
                           'LLM tokens consumed across all keys, windowed',
                           [({'window': window}, tokens)])
        _emit_gauge(out, 'tofu_active_keys',
                     'Distinct API keys with recorded activity',
                     [({}, len(all_keys_with_activity()))])
    except Exception as e:
        logger.debug('[Metrics] usage block failed: %s', e)


def _collect_task_metrics(out: list) -> None:
    try:
        from routes.api_v1.tasks import _registries
        inflight = []
        registry_size = []
        registry_capacity = []
        registry_ttl = []
        registry_over_capacity = []
        retained_events = []
        event_limits = []
        totals: dict[tuple[str, str], int] = {}
        for kind, rt in _registries().items():
            try:
                lifecycle = rt.stats()
                stats = rt.retention_stats()
            except Exception as e:
                logger.debug('[Metrics] task stats for kind=%s failed: %s', kind, e)
                continue
            inflight.append(({'kind': kind}, lifecycle.get('running', 0)))
            labels = {'kind': kind}
            registry_size.append((labels, stats['tasks']))
            registry_capacity.append((labels, stats['max_tasks']))
            registry_ttl.append((labels, stats['ttl_seconds']))
            registry_over_capacity.append((labels, stats['over_capacity']))
            retained_events.append((labels, stats['events']))
            event_limits.append((labels, stats['max_events_per_task']))
            for status in ('pending', 'running', 'done', 'error', 'aborted'):
                totals[(kind, status)] = int(lifecycle.get(status, 0))
        _emit_gauge(out, 'tofu_tasks_inflight',
                     'Tasks currently running, by kind',
                     inflight)
        total_samples = [({'kind': k, 'status': s}, v)
                          for (k, s), v in sorted(totals.items())]
        _emit_gauge(out, 'tofu_tasks_total',
                     'Tasks in registry by kind+status (snapshot)',
                     total_samples)
        _emit_gauge(out, 'tofu_task_registry_size',
                    'Task records retained in memory by kind', registry_size)
        _emit_gauge(out, 'tofu_task_registry_capacity',
                    'Terminal task retention capacity by kind',
                    registry_capacity)
        _emit_gauge(out, 'tofu_task_registry_ttl_seconds',
                    'Terminal task retention time by kind', registry_ttl)
        _emit_gauge(out, 'tofu_task_registry_over_capacity',
                    'Active task records temporarily above retention capacity',
                    registry_over_capacity)
        _emit_gauge(out, 'tofu_task_events_retained',
                    'Replay events currently retained in memory by task kind',
                    retained_events)
        _emit_gauge(out, 'tofu_task_event_retention_limit',
                    'Maximum replay events retained per task', event_limits)
        from lib.production.runtime import production_retention_stats
        production_stats = production_retention_stats()
        _emit_gauge(
            out, 'tofu_task_dedup_index_size',
            'Live production-task deduplication keys by capability',
            [({'kind': row['kind']}, row['size'])
             for row in production_stats],
        )
        _emit_gauge(
            out, 'tofu_task_dedup_index_capacity',
            'Target deduplication-key capacity by capability',
            [({'kind': row['kind']}, row['capacity'])
             for row in production_stats],
        )
        _emit_gauge(
            out, 'tofu_task_dedup_index_over_capacity',
            'Active deduplication keys temporarily above capacity',
            [({'kind': row['kind']}, row['over_capacity'])
             for row in production_stats],
        )
        eviction_samples = []
        for row in production_stats:
            for reason, count in sorted(row['evictions'].items()):
                eviction_samples.append((
                    {'kind': row['kind'], 'reason': reason}, count))
        _emit_counter(
            out, 'tofu_task_dedup_index_evictions_total',
            'Production deduplication keys removed by bounded reason',
            eviction_samples,
        )
    except Exception as e:
        logger.debug('[Metrics] task block failed: %s', e)


def _collect_storage_metrics(out: list) -> None:
    try:
        from lib.storage import get_storage_client

        metrics = get_storage_client().metrics() or {}
        backend = str(metrics.get('backend') or 'unknown')
        labels = {'backend': backend}
        query_metrics = metrics.get('queries')
        if not isinstance(query_metrics, dict):
            query_metrics = metrics
        writer = metrics.get('writer')
        if not isinstance(writer, dict):
            writer = {}
        _emit_counter(
            out, 'tofu_storage_queries_total',
            'Semantic storage queries completed by the authority',
            [(labels, query_metrics.get('queries', 0))])
        _emit_counter(
            out, 'tofu_storage_query_failures_total',
            'Semantic storage queries failed in the authority',
            [(labels, query_metrics.get(
                'query_failures', metrics.get('failures', 0)))])
        _emit_counter(
            out, 'tofu_storage_commands_total',
            'Semantic storage commands submitted to the authority',
            [(labels, writer.get('submitted', metrics.get('commands', 0)))])
        _emit_counter(
            out, 'tofu_storage_command_failures_total',
            'Semantic storage commands failed in the authority',
            [(labels, writer.get('failed', metrics.get('failures', 0)))])
        _emit_counter(
            out, 'tofu_storage_command_timeouts_total',
            'Semantic storage commands that exceeded their deadline',
            [(labels, writer.get('timed_out', 0))])
        _emit_gauge(
            out, 'tofu_storage_writer_active',
            'Whether the storage authority is executing a write command',
            [(labels, int(bool(writer.get('current'))))])
        _emit_gauge(
            out, 'tofu_storage_writer_max_queue_depth',
            'Largest observed storage writer queue depth',
            [(labels, writer.get('max_queue_depth', 0))])
        queue_depths = writer.get('queue_depths')
        if isinstance(queue_depths, dict):
            _emit_gauge(
                out, 'tofu_storage_writer_queue_depth',
                'Current storage writer queue depth by priority',
                [({'backend': backend, 'priority': priority}, depth)
                 for priority, depth in sorted(queue_depths.items())])
        commit_latency = writer.get('commit_latency')
        if isinstance(commit_latency, dict):
            _emit_gauge(
                out, 'tofu_storage_commit_latency_milliseconds',
                'Observed SQLite commit latency by rolling statistic',
                [
                    ({'backend': backend, 'statistic': statistic},
                     commit_latency.get(key, 0))
                    for statistic, key in (
                        ('p50', 'p50_ms'), ('p95', 'p95_ms'), ('max', 'max_ms'))
                ])
            _emit_gauge(
                out, 'tofu_storage_commit_latency_samples',
                'Commit samples retained by the storage authority',
                [(labels, commit_latency.get('samples', 0))])
        _emit_counter(
            out, 'tofu_storage_writer_stall_interrupts_total',
            'Writer deadline stalls that required sqlite3_interrupt',
            [(labels, writer.get('stall_interrupts', 0))])
        _emit_counter(
            out, 'tofu_storage_writer_batches_total',
            'Physical group-commit batches executed by SQLite',
            [(labels, writer.get('batches', 0))])
        _emit_counter(
            out, 'tofu_storage_writer_batched_jobs_total',
            'Logical storage jobs admitted to group commits',
            [(labels, writer.get('batched_jobs', 0))])
        current = writer.get('current')
        if isinstance(current, dict) and current.get('phase'):
            _emit_gauge(
                out, 'tofu_storage_writer_phase',
                'Current SQLite writer blocking phase',
                [({'backend': backend, 'phase': current['phase']}, 1)])
        _emit_gauge(
            out, 'tofu_storage_writer_cache_bytes',
            'Configured SQLite writer page-cache ceiling',
            [(labels, int(metrics.get('writer_cache_mib') or 0) * 1024 ** 2)])
        sqlite_version = str(metrics.get('sqlite_version') or '').strip()
        if backend == 'sqlite' and sqlite_version:
            _emit_gauge(
                out, 'tofu_storage_sqlite_runtime_info',
                'SQLite runtime version linked into the storage authority',
                [({'backend': backend, 'version': sqlite_version}, 1)])
        fastpath = metrics.get('fastpath')
        if isinstance(fastpath, dict):
            _emit_gauge(
                out, 'tofu_storage_fastpath_active',
                'Whether SQLite writes use the measured-local front',
                [(labels, int(bool(fastpath.get('active'))))])
            shipper = fastpath.get('shipper')
            if isinstance(shipper, dict):
                _emit_gauge(
                    out, 'tofu_storage_fastpath_ship_lag_bytes',
                    'Committed local WAL bytes not yet shipped durably',
                    [(labels, shipper.get('ship_lag_bytes', 0))])
                if shipper.get('last_ship_age_s') is not None:
                    _emit_gauge(
                        out, 'tofu_storage_fastpath_last_ship_age_seconds',
                        'Seconds since the durable shadow last advanced',
                        [(labels, shipper['last_ship_age_s'])])
    except Exception as e:
        logger.debug('[Metrics] storage block failed: %s', e)


def _collect_infra_metrics(out: list) -> None:
    _collect_storage_metrics(out)
    try:
        from lib.idempotency import cache_stats
        s = cache_stats() or {}
        _emit_gauge(out, 'tofu_idempotency_cache_size',
                     'Cached idempotency replays in memory',
                     [({}, s.get('size', 0))])
        _emit_gauge(out, 'tofu_idempotency_cache_capacity',
                    'Maximum cached idempotency replays',
                    [({}, s.get('max_size', 0) or 0)])
        _emit_gauge(out, 'tofu_idempotency_cache_ttl_seconds',
                    'Idempotency replay retention time',
                    [({}, s.get('ttl', 0) or 0)])
        _emit_counter(out, 'tofu_idempotency_cache_hits_total',
                      'Idempotency replay cache hits',
                      [({}, s.get('hits', 0))])
        _emit_counter(out, 'tofu_idempotency_cache_misses_total',
                      'Idempotency replay cache misses',
                      [({}, s.get('misses', 0))])
        _emit_counter(out, 'tofu_idempotency_cache_evictions_total',
                      'Idempotency replay cache evictions by reason', [
                          ({'reason': 'expired'}, s.get('expired_evicts', 0)),
                          ({'reason': 'capacity'}, s.get('size_evicts', 0)),
                      ])
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
        from lib.agent_core.push import hub
        size = hub.client_count
        _emit_gauge(out, 'tofu_push_subscribers',
                     'Open /api/push WebSocket subscribers',
                     [({}, size)])
        bus = hub.bus_health()
        backend = str(bus.get('backend') or 'unknown')
        _emit_gauge(out, 'tofu_push_bus_publisher_available',
                    'Whether push fan-out can currently publish fleet-wide',
                    [({'backend': backend},
                      1 if bus.get('publisher_available') else 0)])
        _emit_gauge(out, 'tofu_push_bus_subscriber_available',
                    'Whether this replica is currently subscribed to fan-out',
                    [({'backend': backend},
                      1 if bus.get('subscriber_available') else 0)])
        _emit_gauge(out, 'tofu_push_bus_reconnect_seconds',
                    'Seconds until the next push-bus reconnect attempt',
                    [({'backend': backend}, bus.get('reconnect_in_s', 0.0))])
        from lib.control_rpc import control_rpc_metrics
        rpc = control_rpc_metrics()
        for metric_name in (
            'accepted', 'completed', 'cancelled', 'timed_out', 'overloaded',
            'failed', 'response_bytes',
        ):
            _emit_counter(
                out,
                f'tofu_control_rpc_{metric_name}_total',
                f'Control RPC {metric_name.replace("_", " ")}',
                [({}, rpc.get(metric_name, 0))],
            )
        _emit_gauge(
            out, 'tofu_control_rpc_active_workers',
            'Blocking control RPC workers that have not actually exited',
            [({}, rpc.get('active_workers', 0))],
        )
        _emit_gauge(
            out, 'tofu_control_rpc_worker_capacity',
            'Maximum blocking control RPC workers in this process',
            [({}, rpc.get('global_workers', 0))],
        )
    except Exception as e:
        logger.debug('[Metrics] push block failed: %s', e)
    try:
        from lib.runtime_state_store import get_store
        store = get_store()
        health_fn = getattr(store, 'health', None)
        if callable(health_fn):
            state = health_fn()
            backend = str(state.get('backend') or 'unknown')
            available = bool(state.get('available'))
            reconnect = state.get('reconnect_in_s', 0.0)
        else:
            backend, available, reconnect = 'inproc', True, 0.0
        _emit_gauge(out, 'tofu_runtime_state_available',
                    'Whether the shared runtime-state backend is reachable',
                    [({'backend': backend}, 1 if available else 0)])
        _emit_gauge(out, 'tofu_runtime_state_reconnect_seconds',
                    'Seconds until the next runtime-state reconnect attempt',
                    [({'backend': backend}, reconnect)])
    except Exception as e:
        logger.debug('[Metrics] runtime-state block failed: %s', e)
    try:
        from lib.http_client import http_pool_stats
        pool = http_pool_stats()
        _emit_gauge(out, 'tofu_http_sync_sessions',
                    'Thread-local pooled requests sessions',
                    [({}, pool['sync_sessions'])])
        _emit_gauge(out, 'tofu_http_async_clients',
                    'Event-loop-scoped pooled HTTPX clients',
                    [({}, pool['async_clients'])])
    except Exception as e:
        logger.debug('[Metrics] HTTP pool block failed: %s', e)


def _build_metrics_body() -> str:
    out: list[str] = []
    _collect_usage_metrics(out)
    _collect_task_metrics(out)
    _collect_infra_metrics(out)
    try:
        from lib.observability import prometheus_lines
        out.extend(prometheus_lines())
    except Exception as e:
        logger.warning('[Metrics] runtime observability block failed: %s', e)
    return '\n'.join(out) + '\n'


def _metrics_snapshot() -> tuple[str, bool]:
    """Return ``(body, cache_hit)`` with a five-second collection TTL."""
    now = time.monotonic()
    body = _metrics_cache['body']
    if body and now - _metrics_cache['at'] < _METRICS_CACHE_TTL_SECONDS:
        return body, True
    with _metrics_cache_lock:
        now = time.monotonic()
        body = _metrics_cache['body']
        if body and now - _metrics_cache['at'] < _METRICS_CACHE_TTL_SECONDS:
            return body, True
        body = _build_metrics_body()
        _metrics_cache['body'] = body
        _metrics_cache['at'] = now
        return body, False


def clear_metrics_snapshot_cache() -> None:
    """Invalidate the exposition cache (public for tests and diagnostics)."""
    with _metrics_cache_lock:
        _metrics_cache['at'] = 0.0
        _metrics_cache['body'] = ''


@metrics_bp.route('/metrics', methods=['GET'])
@require_scope('admin')
@api_meta(summary='Prometheus metrics exposition (admin)',
          description='Standard Prometheus text format. Configure your '
                       'scraper with `Authorization: Bearer tofu_admin_…`.',
          tags=['admin'], scope='admin',
          responses={
              '200': {'description': 'Prometheus text format',
                       'content': {'text/plain': {
                           'schema': {'type': 'string'}}}},
          })
def metrics():
    body, cache_hit = _metrics_snapshot()
    response = Response(
        body, mimetype='text/plain; version=0.0.4; charset=utf-8')
    response.headers['X-Tofu-Metrics-Cache'] = 'hit' if cache_hit else 'miss'
    return response


__all__ = ['clear_metrics_snapshot_cache', 'metrics_bp']
