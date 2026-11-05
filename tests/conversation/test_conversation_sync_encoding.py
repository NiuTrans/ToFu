"""Conversation transport encoding preserves loop progress and owned teardown."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import threading

import pytest

pytestmark = pytest.mark.unit


def _stream_fixture(monkeypatch):
    from quart import Quart
    from lib.conversation_sync.broker import ConversationWakeBroker
    from routes import conversation_sync_v3 as routes

    events = [
        {"contract": "tofu.conversation-sync.event/v1", "type": "turn.upsert",
         "conversationId": "encode-test", "syncSeq": sequence,
         "occurredAt": sequence, "payload": {"content": "完整证据 🧪\n" * 100}}
        for sequence in (1, 2)
    ]
    reset = {"contract": "tofu.conversation-sync.event/v1",
             "type": "sync.reset_required", "conversationId": "encode-test",
             "cursor": "reset-2", "reason": "test_complete"}

    class Service:
        heartbeat_interval_ms = 1

        @staticmethod
        def sequence_from_cursor(*_args):
            return 0

        @staticmethod
        def cursor_for_sequence(_cid, _owner, sequence):
            return f"cursor-{sequence}"

        @staticmethod
        def changes(_cid, _owner, *, after_sequence, limit):
            if limit == 1:
                return {"events": [], "reset": None, "hasMore": False}
            if after_sequence == 0:
                return {"events": events, "reset": None, "hasMore": True}
            return {"events": [], "reset": reset, "hasMore": False}

    broker = ConversationWakeBroker(owner_history_capacity=16)
    monkeypatch.setattr(routes, "broker", broker)
    monkeypatch.setattr(routes, "_service", Service())
    monkeypatch.setattr(routes, "sse_response", lambda body, **_kwargs: body)
    return Quart(__name__), routes, broker, events, reset


def _authorize():
    from quart import g
    from lib.api_keys import local_admin_context

    g.auth_ctx = local_admin_context()
    g.auth_ctx.user_id = 7
    g.rate_decision = None


def test_demanded_sse_encoding_is_off_loop_and_stays_ordered(monkeypatch):
    app, routes, broker, events, reset = _stream_fixture(monkeypatch)
    original = routes._sse_frame
    entered, release = threading.Event(), threading.Event()
    encoded = []
    loop_thread = threading.get_ident()

    def blocked_encoder(event, **kwargs):
        entered.set()
        assert threading.get_ident() != loop_thread, "SSE encoding ran on the serving loop"
        encoded.append(event)
        if len(encoded) == 1:
            entered.set()
            assert release.wait(5), "test must release encoding worker"
        return original(event, **kwargs)

    monkeypatch.setattr(routes, "_sse_frame", blocked_encoder)

    async def exercise():
        async with app.test_request_context("/api/v4/conversations/encode-test/events"):
            _authorize()
            stream = await routes.conversation_sync_events("encode-test")
            first = asyncio.create_task(anext(stream))
            try:
                await asyncio.sleep(0)
                if first.done():
                    await first  # Surface the baseline's intended loop assertion.
                assert await asyncio.to_thread(entered.wait, 2)
                if first.done():
                    await first
                assert broker.snapshot()["active"] == 1
                assert encoded == events[:1], "backpressure must not encode later frames eagerly"
                progressed = asyncio.Event()
                asyncio.get_running_loop().call_soon(progressed.set)
                await asyncio.wait_for(progressed.wait(), 1)
                assert not first.done()
                release.set()
                frames = [await first, await anext(stream), await anext(stream)]
                assert [json.loads(frame.split("data: ", 1)[1]) for frame in frames] == [*events, reset]
                assert frames[0].startswith("id: cursor-1\nevent: turn.upsert\n")
                assert frames[1].startswith("id: cursor-2\nevent: turn.upsert\n")
                with pytest.raises(StopAsyncIteration):
                    await anext(stream)
            finally:
                release.set()
                await asyncio.gather(first, return_exceptions=True)
                await stream.aclose()
            assert broker.snapshot()["active"] == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("encoder_fails", [False, True])
def test_cancelled_sse_joins_the_exact_encoder_before_releasing_subscription(monkeypatch, encoder_fails):
    app, routes, broker, _events, _reset = _stream_fixture(monkeypatch)
    original = routes._sse_frame
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def blocked_encoder(event, **kwargs):
        entered.set()
        assert threading.get_ident() != loop_thread, "SSE encoding ran on the serving loop"
        try:
            assert release.wait(5), "test must release encoding worker"
            if encoder_fails:
                raise RuntimeError("injected encoder failure")
            return original(event, **kwargs)
        finally:
            exited.set()

    monkeypatch.setattr(routes, "_sse_frame", blocked_encoder)

    async def exercise():
        async with app.test_request_context("/api/v4/conversations/encode-test/events"):
            _authorize()
            stream = await routes.conversation_sync_events("encode-test")
            first = asyncio.create_task(anext(stream))
            try:
                await asyncio.sleep(0)
                if first.done():
                    await first
                assert await asyncio.to_thread(entered.wait, 2)
                if first.done():
                    await first
                first.cancel()
                await asyncio.sleep(0)
                first.cancel()
                await asyncio.sleep(0)
                assert not first.done(), "cancellation must retain physical encoding ownership"
                assert not exited.is_set()
                release.set()
                if encoder_fails:
                    with pytest.raises(BaseExceptionGroup) as caught:
                        await first
                    assert [type(error) for error in caught.value.exceptions] == [
                        asyncio.CancelledError, RuntimeError,
                    ]
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await first
                assert exited.is_set()
                assert broker.snapshot()["active"] == 0
            finally:
                release.set()
                await asyncio.gather(first, return_exceptions=True)
                await stream.aclose()

    asyncio.run(exercise())


def test_snapshot_response_encoding_is_off_loop(monkeypatch):
    from quart import Quart
    from routes import conversation_sync_v3 as routes

    original = routes.api_prevalidated_payload
    loop_thread = threading.get_ident()
    payload = {"ok": True, "content": "Synthetic snapshot 🧪\n" * 50000}

    class Service:
        @staticmethod
        def snapshot(*_args, **_kwargs):
            return payload

    def encode(value):
        assert threading.get_ident() != loop_thread, "snapshot encoding ran on the serving loop"
        return original(value)

    monkeypatch.setattr(routes, "_service", Service())
    monkeypatch.setattr(routes, "push_withheld_for_conv", lambda _cid: None)
    monkeypatch.setattr(routes, "api_prevalidated_payload", encode)
    monkeypatch.setattr("lib.translate.terminal.schedule_translation_recovery", lambda *_args, **_kwargs: None)
    app = Quart(__name__)

    async def exercise():
        async with app.test_request_context("/api/v4/conversations/encode-test/sync"):
            _authorize()
            response, status = await routes.conversation_sync_snapshot("encode-test")
            assert status == 200
            assert await response.get_json() == payload
            assert "snapshot;dur=" in response.headers["Server-Timing"]
            assert "encode;dur=" in response.headers["Server-Timing"]

    asyncio.run(exercise())


@pytest.mark.parametrize("value", [
    {"text": "中文 🧪\n\r\u0000", "number": 1.25, "flag": True, "nil": None},
    {"integer": 2**80, "negative": -(2**80)},
])
def test_sse_json_preserves_values_without_mutating_authority(value):
    from routes.conversation_sync_v3 import _sse_frame

    event = {"type": "turn.upsert", "payload": value}
    before = deepcopy(event)
    frame = _sse_frame(event, cursor="opaque-cursor")
    assert frame.startswith("id: opaque-cursor\nevent: turn.upsert\ndata: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame.split("data: ", 1)[1]) == before
    assert event == before


@pytest.mark.parametrize("encoder_fails", [False, True])
def test_snapshot_cancellation_retains_admission_until_encoder_exit(monkeypatch, encoder_fails):
    from quart import Quart
    from lib.conversation_sync.snapshot_admission import ConversationSnapshotAdmission
    from routes import conversation_sync_v3 as routes

    gate = ConversationSnapshotAdmission(max_active=4)
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    original = routes.api_prevalidated_payload
    payload = {"ok": True, "content": "exact snapshot 🧪"}

    class Service:
        @staticmethod
        def snapshot(*_args, **_kwargs):
            return payload

    def blocked_encoder(value):
        assert value is payload
        entered.set()
        try:
            assert release.wait(5), "test must release snapshot encoder"
            if encoder_fails:
                raise RuntimeError("injected snapshot encoder failure")
            return original(value)
        finally:
            exited.set()

    monkeypatch.setattr(routes, "_service", Service())
    monkeypatch.setattr(routes, "snapshot_admission", gate)
    monkeypatch.setattr(routes, "push_withheld_for_conv", lambda _cid: None)
    monkeypatch.setattr(routes, "api_prevalidated_payload", blocked_encoder)
    app = Quart(__name__)
    app.register_blueprint(routes.conversation_sync_v3_bp)

    async def exercise():
        async with app.test_request_context(
            "/api/v4/conversations/encode-test/sync", headers={"X-Request-ID": "abc123-1"},
        ):
            _authorize()
            pending = asyncio.create_task(routes.conversation_sync_snapshot("encode-test"))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                pending.cancel()
                await asyncio.sleep(0)
                pending.cancel()
                await asyncio.sleep(0)
                assert not pending.done()
                assert not exited.is_set()
                assert gate.active_count() == 1
                response, status = await routes.conversation_sync_snapshot("encode-test")
                assert status == 429
                assert (await response.get_json())["error"] == "snapshot_in_flight"
                assert gate.active_count() == 1
                release.set()
                if encoder_fails:
                    with pytest.raises(BaseExceptionGroup) as caught:
                        await pending
                    assert [type(error) for error in caught.value.exceptions] == [
                        asyncio.CancelledError, RuntimeError,
                    ]
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                assert exited.is_set()
                assert gate.active_count() == 0
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_sse_nonfinite_numbers_retain_existing_json_behavior(number):
    from routes.conversation_sync_v3 import _sse_frame

    event = {"type": "turn.upsert", "payload": {"values": [{"number": number}]}}
    expected = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    assert _sse_frame(event) == f"event: turn.upsert\ndata: {expected}\n\n"
    assert json.dumps(event, ensure_ascii=False, separators=(",", ":")) == expected


@pytest.mark.parametrize("supersede_during", ["encode", "after_frame"])
def test_superseded_stream_joins_encoder_without_publishing_or_encoding_more(monkeypatch, supersede_during):
    app, routes, broker, events, _reset = _stream_fixture(monkeypatch)
    original = routes._sse_frame
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    encoded = []

    def tracked_encoder(event, **kwargs):
        encoded.append(event)
        entered.set()
        try:
            if supersede_during == "encode":
                assert release.wait(5), "test must release superseded encoder"
            return original(event, **kwargs)
        finally:
            exited.set()

    monkeypatch.setattr(routes, "_sse_frame", tracked_encoder)

    async def exercise():
        async with app.test_request_context(
            "/api/v4/conversations/encode-test/events?streamClientId=page1&streamGeneration=1",
        ):
            _authorize()
            stream = await routes.conversation_sync_events("encode-test")
            pending = asyncio.create_task(anext(stream))
            replacement = None
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                if supersede_during == "after_frame":
                    assert "id: cursor-1" in await pending
                replacement = broker.subscribe(
                    7, "encode-test", principal_key=routes.principal_key(routes.current_auth()),
                    stream_client_id="page1", stream_generation=2,
                )
                assert not replacement.closed
                assert broker.snapshot()["active"] == 1
                if supersede_during == "encode":
                    await asyncio.sleep(0)
                    assert not pending.done(), "supersession must join the admitted encoder"
                    assert not exited.is_set()
                    release.set()
                    with pytest.raises(StopAsyncIteration):
                        await pending
                else:
                    with pytest.raises(StopAsyncIteration):
                        await anext(stream)
                assert exited.is_set()
                assert encoded == events[:1], "retired stream must not encode its remaining page"
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
                await stream.aclose()
                if replacement is not None:
                    replacement.close()
            assert broker.snapshot()["active"] == 0

    asyncio.run(exercise())
