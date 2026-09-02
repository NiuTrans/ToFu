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
        client = PushClient(user_id=1)
        _handle_client_frame(client, {'action': 'ping', 't': 1234567})

        # The pong lands on the SAME queue _sender drains — proving it travels
        # the single-writer path, not a second direct socket write.
        frame = asyncio.run(client.drain())
        assert frame == {'channel': 'system', 'type': 'pong', 't': 1234567}

    def test_ping_without_timestamp_echoes_none(self):
        client = PushClient(user_id=1)
        _handle_client_frame(client, {'action': 'ping'})
        frame = asyncio.run(client.drain())
        assert frame == {'channel': 'system', 'type': 'pong', 't': None}

    def test_non_ping_frames_do_not_produce_a_pong(self):
        client = PushClient(user_id=1)
        # subscribe/unsubscribe/abort/garbage must not enqueue a pong.
        _handle_client_frame(client, {'action': 'unsubscribe', 'channel': 'chat',
                                      'taskId': 'task-1'})
        _handle_client_frame(client, {'action': 'bogus'})
        _handle_client_frame(client, None)
        assert client._queue.empty()

    def test_pong_jumps_the_data_backlog_single_writer(self):
        # pt_afbaf3d7: the pong travels the SAME single-writer path (the queue
        # _sender drains — never a second direct socket write), but via the
        # CONTROL LANE: it is drained BEFORE an already-queued data backlog.
        # Under loop congestion a FIFO pong would arrive past the client's 8s
        # watchdog, which then force-closes a HEALTHY socket. Nothing is lost:
        # the data frame is delivered right after.
        client = PushClient(user_id=1)
        client.enqueue({'channel': 'chat', 'taskId': 't', 'type': 'content_delta',
                        'delta': 'hello'})
        client.enqueue({'channel': 'chat', 'taskId': 't', 'type': 'content_delta',
                        'delta': 'world'})
        _handle_client_frame(client, {'action': 'ping', 't': 42})

        first = asyncio.run(client.drain())
        second = asyncio.run(client.drain())
        third = asyncio.run(client.drain())
        assert first == {'channel': 'system', 'type': 'pong', 't': 42}
        assert second['type'] == 'content_delta' and second['delta'] == 'hello'
        assert third['type'] == 'content_delta' and third['delta'] == 'world'

    def test_pong_wakes_a_drain_sleeping_on_an_empty_queue(self):
        # An IDLE socket has no data traffic: drain() is asleep in queue.get().
        # A control frame must wake it PROMPTLY (not after the 30s keepalive
        # timeout), or every idle ping would outlive the client watchdog.
        async def scenario():
            client = PushClient(user_id=1)
            task = asyncio.ensure_future(client.drain())
            await asyncio.sleep(0)  # let drain() arm its waiters
            await asyncio.sleep(0)
            _handle_client_frame(client, {'action': 'ping', 't': 7})
            return await asyncio.wait_for(task, timeout=2)

        frame = asyncio.run(scenario())
        assert frame == {'channel': 'system', 'type': 'pong', 't': 7}

    def test_data_lane_stays_fifo_without_control_frames(self):
        # The priority lane must not reorder ordinary traffic: two data frames
        # with no interleaved pong drain in their original order.
        client = PushClient(user_id=1)
        client.enqueue({'channel': 'chat', 'taskId': 't', 'type': 'content_delta',
                        'delta': 'a'})
        client.enqueue({'channel': 'chat', 'taskId': 't', 'type': 'content_delta',
                        'delta': 'b'})
        first = asyncio.run(client.drain())
        second = asyncio.run(client.drain())
        assert first['delta'] == 'a'
        assert second['delta'] == 'b'
