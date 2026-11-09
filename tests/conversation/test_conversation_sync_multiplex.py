"""Several conversations' ordered replay share one SSE response.

An HTTP/1.x browser holds one connection per long-lived stream and at most six
per origin, so one stream per conversation starves every other request once six
conversations are live. Each multiplexed stream keeps the exact semantics of
its own /events connection, and one stream's failure closes only that stream.
Storage is the only double here: the route and ConversationSyncService are real.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from tests.support.conversation_replay import ScriptedConversationRepository


pytestmark = [pytest.mark.api, pytest.mark.auth_mode("open")]

ALPHA = "conv-mux-alpha"
BETA = "conv-mux-beta"
WINDOW_BYTES = 65_536
ROUTE = "/api/v4/conversation-events"


def _change(conversation_id: str, sequence: int, note_bytes: int = 10) -> dict:
    return {
        "contract": "tofu.conversation-sync.event/v1",
        "type": "conversation.activity",
        "conversationId": conversation_id,
        "syncSeq": sequence,
        "occurredAt": sequence,
        "payload": {"note": "x" * note_bytes},
    }


def _page(head: int, events: list[dict], *, has_more: bool = False) -> dict:
    return {"head": head, "events": events, "resetRequired": False, "hasMore": has_more}


def _backlog(conversation_id: str) -> list[dict]:
    return [_page(3, [_change(conversation_id, sequence, 40_000) for sequence in (1, 2, 3)])]


def _frames(body: bytes) -> list[dict]:
    frames = []
    for block in body.decode("utf-8").split("\n\n"):
        if not block.strip():
            continue
        fields: dict[str, str] = {}
        data: list[str] = []
        for line in block.split("\n"):
            name, value = line.split(": ", 1)
            if name == "data":
                data.append(value)
            else:
                fields[name] = value
        index, document = "\n".join(data).split("\n", 1)
        frames.append({
            "id": fields.get("id"),
            "event": fields["event"],
            "stream": int(index),
            "data": json.loads(document),
        })
    return frames


def _cursor(conversation_id: str, sequence: int) -> str:
    from lib.conversation_sync.cursor import encode_cursor

    return encode_cursor(conversation_id, 1, sequence)


def _closed(conversation_id: str, status: int, code: str) -> dict:
    return {
        "contract": "tofu.conversation-sync.stream-closed/v1",
        "type": "stream.closed",
        "conversationId": conversation_id,
        "status": status,
        "code": code,
    }


@pytest.fixture()
def multiplex(flask_client, monkeypatch):
    from lib.conversation_sync.broker import ConversationWakeBroker
    from lib.conversation_sync.service import ConversationSyncService
    from routes import conversation_sync_v3 as sync_routes

    class FastHeartbeatService(ConversationSyncService):
        heartbeat_interval_ms = 1

    broker = ConversationWakeBroker(owner_history_capacity=16)
    monkeypatch.setattr(sync_routes, "broker", broker)

    def open_streams(scripts: dict, streams, *, query: dict | None = None) -> list[dict]:
        monkeypatch.setattr(
            sync_routes, "_service",
            FastHeartbeatService(ScriptedConversationRepository(scripts, broker)))
        response = flask_client.get(ROUTE, query_string={
            "streamClientId": "page-a",
            "diagnosticPayload": "none",
            "streams": json.dumps(streams),
            **(query or {}),
        })
        assert response.status_code == 200, response.data
        assert response.headers["Content-Type"].startswith("text/event-stream")
        return _frames(response.data)

    open_streams.broker = broker
    return open_streams


def _stream(frames: list[dict], index: int) -> list[dict]:
    return [frame for frame in frames if frame["stream"] == index]


def test_each_stream_replays_its_own_changes_in_order_tagged_by_index(multiplex):
    frames = multiplex(
        {ALPHA: [_page(2, [_change(ALPHA, 1), _change(ALPHA, 2)])],
         BETA: [_page(1, [_change(BETA, 1)])]},
        [{"conversationId": ALPHA, "streamGeneration": 1},
         {"conversationId": BETA, "streamGeneration": 1}],
    )

    for index, conversation_id, sequences in ((0, ALPHA, [1, 2]), (1, BETA, [1])):
        stream = _stream(frames, index)
        assert {frame["data"]["conversationId"] for frame in stream} == {conversation_id}
        changes = [frame for frame in stream if frame["event"] == "conversation.activity"]
        assert [frame["data"]["syncSeq"] for frame in changes] == sequences
        # Ids stay the stream's own cursor, so the client resumes it exactly.
        assert [frame["id"] for frame in changes] == [
            _cursor(conversation_id, sequence) for sequence in sequences]
        assert [frame["event"] for frame in stream[-2:]] == [
            "sync.reset_required", "stream.closed"]
        assert stream[-1]["id"] is None
        assert stream[-1]["data"] == _closed(conversation_id, 204, "ended")


def test_a_stream_that_cannot_open_closes_alone_with_its_own_status(multiplex):
    frames = multiplex(
        {ALPHA: None, BETA: [_page(1, [_change(BETA, 1)])]},
        [{"conversationId": ALPHA, "streamGeneration": 1},
         {"conversationId": BETA, "streamGeneration": 1, "after": "not-a-cursor"},
         {"conversationId": "conv-mux-gamma", "streamGeneration": 1}],
    )

    assert [frame["data"] for frame in _stream(frames, 0)] == [_closed(ALPHA, 404, "not_found")]
    assert [frame["data"] for frame in _stream(frames, 1)] == [
        _closed(BETA, 400, "invalid_cursor")]
    gamma = _stream(frames, 2)
    assert [frame["data"] for frame in gamma] == [
        _closed("conv-mux-gamma", 404, "not_found")]


def test_one_stream_failing_mid_replay_leaves_the_others_streaming(multiplex):
    from lib.storage.errors import StorageError

    frames = multiplex(
        {ALPHA: StorageError("database_internal", "injected page failure"),
         BETA: [_page(2, [_change(BETA, 1), _change(BETA, 2)])]},
        [{"conversationId": ALPHA, "streamGeneration": 1},
         {"conversationId": BETA, "streamGeneration": 1}],
    )

    assert [frame["data"] for frame in _stream(frames, 0)] == [
        _closed(ALPHA, 500, "database_internal")]
    assert [frame["data"].get("syncSeq") for frame in _stream(frames, 1)][:2] == [1, 2]


def test_catch_up_budget_and_snapshot_exemption_apply_per_stream(multiplex):
    frames = multiplex(
        {ALPHA: _backlog(ALPHA), BETA: _backlog(BETA)},
        [{"conversationId": ALPHA, "streamGeneration": 1, "windowBytes": WINDOW_BYTES},
         {"conversationId": BETA, "streamGeneration": 1, "windowBytes": WINDOW_BYTES,
          "after": _cursor(BETA, 0), "cursorSource": "snapshot"}],
    )

    alpha = _stream(frames, 0)
    assert alpha[0]["event"] == "sync.reset_required"
    assert alpha[0]["data"]["reason"] == "replay_exceeds_window"
    assert alpha[0]["id"] == _cursor(ALPHA, 3)
    assert [frame["data"].get("syncSeq") for frame in _stream(frames, 1)][:3] == [1, 2, 3]


def test_a_resumed_stream_replays_only_changes_after_its_cursor(multiplex):
    frames = multiplex(
        {ALPHA: [_page(3, [_change(ALPHA, 3)])]},
        [{"conversationId": ALPHA, "streamGeneration": 1, "after": _cursor(ALPHA, 2),
          "windowBytes": WINDOW_BYTES}],
    )

    changes = [frame for frame in frames if frame["event"] == "conversation.activity"]
    assert [frame["id"] for frame in changes] == [_cursor(ALPHA, 3)]


def test_scripted_cursor_expiry_finishes_the_real_stream(monkeypatch):
    import asyncio

    from lib.conversation_sync.broker import ConversationWakeBroker
    from lib.conversation_sync.service import ConversationSyncService
    from routes import conversation_sync_v3 as routes

    broker = ConversationWakeBroker(owner_history_capacity=16)
    repository = ScriptedConversationRepository(
        {ALPHA: [_page(3, [_change(ALPHA, 3)])]}, broker)
    monkeypatch.setattr(routes, "broker", broker)
    monkeypatch.setattr(routes, "_service", ConversationSyncService(repository))

    async def exercise():
        unrelated = [broker.subscribe(1, BETA), broker.subscribe(2, ALPHA)]
        body = routes._multiplexed_stream_frames(
            0, {"conversationId": ALPHA, "streamGeneration": 1,
                "after": _cursor(ALPHA, 2), "windowBytes": WINDOW_BYTES},
            user_id=1, principal="owner:1", stream_client_id="page-a",
            diagnostic_options={"diagnostic_payload": "none"},
        )
        try:
            frames = _frames("".join([frame async for frame in body]).encode())
            assert broker.snapshot()["active"] == len(unrelated)
            for subscription in unrelated:
                assert not await subscription.wait(0)
        finally:
            await body.aclose()
            for subscription in unrelated:
                subscription.close()
        assert [frame["event"] for frame in frames] == [
            "conversation.activity", "sync.reset_required", "stream.closed"]
        assert frames[0]["id"] == _cursor(ALPHA, 3)
        assert frames[1]["data"]["reason"] == "cursor_expired"
        assert frames[2]["data"] == _closed(ALPHA, 204, "ended")
        assert broker.snapshot()["active"] == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("forward", [
    pytest.param(lambda value: value, id="direct"),
    pytest.param(lambda value: quote(value, safe=""), id="ide-port-proxy"),
])
def test_streams_open_whether_or_not_the_ide_port_proxy_encoded_them_again(
        multiplex, forward):
    """code-server's ``/proxy/<port>/`` forwards
    ``encodeURI(path + '?' + qs.stringify(req.query))``, encoding the page's
    already-encoded query once more; a page served from a VS Code forwarded
    port reaches the app only that way."""
    streams = [
        {"conversationId": ALPHA, "streamGeneration": 1, "after": _cursor(ALPHA, 2),
         "windowBytes": WINDOW_BYTES, "cursorSource": "snapshot"},
        {"conversationId": BETA, "streamGeneration": 1},
    ]

    frames = multiplex(
        {ALPHA: [_page(3, [_change(ALPHA, 3)])], BETA: [_page(1, [_change(BETA, 1)])]},
        streams,
        query={"streams": forward(json.dumps(streams))},
    )

    changes = [[frame for frame in _stream(frames, index)
                if frame["event"] == "conversation.activity"] for index in (0, 1)]
    assert [frame["id"] for frame in changes[0]] == [_cursor(ALPHA, 3)]
    assert [frame["data"]["syncSeq"] for frame in changes[1]] == [1]


def test_an_older_generation_stops_like_its_own_connection(multiplex):
    multiplex({ALPHA: []}, [{"conversationId": ALPHA, "streamGeneration": 2}])

    frames = multiplex({ALPHA: []}, [{"conversationId": ALPHA, "streamGeneration": 1}])

    assert [frame["data"] for frame in frames] == [_closed(ALPHA, 204, "stale_generation")]


def test_a_disconnected_client_releases_every_live_subscription(monkeypatch):
    import asyncio

    from quart import Quart, g

    from lib.api_keys import local_admin_context
    from lib.conversation_sync.broker import ConversationWakeBroker
    from routes import conversation_sync_v3 as routes

    class IdleService:
        """Streams that never end: no changes, heartbeats only."""

        heartbeat_interval_ms = 1

        @staticmethod
        def sequence_from_cursor(*_args):
            return 0

        @staticmethod
        def changes(_conversation_id, _owner, *, after_sequence, limit, **_options):
            return {"head": after_sequence, "events": [], "reset": None, "hasMore": False}

        @staticmethod
        def heartbeat(conversation_id, _owner, sequence, *, degraded, push_withheld=False):
            return {"type": "sync.heartbeat", "conversationId": conversation_id,
                    "cursor": f"cursor-{sequence}", "degraded": degraded,
                    "pushWithheld": push_withheld}

    broker = ConversationWakeBroker(owner_history_capacity=16)
    monkeypatch.setattr(routes, "broker", broker)
    monkeypatch.setattr(routes, "_service", IdleService())
    monkeypatch.setattr(routes, "sse_response", lambda body, **_kwargs: body)
    streams = json.dumps([{"conversationId": ALPHA, "streamGeneration": 1},
                          {"conversationId": BETA, "streamGeneration": 1}])

    async def exercise():
        async with Quart(__name__).test_request_context(
                ROUTE, query_string={"streamClientId": "page-a", "streams": streams}):
            g.auth_ctx = local_admin_context()
            g.auth_ctx.user_id = 7
            g.rate_decision = None
            body = await routes.conversation_sync_multiplexed_events()
            seen: set[int] = set()
            while seen != {0, 1}:
                frame = await asyncio.wait_for(anext(body), 5)
                seen.add(int(frame.split("data: ", 1)[1].split("\n", 1)[0]))
            assert broker.snapshot()["active"] == 2
            await body.aclose()
            assert broker.snapshot()["active"] == 0

    asyncio.run(exercise())


def test_every_subscription_is_released_when_the_response_ends(multiplex):
    multiplex(
        {ALPHA: [_page(1, [_change(ALPHA, 1)])], BETA: None},
        [{"conversationId": ALPHA, "streamGeneration": 1},
         {"conversationId": BETA, "streamGeneration": 1}],
    )

    assert multiplex.broker.snapshot()["active"] == 0


@pytest.mark.parametrize("query", [
    {},
    {"streams": "not json"},
    {"streams": "[]"},
    {"streams": json.dumps([{"conversationId": ALPHA}])},
    {"streams": json.dumps([{"conversationId": ALPHA, "streamGeneration": 0}])},
    {"streams": json.dumps([{"conversationId": ALPHA, "streamGeneration": 1,
                             "windowBytes": 1024}])},
    {"streams": json.dumps([{"conversationId": ALPHA, "streamGeneration": 1,
                             "cursorSource": "retained"}])},
    {"streams": json.dumps([{"conversationId": ALPHA, "streamGeneration": 1,
                             "unknown": True}])},
    {"streams": json.dumps([{"conversationId": ALPHA, "streamGeneration": 1},
                            {"conversationId": ALPHA, "streamGeneration": 2}])},
    {"streams": json.dumps([{"conversationId": ALPHA, "streamGeneration": 1}]),
     "streamClientId": ""},
    {"streams": json.dumps([{"conversationId": ALPHA, "streamGeneration": 1}]),
     "diagnosticPayload": "partial"},
    # Undoing the IDE port proxy's extra encoding grants no other leniency.
    {"streams": "%zz"},
    {"streams": quote("[]", safe="")},
    {"streams": quote(json.dumps([{"conversationId": ALPHA}]), safe="")},
])
def test_malformed_requests_are_rejected_before_any_subscription(flask_client, query):
    base = {"streamClientId": "page-a"}
    response = flask_client.get(ROUTE, query_string={**base, **query})

    assert response.status_code == 400, response.data
