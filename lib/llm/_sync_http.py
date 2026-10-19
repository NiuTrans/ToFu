"""Requests connection-phase and upload policy for model transport pools.

Entry point model_http_adapter creates private HTTP/HTTPS pool classes. TCP/TLS
setup keeps urllib3's connect budget; headers/body use a separate write budget.
urllib3 resets the socket to the caller's read budget afterward. No global
connection-class mutation or change to non-model HTTP clients.
"""

import requests
from urllib.parse import urlsplit
from urllib3.connection import HTTPSConnection
from urllib3.exceptions import ConnectTimeoutError, ReadTimeoutError

from lib.llm.llm_errors import RequestUploadTimeoutError


def model_http_adapter(*, write_timeout, **adapter_options):
    """Create pools with independent upload timeouts, including proxy pools."""
    class UploadBudget:
        def connect(self):
            try:
                return super().connect()
            except (TimeoutError, ReadTimeoutError) as exc:
                # Proxy TCP can be connected while CONNECT/TLS is unfinished.
                # urllib3 otherwise calls this a response-read timeout, losing
                # proof that no model request was sent. Keep that proof at the
                # actual setup boundary; never infer it from error text.
                raise ConnectTimeoutError(
                    'Model connection setup timed out') from exc

        def request(self, method, url, body=None, headers=None, **kwargs):
            if self.sock is None:
                self.connect()
            connect_budget = self.timeout
            self.timeout = write_timeout
            try:
                return super().request(method, url, body=body,
                                       headers=headers, **kwargs)
            except TimeoutError as exc:
                request_bytes = (len(body) if isinstance(body, (bytes, bytearray))
                                 else None)
                scheme = 'https' if isinstance(self, HTTPSConnection) else 'http'
                host = getattr(self, '_tunnel_host', None) or self.host
                port = getattr(self, '_tunnel_port', None) or self.port
                if ':' in host and not host.startswith('['):
                    host = f'[{host}]'
                endpoint = (url if urlsplit(url).scheme in ('http', 'https')
                            else f'{scheme}://{host}:{port}{url}')
                raise RequestUploadTimeoutError(
                    base_url=endpoint,
                    timeout_seconds=write_timeout, request_bytes=request_bytes,
                    cause_type=type(exc).__name__) from exc
            finally:
                self.timeout = connect_budget

    wrapped_pools = {}

    def with_upload_budget(pools):
        result = {}
        for scheme, original_pool in pools.items():
            if issubclass(original_pool.ConnectionCls, UploadBudget):
                result[scheme] = original_pool
                continue
            if original_pool not in wrapped_pools:
                # Preserve the original transport (including SOCKS DNS and
                # handshake policy) instead of replacing it with plain TCP.
                class ModelConnection(UploadBudget, original_pool.ConnectionCls):
                    pass

                class ModelPool(original_pool):
                    ConnectionCls = ModelConnection

                wrapped_pools[original_pool] = ModelPool
            result[scheme] = wrapped_pools[original_pool]
        return result

    class ModelHTTPAdapter(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = with_upload_budget(
                self.poolmanager.pool_classes_by_scheme)

        def proxy_manager_for(self, *args, **kwargs):
            manager = super().proxy_manager_for(*args, **kwargs)
            manager.pool_classes_by_scheme = with_upload_budget(
                manager.pool_classes_by_scheme)
            return manager

    return ModelHTTPAdapter(**adapter_options)
