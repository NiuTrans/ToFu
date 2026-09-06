"""Bounded, dependency-free runtime metrics.

The project deliberately does not depend on ``prometheus_client``.  This
module owns the small amount of process-local state needed by the HTTP,
executor, transport and LLM hot paths, while :mod:`routes.metrics` owns the
authenticated exposition endpoint.

All label values pass through a closed or bounded normalizer.  In particular,
request/conversation/task ids are never accepted as labels.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Iterable

from lib.log import get_logger


logger = get_logger(__name__)

_HTTP_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_WAIT_BUCKETS = (0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0)
_LLM_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 180.0)
_ROUND_BUCKETS = (1, 2, 3, 4, 6, 8, 12, 20, 40, 80)
_TOKEN_BUCKETS = (1_000, 4_000, 16_000, 32_000, 64_000, 128_000, 256_000)
_COST_BUCKETS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 20.0)
_CGROUP_RELIEF_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1,
                          0.25, 0.5, 1.0, 2.5, 5.0)
_METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'})
_TRANSPORTS = frozenset({'sse', 'ws'})
_CONNECTION_OUTCOMES = frozenset({'completed', 'disconnected', 'error'})
_STREAM_ADMISSION_OUTCOMES = frozenset({
    'admitted', 'capacity', 'evicted', 'stale', 'superseded',
})
_BACKGROUND_JOB_KINDS = frozenset({'pricing_refresh'})
_BACKGROUND_JOB_OUTCOMES = frozenset({'success', 'error', 'cancelled'})
_RUNTIME_PROBE_SOURCES = frozenset({'loadavg', 'cgroup_memory'})
_EXECUTION_OUTCOMES = frozenset({
    'completed', 'failed', 'cancelled', 'timed_out',
})
_EXECUTION_RESOURCE_DISPOSITIONS = frozenset({
    'released', 'deferred', 'failed',
})
_DYNAMIC_SEGMENT = re.compile(
    r'^(?:\d+|[0-9a-f]{8,}|[A-Za-z0-9_-]{16,})$', re.IGNORECASE)


def _bounded(value: Any, *, fallback: str = 'unknown', limit: int = 96) -> str:
    text = str(value or fallback).strip()
    if not text:
        text = fallback
    # Prometheus escaping happens at rendering.  Drop control characters here
    # so labels remain one logical value even when fed hostile provider names.
    text = ''.join(ch if ch >= ' ' else ' ' for ch in text)
    return text[:limit]


def normalize_route_template(value: Any) -> str:
    """Return a bounded-cardinality route label.

    Prefer Quart's matched rule (``/api/tasks/<task_id>``).  The fallback is
    intentionally lossy: identifier-looking path segments collapse to
    ``<id>`` and all static assets collapse to one template.
    """
    path = _bounded(value, fallback='unmatched', limit=180).split('?', 1)[0]
    if path.startswith('/static/'):
        return '/static/<path>'
    if path in ('', '/'):
        return path or '/'
    parts = []
    for segment in path.split('/'):
        parts.append('<id>' if _DYNAMIC_SEGMENT.match(segment or '') else segment)
    return _bounded('/'.join(parts), fallback='unmatched', limit=140)


def route_template_for_request(req: Any) -> str:
    rule = getattr(getattr(req, 'url_rule', None), 'rule', None)
    if rule:
        return normalize_route_template(rule)
    path = str(getattr(req, 'path', '') or '')
    return '/static/<path>' if path.startswith('/static/') else 'unmatched'


class _Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self.gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self.label_values: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.histograms: dict[
            tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]
        ] = {}

    def _labels(self, name: str, labels: dict[str, Any] | None) \
            -> tuple[tuple[str, str], ...]:
        normalized = []
        for key, value in (labels or {}).items():
            axis = str(key)
            text = _bounded(value)
            seen = self.label_values[(name, axis)]
            # The route inventory is larger than provider/task dimensions but
            # still finite.  Once a bad caller exceeds the cap, fold novel
            # values into one stable series instead of growing forever.
            cap = 512 if axis == 'route' else 128
            if text not in seen:
                if len(seen) >= cap:
                    text = 'other'
                seen.add(text)
            normalized.append((axis, text))
        return tuple(sorted(normalized))

    def inc(self, name: str, value: float = 1.0, **labels: Any) -> None:
        with self.lock:
            key = (name, self._labels(name, labels))
            self.counters[key] += float(value)

    def set(self, name: str, value: float, **labels: Any) -> None:
        with self.lock:
            key = (name, self._labels(name, labels))
            self.gauges[key] = float(value)

    def add_gauge(self, name: str, delta: float, **labels: Any) -> None:
        with self.lock:
            key = (name, self._labels(name, labels))
            self.gauges[key] = self.gauges.get(key, 0.0) + float(delta)

    def observe(self, name: str, value: float, buckets: Iterable[float],
                **labels: Any) -> None:
        bucket_tuple = tuple(float(v) for v in buckets)
        with self.lock:
            key = (name, self._labels(name, labels))
            state = self.histograms.setdefault(key, {
                'buckets': bucket_tuple,
                'counts': [0] * len(bucket_tuple),
                'sum': 0.0,
                'count': 0,
            })
            number = max(0.0, float(value))
            state['sum'] += number
            state['count'] += 1
            for index, upper in enumerate(state['buckets']):
                if number <= upper:
                    state['counts'][index] += 1

    def snapshot(self) -> tuple[dict, dict, dict]:
        with self.lock:
            counters = dict(self.counters)
            gauges = dict(self.gauges)
            histograms = {
                key: {
                    'buckets': tuple(value['buckets']),
                    'counts': list(value['counts']),
                    'sum': value['sum'],
                    'count': value['count'],
                }
                for key, value in self.histograms.items()
            }
        return counters, gauges, histograms


_STORE = _Store()


def record_http_request(method: Any, route: Any, status: Any, duration_s: float) -> None:
    method_label = str(method or 'OTHER').upper()
    if method_label not in _METHODS:
        method_label = 'OTHER'
    try:
        status_label = str(int(status))
    except (TypeError, ValueError) as exc:
        logger.debug('invalid HTTP status for metrics: %r (%s)', status, exc)
        status_label = '0'
    labels = {
        'method': method_label,
        'route': normalize_route_template(route),
        'status': status_label,
    }
    _STORE.inc('tofu_http_requests_total', **labels)
    _STORE.observe('tofu_http_request_duration_seconds', duration_s,
                   _HTTP_BUCKETS, **labels)


def set_event_loop_lag(seconds: float) -> None:
    _STORE.set('tofu_event_loop_lag_seconds', max(0.0, float(seconds)))


def record_runtime_probe_failure(source: Any) -> None:
    """Aggregate host-probe failures without emitting periodic log noise.

    Probe names come from a closed inventory so exceptions, paths and host
    identifiers can never create unbounded Prometheus label cardinality.
    """
    source_text = str(source)
    source_label = source_text if source_text in _RUNTIME_PROBE_SOURCES \
        else 'other'
    _STORE.inc('tofu_runtime_probe_failures_total', source=source_label)


def record_cgroup_relief(
    *,
    duration_s: float,
    cgroup_reclaimed_bytes: int | None,
    process_rss_reclaimed_bytes: int | None,
    cgroup_cache_reclaimed_bytes: int | None,
    heap_window_reclaimed_bytes: int | None,
    log_window_reclaimed_bytes: int | None,
    cache_entries_dropped: int,
    log_files_advised: int,
    log_bytes_advised: int,
) -> None:
    """Record one relief pass without claiming shared deltas are causal.

    ``process_rss`` is process-owned. The remaining byte sources are observed
    shared-cgroup counter windows and can include concurrent sibling activity;
    their closed labels make that distinction explicit and cardinality finite.
    """
    _STORE.inc('tofu_cgroup_relief_attempts_total')
    sources = {
        'shared_cgroup': cgroup_reclaimed_bytes,
        'process_rss': process_rss_reclaimed_bytes,
        'cgroup_cache': cgroup_cache_reclaimed_bytes,
        'heap_window': heap_window_reclaimed_bytes,
        'log_window': log_window_reclaimed_bytes,
    }
    for source, raw_value in sources.items():
        if raw_value is None:
            continue
        value = max(0.0, float(raw_value))
        _STORE.inc(
            'tofu_cgroup_relief_reclaimed_bytes_total', value, source=source)
        _STORE.set(
            'tofu_cgroup_relief_reclaimed_bytes_latest', value, source=source)
    _STORE.inc(
        'tofu_cgroup_relief_cache_entries_dropped_total',
        max(0, int(cache_entries_dropped)))
    _STORE.inc(
        'tofu_cgroup_relief_log_files_advised_total',
        max(0, int(log_files_advised)))
    _STORE.inc(
        'tofu_cgroup_relief_log_bytes_advised_total',
        max(0, int(log_bytes_advised)))
    _STORE.observe(
        'tofu_cgroup_relief_duration_seconds', duration_s,
        _CGROUP_RELIEF_BUCKETS)


def record_task_queue_wait(kind: Any, seconds: float) -> None:
    _STORE.observe('tofu_task_queue_wait_seconds', seconds, _WAIT_BUCKETS,
                   kind=_bounded(kind, fallback='chat', limit=48))


def record_llm_first_token(model: Any, seconds: float, provider: Any = '') -> None:
    labels = {
        'provider': _bounded(provider, fallback='unknown', limit=64),
        'model': _bounded(model, fallback='unknown', limit=96),
    }
    _STORE.observe('tofu_llm_first_token_seconds', seconds, _LLM_BUCKETS, **labels)


def record_llm_task(
    model: Any,
    provider: Any,
    duration_s: float,
    api_rounds: int,
    usage: dict | None,
    cost_usd: float = 0.0,
    outcome: str = 'success',
) -> None:
    """Record one terminal LLM task without using task/request ids as labels."""
    labels = {
        'provider': _bounded(provider, fallback='unknown', limit=64),
        'model': _bounded(model, fallback='unknown', limit=96),
        'outcome': outcome if outcome in {'success', 'error', 'aborted', 'budget'}
        else 'error',
    }
    try:
        from lib.cost import normalize_usage
        normalized = normalize_usage(usage)
    except Exception as exc:
        logger.debug('LLM usage normalization failed for metrics: %s', exc)
        normalized = {'input': 0, 'output': 0, 'cache_write': 0,
                      'cache_read': 0}
    context_tokens = (int(normalized.get('input') or 0)
                      + int(normalized.get('cache_write') or 0)
                      + int(normalized.get('cache_read') or 0))
    rounds = max(0, int(api_rounds or 0))
    cost = max(0.0, float(cost_usd or 0.0))
    _STORE.inc('tofu_llm_tasks_total', **labels)
    _STORE.observe('tofu_llm_total_duration_seconds', duration_s,
                   _LLM_BUCKETS, **labels)
    _STORE.observe('tofu_llm_api_rounds', rounds, _ROUND_BUCKETS, **labels)
    _STORE.observe('tofu_llm_context_tokens', context_tokens,
                   _TOKEN_BUCKETS, **labels)
    _STORE.observe('tofu_llm_estimated_cost_usd', cost,
                   _COST_BUCKETS, **labels)
    token_labels = {key: labels[key] for key in ('provider', 'model')}
    _STORE.inc('tofu_llm_context_tokens_total', context_tokens, **token_labels)
    _STORE.inc('tofu_llm_cache_read_tokens_total',
               int(normalized.get('cache_read') or 0), **token_labels)
    _STORE.inc('tofu_llm_output_tokens_total',
               int(normalized.get('output') or 0), **token_labels)
    _STORE.inc('tofu_llm_estimated_cost_usd_total', cost, **token_labels)
    if int(normalized.get('cache_read') or 0) > 0:
        _STORE.inc('tofu_llm_cache_hit_tasks_total', **token_labels)


def connection_open(transport: str, channel: Any) -> None:
    transport = transport if transport in _TRANSPORTS else 'other'
    labels = {'transport': transport, 'channel': _bounded(channel, limit=48)}
    _STORE.inc('tofu_stream_connections_total', **labels)
    _STORE.add_gauge('tofu_stream_connections_active', 1, **labels)


def connection_close(transport: str, channel: Any, outcome: str) -> None:
    transport = transport if transport in _TRANSPORTS else 'other'
    outcome = outcome if outcome in _CONNECTION_OUTCOMES else 'error'
    base = {'transport': transport, 'channel': _bounded(channel, limit=48)}
    _STORE.add_gauge('tofu_stream_connections_active', -1, **base)
    _STORE.inc('tofu_stream_disconnects_total', outcome=outcome, **base)


def record_replay(transport: str, kind: Any, count: int, *, reset: bool = False) -> None:
    labels = {
        'transport': transport if transport in _TRANSPORTS else 'other',
        'kind': _bounded(kind, limit=48),
        'reset': 'true' if reset else 'false',
    }
    _STORE.inc('tofu_stream_replayed_events_total', max(0, int(count)), **labels)


def record_stream_admission(channel: Any, outcome: str, count: int = 1) -> None:
    """Count bounded stream ownership/admission decisions without IDs."""
    normalized_outcome = (
        outcome if outcome in _STREAM_ADMISSION_OUTCOMES else 'capacity')
    _STORE.inc(
        'tofu_stream_admission_total',
        max(0, int(count)),
        channel=_bounded(channel, limit=48),
        outcome=normalized_outcome,
    )


def record_registry_eviction(kind: Any, reason: str, count: int = 1) -> None:
    """Count bounded task-registry removals without labeling task ids."""
    reason_label = reason if reason in {'ttl', 'discard', 'capacity', 'pressure'} \
        else 'other'
    amount = max(0, int(count or 0))
    if amount:
        _STORE.inc(
            'tofu_task_registry_evictions_total', amount,
            kind=_bounded(kind, fallback='unknown', limit=48),
            reason=reason_label,
        )


def record_task_event_eviction(kind: Any, count: int = 1) -> None:
    """Count replay events removed by the bounded in-memory retention ring."""
    amount = max(0, int(count or 0))
    if amount:
        _STORE.inc(
            'tofu_task_event_evictions_total', amount,
            kind=_bounded(kind, fallback='unknown', limit=48),
        )


def record_execution_started(kind: Any, phase: Any = 'created') -> None:
    """Account one active resource-owning execution without identity labels."""
    labels = {
        'kind': _bounded(kind, fallback='execution', limit=64),
        'phase': _bounded(phase, fallback='created', limit=32),
    }
    _STORE.inc('tofu_execution_sessions_started_total', kind=labels['kind'])
    _STORE.add_gauge('tofu_execution_sessions_active', 1, **labels)


def record_execution_phase_transition(kind: Any, source: Any, target: Any) -> None:
    kind_label = _bounded(kind, fallback='execution', limit=64)
    source_label = _bounded(source, fallback='unknown', limit=32)
    target_label = _bounded(target, fallback='unknown', limit=32)
    _STORE.add_gauge(
        'tofu_execution_sessions_active', -1,
        kind=kind_label, phase=source_label,
    )
    if target_label not in _EXECUTION_OUTCOMES:
        _STORE.add_gauge(
            'tofu_execution_sessions_active', 1,
            kind=kind_label, phase=target_label,
        )


def record_execution_resource_release(
    kind: Any,
    resource: Any,
    disposition: Any,
) -> None:
    disposition_text = str(disposition)
    disposition_label = (
        disposition_text
        if disposition_text in _EXECUTION_RESOURCE_DISPOSITIONS else 'failed'
    )
    _STORE.inc(
        'tofu_execution_resource_releases_total',
        kind=_bounded(kind, fallback='execution', limit=64),
        resource=_bounded(resource, fallback='unknown', limit=64),
        disposition=disposition_label,
    )


def record_execution_terminal(
    kind: Any,
    outcome: Any,
    invariants_satisfied: bool,
    duration_seconds: float,
) -> None:
    outcome_text = str(outcome)
    outcome_label = outcome_text if outcome_text in _EXECUTION_OUTCOMES else 'failed'
    labels = {
        'kind': _bounded(kind, fallback='execution', limit=64),
        'outcome': outcome_label,
        'invariants': 'satisfied' if invariants_satisfied else 'failed',
    }
    execution_settling_phase = 'settling'
    _STORE.add_gauge(
        'tofu_execution_sessions_active', -1,
        kind=labels['kind'], phase=execution_settling_phase,
    )
    _STORE.inc('tofu_execution_sessions_terminal_total', **labels)
    _STORE.observe(
        'tofu_execution_session_duration_seconds', duration_seconds,
        _LLM_BUCKETS, **labels,
    )


def record_execution_deadline(kind: Any) -> None:
    _STORE.inc(
        'tofu_execution_deadlines_total',
        kind=_bounded(kind, fallback='execution', limit=64),
    )


def background_job_started(kind: Any) -> None:
    """Record one owned process-local job using a closed kind inventory."""
    kind_label = str(kind) if str(kind) in _BACKGROUND_JOB_KINDS else 'other'
    _STORE.inc('tofu_background_jobs_started_total', kind=kind_label)
    _STORE.add_gauge('tofu_background_jobs_active', 1, kind=kind_label)


def background_job_finished(kind: Any, outcome: Any, duration_s: float) -> None:
    """Balance an owned job and record its finite terminal outcome."""
    kind_label = str(kind) if str(kind) in _BACKGROUND_JOB_KINDS else 'other'
    outcome_text = str(outcome)
    outcome_label = outcome_text if outcome_text in _BACKGROUND_JOB_OUTCOMES \
        else 'error'
    labels = {'kind': kind_label, 'outcome': outcome_label}
    _STORE.add_gauge('tofu_background_jobs_active', -1, kind=kind_label)
    _STORE.inc('tofu_background_jobs_completed_total', **labels)
    _STORE.observe('tofu_background_job_duration_seconds', duration_s,
                   _LLM_BUCKETS, **labels)


async def instrument_sse(generator: Any, channel: str = 'task') -> AsyncIterator[Any]:
    """Wrap a sync/async SSE iterable without changing its yielded frames."""
    connection_open('sse', channel)
    outcome = 'completed'
    try:
        if hasattr(generator, '__aiter__'):
            async for item in generator:
                yield item
        else:
            for item in generator:
                yield item
    except (asyncio.CancelledError, GeneratorExit):
        outcome = 'disconnected'
        raise
    except BaseException:
        outcome = 'error'
        raise
    finally:
        connection_close('sse', channel, outcome)
        closer = getattr(generator, 'aclose', None)
        if callable(closer):
            result = closer()
            if inspect.isawaitable(result):
                await result


class InstrumentedThreadPoolExecutor(ThreadPoolExecutor):
    """Thread pool with metrics and an explicit idle-retirement contract.

    CPython workers never expire after a burst.  The serving-loop owner can
    replace this executor once work above ``idle_retain_threads`` has been
    quiet for a bounded window. Local pending/active accounting is independent
    of the metrics store and balances futures cancelled before worker entry.
    """

    def __init__(
        self,
        *args: Any,
        metric_pool: str,
        idle_retain_threads: int = 0,
        **kwargs: Any,
    ) -> None:
        self.metric_pool = _bounded(metric_pool, limit=32)
        self._lifecycle_lock = threading.Lock()
        self._pending_jobs = 0
        self._active_jobs = 0
        self._last_excess_activity = time.monotonic()
        super().__init__(*args, **kwargs)
        self._idle_retain_threads = max(
            0, min(self._max_workers, int(idle_retain_threads)))
        _STORE.set('tofu_executor_workers', self._max_workers,
                   pool=self.metric_pool)
        _STORE.set('tofu_executor_resident_threads', 0,
                   pool=self.metric_pool)

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any):
        queued_at = time.monotonic()
        state = {
            'started': False,
            'pending_accounted': True,
            'excess_work': False,
        }

        with self._lifecycle_lock:
            self._pending_jobs += 1
            state['excess_work'] = bool(
                self._pending_jobs + self._active_jobs
                > self._idle_retain_threads)
            if state['excess_work']:
                self._last_excess_activity = queued_at

        def measured(*call_args: Any, **call_kwargs: Any):
            with self._lifecycle_lock:
                state['started'] = True
                if state['pending_accounted']:
                    self._pending_jobs = max(0, self._pending_jobs - 1)
                    state['pending_accounted'] = False
                self._active_jobs += 1
            wait = max(0.0, time.monotonic() - queued_at)
            _STORE.observe('tofu_executor_queue_wait_seconds', wait,
                           _WAIT_BUCKETS, pool=self.metric_pool)
            try:
                depth = self._work_queue.qsize()
            except (AttributeError, NotImplementedError) as exc:
                logger.debug('executor queue depth unavailable: %s', exc)
                depth = 0
            _STORE.set('tofu_executor_queued', depth, pool=self.metric_pool)
            _STORE.add_gauge('tofu_executor_active', 1, pool=self.metric_pool)
            try:
                return fn(*call_args, **call_kwargs)
            finally:
                with self._lifecycle_lock:
                    self._active_jobs = max(0, self._active_jobs - 1)
                    if state['excess_work']:
                        self._last_excess_activity = time.monotonic()
                _STORE.add_gauge('tofu_executor_active', -1,
                                 pool=self.metric_pool)

        try:
            future = super().submit(measured, *args, **kwargs)
        except BaseException as exc:
            with self._lifecycle_lock:
                if state['pending_accounted']:
                    self._pending_jobs = max(0, self._pending_jobs - 1)
                    state['pending_accounted'] = False
            if isinstance(exc, RuntimeError):
                _STORE.inc('tofu_executor_rejected_total',
                           pool=self.metric_pool)
            raise

        def _balance_cancelled_before_entry(done_future) -> None:
            if not done_future.cancelled():
                return
            with self._lifecycle_lock:
                if not state['started'] and state['pending_accounted']:
                    self._pending_jobs = max(0, self._pending_jobs - 1)
                    state['pending_accounted'] = False
                    if state['excess_work']:
                        self._last_excess_activity = time.monotonic()

        future.add_done_callback(_balance_cancelled_before_entry)
        try:
            depth = self._work_queue.qsize()  # CPython's executor queue.
        except (AttributeError, NotImplementedError) as exc:
            logger.debug('executor queue depth unavailable after submit: %s', exc)
            depth = 0
        _STORE.set('tofu_executor_queued', depth, pool=self.metric_pool)
        _STORE.set('tofu_executor_resident_threads', len(self._threads),
                   pool=self.metric_pool)
        return future

    def idle_retirement_snapshot(
        self,
        idle_seconds: float,
        *,
        now: float | None = None,
    ) -> dict[str, int | float | bool]:
        """Return bounded state used by the serving-loop retirement owner."""
        observed_at = time.monotonic() if now is None else float(now)
        with self._lifecycle_lock:
            pending = self._pending_jobs
            active = self._active_jobs
            quiet_for = max(0.0, observed_at - self._last_excess_activity)
            resident = len(self._threads)
            due = bool(
                idle_seconds > 0
                and not self._shutdown
                and pending == 0
                and active == 0
                and resident > self._idle_retain_threads
                and quiet_for >= idle_seconds
            )
        return {
            'due': due,
            'pending': pending,
            'active': active,
            'resident_threads': resident,
            'retain_threads': self._idle_retain_threads,
            'quiet_for_seconds': quiet_for,
        }

    def record_idle_retirement(self, resident_threads: int) -> None:
        """Publish one owner-approved retirement after replacement is live."""
        retired = max(0, int(resident_threads))
        _STORE.inc('tofu_executor_idle_retirements_total',
                   pool=self.metric_pool)
        _STORE.inc('tofu_executor_idle_retired_threads_total', retired,
                   pool=self.metric_pool)
        _STORE.set('tofu_executor_resident_threads', 0,
                   pool=self.metric_pool)


def publish_executor_state(
    pool: str,
    *,
    workers: int,
    queued: int,
    active: int,
    resident_threads: int,
    abandoned: int = 0,
) -> None:
    """Publish one internally-consistent executor scheduling snapshot.

    The ordinary sync pool updates these gauges incrementally above.  The
    recoverable agent executor owns a logical worker set that may temporarily
    include a quarantined, physically-stuck thread, so it publishes the full
    bounded snapshot after each state transition instead.
    """
    metric_pool = _bounded(pool, limit=32)
    _STORE.set('tofu_executor_workers', max(0, int(workers)), pool=metric_pool)
    _STORE.set('tofu_executor_queued', max(0, int(queued)), pool=metric_pool)
    _STORE.set('tofu_executor_active', max(0, int(active)), pool=metric_pool)
    _STORE.set(
        'tofu_executor_resident_threads',
        max(0, int(resident_threads)),
        pool=metric_pool,
    )
    _STORE.set(
        'tofu_executor_abandoned', max(0, int(abandoned)), pool=metric_pool,
    )


def observe_executor_queue_wait(pool: str, seconds: float) -> None:
    """Record how long one accepted executor job waited before entry."""
    _STORE.observe(
        'tofu_executor_queue_wait_seconds',
        max(0.0, float(seconds)),
        _WAIT_BUCKETS,
        pool=_bounded(pool, limit=32),
    )


def record_executor_rejection(pool: str, *, reason: str) -> None:
    """Record one bounded-executor refusal without exposing job identity."""
    metric_pool = _bounded(pool, limit=32)
    _STORE.inc(
        'tofu_executor_rejected_total', pool=metric_pool,
    )
    _STORE.inc(
        'tofu_executor_rejection_reasons_total',
        pool=metric_pool,
        reason=_bounded(reason, limit=32),
    )


def record_executor_abandonment(
    pool: str,
    *,
    recovered: bool,
    failure_reason: str = 'budget_exhausted',
) -> None:
    """Record whether a wedged physical worker received a logical replacement."""
    _STORE.inc(
        'tofu_executor_abandonments_total',
        pool=_bounded(pool, limit=32),
        outcome=(
            'recovered' if recovered
            else _bounded(failure_reason or 'unknown', limit=32)
        ),
    )


def record_executor_idle_retirement(pool: str, resident_threads: int) -> None:
    """Record one owner-approved idle generation retirement."""
    metric_pool = _bounded(pool, limit=32)
    retired = max(0, int(resident_threads))
    _STORE.inc('tofu_executor_idle_retirements_total', pool=metric_pool)
    _STORE.inc(
        'tofu_executor_idle_retired_threads_total', retired, pool=metric_pool,
    )


_HELP = {
    'tofu_http_requests_total': 'HTTP requests grouped by method, route template and status.',
    'tofu_http_request_duration_seconds': 'HTTP request duration in seconds.',
    'tofu_event_loop_lag_seconds': 'Latest measured event-loop scheduling lag.',
    'tofu_runtime_probe_failures_total': 'Best-effort runtime host probe failures.',
    'tofu_cgroup_relief_attempts_total': 'Process-local memory relief passes.',
    'tofu_cgroup_relief_reclaimed_bytes_total': 'Observed bytes reclaimed during memory relief.',
    'tofu_cgroup_relief_reclaimed_bytes_latest': 'Latest observed bytes reclaimed during memory relief.',
    'tofu_cgroup_relief_cache_entries_dropped_total': 'TTL cache entries dropped by memory relief.',
    'tofu_cgroup_relief_log_files_advised_total': 'Log files given a DONTNEED page-cache hint.',
    'tofu_cgroup_relief_log_bytes_advised_total': 'Apparent log bytes covered by DONTNEED hints.',
    'tofu_cgroup_relief_duration_seconds': 'Memory relief pass duration in seconds.',
    'tofu_task_queue_wait_seconds': 'Task queue wait before a worker starts.',
    'tofu_llm_first_token_seconds': 'LLM time to first token.',
    'tofu_llm_tasks_total': 'Terminal LLM tasks by bounded outcome.',
    'tofu_llm_total_duration_seconds': 'End-to-end LLM task duration.',
    'tofu_llm_api_rounds': 'Model API rounds per terminal task.',
    'tofu_llm_context_tokens': 'Context tokens consumed per terminal task.',
    'tofu_llm_estimated_cost_usd': 'Estimated USD cost per terminal task.',
    'tofu_llm_context_tokens_total': 'Context tokens consumed by LLM tasks.',
    'tofu_llm_cache_read_tokens_total': 'Cached prompt tokens read by LLM tasks.',
    'tofu_llm_output_tokens_total': 'Output tokens produced by LLM tasks.',
    'tofu_llm_estimated_cost_usd_total': 'Estimated USD cost of LLM tasks.',
    'tofu_llm_cache_hit_tasks_total': 'LLM tasks with at least one cached token.',
    'tofu_stream_connections_total': 'SSE and WebSocket connections opened.',
    'tofu_stream_connections_active': 'Currently open SSE and WebSocket connections.',
    'tofu_stream_disconnects_total': 'Stream connection terminal outcomes.',
    'tofu_stream_replayed_events_total': 'Events replayed after reconnect.',
    'tofu_stream_admission_total': 'Bounded stream admission and ownership decisions.',
    'tofu_task_registry_evictions_total': 'Tasks removed from an in-process registry.',
    'tofu_task_event_evictions_total': 'Task replay events removed at the retention limit.',
    'tofu_execution_sessions_started_total': 'Resource-owning execution sessions started.',
    'tofu_execution_sessions_active': 'Active execution sessions by monotonic phase.',
    'tofu_execution_sessions_terminal_total': 'Execution terminal outcomes and invariant verdicts.',
    'tofu_execution_session_duration_seconds': 'Execution lifetime through terminal resource settlement.',
    'tofu_execution_resource_releases_total': 'Execution resources released, deferred to recovery, or failed.',
    'tofu_execution_deadlines_total': 'Execution deadlines that requested owner cancellation.',
    'tofu_executor_workers': 'Configured executor worker capacity.',
    'tofu_executor_resident_threads': 'Currently materialized executor worker threads.',
    'tofu_executor_active': 'Executor jobs currently running.',
    'tofu_executor_queued': 'Executor jobs waiting to start.',
    'tofu_executor_abandoned': 'Wedged executor jobs quarantined behind replacement workers.',
    'tofu_executor_rejected_total': 'Executor submissions rejected before acceptance.',
    'tofu_executor_rejection_reasons_total': 'Executor submission refusals grouped by bounded reason.',
    'tofu_executor_abandonments_total': 'Wedged executor jobs grouped by bounded recovery outcome.',
    'tofu_executor_queue_wait_seconds': 'Executor queue wait before work starts.',
    'tofu_executor_idle_retirements_total': 'Idle executor generations retired.',
    'tofu_executor_idle_retired_threads_total': 'Worker threads released by idle retirement.',
    'tofu_background_jobs_started_total': 'Owned process-local background jobs started.',
    'tofu_background_jobs_active': 'Owned process-local background jobs currently active.',
    'tofu_background_jobs_completed_total': 'Owned background job terminal outcomes.',
    'tofu_background_job_duration_seconds': 'Owned background job duration in seconds.',
}


def _escape(value: str) -> str:
    return value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')


def _label_text(labels: tuple[tuple[str, str], ...], extra: tuple | None = None) -> str:
    pairs = list(labels)
    if extra:
        pairs.append(extra)
    if not pairs:
        return ''
    return '{' + ','.join(f'{key}="{_escape(value)}"' for key, value in pairs) + '}'


def prometheus_lines() -> list[str]:
    """Render a consistent snapshot in Prometheus text format."""
    counters, gauges, histograms = _STORE.snapshot()
    lines: list[str] = []
    names = sorted({key[0] for key in counters} | {key[0] for key in gauges})
    for name in names:
        metric_type = 'counter' if any(key[0] == name for key in counters) else 'gauge'
        lines.extend((f'# HELP {name} {_HELP.get(name, name)}',
                      f'# TYPE {name} {metric_type}'))
        source = counters if metric_type == 'counter' else gauges
        for (metric_name, labels), value in sorted(source.items()):
            if metric_name == name:
                lines.append(f'{name}{_label_text(labels)} {value}')

    for name in sorted({key[0] for key in histograms}):
        lines.extend((f'# HELP {name} {_HELP.get(name, name)}',
                      f'# TYPE {name} histogram'))
        for (metric_name, labels), state in sorted(histograms.items()):
            if metric_name != name:
                continue
            for upper, count in zip(state['buckets'], state['counts']):
                lines.append(
                    f'{name}_bucket{_label_text(labels, ("le", str(upper)))} {count}')
            lines.append(f'{name}_bucket{_label_text(labels, ("le", "+Inf"))} '
                         f'{state["count"]}')
            lines.append(f'{name}_sum{_label_text(labels)} {state["sum"]}')
            lines.append(f'{name}_count{_label_text(labels)} {state["count"]}')
    return lines


def reset_for_tests() -> None:
    with _STORE.lock:
        _STORE.counters.clear()
        _STORE.gauges.clear()
        _STORE.histograms.clear()
        _STORE.label_values.clear()


__all__ = [
    'InstrumentedThreadPoolExecutor', 'connection_close', 'connection_open',
    'background_job_finished', 'background_job_started',
    'instrument_sse', 'normalize_route_template', 'prometheus_lines',
    'record_http_request', 'record_llm_first_token', 'record_llm_task',
    'observe_executor_queue_wait', 'publish_executor_state',
    'record_executor_abandonment', 'record_executor_idle_retirement',
    'record_executor_rejection',
    'record_registry_eviction', 'record_replay', 'record_task_event_eviction',
    'record_stream_admission',
    'record_execution_deadline', 'record_execution_phase_transition',
    'record_execution_resource_release', 'record_execution_started',
    'record_execution_terminal',
    'record_cgroup_relief',
    'record_runtime_probe_failure',
    'record_task_queue_wait', 'reset_for_tests', 'route_template_for_request',
    'set_event_loop_lag',
]
