"""Read-side wake hints cannot postpone the canonical stream heartbeat."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('initial_change', [False, True])
@pytest.mark.parametrize('diagnostic_query', ['', '?diagnosticPayload=none'])
def test_quiet_stream_confirms_its_cursor_and_noop_wakes_keep_heartbeat_deadline(
    monkeypatch, initial_change, diagnostic_query,
):
    from quart import Quart, g
    from lib.api_keys import local_admin_context
    from routes import conversation_sync_v3 as routes

    clock = [100.0]
    waits = []

    class Subscription:
        closed = False
        close_reason = ''

        def close(self, reason):
            self.closed = True
            self.close_reason = reason

        def arm_body_start_deadline(self, _deadline):
            pass

        def mark_body_started(self):
            pass

        async def wait(self, timeout):
            waits.append(timeout)
            # A repeated hint can describe a commit the reader already saw.
            # The old implementation restarted its entire wait on every hint.
            assert len(waits) <= 3, 'no-op wakes starved the heartbeat'
            assert timeout > 0, 'the stream must not spin at an expired deadline'
            clock[0] += 5
            return True

    subscription = Subscription()

    class Service:
        heartbeat_interval_ms = 15_000

        @staticmethod
        def sequence_from_cursor(*_args):
            return 0

        @staticmethod
        def cursor_for_sequence(_cid, _owner, sequence):
            return f'cursor-{sequence}'

        @staticmethod
        def changes(cid, _owner, *, after_sequence, **_kwargs):
            assert _kwargs.get('diagnostic_payload', 'full') == ('none' if diagnostic_query else 'full')
            events = ([{'type': 'turn.upsert', 'syncSeq': 1,
                        'conversationId': cid, 'payload': {}}]
                      if initial_change and after_sequence == 0 else [])
            return {'events': events, 'reset': None, 'hasMore': False}

        @staticmethod
        def heartbeat(_cid, _owner, sequence, **_kwargs):
            return {'type': 'sync.heartbeat', 'cursor': f'cursor-{sequence}'}

    async def encode(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(routes, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(routes, '_service', Service())
    monkeypatch.setattr(routes, 'broker', SimpleNamespace(subscribe=lambda *_a, **_k: subscription))
    monkeypatch.setattr(routes, 'sse_response', lambda body, **_kwargs: body)
    monkeypatch.setattr(routes, '_encode_transport', encode)
    monkeypatch.setattr(routes, 'push_withheld_for_conv', lambda _cid: None)
    app = Quart(__name__)

    async def exercise():
        async with app.test_request_context('/api/v4/conversations/conv/events' + diagnostic_query):
            g.auth_ctx = local_admin_context()
            g.auth_ctx.user_id = 7
            g.rate_decision = None
            stream = await routes.conversation_sync_events('conv')
            try:
                first = await anext(stream)
                assert not waits, 'confirm an idle stream without an initial heartbeat wait'
                assert ('event: turn.upsert' if initial_change else 'event: sync.heartbeat') in first
                heartbeat = await anext(stream)
                assert 'event: sync.heartbeat' in heartbeat
                assert f'cursor-{int(initial_change)}' in heartbeat
                assert waits == [15, 10, 5]
                assert clock[0] == 115
            finally:
                await stream.aclose()
            assert subscription.closed

    asyncio.run(exercise())


@pytest.mark.parametrize('query', ['?diagnosticPayload=invalid', '?diagnosticPayload=',
                                 '?diagnosticPayload=none&diagnosticPayload=full'])
def test_invalid_diagnostic_selector_refuses_before_subscription(monkeypatch, query):
    from quart import Quart, g
    from lib.api_keys import local_admin_context
    from routes import conversation_sync_v3 as routes

    def forbidden(*_args, **_kwargs):
        pytest.fail('invalid representation allocated a stream')

    monkeypatch.setattr(routes, 'broker', SimpleNamespace(subscribe=forbidden))
    app = Quart(__name__)

    async def exercise():
        async with app.test_request_context('/api/v4/conversations/conv/events' + query):
            g.auth_ctx = local_admin_context()
            g.rate_decision = None
            response = await routes.conversation_sync_events('conv')
            assert response[1] == 400

    asyncio.run(exercise())
