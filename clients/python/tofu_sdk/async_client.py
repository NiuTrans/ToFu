"""Public asynchronous client facade with explicit transport/resource owners.

AsyncTransport owns HTTPX configuration and cleanup. Domain wrappers own task
submission and replay; generated API contracts remain independent consumers.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Optional
import httpx

from ._version import __version__
from ._protocol import _native_model_payload
from ._async_transport import AsyncTransport
from ._async_resources import _AsyncTasksAPI, _AsyncAgentsAPI


class AsyncTofu(AsyncTransport):
    """Async client backed by an owned or explicitly borrowed HTTPX client."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = '',
        timeout: float = 600.0,
        verify: bool = True,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        max_concurrency: int = 16,
        client: httpx.AsyncClient | None = None,
        user_agent: str = f'tofu-sdk-python/{__version__}',
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, timeout=timeout,
                         verify=verify, max_retries=max_retries,
                         backoff_base=backoff_base, client=client, user_agent=user_agent)
        self._agent_slots = asyncio.Semaphore(max(1, int(max_concurrency)))
        self.tasks = _AsyncTasksAPI(self)
        self.agents = _AsyncAgentsAPI(self)

    async def capabilities(self) -> dict:
        return await self._json(
            'GET', '/api/v1/capabilities', retryable=True)

    async def chat(self, *, messages: list, model: Mapping[str, str],
                   routing: Optional[dict] = None,
                   config: Optional[dict] = None, **extra) -> dict:
        body = {
            'messages': messages,
            'model': _native_model_payload(model),
            **extra,
        }
        if routing:
            body['routing'] = routing
        if config:
            body['config'] = config
        return await self._json(
            'POST', '/api/v1/chat/completions', json_body=body)

    async def __aenter__(self) -> 'AsyncTofu':
        return self

TofuAsync = AsyncTofu

__all__ = ['AsyncTofu', 'TofuAsync']
