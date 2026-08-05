"""Tests for the dual-mode ``_db_safe`` decorator (routes/common.py).

``_db_safe`` wraps conversation CRUD handlers. For the native-async migration
it MUST be dual-mode: wrapping an ``async def`` handler in a SYNC passthrough
makes ``asyncio.iscoroutinefunction(wrapper)`` False, so Quart runs it in the
thread pool and serializes the returned coroutine OBJECT as the response
(broken / never-awaited). This pins the fix.

Run:  pytest tests/test_db_safe_dual_mode.py -m unit
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.unit
class TestDbSafeDualMode:
    def test_async_handler_stays_coroutine(self):
        from routes.common import _db_safe

        async def handler():
            return 'ok'

        wrapped = _db_safe(handler)
        assert asyncio.iscoroutinefunction(wrapped), (
            '_db_safe must emit an async wrapper for async handlers, else '
            'Quart leaks the coroutine object as the response')

    def test_sync_handler_stays_sync(self):
        from routes.common import _db_safe

        def handler():
            return 'ok'

        wrapped = _db_safe(handler)
        assert not asyncio.iscoroutinefunction(wrapped)

    def test_async_handler_result_is_awaitable_and_correct(self):
        from routes.common import _db_safe

        async def handler():
            return 42

        wrapped = _db_safe(handler)
        result = asyncio.new_event_loop().run_until_complete(wrapped())
        assert result == 42

    def test_sync_handler_passthrough(self):
        from routes.common import _db_safe

        def handler(x):
            return x * 2

        assert _db_safe(handler)(21) == 42

    def test_wraps_preserves_name(self):
        from routes.common import _db_safe

        async def my_async_view():
            return 'x'

        def my_sync_view():
            return 'y'

        assert _db_safe(my_async_view).__name__ == 'my_async_view'
        assert _db_safe(my_sync_view).__name__ == 'my_sync_view'

    def test_db_locked_returns_503_envelope(self):
        """The 503 'database_busy' path must return the envelope — with
        ``api_error`` unimported (migration-era missing import, epic
        pt_551fc875f3034f38) this NameError'd into a 500."""
        import sqlite3

        from quart import Quart

        from routes.common import _db_safe

        def handler():
            raise sqlite3.OperationalError('database is locked')

        app = Quart(__name__)

        async def go():
            async with app.test_request_context('/api/v1/conversations',
                                                method='PUT'):
                resp, status = _db_safe(handler)()
                return status, await resp.get_json()

        status, body = asyncio.new_event_loop().run_until_complete(go())
        assert status == 503
        assert body['ok'] is False
        assert body['error'] == 'database_busy'
