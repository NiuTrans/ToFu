"""Async Stop owns/joins HTTP setup even before a provider sends headers."""
from __future__ import annotations

import asyncio
import httpx
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.llm import astream, _transport
from lib.llm.llm_errors import AbortedError


pytestmark = pytest.mark.api

@pytest.mark.parametrize('stop_kind', ['abort_check', 'task_cancel'])
def test_stop_before_headers_closes_socket_and_dumper_once(monkeypatch, stop_kind):
    async def run():
        received = asyncio.Event()
        disconnected = asyncio.Event()
        stop = asyncio.Event()
        requests = []
        handlers = set()

        async def peer(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                headers = await reader.readuntil(b'\r\n\r\n')
                length = next(int(line.split(b':', 1)[1]) for line in headers.splitlines()
                              if line.lower().startswith(b'content-length:'))
                requests.append(await reader.readexactly(length))
                received.set()
                # A provider/proxy can accept the entire prompt and never send
                # response headers. It must not own Stop's latency budget.
                assert await reader.read() == b''
                disconnected.set()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        server = await asyncio.start_server(peer, '127.0.0.1', 0)
        dumper = SimpleNamespace(enabled=True, _fh=object(), finish=Mock())
        client = httpx.AsyncClient(trust_env=False, timeout=None)
        monkeypatch.setattr(astream, 'get_async_client', lambda proxy: client)
        monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **kw: 'direct')
        monkeypatch.setattr('lib.proxy.subscription_route_candidates', lambda url: [])
        monkeypatch.setattr(astream, '_resolve_async_route', lambda url: (None, {}))
        monkeypatch.setattr(_transport, 'ABORT_POLL_INTERVAL', 0.01)
        port = server.sockets[0].getsockname()[1]
        plan = SimpleNamespace(
            hdrs={}, url=f'http://127.0.0.1:{port}/v1/chat/completions',
            body={'model': 'test', 'messages': []},
            raw_archive_capture=None, raw_dumper=dumper)
        monkeypatch.setattr(astream, 'prepare_request', lambda *a, **kw: plan)

        task = asyncio.create_task(astream._async_stream_chat_once(
            {'model': 'test', 'messages': []}, abort_check=stop.is_set))
        try:
            await asyncio.wait_for(received.wait(), 2)
            if stop_kind == 'task_cancel':
                task.cancel()
            else:
                stop.set()
            expected = asyncio.CancelledError if stop_kind == 'task_cancel' else AbortedError
            with pytest.raises(expected):
                await asyncio.wait_for(task, 0.5)
            await asyncio.wait_for(disconnected.wait(), 0.5)
            assert len(requests) == 1  # Stop must not retry a possibly accepted prompt.
            dumper.finish.assert_called_once_with(error=True)
            if handlers:
                await asyncio.wait_for(asyncio.gather(*handlers), 0.5)
            assert not handlers
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            server.close()
            await server.wait_closed()
            if handlers:
                await asyncio.gather(*handlers, return_exceptions=True)
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize('scenario', ['success', 'pre_stopped', 'headers_race', 'open_error', 'body_error'])
def test_header_open_context_ownership(scenario):
    from contextlib import asynccontextmanager

    async def run():
        stop = scenario == 'pre_stopped'
        events = []

        @asynccontextmanager
        async def connection():
            nonlocal stop
            events.append('opening')
            if scenario == 'open_error':
                raise OSError('injected connect failure')
            try:
                if scenario == 'headers_race':
                    stop = True
                yield 'response'
            except ValueError:
                events.append('body_error')
                raise
            finally:
                events.append('closed')

        async def consume():
            async with astream._abortable_stream_open(connection(), lambda: stop) as result:
                events.append('consuming')
                assert result == 'response'
                if scenario == 'body_error':
                    raise ValueError('injected parser failure')

        expected_error = {'pre_stopped': AbortedError, 'headers_race': AbortedError,
                          'open_error': OSError, 'body_error': ValueError}.get(scenario)
        if expected_error:
            with pytest.raises(expected_error):
                await consume()
        else:
            await consume()
        assert events == {
            'success': ['opening', 'consuming', 'closed'],
            'pre_stopped': [],
            'headers_race': ['opening', 'closed'],
            'open_error': ['opening'],
            'body_error': ['opening', 'consuming', 'body_error', 'closed'],
        }[scenario]

    asyncio.run(run())
