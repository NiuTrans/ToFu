"""Unit tests for explicit synchronous Quart boundaries."""

from __future__ import annotations

import asyncio
import inspect
import threading
from contextvars import ContextVar

import pytest

from lib import quart_sync


pytestmark = pytest.mark.unit


def test_resolve_plain_value_is_passthrough():
    marker = object()
    assert quart_sync.resolve(marker) is marker


@pytest.mark.parametrize('failure', [None, OSError('command failed')])
def test_owned_command_joins_repeated_cancellation_and_preserves_request_context(failure):
    identity = ContextVar('fixture-owner')
    entered, release = threading.Event(), threading.Event()
    completed = []

    @quart_sync.owned_sync_command
    def command():
        owner = identity.get()
        entered.set()
        if not release.wait(5):
            raise TimeoutError('test did not release command')
        completed.append(owner)
        if failure is not None:
            raise failure
        return owner

    async def exercise():
        identity.set(41)
        task = asyncio.create_task(command())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            identity.set(42)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done() and not completed
        finally:
            release.set()
        if failure is None:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(BaseExceptionGroup) as caught:
                await task
            assert isinstance(caught.value.exceptions[0], asyncio.CancelledError)
            assert caught.value.exceptions[1] is failure
        assert completed == [41]

    asyncio.run(exercise())


@pytest.mark.parametrize('failure', [KeyboardInterrupt('interrupted'), SystemExit('exit')])
def test_owned_command_delivers_control_failure_to_its_awaiting_owner(failure):
    @quart_sync.owned_sync_command
    def command():
        raise failure

    async def exercise():
        with pytest.raises(type(failure)) as caught:
            await command()
        assert caught.value is failure

    asyncio.run(exercise())


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
