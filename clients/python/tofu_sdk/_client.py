"""Synchronous public client facade: protocol operations and domain APIs.

SyncTransport owns HTTP/session lifetime; task/account/agent modules own their
endpoint workflows. This facade retains the public constructor and methods.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Iterator, Optional

from ._version import __version__
from ._protocol import TofuError, _native_model_payload, error_body
from ._sync_transport import SyncTransport
from ._streaming import _parse_sse
from ._tasks import _TasksAPI
from ._agents import _AgentsAPI
from ._accounts import _KeysAPI, _WebhooksAPI
from .api_v4_generated import ApiMetaResponse, require_desktop_api_compatibility


class Tofu(SyncTransport):
    """Synchronous Tofu API client with an owned reusable connection pool."""

    def __init__(self, *, base_url: str, api_key: str = '',
                 timeout: float = 600.0, verify: bool = True,
                 max_retries: int = 3, backoff_base: float = 0.5,
                 user_agent: str = f'tofu-sdk-python/{__version__}'):
        super().__init__(base_url=base_url, api_key=api_key, timeout=timeout,
                         verify=verify, max_retries=max_retries,
                         backoff_base=backoff_base, user_agent=user_agent)
        self.tasks = _TasksAPI(self)
        self.agents = _AgentsAPI(self)
        self.keys = _KeysAPI(self)
        self.webhooks = _WebhooksAPI(self)

    def __enter__(self) -> 'Tofu':
        return self

    def capabilities(self) -> dict:
        """``GET /api/v1/capabilities`` — runtime registry."""
        return self._json('GET', '/api/v1/capabilities')

    def api_meta(self) -> ApiMetaResponse:
        """``GET /api/v4/meta`` — reject an incompatible SDK build."""
        return require_desktop_api_compatibility(
            self._json('GET', '/api/v4/meta'),
            __version__,
        )

    def chat(self, *, messages: list, model: Mapping[str, str],
              routing: Optional[dict] = None,
              config: Optional[dict] = None, **kwargs) -> dict:
        """Sync chat completion via ``POST /api/v1/chat/completions``."""
        body = {'messages': messages, 'model': _native_model_payload(model)}
        if routing:
            body['routing'] = routing
        if config:
            body['config'] = config
        for k in ('temperature', 'max_tokens', 'top_p', 'stop',
                   'tools', 'tool_choice', 'response_format',
                   'idempotency_key', 'conversation_id', 'timeout_s'):
            if k in kwargs:
                body[k] = kwargs[k]
        return self._json('POST', '/api/v1/chat/completions', json_body=body)

    def stream(self, *, messages: list, model: Mapping[str, str],
                routing: Optional[dict] = None,
                config: Optional[dict] = None, **kwargs
                ) -> Iterator[dict]:
        """Streaming chat completion. Yields parsed SSE event payloads."""
        body = {
            'messages': messages,
            'model': _native_model_payload(model),
            'stream': True,
        }
        if routing:
            body['routing'] = routing
        if config:
            body['config'] = config
        for k in ('temperature', 'max_tokens', 'top_p', 'stop',
                   'tools', 'tool_choice', 'idempotency_key',
                   'conversation_id'):
            if k in kwargs:
                body[k] = kwargs[k]
        resp = self._request('POST', '/api/v1/chat/completions',
                              json_body=body, stream=True,
                              headers={'Accept': 'text/event-stream'})
        try:
            if not (200 <= resp.status_code < 300):
                raise TofuError(resp.status_code, error_body(resp))
            yield from _parse_sse(resp)
        finally:
            resp.close()
