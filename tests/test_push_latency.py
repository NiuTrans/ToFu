"""Unit tests for the push-channel latency probe (ping → pong echo).

The frontend network-latency signal indicator measures RTT by sending
``{action:'ping', t}`` on the already-open ``/api/push`` WebSocket and timing
the matching ``{type:'pong', t}`` echo.

CRITICAL INVARIANT under test: the pong MUST be routed through the client's
OUTBOUND QUEUE (``PushClient.enqueue``), which the single ``_sender`` coroutine
drains — NOT written directly to the socket from ``_receiver``. Two coroutines
writing the same ASGI WebSocket concurrently can interleave/corrupt frames, so
the pong has to share the single-writer path with every other outbound frame.
We prove the pong lands on that queue (the same one ``drain()`` serves), and
that ``t`` is echoed verbatim.

Pure-logic: no live WebSocket, no DB, no network → ``unit`` marker.
"""
from __future__ import annotations

import asyncio

import pytest

from lib.agent_core.push import PushClient
from routes.push import _handle_client_frame


@pytest.mark.unit
class TestPushLatencyPingPong:
    def test_ping_enqueues_pong_with_echoed_timestamp(self):
        client = PushClient()
        _handle_client_frame(client, {'action': 'ping', 't': 1234567})

        # The pong lands on the SAME queue _sender drains — proving it travels
        # the single-writer path, not a second direct socket write.
        frame = asyncio.run(client.drain())
        assert frame == {'channel': 'system', 'type': 'pong', 't': 1234567}

    def test_ping_without_timestamp_echoes_none(self):
        client = PushClient()
        _handle_client_frame(client, {'action': 'ping'})
        frame = asyncio.run(client.drain())
        assert frame == {'channel': 'system', 'type': 'pong', 't': None}

    def test_non_ping_frames_do_not_produce_a_pong(self):
        client = PushClient()
        # subscribe/unsubscribe/abort/garbage must not enqueue a pong.
        _handle_client_frame(client, {'action': 'unsubscribe', 'channel': 'chat',
                                      'taskId': 'task-1'})
        _handle_client_frame(client, {'action': 'bogus'})
        _handle_client_frame(client, None)
        assert client._queue.empty()

    def test_pong_shares_the_single_writer_queue_ordering(self):
        # A frame already queued by the (single) writer path, then a ping:
        # the pong is appended AFTER it — same FIFO queue, one writer, no
        # out-of-band interleaving.
        client = PushClient()
        client.enqueue({'channel': 'chat', 'taskId': 't', 'type': 'content_delta',
                        'delta': 'hello'})
        _handle_client_frame(client, {'action': 'ping', 't': 42})

        first = asyncio.run(client.drain())
        second = asyncio.run(client.drain())
        assert first['type'] == 'content_delta'
        assert second == {'channel': 'system', 'type': 'pong', 't': 42}
