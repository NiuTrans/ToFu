"""Managed requests sessions and per-request network routing (docs/NETWORK.md).

The adapter owns connect-only route failover. Requests retains preparation,
redirects, authentication, hooks and response-body lifecycle. Explicit proxy
arguments pin a route; automatic requests use the shared route manager.
"""
from __future__ import annotations

from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import threading
import time

import requests

from lib.http_transport.connection_setup import (
    NetworkRoutesExhausted, connection_http_adapter,
)


@dataclass(frozen=True)
class RequestNetworkPolicy:
    scope: str | None = None
    explicit_proxy: bool = False


_request_policy: ContextVar[RequestNetworkPolicy | None] = ContextVar(
    'tofu_request_network_policy', default=None)
_cancelled = ContextVar('tofu_network_cancelled', default=None)


class NetworkRequestCancelled(requests.exceptions.RequestException):
    """The request owner cancelled before application bytes were sent."""


@contextmanager
def network_cancellation(cancelled):
    """Bind an owner's cancellation to synchronous route waits and failover."""
    token = _cancelled.set(cancelled)
    try:
        yield
    finally:
        _cancelled.reset(token)


def _check_cancelled():
    cancelled = _cancelled.get()
    if cancelled is not None and cancelled():
        raise NetworkRequestCancelled('Network request cancelled before delivery')


def route_metadata(route) -> dict:
    return {'routeId': route.route_id, 'routeMode': route.mode,
            'decisionReason': 'network_route',
            **({'poolId': route.pool_id} if route.pool_id else {})}


def explicit_route(url: str, proxies: dict):
    from lib.network_routes import Route
    from lib.proxy import network_route_specs
    selected = requests.utils.select_proxy(url, proxies)
    if not selected:
        return Route('direct', 'direct', 'direct')
    for route in network_route_specs(url, scope='subscription'):
        if route.proxy_url == selected:
            return route
    selected = str(getattr(selected, 'url', selected))
    identity = hashlib.sha256(selected.encode()).hexdigest()
    return Route('explicit:' + identity, 'explicit proxy', 'proxy', proxy_url=selected)


def append_untried(routes, attempted, candidates):
    known = {route.route_id for route in routes}
    for route in candidates:
        if route.route_id not in attempted and route.route_id not in known:
            routes.append(route)
            known.add(route.route_id)


class RoutingHTTPAdapter(requests.adapters.HTTPAdapter):
    """Route one prepared request before any response body can be consumed."""

    def __init__(self, *, url_validator=None, **kwargs):
        self._url_validator = url_validator
        self._proxy_pool_lock = threading.RLock()
        self._proxy_epoch = None
        super().__init__(**kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        from lib.proxy import network_topology_epoch
        with self._proxy_pool_lock:
            epoch = network_topology_epoch()
            if epoch != self._proxy_epoch:
                self._proxy_epoch = epoch
                retired, self.proxy_manager = self.proxy_manager, {}
                for manager in retired.values():
                    # urllib3 drops idle sockets; borrowed connections finish
                    # normally and are discarded on their eventual return.
                    manager.clear()
            return super().proxy_manager_for(*args, **kwargs)

    def send(self, request, **kwargs):
        if self._url_validator is not None:
            self._url_validator(request.url)
        policy = _request_policy.get() or RequestNetworkPolicy(
            explicit_proxy=kwargs.get('proxies') is not None)
        return send_routed_request(super().send, request, policy=policy, **kwargs)


def send_routed_request(wire_send, request, *, policy, **kwargs):
    """SDK transport port after preparation and domain URL authorization."""
    from lib import proxy
    from lib.network_routes import is_safe_connect_failure

    _check_cancelled()
    routes = ([explicit_route(request.url, kwargs.get('proxies') or {})]
              if policy.explicit_proxy else
              list(proxy.network_route_candidates(
                  request.url, scope=policy.scope, cancelled=_cancelled.get())))
    attempted = set()
    for route in routes:
        _check_cancelled()
        attempted.add(route.route_id)
        started = time.monotonic()
        options = {**kwargs, 'proxies': route.requests_proxies()}
        try:
            response = wire_send(request, **options)
        except requests.exceptions.RequestException as error:
            if not is_safe_connect_failure(error):
                raise
            proxy.report_network_route(request.url, route, False)
            if policy.explicit_proxy:
                raise
            append_untried(routes, attempted, proxy.network_route_candidates(
                request.url, scope=policy.scope, cancelled=_cancelled.get()))
            continue
        latency = (time.monotonic() - started) * 1000.0
        proxy.report_network_route(request.url, route, True, latency)
        response.tofu_network_route = route_metadata(route)
        response.tofu_network_latency_ms = latency
        response.tofu_route = route
        return response
    _check_cancelled()
    raise NetworkRoutesExhausted('No configured network route connected') from None


class ManagedSession(requests.Session):
    """Request-scoped proxy policy without changing global library behavior."""

    def __init__(self, *, scope=None):
        super().__init__()
        self.network_scope = scope

    def request(self, method, url, **kwargs):
        scope = kwargs.pop('network_scope', self.network_scope)
        if self.proxies and kwargs.get('proxies') is None:
            kwargs['proxies'] = dict(self.proxies)
        token = _request_policy.set(RequestNetworkPolicy(
            scope=scope, explicit_proxy=kwargs.get('proxies') is not None))
        try:
            return super().request(method, url, **kwargs)
        finally:
            _request_policy.reset(token)

    def send(self, request, **kwargs):
        # SDKs can send PreparedRequest directly, outside Session.request.
        if _request_policy.get() is not None:
            return super().send(request, **kwargs)
        token = _request_policy.set(RequestNetworkPolicy(
            scope=self.network_scope, explicit_proxy=kwargs.get('proxies') is not None))
        try:
            return super().send(request, **kwargs)
        finally:
            _request_policy.reset(token)


def managed_session(*, scope=None, adapter=None, **adapter_options) -> ManagedSession:
    """Create an owned session for an application or SDK; callers close it."""
    session = ManagedSession(scope=scope)
    try:
        if adapter is None:
            adapter = connection_http_adapter(
                adapter_class=RoutingHTTPAdapter, **adapter_options)
        if not isinstance(adapter, RoutingHTTPAdapter):
            raise TypeError('managed sessions require a routing HTTP adapter')
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session
    except BaseException:
        session.close()
        raise
