"""Owned WebSocket encoding at the Hypercorn application boundary.

Entry: with_websocket_fairness wraps a Hypercorn AppWrapper. Its per-stream
send adapter moves large-message compression to the existing demand executor.
wsproto keeps parsing, compression and wire framing. No global protocol hooks,
new pool, admission limit or application-message change is introduced.
"""
from __future__ import annotations

import asyncio
from contextvars import copy_context

from hypercorn.protocol.events import Body, Data, StreamClosed
from hypercorn.protocol.ws_stream import FrameTooLargeError, WSStream
from wsproto.connection import ConnectionState
from wsproto.events import CloseConnection, Message, Ping
from wsproto.utilities import LocalProtocolError
from wsproto.frame_protocol import CloseReason


# Offload threshold, never a message/admission limit. Counting text characters
# avoids allocating an entire UTF-8 copy merely to choose the execution lane.
_OFFLOAD_LENGTH = 64 * 1024


async def _encode_owned(function, event):
    future = asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, function, event)
    interrupted = None
    try:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError as error:
                interrupted = interrupted or error
        result = future.result()
    except BaseException as error:
        if interrupted is not None:
            raise BaseExceptionGroup('WebSocket encoding cancelled and failed',
                                     [interrupted, error]) from None
        raise
    if interrupted is not None:
        raise interrupted
    return result


def _install_stream_sender(stream: WSStream) -> None:
    original_handle = stream.handle
    send_lock = asyncio.Lock()
    codec_lock = asyncio.Lock()
    send_failed = False

    async def send_event(event):
        nonlocal send_failed
        async with send_lock:
            if stream.closed or send_failed:
                return
            try:
                async with codec_lock:
                    try:
                        if isinstance(event, Message) and len(event.data) >= _OFFLOAD_LENGTH:
                            data = await _encode_owned(stream.connection.send, event)
                        else:
                            data = stream.connection.send(event)
                    except LocalProtocolError:
                        return  # Preserve Hypercorn's closed/protocol-state handling.
                if not stream.closed:
                    await stream.send(Data(stream_id=stream.stream_id, data=data))
            except BaseException as error:
                # The compressor may have advanced even if bytes weren't sent.
                # Join encoding before releasing its lock, then fence reuse and
                # close through the existing owner. No late worker can write.
                send_failed = True
                try:
                    await stream.send(StreamClosed(stream_id=stream.stream_id))
                except BaseException as cleanup_error:
                    raise BaseExceptionGroup('WebSocket send and close failed',
                                             [error, cleanup_error]) from None
                raise

    async def handle(event):
        if (stream.closed or not isinstance(event, (Body, Data))
                or not stream.handshake.accepted):
            return await original_handle(event)
        if send_failed:
            return
        # wsproto connection state and compression context are not thread-safe.
        # Parse under the same finite codec lock, but never hold it while an
        # inbound ping/close awaits the outbound send owner or network drain.
        async with codec_lock:
            if stream.closed:
                return
            stream.connection.receive_data(event.data)
            events = stream.connection.events()
        # Hypercorn 0.18 WSStream._handle_events dispatch; parsing and message
        # assembly remain upstream-owned. Kept explicit to avoid global hooks.
        while True:
            async with codec_lock:
                if stream.closed:
                    return
                frame = next(events, None)
            if frame is None:
                break
            if isinstance(frame, Message):
                try:
                    stream.buffer.extend(frame)
                except FrameTooLargeError:
                    await send_event(CloseConnection(code=CloseReason.MESSAGE_TOO_BIG))
                    break
                if frame.message_finished:
                    await stream.app_put(stream.buffer.to_message())
                    stream.buffer.clear()
            elif isinstance(frame, Ping):
                await send_event(frame.response())
            elif isinstance(frame, CloseConnection):
                if stream.connection.state == ConnectionState.REMOTE_CLOSING:
                    await send_event(frame.response())
                await stream.send(StreamClosed(stream_id=stream.stream_id))

    stream._send_wsproto_event = send_event
    stream.handle = handle


def with_websocket_fairness(app):
    """Install once before this connection's application can accept/send.

    Hypercorn supplies WSStream.app_send to its AppWrapper, for both H1 and
    H2. Keep this private adapter at that owned seam rather than copying the
    protocol constructors or changing the process-wide WSStream class.
    """
    async def application(scope, receive, send, sync_spawn, call_soon):
        if scope['type'] == 'websocket':
            stream = getattr(send, '__self__', None)
            if not isinstance(stream, WSStream):
                raise RuntimeError('Hypercorn WebSocket send owner is unavailable')
            _install_stream_sender(stream)
        await app(scope, receive, send, sync_spawn, call_soon)

    return application
