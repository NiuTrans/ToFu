"""httpx transport using the same route authority as managed requests.

Entry point managed_async_client creates one caller-owned client. Its concrete
proxy transports are lazy and closed together. HTTPX owns body preparation,
redirects and response consumption, so failover cannot replay a read failure.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from lib.http_transport.outbound import append_untried, route_metadata
from lib.log import get_logger

logger = get_logger(__name__)
from lib.http_transport.connect_trace import ConnectTrace
from lib.http_transport.connection_setup import RoutesExhausted


class RoutingAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, proxy=None, automatic=True, scope=None,
                 http_module=httpx, **options):
        self._proxy = proxy
        self._automatic = automatic
        self._scope = scope
        self._options = options
        self._http = http_module
        self._transports = {}
        self._closed = False
        self._epoch = None
        self._closing = None

    async def _acquire(self, proxy_url):
        from lib.proxy import network_topology_epoch
        if self._closed:
            raise RuntimeError('network transport is closed')
        epoch = network_topology_epoch()
        if epoch != self._epoch:
            self._epoch = epoch
            retired = []
            for key, entry in list(self._transports.items()):
                if not entry['active']:
                    del self._transports[key]
                    retired.append(entry)
            await _joined_close(_close_entries(retired))
        if self._closed:
            raise RuntimeError('network transport is closed')
        key = (epoch, proxy_url)
        if key not in self._transports:
            self._transports[key] = {
                'active': 0, 'transport': self._http.AsyncHTTPTransport(
                    proxy=transport_proxy(self._http, proxy_url, self._proxy, self._options),
                    trust_env=False, **self._options)}
        entry = self._transports[key]
        entry['active'] += 1
        return key, entry['transport']

    async def _release(self, key):
        entry = self._transports.get(key)
        if entry is None:
            return
        entry['active'] -= 1
        if not entry['active'] and key[0] != self._epoch:
            del self._transports[key]
            await entry['transport'].aclose()

    def _leased_stream(self, stream, key):
        owner = self
        class LeasedStream(self._http.AsyncByteStream):
            closing = None

            async def __aiter__(self):
                async for chunk in stream:
                    yield chunk

            async def aclose(self):
                async def close():
                    try:
                        await stream.aclose()
                    finally:
                        await owner._release(key)
                if self.closing is None:
                    self.closing = asyncio.create_task(close())
                await _joined_close(self.closing)
        return LeasedStream()

    async def handle_async_request(self, request):
        from lib import proxy
        from lib.http_transport.outbound import explicit_route

        url = str(request.url)
        scope = request.extensions.get('tofu_network_scope', self._scope)
        routes = (list(await proxy.async_network_route_candidates(url, scope=scope))
                  if self._automatic else [explicit_route(
                      url, {'http': self._proxy, 'https': self._proxy})])
        attempted = set()
        for route in routes:
            attempted.add(route.route_id)
            started = time.monotonic()
            key, transport = await self._acquire(route.async_proxy_url())
            trace = ConnectTrace(request)
            request.extensions['trace'] = trace.asynchronous
            try:
                response = await transport.handle_async_request(request)
            except (self._http.ConnectError, self._http.ConnectTimeout, self._http.ProxyError, self._http.ReadTimeout) as error:
                await _joined_close(self._release(key))
                if isinstance(error, self._http.ReadTimeout) and not trace.connecting:
                    raise
                proxy.report_network_route(url, route, False)
                if not self._automatic:
                    raise
                append_untried(routes, attempted, await proxy.async_network_route_candidates(
                    url, scope=scope))
                continue
            except BaseException:
                await _joined_close(self._release(key))
                raise
            finally:
                trace.restore()
            latency = (time.monotonic() - started) * 1000.0
            proxy.report_network_route(url, route, True, latency)
            response.extensions['tofu_network_route'] = route_metadata(route)
            response.extensions['tofu_network_latency_ms'] = latency
            response.extensions['tofu_route'] = route
            if response.is_closed:
                await _joined_close(self._release(key))
            else:
                response.stream = self._leased_stream(response.stream, key)
            return response
        class Exhausted(self._http.ConnectError, RoutesExhausted):
            pass
        raise Exhausted('No configured network route connected', request=request) from None

    async def aclose(self):
        if self._closing is None:
            self._closed = True
            self._closing = asyncio.create_task(self._close_transports())
        await _joined_close(self._closing)

    async def _close_transports(self):
        entries, self._transports = list(self._transports.values()), {}
        await _close_entries(entries)


async def _close_entries(entries):
    first_error = None
    for entry in entries:
        try:
            await entry['transport'].aclose()
        except BaseException as error:
            logger.debug('Transport close failed while joining all transports: %s', error)
            first_error = first_error or error
    if first_error is not None:
        raise first_error


async def _joined_close(awaitable):
    """Cancellation returns only after physical transport cleanup completes."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException as error:
                logger.debug('Joined transport close failed after cancellation: %s', error)
                break
        if not task.cancelled():
            task.exception()
        raise


def managed_async_client(*, proxy=None, automatic=None, scope=None, verify=True,
                         limits=None, http_module=httpx, **options):
    """Owned httpx client with route selection, explicit TLS and proxy policy."""
    automatic = proxy is None if automatic is None else automatic
    configured_transport = transport_options(http_module, verify, limits, options)
    transport = RoutingAsyncTransport(
        proxy=proxy, automatic=automatic, scope=scope,
        http_module=http_module, **configured_transport)
    return http_module.AsyncClient(transport=transport, trust_env=False, **options)


def transport_options(http_module, verify, limits, options):
    # Proxy policy is explicit, but the caller's system CA policy still applies.
    trust_env = options.pop('trust_env', True)
    context = http_module.create_ssl_context(
        verify=verify, cert=options.pop('cert', None), trust_env=trust_env)
    result = {'verify': context, 'limits': limits or http_module.Limits(
        max_connections=None)}
    for key in ('http1', 'http2'):
        if key in options:
            result[key] = options.pop(key)
    return result


def transport_proxy(http_module, url, explicit, options):
    if (explicit is not None and isinstance(explicit, http_module.Proxy)
            and url == str(explicit.url)):
        return explicit
    if url and url.lower().startswith('https://'):
        return http_module.Proxy(url, ssl_context=options['verify'])
    return url
