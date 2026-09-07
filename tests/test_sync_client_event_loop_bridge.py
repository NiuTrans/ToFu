"""Regression for the sync Quart client inside a thread with a live loop."""

import asyncio

import pytest

pytestmark = pytest.mark.unit


def test_run_coro_uses_helper_thread_when_loop_is_already_running():
    from tests.conftest import _run_coro

    async def outer():
        return _run_coro(asyncio.sleep(0, result='ok'))

    assert asyncio.run(outer()) == 'ok'
