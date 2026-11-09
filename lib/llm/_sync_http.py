"""Requests connection-phase and upload policy for model transport pools.

Entry point model_http_adapter creates private HTTP/HTTPS pool classes. TCP/TLS
setup keeps urllib3's connect budget; headers/body use a separate write budget.
urllib3 resets the socket to the caller's read budget afterward. No global
connection-class mutation or change to non-model HTTP clients.
"""

from urllib.parse import urlsplit
from urllib3.connection import HTTPSConnection

from lib.http_transport.connection_setup import ConnectionSetupTimeout, connection_http_adapter

from lib.llm.llm_errors import RequestUploadTimeoutError


def model_http_adapter(*, write_timeout, **adapter_options):
    """Create pools with independent upload timeouts, including proxy pools."""
    class UploadBudget(ConnectionSetupTimeout):
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

    from lib.http_transport.outbound import RoutingHTTPAdapter
    return connection_http_adapter(
        UploadBudget, adapter_class=RoutingHTTPAdapter, **adapter_options)
