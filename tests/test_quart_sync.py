"""Unit tests for explicit synchronous Quart boundaries."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from lib import quart_sync


pytestmark = pytest.mark.unit


def test_resolve_plain_value_is_passthrough():
    marker = object()
    assert quart_sync.resolve(marker) is marker


def test_resolve_awaitable_without_running_loop():
    async def value():
        return 42

    assert quart_sync.resolve(value()) == 42


def test_resolve_refuses_event_loop_thread():
    async def value():
        return 42

    async def run():
        with pytest.raises(RuntimeError, match='await Quart directly'):
            quart_sync.resolve(value())

    asyncio.run(run())


def test_timeout_configuration(monkeypatch):
    monkeypatch.setenv('TOFU_SYNC_BODY_TIMEOUT', '12.5')
    assert quart_sync.sync_boundary_timeout() == 12.5
    monkeypatch.setenv('TOFU_SYNC_BODY_TIMEOUT', '0')
    assert quart_sync.sync_boundary_timeout() is None
    monkeypatch.setenv('TOFU_SYNC_BODY_TIMEOUT', 'invalid')
    assert quart_sync.sync_boundary_timeout() == 300.0


def test_send_file_preserves_native_quart_helper(monkeypatch):
    import quart

    original = quart.send_file
    received = {}

    async def fake_send_file(path, *, download_name=None):
        received.update(path=path, download_name=download_name)
        return 'response'

    monkeypatch.setattr(quart, 'send_file', fake_send_file)
    assert quart_sync.send_file('/tmp/example', download_name='example.txt') == 'response'
    assert received == {
        'path': '/tmp/example',
        'download_name': 'example.txt',
    }
    monkeypatch.undo()
    assert quart.send_file is original
    assert inspect.iscoroutinefunction(quart.send_file)
