"""Catch-up replay is bounded by the caller's snapshot window.

A browser that reconnects after a long absence should re-anchor with one
snapshot instead of replaying every superseded intermediate change. Storage is
the only double here: the route and ConversationSyncService are real.
"""

from __future__ import annotations

import json

import pytest

from tests.support.conversation_replay import ScriptedConversationRepository


pytestmark = [pytest.mark.api, pytest.mark.auth_mode("open")]

CONVERSATION = "conv-catch-up"
WINDOW_BYTES = 65_536


def _change(sequence: int, note_bytes: int) -> dict:
    return {
        "contract": "tofu.conversation-sync.event/v1",
        "type": "conversation.activity",
        "conversationId": CONVERSATION,
        "syncSeq": sequence,
        "occurredAt": sequence,
        "payload": {"note": "x" * note_bytes},
    }


def _page(head: int, events: list[dict], *, has_more: bool = False) -> dict:
    return {"head": head, "events": events, "resetRequired": False, "hasMore": has_more}


def _frames(body: bytes) -> list[dict]:
    frames = []
    for block in body.decode("utf-8").split("\n\n"):
        if not block.strip():
            continue
        fields = dict(line.split(": ", 1) for line in block.split("\n"))
        frames.append({
            "id": fields.get("id"),
            "event": fields["event"],
            "data": json.loads(fields["data"]),
        })
    return frames


@pytest.fixture()
def stream(flask_client, monkeypatch):
    from lib.conversation_sync.broker import ConversationWakeBroker
    from lib.conversation_sync.service import ConversationSyncService
    from routes import conversation_sync_v3 as sync_routes

    class FastHeartbeatService(ConversationSyncService):
        heartbeat_interval_ms = 1

    def open_stream(pages, *, query=None, last_event_id=None):
        broker = ConversationWakeBroker(owner_history_capacity=16)
        repository = ScriptedConversationRepository({CONVERSATION: pages}, broker)
        monkeypatch.setattr(
            sync_routes, "_service", FastHeartbeatService(repository))
        monkeypatch.setattr(sync_routes, "broker", broker)
        response = flask_client.get(
            f"/api/v4/conversations/{CONVERSATION}/events",
            query_string={"streamClientId": "page-a", "streamGeneration": "1",
                          "diagnosticPayload": "none", **(query or {})},
            headers={"Last-Event-ID": last_event_id} if last_event_id else {},
        )
        assert response.status_code == 200, response.data
        return _frames(response.data)

    return open_stream


def _cursor(sequence: int) -> str:
    from lib.conversation_sync.cursor import encode_cursor

    return encode_cursor(CONVERSATION, 1, sequence)


def _large_backlog() -> list[dict]:
    return [_page(3, [_change(sequence, 40_000) for sequence in (1, 2, 3)])]


def test_backlog_beyond_the_window_resets_without_replaying_it(stream):
    frames = stream(_large_backlog(), query={"windowBytes": str(WINDOW_BYTES)})

    assert [frame["event"] for frame in frames] == ["sync.reset_required"]
    assert frames[0]["data"]["reason"] == "replay_exceeds_window"
    assert frames[0]["data"]["cursor"] == _cursor(3)


def test_backlog_within_the_window_replays_every_change_in_order(stream):
    frames = stream(
        [_page(3, [_change(sequence, 100) for sequence in (1, 2, 3)])],
        query={"windowBytes": str(WINDOW_BYTES)},
    )

    changes = [frame for frame in frames if frame["event"] == "conversation.activity"]
    assert [frame["data"]["syncSeq"] for frame in changes] == [1, 2, 3]
    assert [frame["id"] for frame in changes] == [_cursor(1), _cursor(2), _cursor(3)]
    assert all(frame["data"].get("reason") != "replay_exceeds_window" for frame in frames)


def test_clients_without_a_window_keep_unbounded_replay(stream):
    frames = stream(_large_backlog())

    assert [frame["data"].get("syncSeq") for frame in frames[:3]] == [1, 2, 3]


def test_first_connection_at_a_snapshot_cursor_replays_its_round_trip(stream):
    frames = stream(_large_backlog(), query={
        "windowBytes": str(WINDOW_BYTES), "cursorSource": "snapshot",
        "after": _cursor(0)})

    assert [frame["data"].get("syncSeq") for frame in frames[:3]] == [1, 2, 3]


def test_native_reconnect_of_a_snapshot_cursor_stream_is_budgeted(stream):
    frames = stream(
        _large_backlog(),
        query={"windowBytes": str(WINDOW_BYTES), "cursorSource": "snapshot",
               "after": _cursor(0)},
        last_event_id=_cursor(0),
    )

    assert frames[0]["data"]["reason"] == "replay_exceeds_window"


def test_changes_committed_after_the_first_read_always_stream(stream):
    frames = stream(
        [_page(1, [_change(1, 100)], has_more=True),
         _page(4, [_change(sequence, 40_000) for sequence in (2, 3, 4)], has_more=True)],
        query={"windowBytes": str(WINDOW_BYTES)},
    )

    assert [frame["data"].get("syncSeq") for frame in frames[:4]] == [1, 2, 3, 4]
    assert frames[4]["data"]["reason"] == "cursor_expired"


def test_heartbeats_carry_their_cursor_as_the_event_id(stream):
    frames = stream([_page(0, [])], query={"windowBytes": str(WINDOW_BYTES)})

    heartbeat = next(frame for frame in frames if frame["event"] == "sync.heartbeat")
    assert heartbeat["id"] == heartbeat["data"]["cursor"] == _cursor(0)


@pytest.mark.parametrize("initial_change", [False, True])
def test_invalidations_reveal_later_pages_after_quiet_or_caught_up_state(stream, initial_change):
    next_sequence = 1 + int(initial_change)
    frames = stream([
        _page(int(initial_change), [_change(1, 100)] if initial_change else []),
        _page(next_sequence, [_change(next_sequence, 100)]),
        _page(next_sequence, []),
        _page(next_sequence + 1, [_change(next_sequence + 1, 100)]),
    ], query={"windowBytes": str(WINDOW_BYTES)})

    changes = [frame for frame in frames if frame["event"] == "conversation.activity"]
    sequences = list(range(1, next_sequence + 2))
    assert [frame["data"]["syncSeq"] for frame in changes] == sequences
    assert [frame["id"] for frame in changes] == [_cursor(value) for value in sequences]
    if not initial_change:
        assert frames[0]["event"] == "sync.heartbeat"
        assert frames[0]["id"] == frames[0]["data"]["cursor"] == _cursor(0)
    assert frames[-1]["event"] == "sync.reset_required"
    assert frames[-1]["data"]["reason"] == "cursor_expired"


@pytest.mark.parametrize("query", [
    {"windowBytes": "1024"},
    {"windowBytes": "big"},
    {"cursorSource": "retained"},
])
def test_invalid_catch_up_parameters_are_rejected(flask_client, query):
    response = flask_client.get(
        f"/api/v4/conversations/{CONVERSATION}/events", query_string=query)

    assert response.status_code == 400
