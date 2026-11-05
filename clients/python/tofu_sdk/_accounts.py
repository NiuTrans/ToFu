"""Synchronous key and webhook APIs over the existing client transport.

Dependencies: the public client transport and shared protocol vocabulary.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ._client import Tofu


class _KeysAPI:
    def __init__(self, client: Tofu):
        self._c = client

    def whoami(self) -> dict:
        return self._c._json('GET', '/api/v1/keys/whoami')

    def list(self) -> dict:
        return self._c._json('GET', '/api/v1/keys')

    def create(self, *, name: str, scopes: list, **kwargs) -> dict:
        body = {'name': name, 'scopes': scopes, **kwargs}
        return self._c._json('POST', '/api/v1/keys', json_body=body)

    def revoke(self, key_id: str) -> dict:
        return self._c._json('DELETE', f'/api/v1/keys/{key_id}')


class _WebhooksAPI:
    def __init__(self, client: Tofu):
        self._c = client

    def list(self) -> dict:
        return self._c._json('GET', '/api/v1/webhooks')

    def subscribe(self, *, url: str, channel: str = '',
                   event_types: Optional[list] = None,
                   task_id: str = '*') -> dict:
        body = {'url': url, 'channel': channel,
                'event_types': event_types or [], 'task_id': task_id}
        return self._c._json('POST', '/api/v1/webhooks', json_body=body)

    def unsubscribe(self, sub_id: str) -> dict:
        return self._c._json('DELETE', f'/api/v1/webhooks/{sub_id}')
