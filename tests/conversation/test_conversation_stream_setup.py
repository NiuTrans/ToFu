"""Every pre-body exit releases the exact conversation stream resources."""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit


def test_conversation_observers_survive_saturated_generation_stream_capacity(monkeypatch):
    """64 independent observers stay live; disconnect retires every owner."""
    from quart import Quart, g
    from lib.api_keys import local_admin_context
    from lib.agent_core.sse_limit import limiter
    from lib.conversation_sync.broker import ConversationWakeBroker
    from routes import conversation_sync_v3 as routes

    calls = []
    def saturated(principal):
        calls.append(principal)
        return None

    class Service:
        @staticmethod
        def sequence_from_cursor(*_args):
            return 0

        @staticmethod
        def cursor_for_sequence(*_args):
            return 'cursor-1'

        @staticmethod
        def changes(cid, _owner, **_kwargs):
            return {'events': [{'type': 'turn.upsert', 'syncSeq': 1,
                                'conversationId': cid, 'payload': {}}],
                    'reset': None, 'hasMore': False}

    broker = ConversationWakeBroker(owner_history_capacity=16)
    monkeypatch.setattr(routes, 'broker', broker)
    monkeypatch.setattr(routes, '_service', Service())
    monkeypatch.setattr(routes, 'sse_response', lambda body, **_kwargs: body)
    monkeypatch.setattr(limiter, 'try_acquire', saturated)
    app = Quart(__name__)

    async def exercise():
        streams = []
        try:
            for index in range(64):
                cid = f'conv-{index}'
                async with app.test_request_context(
                    f'/api/v4/conversations/{cid}/events?streamClientId=page&streamGeneration=1',
                ):
                    g.auth_ctx = local_admin_context()
                    g.auth_ctx.user_id = 7
                    g.rate_decision = None
                    stream = await routes.conversation_sync_events(cid)
                    assert hasattr(stream, '__anext__'), 'observer refused behind generation quota'
                    streams.append(stream)
                    assert 'event: turn.upsert' in await anext(stream)
            assert broker.snapshot()['active'] == 64
            assert calls == []
        finally:
            for stream in streams:
                await stream.aclose()
        assert broker.snapshot()['active'] == broker.snapshot()['owned'] == 0
        assert broker.snapshot()['ownerHistory'] <= 16

    asyncio.run(exercise())


@pytest.mark.parametrize('phase', ['observation', 'probe', 'response', 'body'])
@pytest.mark.parametrize('failure_type', [RuntimeError, asyncio.CancelledError])
def test_setup_failure_releases_subscription(monkeypatch, phase, failure_type):
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

    broker = ConversationWakeBroker(owner_history_capacity=16)
    monkeypatch.setattr(routes, 'broker', broker)
    monkeypatch.setattr(routes, '_service', Service())
    if phase == 'observation':
        monkeypatch.setattr(routes, 'record_stream_admission', fail)
    if phase == 'response':
        monkeypatch.setattr(routes, 'sse_response', fail)
    if phase == 'body':
        monkeypatch.setattr(routes, 'sse_response', lambda body, **_kwargs: body)
        monkeypatch.setattr('lib.conversation_sync.broker.ConversationWakeSubscription.mark_body_started', fail)

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

    broker = ConversationWakeBroker(owner_history_capacity=16)
    monkeypatch.setattr(routes, 'broker', broker)
    monkeypatch.setattr(routes, '_service', Service())
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
            finally:
                release_read.set()
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    asyncio.run(exercise())  # Joins the read worker before returning.
