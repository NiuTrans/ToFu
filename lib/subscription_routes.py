"""Health-aware server egress routing for subscription providers.

The generic :mod:`lib.netpath` selector compares direct traffic with one
environment proxy.  Subscription traffic has a different topology: direct,
zero or more explicitly scoped pool proxies, and an optional environment
proxy are all independent routes.  This module races lightweight probes over
those routes, remembers health per ``(target host, route)``, and returns an
ordered plan for the real request.

Only probes are raced.  A model request itself is never hedged, so selecting a
faster route cannot duplicate billing or tool execution.  Transports may move
to the next route only when connection setup failed before response headers.
"""

from __future__ import annotations

import concurrent.futures
import random
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'Route', 'RouteManager', 'ProbeResult',
    'manager', 'probe_route', 'is_safe_connect_failure',
]

_PROBE_CONNECT_TIMEOUT_S = 2.0
_PROBE_READ_TIMEOUT_S = 3.0
_COLD_RACE_TIMEOUT_S = 5.5
_HEALTH_REFRESH_S = 60.0
_NETWORK_BACKOFF_S = (5.0, 15.0, 30.0, 60.0, 120.0)
_POLICY_BACKOFF_S = 300.0
_EWMA_ALPHA = 0.3
_PREFERRED_HYSTERESIS = 1.25
# Proxy pool allows 16 entries; 32 workers let direct + env + every pool row
# for one cold host truly race at once while still bounding cross-host bursts.
_MAX_PROBE_WORKERS = 32


@dataclass(frozen=True, repr=False)
class Route:
    """One concrete server-side path.

    ``proxy_url`` can contain credentials and is therefore deliberately
    omitted from ``repr``.  ``mode`` is one of ``direct``, ``proxy``, or
    ``env``.  Priority is only a deterministic tie-breaker; measured latency
    and the current preferred route lead normal selection.
    """

    route_id: str
    label: str
    mode: str
    priority: int = 0
    proxy_url: str = ''
    pool_id: str = ''

    def requests_proxies(self) -> dict:
        if self.mode == 'direct':
            return {'no_proxy': '*'}
        if self.mode == 'proxy':
            return {'http': self.proxy_url, 'https': self.proxy_url}
        return {}

    def async_proxy_url(self) -> 'str | None':
        if self.mode == 'direct':
            return None
        return self.proxy_url or None


@dataclass(frozen=True)
class ProbeResult:
    verdict: str
    latency_ms: 'float | None' = None
    status_code: int = 0

    @property
    def ok(self) -> bool:
        return self.verdict == 'ok'


def is_safe_connect_failure(error: BaseException) -> bool:
    """Whether retrying another route cannot replay an accepted request.

    Proxy negotiation, DNS/TCP connect, TLS handshake, and connect timeout
    failures happen before the provider can accept the HTTP request.  A plain
    post-send connection reset is intentionally excluded: no response headers
    does not prove the provider failed to receive the body.
    """
    try:
        import requests
        safe_requests = (
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ProxyError,
            requests.exceptions.SSLError,
        )
        if isinstance(error, safe_requests):
            return True
    except Exception as error:
        logger.debug('[SubscriptionRoute] requests error types unavailable: %s',
                     type(error).__name__)
    try:
        from urllib3 import exceptions as urllib3_errors
        safe_urllib3 = tuple(
            cls for cls in (
                getattr(urllib3_errors, 'ConnectTimeoutError', None),
                getattr(urllib3_errors, 'NewConnectionError', None),
                getattr(urllib3_errors, 'NameResolutionError', None),
                getattr(urllib3_errors, 'ProxyError', None),
                getattr(urllib3_errors, 'SSLError', None),
            ) if cls is not None)
    except Exception as error:
        logger.debug('[SubscriptionRoute] urllib3 error types unavailable: %s',
                     type(error).__name__)
        safe_urllib3 = ()
    seen = set()
    stack = [error]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if safe_urllib3 and isinstance(current, safe_urllib3):
            return True
        cause = getattr(current, '__cause__', None)
        context = getattr(current, '__context__', None)
        if isinstance(cause, BaseException):
            stack.append(cause)
        if isinstance(context, BaseException):
            stack.append(context)
        for arg in getattr(current, 'args', ()):
            if isinstance(arg, BaseException):
                stack.append(arg)
    return False


@dataclass
class _Health:
    has_success: bool = False
    consecutive_failures: int = 0
    ewma_ms: 'float | None' = None
    samples: int = 0
    last_success: float = 0.0
    last_failure: float = 0.0
    last_probe: float = 0.0
    circuit_until: float = 0.0
    backoff_step: int = 0
    failure_kind: str = ''


def probe_route(url: str, route: Route) -> ProbeResult:
    """Reach the real endpoint without auth over exactly ``route``.

    A normal application response (including provider 5xx) proves the network
    path.  Policy/geo blocks and proxy-auth challenges do not.  A
    dedicated session with ``trust_env=False`` makes direct and pool probes
    immune to ambient proxy variables; only the explicit ``env`` route uses
    them.
    """
    import requests

    started = time.monotonic()
    session = requests.Session()
    session.trust_env = route.mode == 'env'
    try:
        response = session.post(
            url,
            json={},
            timeout=(_PROBE_CONNECT_TIMEOUT_S, _PROBE_READ_TIMEOUT_S),
            proxies=route.requests_proxies() if route.mode == 'proxy' else None,
            allow_redirects=False,
            stream=True,
        )
        status = int(response.status_code or 0)
        response.close()
        latency = (time.monotonic() - started) * 1000.0
        if status == 403:
            return ProbeResult('policy_blocked', latency, status)
        if status == 407:
            return ProbeResult('proxy_auth', latency, status)
        if status <= 0:
            return ProbeResult('network_fail', latency, status)
        # A 5xx may be generated by the provider itself.  It still proves
        # that this network route reached the application layer; provider
        # availability is the dispatcher's concern, not route health.
        return ProbeResult('ok', latency, status)
    except Exception as error:
        logger.debug('[SubscriptionRoute] probe via %s failed (%s)',
                     route.label, type(error).__name__)
        return ProbeResult('network_fail')
    finally:
        session.close()


class RouteManager:
    """Concurrent probe race plus per-host circuit breaker.

    Unknown or half-open routes are probed concurrently.  With no known-good
    path, callers wait only until the first successful probe.  If a healthy
    path already exists, it is returned immediately while stale and recovered
    routes are refreshed in the background.  Per-route singleflight prevents
    a burst of requests from multiplying probes.  Probe workers exist only
    while a batch is in flight; the executor is rebuilt lazily for the next
    network race so one cold request does not leave idle threads resident.
    """

    def __init__(self, *, probe=probe_route, clock=time.monotonic,
                 jitter=None, max_workers: int = _MAX_PROBE_WORKERS):
        self._probe = probe
        self._clock = clock
        self._jitter = jitter or (lambda seconds: seconds * random.uniform(
            0.8, 1.2))
        self._lock = threading.RLock()
        self._health: dict[tuple[str, str], _Health] = {}
        self._preferred: dict[str, str] = {}
        self._inflight: dict[tuple[str, str], concurrent.futures.Future] = {}
        self._generation = 0
        self._max_workers = max_workers
        self._executor: 'concurrent.futures.ThreadPoolExecutor | None' = None
        self._closed = False

    @staticmethod
    def _host(url: str) -> str:
        return (urlparse(url).hostname or '').lower()

    @staticmethod
    def _dedupe(routes: list[Route]) -> list[Route]:
        seen = set()
        out = []
        for route in routes:
            if route.route_id in seen:
                continue
            seen.add(route.route_id)
            out.append(route)
        return out

    def reset(self) -> None:
        """Forget all route measurements after a topology change."""
        with self._lock:
            self._generation += 1
            futures = list(self._inflight.values())
            self._inflight.clear()
            self._health.clear()
            self._preferred.clear()
            executor = self._executor
            self._executor = None
        for future in futures:
            future.cancel()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        """Release worker threads (primarily for isolated tests)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            futures = list(self._inflight.values())
            self._inflight.clear()
            self._health.clear()
            self._preferred.clear()
            executor = self._executor
            self._executor = None
        for future in futures:
            future.cancel()
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    def _record(self, host: str, route: Route, result: ProbeResult, *,
                source: str = 'request') -> None:
        now = self._clock()
        recovered = False
        opened_for = 0.0
        with self._lock:
            state = self._health.setdefault((host, route.route_id), _Health())
            state.last_probe = now
            if result.ok:
                recovered = state.consecutive_failures > 0
                state.has_success = True
                state.consecutive_failures = 0
                state.circuit_until = 0.0
                state.backoff_step = 0
                state.failure_kind = ''
                state.last_success = now
                if result.latency_ms is not None and result.latency_ms > 0:
                    state.samples += 1
                    if state.ewma_ms is None:
                        state.ewma_ms = float(result.latency_ms)
                    else:
                        state.ewma_ms = (
                            _EWMA_ALPHA * float(result.latency_ms)
                            + (1.0 - _EWMA_ALPHA) * state.ewma_ms)
            else:
                state.consecutive_failures += 1
                state.last_failure = now
                state.failure_kind = result.verdict or 'network_fail'
                state.has_success = False
                if state.failure_kind in ('policy_blocked', 'proxy_auth'):
                    opened_for = _POLICY_BACKOFF_S
                else:
                    idx = min(state.backoff_step,
                              len(_NETWORK_BACKOFF_S) - 1)
                    opened_for = self._jitter(_NETWORK_BACKOFF_S[idx])
                    state.backoff_step = min(
                        state.backoff_step + 1,
                        len(_NETWORK_BACKOFF_S) - 1)
                state.circuit_until = now + max(0.0, opened_for)
                if self._preferred.get(host) == route.route_id:
                    self._preferred.pop(host, None)
        if result.ok and recovered:
            logger.info('[SubscriptionRoute] %s via %s recovered',
                        host, route.label)
        elif not result.ok and source == 'probe':
            # A background reachability race is expected to reject unavailable
            # alternatives; the circuit opening is the successful outcome.
            # Real request failures still warn through ``report`` below.
            logger.debug(
                '[SubscriptionRoute] probe: %s via %s unavailable (%s) — '
                'circuit open %.1fs',
                host, route.label, result.verdict, opened_for)
        elif not result.ok:
            logger.warning(
                '[SubscriptionRoute] %s via %s unavailable (%s) — '
                'circuit open %.1fs',
                host, route.label, result.verdict, opened_for)

    def report(self, url: str, route: Route, ok: bool,
               latency_ms: 'float | None' = None,
               failure_kind: str = 'network_fail') -> None:
        """Feed an actual request's connection outcome to the scorer."""
        host = self._host(url)
        if not host:
            return
        self._record(
            host, route,
            ProbeResult('ok' if ok else failure_kind, latency_ms))

    def _run_probe(self, url: str, host: str, route: Route,
                   generation: int) -> ProbeResult:
        try:
            result = self._probe(url, route)
            if not isinstance(result, ProbeResult):
                result = ProbeResult('network_fail')
        except Exception as error:
            logger.debug('[SubscriptionRoute] probe worker via %s raised (%s)',
                         route.label, type(error).__name__)
            result = ProbeResult('network_fail')
        with self._lock:
            if generation != self._generation:
                return result
        self._record(host, route, result, source='probe')
        return result

    def _probe_done(
            self, key: tuple[str, str],
            future: concurrent.futures.Future,
            executor: concurrent.futures.ThreadPoolExecutor) -> None:
        executor_to_shutdown = None
        with self._lock:
            current = self._inflight.get(key)
            if current is future:
                self._inflight.pop(key, None)
                if not self._inflight and self._executor is executor:
                    self._executor = None
                    executor_to_shutdown = executor
        if executor_to_shutdown is not None:
            # This callback can run on the executor's final worker. A
            # non-waiting shutdown lets that worker return normally.
            executor_to_shutdown.shutdown(wait=False, cancel_futures=False)

    def _executor_locked(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._closed:
            raise RuntimeError('subscription route manager is closed')
        executor = self._executor
        if executor is None:
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix='subscription-probe')
            self._executor = executor
        return executor

    def _ensure_probe(self, url: str, host: str, route: Route):
        key = (host, route.route_id)
        executor_to_shutdown = None
        try:
            with self._lock:
                existing = self._inflight.get(key)
                if existing is not None:
                    return existing
                generation = self._generation
                executor = self._executor_locked()
                try:
                    future = executor.submit(
                        self._run_probe, url, host, route, generation)
                except BaseException:
                    if not self._inflight and self._executor is executor:
                        self._executor = None
                        executor_to_shutdown = executor
                    raise
                self._inflight[key] = future
        except BaseException:
            if executor_to_shutdown is not None:
                executor_to_shutdown.shutdown(
                    wait=False, cancel_futures=True)
            raise
        future.add_done_callback(
            lambda done: self._probe_done(key, done, executor))
        return future

    def _healthy(self, host: str, routes: list[Route], now: float) -> list[Route]:
        with self._lock:
            return [
                route for route in routes
                if ((state := self._health.get((host, route.route_id)))
                    is not None
                    and state.has_success
                    and state.consecutive_failures == 0
                    and now >= state.circuit_until)
            ]

    def _due(self, host: str, route: Route, now: float,
             *, force: bool = False) -> bool:
        with self._lock:
            if (host, route.route_id) in self._inflight:
                return True
            state = self._health.get((host, route.route_id))
            if force or state is None:
                return True
            if now < state.circuit_until:
                return False
            if state.consecutive_failures:
                return True
            return now - state.last_probe >= _HEALTH_REFRESH_S

    def _rank(self, host: str, routes: list[Route]) -> list[Route]:
        with self._lock:
            health = {
                route.route_id: self._health.get((host, route.route_id))
                for route in routes
            }
            preferred = self._preferred.get(host, '')

            def score(route: Route):
                state = health[route.route_id]
                latency = (state.ewma_ms if state and state.ewma_ms is not None
                           else float('inf'))
                return latency, route.priority, route.route_id

            ordered = sorted(routes, key=score)
            best = ordered[0]
            by_id = {route.route_id: route for route in ordered}
            incumbent = by_id.get(preferred)
            if incumbent is not None:
                best_state = health[best.route_id]
                incumbent_state = health[incumbent.route_id]
                best_ms = best_state.ewma_ms if best_state else None
                incumbent_ms = incumbent_state.ewma_ms if incumbent_state else None
                if (best_ms is None or incumbent_ms is None
                        or incumbent_ms <= best_ms * _PREFERRED_HYSTERESIS):
                    best = incumbent
            self._preferred[host] = best.route_id
        return [best] + [r for r in ordered if r.route_id != best.route_id]

    def candidates(self, url: str, routes: list[Route], *,
                   wait_timeout: float = _COLD_RACE_TIMEOUT_S,
                   force_probe: bool = False) -> list[Route]:
        """Return healthy routes, fastest stable route first.

        Cold start waits for the first successful concurrent probe.  A known
        healthy route returns without waiting; any stale/half-open alternatives
        continue probing in the executor.
        """
        host = self._host(url)
        routes = self._dedupe(routes)
        if not host or not routes:
            return []
        now = self._clock()
        healthy = self._healthy(host, routes, now)
        futures = []
        for route in routes:
            if self._due(host, route, now, force=force_probe):
                futures.append(self._ensure_probe(url, host, route))
        if healthy:
            return self._rank(host, healthy)

        pending = set(futures)
        deadline = time.monotonic() + max(0.0, wait_timeout)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = concurrent.futures.wait(
                pending, timeout=remaining,
                return_when=concurrent.futures.FIRST_COMPLETED)
            if not done:
                break
            healthy = self._healthy(host, routes, self._clock())
            if healthy:
                return self._rank(host, healthy)
        healthy = self._healthy(host, routes, self._clock())
        return self._rank(host, healthy) if healthy else []

    def cached_candidates(self, url: str, routes: list[Route]) -> list[Route]:
        """Return the current healthy plan without probing or waiting."""
        host = self._host(url)
        routes = self._dedupe(routes)
        if not host or not routes:
            return []
        healthy = self._healthy(host, routes, self._clock())
        return self._rank(host, healthy) if healthy else []

    def verdict(self, url: str, routes: list[Route]) -> str:
        """Summarize cached route health without initiating network I/O."""
        host = self._host(url)
        routes = self._dedupe(routes)
        if not host or not routes:
            return 'unknown'
        now = self._clock()
        if self._healthy(host, routes, now):
            return 'ok'
        with self._lock:
            states = [self._health.get((host, route.route_id))
                      for route in routes]
        if any(state is None for state in states):
            return 'unknown'
        kinds = {state.failure_kind for state in states if state}
        if kinds and kinds <= {'policy_blocked', 'proxy_auth'}:
            return 'geo_blocked'
        return 'network_fail'

    def status(self) -> dict:
        """Credential-free diagnostics for tests and status surfaces."""
        now = self._clock()
        with self._lock:
            routes = {}
            for (host, route_id), state in self._health.items():
                routes.setdefault(host, {})[route_id] = {
                    'healthy': bool(
                        state.has_success
                        and state.consecutive_failures == 0
                        and now >= state.circuit_until),
                    'ewma_ms': (None if state.ewma_ms is None
                                else round(state.ewma_ms, 1)),
                    'failures': state.consecutive_failures,
                    'failure_kind': state.failure_kind,
                    'retry_in_s': round(max(0.0, state.circuit_until - now), 1),
                }
            return {'preferred': dict(self._preferred), 'routes': routes}


manager = RouteManager()
