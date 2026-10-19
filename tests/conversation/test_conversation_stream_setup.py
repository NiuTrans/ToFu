"""Every pre-body exit releases the exact conversation stream resources."""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('phase', ['accounting', 'observation', 'probe', 'response', 'body'])
@pytest.mark.parametrize('failure_type', [RuntimeError, asyncio.CancelledError])
def test_setup_failure_releases_subscription_and_accounting(monkeypatch, phase, failure_type):
    from quart import Quart, g
    from lib.api_keys import local_admin_context
    from lib.conversation_sync.broker import ConversationWakeBroker
    from routes import conversation_sync_v3 as routes

    def fail(*_args, **_kwargs):
        raise failure_type('injected setup failure')

    class Service:
        @staticmethod
        def sequence_from_cursor(*_args):
            return 0

        @staticmethod
        def changes(*_args, **_kwargs):
            if phase == 'probe':
                fail()
            return {'events': [], 'reset': None, 'hasMore': False}

    released = []

    class Accounting:
        @property
        def refresh_interval_seconds(self):
            if phase == 'body':
                fail()
            return 60

        @staticmethod
        def try_acquire(_principal):
            if phase == 'accounting':
                fail()
            return 'exact-token'

        @staticmethod
        def release(token):
            released.append(token)

    broker = ConversationWakeBroker(owner_history_capacity=16)
    monkeypatch.setattr(routes, 'broker', broker)
    monkeypatch.setattr(routes, '_service', Service())
    monkeypatch.setattr(routes, 'sse_limiter', Accounting())
    if phase == 'observation':
        monkeypatch.setattr(routes, 'record_stream_admission', fail)
    if phase == 'response':
        monkeypatch.setattr(routes, 'sse_response', fail)
    if phase == 'body':
        monkeypatch.setattr(routes, 'sse_response', lambda body, **_kwargs: body)

    app = Quart(__name__)

    async def exercise():
        async with app.test_request_context('/api/v4/conversations/conv/events'):
            g.auth_ctx = local_admin_context()
            g.auth_ctx.user_id = 7
            g.rate_decision = None
            with pytest.raises(failure_type, match='injected setup failure'):
                response = await routes.conversation_sync_events('conv')
                await anext(response)
            assert broker.snapshot()['active'] == 0
            assert released == ([] if phase == 'accounting' else ['exact-token'])

    asyncio.run(exercise())


def test_request_cancellation_during_ownership_probe_releases_resources(monkeypatch):
    import threading
    from quart import Quart, g
    from lib.api_keys import local_admin_context
    from lib.conversation_sync.broker import ConversationWakeBroker
    from routes import conversation_sync_v3 as routes

    entered = threading.Event()
    release_read = threading.Event()

    class Service:
        @staticmethod
        def sequence_from_cursor(*_args):
            return 0

        @staticmethod
        def changes(*_args, **_kwargs):
            entered.set()
            assert release_read.wait(5), 'test must release its read worker'
            return {'events': [], 'reset': None, 'hasMore': False}

    released = []

    class Accounting:
        @staticmethod
        def try_acquire(_principal):
            return 'cancelled-token'

        @staticmethod
        def release(token):
            released.append(token)

    broker = ConversationWakeBroker(owner_history_capacity=16)
    monkeypatch.setattr(routes, 'broker', broker)
    monkeypatch.setattr(routes, '_service', Service())
    monkeypatch.setattr(routes, 'sse_limiter', Accounting())
    app = Quart(__name__)

    async def exercise():
        async with app.test_request_context('/api/v4/conversations/conv/events'):
            g.auth_ctx = local_admin_context()
            g.auth_ctx.user_id = 7
            g.rate_decision = None
            request = asyncio.create_task(routes.conversation_sync_events('conv'))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert broker.snapshot()['active'] == 0
                assert released == ['cancelled-token']
            finally:
                release_read.set()
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    asyncio.run(exercise())  # Joins the read worker before returning.
