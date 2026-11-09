"""Public host transport ports, after request preparation and URL validation.

Standalone sessions retain Requests behavior. A host can supply route policy
without replacing parser, cookie, redirect, TLS or response-budget owners.
"""
from contextvars import ContextVar

import requests

from tofu_search.config import get_config

_explicit_proxy = ContextVar('search_explicit_proxy', default=False)


class NetworkAdapter(requests.adapters.HTTPAdapter):
    def send(self, request, **options):
        transport = get_config().requests_transport
        if transport is None:
            return super().send(request, **options)
        return transport(self, super().send, request,
                         explicit_proxy=_explicit_proxy.get(), **options)


class NetworkSession(requests.Session):
    def __init__(self):
        super().__init__()
        adapter = NetworkAdapter()
        self.mount('http://', adapter)
        self.mount('https://', adapter)

    def request(self, method, url, **options):
        token = _explicit_proxy.set(options.get('proxies') is not None or bool(self.proxies))
        try:
            return super().request(method, url, **options)
        finally:
            _explicit_proxy.reset(token)


def request(method, url, **options):
    with NetworkSession() as session:
        return session.request(method, url, **options)
