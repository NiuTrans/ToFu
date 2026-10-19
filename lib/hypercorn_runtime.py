"""Hypercorn transport owner: build_hypercorn_config, serve, and worker_serve.

Keep listener admission, bounded connection draining, and ASGI lifespan in one
owner. Protocol implementations remain with Hypercorn; no global hooks change.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import math
import os
import platform
import signal
import socket as socket_options
import ssl
import struct
import sys
import warnings
from asyncio import TaskGroup
from collections.abc import Awaitable, Callable, Mapping
from os import getpid
from random import randint
from socket import socket
from typing import Any, Literal

from hypercorn.asyncio.lifespan import Lifespan
from hypercorn.asyncio.statsd import StatsdLogger
from hypercorn.asyncio.tcp_server import MAX_RECV, TCPServer
from hypercorn.asyncio.udp_server import UDPServer
from hypercorn.asyncio.worker_context import WorkerContext
from hypercorn.events import Closed, RawData, Updated
from hypercorn.config import Config, Sockets
from hypercorn.typing import AppWrapper, Framework, LifespanState
from hypercorn.utils import ShutdownError, raise_shutdown, repr_socket_addr, wrap_app

from lib.storage.startup_budget import lifespan_startup_timeout


class _PeerWriteStall(TimeoutError):
    """The owned progress deadline expired, independently of application errors."""


class _PeerDisconnected(ConnectionResetError):
    """The owned send port proved the peer transport cannot receive bytes."""


def _pending_tcp_bytes(writer) -> int | None:
    """Observe unsent/unacknowledged plain TCP bytes before releasing its FD.

    Linux SIOCOUTQ includes the kernel queue that StreamWriter.drain cannot
    observe. TLS retains its upstream shutdown handshake; other platforms and
    unavailable socket observations retain upstream close semantics.
    """
    if sys.platform != 'linux':
        return None
    get_extra_info = getattr(writer, 'get_extra_info', None)
    if get_extra_info is None:
        return None
    try:
        if get_extra_info('ssl_object') is not None:
            return None
        sock = get_extra_info('socket')
        if sock is None:
            return None
        descriptor = sock.fileno()
        if descriptor < 0:
            return None
        import fcntl
        # Linux asm-generic/ioctls.h: TIOCOUTQ / net/sock.h: SIOCOUTQ.
        return struct.unpack('=I', fcntl.ioctl(descriptor, 0x5411, bytes(4)))[0]
    except (OSError, AttributeError, RuntimeError):
        # Released or unobservable transports still run upstream cleanup.
        return None


def _write_progress(writer) -> tuple[int | None, int | None]:
    """Observe userspace drain and, on Linux, acknowledged TCP bytes.

    TCP_INFO's stable Linux UAPI prefix puts tcpi_bytes_acked at byte 120.
    Older kernels and other transports fall back to userspace buffer progress.
    ACKs matter when a full kernel buffer holds the Python buffer constant.
    """
    transport = getattr(writer, 'transport', None)
    try:
        buffered = transport.get_write_buffer_size() if transport is not None else None
    except (AttributeError, RuntimeError):
        # SSL transports can release their protocol during close; observation
        # is optional and must not prevent the actual cleanup from running.
        buffered = None
    acknowledged = None
    if sys.platform == 'linux' and hasattr(socket_options, 'TCP_INFO'):
        get_extra_info = getattr(writer, 'get_extra_info', None)
        try:
            sock = get_extra_info('socket') if get_extra_info is not None else None
        except (AttributeError, RuntimeError):
            # A released SSL protocol also loses its socket observation.
            sock = None
        if sock is not None:
            try:
                info = sock.getsockopt(socket_options.IPPROTO_TCP, socket_options.TCP_INFO, 128)
                if len(info) >= 128:
                    acknowledged = struct.unpack_from('=Q', info, 120)[0]
            except OSError:
                # Closed sockets and kernels without this TCP_INFO prefix
                # still have the portable userspace drain observation.
                pass
    return buffered, acknowledged


async def _wait_for_write_progress(writer, operation: Callable[[], Awaitable], budget: float):
    """Expire only after an interval without progress; own and join the observer."""
    # The observer arms expiry after its final progress check. A loaded event
    # loop must not fire a stale timer before noticing newly acknowledged data.
    deadline = asyncio.timeout(None)
    previous = _write_progress(writer)
    loop = asyncio.get_running_loop()
    last_progress = loop.time()

    async def observe() -> None:
        nonlocal previous, last_progress
        while True:
            await asyncio.sleep(min(1.0, budget / 4))
            current = _write_progress(writer)
            drained = (previous[0] is not None and current[0] is not None
                       and current[0] < previous[0])
            acknowledged = (previous[1] is not None and current[1] is not None
                            and current[1] > previous[1])
            if drained or acknowledged:
                last_progress = loop.time()
            previous = current
            if loop.time() - last_progress >= budget:
                deadline.reschedule(loop.time())
                return

    observer = None
    try:
        async with deadline:
            observer = asyncio.create_task(observe())
            return await operation()
    except TimeoutError as error:
        if deadline.expired():
            raise _PeerWriteStall('peer made no write progress') from error
        raise
    finally:
        if observer is not None:
            observer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await observer
        # Upstream wait_closed contains CancelledError. Reclaim the peer even
        # if it swallowed this deadline's cancellation; never cancel app work.
        if deadline.expired():
            _abort_peer(writer)


def _install_late_h2_data_guard(wrapper) -> None:
    """Keep early-response DATA on closed streams inside one owned connection.

    Hypercorn already tolerates late StreamEnded, but its DataReceived branch
    indexes an ASGI stream that may have finished before an empty POST DATA
    frame arrives. Drop only dispatch to removed ASGI streams; acknowledge their
    bytes exactly once so other streams retain connection flow-control credit.
    Protocol parsing and every live-stream event remain upstream-owned.
    """
    from hypercorn.protocol.h2 import H2Protocol
    from h2.events import DataReceived
    protocol = getattr(wrapper, 'protocol', None)
    if not isinstance(protocol, H2Protocol) or getattr(protocol, '_tofu_late_data_guard', False):
        return
    original = protocol._handle_events
    def admitted_events(events):
        for event in events:
            if isinstance(event, DataReceived) and event.stream_id not in protocol.streams:
                protocol.connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            else:
                yield event
    async def handle_events(events):
        return await original(admitted_events(events))
    protocol._handle_events = handle_events
    protocol._tofu_late_data_guard = True


class _OwnedTCPServer(TCPServer):
    """Keep peer socket expiry inside the transport cleanup boundary."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._protocol_closed = False
        self._read_task: asyncio.Task | None = None

    def _signal_close(self) -> None:
        self._protocol_closed = True
        if self._read_task is not None:
            self._read_task.cancel()

    async def run(self) -> None:
        try:
            await super().run()
        except _PeerDisconnected:
            logging.getLogger(__name__).debug('Peer disconnected during transport send')
        except BaseExceptionGroup as errors:
            # A protocol reply from the read owner can fail inside Hypercorn's
            # task group. Contain only failures witnessed at our send port;
            # mixed groups retain every unrelated protocol/cleanup failure.
            disconnected, unexpected = errors.split(_PeerDisconnected)
            if disconnected is None:
                raise
            logging.getLogger(__name__).debug('Peer disconnected during transport send')
            if unexpected is not None:
                raise unexpected

    async def protocol_send(self, event) -> None:
        if isinstance(event, Closed):
            # ASGI frameworks cancel their response task after disconnect.
            # Signal the connection owner; flushing in that response task
            # would turn normal application completion into a peer reset.
            self._signal_close()
            return
        if isinstance(event, Updated) and self._protocol_closed:
            # A late ASGI completion is an idle hint, not authority to restart
            # the timer after the read owner has closed this connection.
            return
        if not isinstance(event, RawData):
            await super().protocol_send(event)
            return
        async with self.send_lock:
            if self.writer.is_closing():
                raise _PeerDisconnected('peer transport is closed')
            try:
                self.writer.write(event.data)
                # Ordinary writes need no observer or extra task. A blocked
                # drain renews its budget on buffer drain or TCP ACK progress.
                if self.writer.transport.get_write_buffer_size() == 0:
                    await self.writer.drain()
                else:
                    await _wait_for_write_progress(
                        self.writer, self.writer.drain,
                        getattr(self.config, 'write_stall_timeout', 300.0),
                    )
            except TimeoutError as error:
                await self.protocol.handle(Closed())
                raise _PeerDisconnected('peer stopped reading the response') from error
            except ConnectionError as error:
                await self.protocol.handle(Closed())
                raise _PeerDisconnected(str(error)) from error
            except RuntimeError:
                await self.protocol.handle(Closed())
                raise

    async def _read_data(self) -> None:
        while not self.reader.at_eof() and not self._protocol_closed:
            # Upstream wait_for already creates this read task. Own its handle
            # so protocol completion can wake it without closing the socket
            # before the connection owner flushes the response.
            read_task = asyncio.create_task(self.reader.read(MAX_RECV))
            self._read_task = read_task
            try:
                data = await asyncio.wait_for(read_task, self.config.read_timeout)
            except asyncio.CancelledError:
                if self._protocol_closed and not asyncio.current_task().cancelling():
                    break
                raise
            except OSError:
                # Preserve upstream containment of peer read/SSL/timeouts.
                break
            finally:
                self._read_task = None
                if not read_task.done():
                    read_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await read_task
            if self._protocol_closed:
                break
            _install_late_h2_data_guard(self.protocol)
            await self.protocol.handle(RawData(data))
        self._signal_close()
        try:
            await self.protocol.handle(Closed())
        finally:
            # Stop/join before task-group drain, including protocol-close faults.
            await self.idle_task.stop()

    async def _initiate_server_close(self) -> None:
        # Idle expiry wakes the same owner rather than releasing its writer.
        await self.protocol.handle(Closed())
        self._signal_close()

    async def _close(self) -> None:
        async def flush_and_close() -> None:
            try:
                if _pending_tcp_bytes(self.writer) is not None:
                    try:
                        self.writer.write_eof()
                    except (NotImplementedError, OSError, RuntimeError):
                        # Upstream also tolerates unsupported/already-closed EOF.
                        pass
                # A drained Python buffer can leave a full kernel send queue.
                # Releasing its descriptor immediately truncates close-delimited
                # responses on some local transports. Keep this connection's
                # owner alive until acknowledgement, expiry, or cancellation.
                while (pending := _pending_tcp_bytes(self.writer)) is not None:
                    buffered, _ = _write_progress(self.writer)
                    if pending == 0 and not buffered:
                        break
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                # Expiry/shutdown cannot enter upstream's flush with an
                # abandoned kernel queue and no remaining cancellation timer.
                _abort_peer(self.writer)
                raise
            finally:
                await super(_OwnedTCPServer, self)._close()

        try:
            await _wait_for_write_progress(
                self.writer, flush_and_close,
                getattr(getattr(self, 'config', None), 'connection_close_timeout', 300.0),
            )
        except ssl.SSLError as error:
            if (getattr(error, 'reason', None) != 'APPLICATION_DATA_AFTER_CLOSE_NOTIFY'
                    or not self.writer.is_closing()):
                raise
            # A peer TLS close can race queued application frames. Upstream
            # already closes the writer and joins its idle timer in finally;
            # contain only this closed-peer outcome at the connection boundary.
            logging.getLogger(__name__).debug('TLS peer closed with application data pending: %s', error)
        except TimeoutError as error:
            # Linux can report ETIMEDOUT only when wait_closed observes the
            # peer's failed transport. Upstream already closes the writer and
            # stops its idle task in finally; no application failure is hidden.
            if not isinstance(error, _PeerWriteStall) and error.errno != errno.ETIMEDOUT:
                raise
            logging.getLogger(__name__).debug(
                'Peer TCP connection timed out during close: %s', error,
            )


def _abort_peer(writer: asyncio.StreamWriter) -> None:
    """Reset an abandoned peer before abort; FIN can wait behind unread data."""
    sock = writer.get_extra_info('socket')
    if sock is not None:
        try:
            # Winsock uses two unsigned shorts; POSIX uses two ints.
            layout = 'HH' if sys.platform == 'win32' else 'ii'
            sock.setsockopt(socket_options.SOL_SOCKET, socket_options.SO_LINGER,
                            struct.pack(layout, 1, 0))
        except OSError as error:
            # A peer may already have closed, or the platform may lack linger.
            # Local abort still releases the owned buffer and descriptor.
            logging.getLogger(__name__).debug('Peer reset unavailable: %s', error)
    writer.transport.abort()


def build_hypercorn_config(
    host: str,
    port: int,
    *,
    keep_alive_timeout: float,
    tls_cert: str = '',
    tls_key: str = '',
    environ: Mapping[str, str] | None = None,
    logger: Any = None,
) -> Config:
    """Build validated transport settings without importing ``server.py``."""
    env = os.environ if environ is None else environ
    log = logger or logging.getLogger(__name__)
    config = Config()
    config.bind = [f'{host}:{int(port)}']
    config.accesslog = logging.getLogger('hypercorn.access')
    config.errorlog = logging.getLogger('hypercorn.error')
    config.keep_alive_timeout = float(keep_alive_timeout)
    try:
        raw_write_stall = (os.environ.get('TOFU_HTTP_WRITE_STALL_SECS', '')
                           if environ is None else env.get('TOFU_HTTP_WRITE_STALL_SECS', ''))
        write_stall_timeout = float(raw_write_stall or '300')
        if not math.isfinite(write_stall_timeout) or write_stall_timeout <= 0:
            raise ValueError('write stall timeout must be positive and finite')
    except (TypeError, ValueError, OverflowError) as error:
        log.debug('[Server] bad TOFU_HTTP_WRITE_STALL_SECS, defaulting: %s', error)
        write_stall_timeout = 300.0
    config.write_stall_timeout = max(1.0, min(86400.0, write_stall_timeout))
    config.connection_close_timeout = config.write_stall_timeout
    # Hypercorn otherwise cancels Quart's entire startup lifespan at 60s.
    # Initial fastpath activation is a bounded, capacity-checked migration and
    # owns a longer sidecar budget; preserve that budget plus time for the
    # remaining required phases instead of killing and restarting the copy.
    config.startup_timeout = lifespan_startup_timeout(env)
    # /api/push accepts only tiny subscription/control frames and 16 KiB
    # control-RPC requests. Reject oversized WebSocket messages in Hypercorn
    # before Quart allocates/parses a 16 MiB default frame.
    config.websocket_max_message_size = 64 * 1024
    # Transport-level grace on shutdown. Hypercorn waits for EVERY open
    # connection task to finish up to this budget, and a connected browser
    # always holds a long-lived push channel — so the full window is burned
    # on every shutdown, after which those connections are hard-closed
    # anyway. In-flight agent work is drained one layer up (task quiesce +
    # TOFU_SHUTDOWN_DRAIN_SECS), and short requests finish in well under a
    # second, so keep the transport window tight; raise it via env only for
    # deployments whose shutdown must nurse slow requests to completion.
    try:
        config.graceful_timeout = float(
            env.get('TOFU_GRACEFUL_TIMEOUT', '') or '1')
    except (TypeError, ValueError, OverflowError) as exc:
        log.debug('[Server] bad TOFU_GRACEFUL_TIMEOUT, defaulting: %s', exc)
        config.graceful_timeout = 1.0

    try:
        backlog = int(env.get('TOFU_LISTEN_BACKLOG', '0') or '0')
    except (TypeError, ValueError, OverflowError) as exc:
        log.debug('[Server] bad TOFU_LISTEN_BACKLOG, defaulting: %s', exc)
        backlog = 0
    config.backlog = backlog if backlog > 0 else 1024

    if tls_cert and tls_key:
        config.certfile = tls_cert
        config.keyfile = tls_key
    return config



# Adapted from Hypercorn 0.18 asyncio.run.worker_serve; Tofu owns bounded
# transport draining before the listener join and lifespan. Source and license:
# https://github.com/pgjones/hypercorn/blob/0.18.0/src/hypercorn/asyncio/run.py
# Copyright P G Jones 2018.
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

def _share_socket(sock: socket) -> socket:
    # Preserve Hypercorn's explicit Windows socket handoff.
    from socket import fromshare
    return fromshare(sock.share(getpid()))


async def serve(
    app: Framework,
    config: Config,
    *,
    shutdown_trigger: Callable[..., Awaitable] | None = None,
    mode: Literal["asgi", "wsgi"] | None = None,
) -> None:
    """Serve using the caller's loop and the application's lifecycle owner."""
    if config.debug:
        warnings.warn("The config `debug` has no effect when using serve", Warning, stacklevel=2)
    if config.workers != 1:
        warnings.warn("The config `workers` has no effect when using serve", Warning, stacklevel=2)
    await worker_serve(
        wrap_app(app, config.wsgi_max_body_size, mode), config,
        shutdown_trigger=shutdown_trigger,
    )


async def worker_serve(
    app: AppWrapper,
    config: Config,
    *,
    sockets: Sockets | None = None,
    shutdown_trigger: Callable[..., Awaitable] | None = None,
) -> None:
    config.set_statsd_logger_class(StatsdLogger)

    loop = asyncio.get_event_loop()

    if shutdown_trigger is None:
        signal_event = asyncio.Event()

        def _signal_handler(*_: Any) -> None:  # noqa: N803
            signal_event.set()

        for signal_name in {"SIGINT", "SIGTERM", "SIGBREAK"}:
            if hasattr(signal, signal_name):
                try:
                    loop.add_signal_handler(getattr(signal, signal_name), _signal_handler)
                except NotImplementedError:
                    # Add signal handler may not be implemented on Windows
                    signal.signal(getattr(signal, signal_name), _signal_handler)

        shutdown_trigger = signal_event.wait

    lifespan_state: LifespanState = {}
    lifespan = Lifespan(app, config, loop, lifespan_state)

    lifespan_task = loop.create_task(lifespan.handle_lifespan())
    await lifespan.wait_for_startup()
    if lifespan_task.done():
        exception = lifespan_task.exception()
        if exception is not None:
            raise exception

    if sockets is None:
        sockets = config.create_sockets()

    ssl_handshake_timeout = None
    if config.ssl_enabled:
        ssl_context = config.create_ssl_context()
        ssl_handshake_timeout = config.ssl_handshake_timeout

    max_requests = None
    if config.max_requests is not None:
        max_requests = config.max_requests + randint(0, config.max_requests_jitter)
    context = WorkerContext(max_requests)
    server_tasks: set[asyncio.Task] = set()
    connection_writers: set[asyncio.StreamWriter] = set()

    async def _server_callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task(loop)
        server_tasks.add(task)
        task.add_done_callback(server_tasks.discard)
        connection_writers.add(writer)
        try:
            await _OwnedTCPServer(app, loop, config, context, lifespan_state, reader, writer)
        finally:
            # Normal TCPServer cleanup already flushed/closed the writer.
            # On cancellation, ensure no buffered transport outlives its owner.
            if not writer.is_closing():
                _abort_peer(writer)
            connection_writers.discard(writer)

    servers = []
    for sock in sockets.secure_sockets:
        if config.workers > 1 and platform.system() == "Windows":
            sock = _share_socket(sock)

        servers.append(
            await asyncio.start_server(
                _server_callback,
                backlog=config.backlog,
                ssl=ssl_context,
                sock=sock,
                ssl_handshake_timeout=ssl_handshake_timeout,
            )
        )
        bind = repr_socket_addr(sock.family, sock.getsockname())
        await config.log.info("Running on https://%s (CTRL + C to quit)", bind)

    for sock in sockets.insecure_sockets:
        if config.workers > 1 and platform.system() == "Windows":
            sock = _share_socket(sock)

        servers.append(
            await asyncio.start_server(_server_callback, backlog=config.backlog, sock=sock)
        )
        bind = repr_socket_addr(sock.family, sock.getsockname())
        await config.log.info("Running on http://%s (CTRL + C to quit)", bind)

    for sock in sockets.quic_sockets:
        if config.workers > 1 and platform.system() == "Windows":
            sock = _share_socket(sock)

        _, protocol = await loop.create_datagram_endpoint(
            lambda: UDPServer(app, loop, config, context, lifespan_state), sock=sock
        )
        task = loop.create_task(protocol.run())
        server_tasks.add(task)
        task.add_done_callback(server_tasks.discard)
        bind = repr_socket_addr(sock.family, sock.getsockname())
        await config.log.info("Running on https://%s (QUIC) (CTRL + C to quit)", bind)

    try:
        async with TaskGroup() as task_group:
            task_group.create_task(raise_shutdown(shutdown_trigger))
            task_group.create_task(raise_shutdown(context.terminate.wait))
    except BaseExceptionGroup as error:
        _, other_errors = error.split((ShutdownError, KeyboardInterrupt))
        if other_errors is not None:
            raise other_errors
    except (ShutdownError, KeyboardInterrupt):
        # These are ordinary shutdown requests; the finally block owns drain.
        pass
    finally:
        await context.terminated.set()

        # Stop admission first. On Python 3.12+, wait_closed also waits for
        # active transports; joining here would bypass graceful_timeout and
        # prevent the application's storage-release lifespan from running.
        for server in servers:
            server.close()

        draining_tasks = tuple(server_tasks)
        try:
            if draining_tasks:
                _, pending = await asyncio.wait(
                    draining_tasks, timeout=config.graceful_timeout,
                )
                # Cancelling TCPServer enters writer.wait_closed() in its
                # finally block. A peer that stopped reading can keep that
                # flush pending forever, so abort expired transports first.
                for writer in tuple(connection_writers):
                    _abort_peer(writer)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*draining_tasks, return_exceptions=True)
        finally:
            for server in servers:
                await server.wait_closed()

            await lifespan.wait_for_shutdown()
            lifespan_task.cancel()
            await lifespan_task


__all__ = ['build_hypercorn_config', 'serve', 'worker_serve']
