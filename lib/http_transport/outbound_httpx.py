"""Synchronous HTTPX SDK port for the shared network authority.

Owns route-specific pools and streaming leases; request preparation, TLS, HTTP/2
and response consumption stay with HTTPX. No client or cookie state is global.
"""
from __future__ import annotations

import threading
import time
import httpx

from lib.http_transport.connect_trace import ConnectTrace
from lib.http_transport.connection_setup import RoutesExhausted
from lib.http_transport.outbound import append_untried, explicit_route, route_metadata
from lib.log import get_logger

logger = get_logger(__name__)


class RoutingHTTPXTransport(httpx.BaseTransport):
    def __init__(self, *, proxy=None, automatic=True, scope=None, **options):
        self._proxy, self._automatic, self._scope = proxy, automatic, scope
        self._options = options
        self._pools = {}
        self._lock = threading.RLock()
        self._closed = False

    def _acquire(self, proxy_url):
        from lib.proxy import network_topology_epoch
        from lib.http_transport.outbound_async import transport_proxy
        epoch = network_topology_epoch()
        with self._lock:
            if self._closed:
                raise RuntimeError('network transport is closed')
            for key, entry in list(self._pools.items()):
                if key[0] != epoch and not entry['active']:
                    del self._pools[key]
                    entry['transport'].close()
            key = epoch, proxy_url
            if key not in self._pools:
                self._pools[key] = {'active': 0, 'transport': httpx.HTTPTransport(
                    proxy=transport_proxy(httpx, proxy_url, self._proxy, self._options),
                    trust_env=False, **self._options)}
            entry = self._pools[key]
            entry['active'] += 1
            return key, entry['transport']

    def _release(self, key):
        from lib.proxy import network_topology_epoch
        with self._lock:
            entry = self._pools.get(key)
            if entry is not None:
                entry['active'] -= 1
                if not entry['active'] and key[0] != network_topology_epoch():
                    del self._pools[key]
                    entry['transport'].close()

    def handle_request(self, request):
        from lib import proxy
        url = str(request.url)
        scope = request.extensions.get('tofu_network_scope', self._scope)
        routes = (list(proxy.network_route_candidates(url, scope=scope))
                  if self._automatic else [explicit_route(
                      url, {'http': self._proxy, 'https': self._proxy})])
        attempted = set()
        for route in routes:
            attempted.add(route.route_id)
            key, transport = self._acquire(route.async_proxy_url())
            started, trace = time.monotonic(), ConnectTrace(request)
            request.extensions['trace'] = trace.sync
            try:
                response = transport.handle_request(request)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError, httpx.ReadTimeout) as error:
                self._release(key)
                if isinstance(error, httpx.ReadTimeout) and not trace.connecting:
                    raise
                proxy.report_network_route(url, route, False)
                if not self._automatic:
                    raise
                append_untried(routes, attempted, proxy.network_route_candidates(url, scope=scope))
                continue
            except BaseException:
                self._release(key)
                raise
            finally:
                trace.restore()
            latency = (time.monotonic() - started) * 1000
            proxy.report_network_route(url, route, True, latency)
            response.extensions.update(tofu_network_route=route_metadata(route),
                                       tofu_network_latency_ms=latency, tofu_route=route)
            if response.is_closed:
                self._release(key)
            else:
                response.stream = _LeasedStream(response.stream, lambda: self._release(key))
            return response
        class Exhausted(httpx.ConnectError, RoutesExhausted):
            pass
        raise Exhausted('No configured network route connected', request=request) from None

    def close(self):
        with self._lock:
            self._closed = True
            pools, self._pools = list(self._pools.values()), {}
        first_error = None
        for entry in pools:
            try:
                entry['transport'].close()
            except BaseException as error:
                logger.debug('Transport close failed while closing all pools: %s', error)
                first_error = first_error or error
        if first_error is not None:
            raise first_error


class _LeasedStream(httpx.SyncByteStream):
    def __init__(self, stream, release):
        self.stream, self.release = stream, release
        self._lock = threading.RLock()
        self._closed = False

    def __iter__(self):
        yield from self.stream

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.stream.close()
            finally:
                self.release()


def managed_httpx_client(*, proxy=None, automatic=None, scope=None, verify=True,
                         limits=None, **options):
    from lib.http_transport.outbound_async import transport_options
    automatic = proxy is None if automatic is None else automatic
    transport = RoutingHTTPXTransport(
        proxy=proxy, automatic=automatic, scope=scope,
        **transport_options(httpx, verify, limits, options))
    return httpx.Client(transport=transport, trust_env=False, **options)
