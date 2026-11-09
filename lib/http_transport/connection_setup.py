"""Requests connection-setup boundaries shared by generic and model HTTP.

Entry points: ConnectionSetupTimeout and connection_http_adapter. Each adapter
owns its pool classes; TCP, proxy CONNECT and TLS timeouts retain proof that
no application request was sent. Response reads and caller retry policy stay
unchanged. Dependencies: requests and urllib3.
"""
from __future__ import annotations

import ssl
import requests
from urllib3.exceptions import ConnectTimeoutError, ReadTimeoutError, SSLError


class RoutesExhausted:
    """Transport-neutral evidence: every route failed before delivery."""


class NetworkRoutesExhausted(requests.exceptions.ConnectionError, RoutesExhausted):
    """Every admitted path failed before application request delivery."""


class TLSConnectionSetupError(SSLError):
    """TLS failed inside connect(), before HTTP request delivery."""


class ConnectionSetupTimeout:
    """Classify timeouts at the connection boundary, never from error text."""

    def connect(self):
        try:
            return super().connect()
        except (TimeoutError, ReadTimeoutError) as error:
            # Proxy TCP can be connected while its CONNECT/TLS handshake is
            # unfinished. urllib3 would otherwise report a response read.
            raise ConnectTimeoutError('HTTP connection setup timed out') from error
        except (ssl.SSLError, SSLError) as error:
            raise TLSConnectionSetupError('TLS connection setup failed') from error


def connection_http_adapter(connection_policy=ConnectionSetupTimeout, *,
                            adapter_class=requests.adapters.HTTPAdapter, **adapter_options):
    """Wrap an adapter's own transports, retaining SOCKS and upload policies."""
    wrapped_pools = {}

    class ConnectionHTTPAdapter(adapter_class):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = _with_connection_policy(
                self.poolmanager.pool_classes_by_scheme, connection_policy, wrapped_pools)

        def proxy_manager_for(self, *args, **kwargs):
            manager = super().proxy_manager_for(*args, **kwargs)
            manager.pool_classes_by_scheme = _with_connection_policy(
                manager.pool_classes_by_scheme, connection_policy, wrapped_pools)
            return manager

    return ConnectionHTTPAdapter(**adapter_options)


def _with_connection_policy(pools, connection_policy, wrapped_pools):
    result = {}
    for scheme, original_pool in pools.items():
        if issubclass(original_pool.ConnectionCls, connection_policy):
            result[scheme] = original_pool
            continue
        if original_pool not in wrapped_pools:
            class PolicyConnection(connection_policy, original_pool.ConnectionCls):
                pass

            class PolicyPool(original_pool):
                ConnectionCls = PolicyConnection

            wrapped_pools[original_pool] = PolicyPool
        result[scheme] = wrapped_pools[original_pool]
    return result


def prepare_sdk_wire_adapter(adapter, request, proxies):
    """Apply setup evidence to an SDK-owned adapter without changing its TLS policy."""
    selected = requests.utils.select_proxy(request.url, proxies or {})
    manager = adapter.proxy_manager_for(selected) if selected else adapter.poolmanager
    cache = getattr(adapter, '_tofu_connection_pools', None)
    if cache is None:
        cache = adapter._tofu_connection_pools = {}
    manager.pool_classes_by_scheme = _with_connection_policy(
        manager.pool_classes_by_scheme, ConnectionSetupTimeout, cache)
