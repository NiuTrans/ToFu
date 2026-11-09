"""Stdio binding of the channel protocol: JSON-RPC 2.0, one JSON object per line.

Responsibility: carry protocol objects between the core and an adapter that
runs in another process or runtime (docs/CHANNEL_PROTOCOL.md P1, the process
model). Both sides share one endpoint: requests are answered by ID on the
reader thread, and incoming requests and notifications run in arrival order on
one dispatcher thread, so a nested call (the core's ``download`` while the
adapter's ``events`` request is in flight) cannot deadlock. A request ends
with its answer or with the binding: when the pipe closes every pending
request fails (an in-flight send is then unknown); the protocol adds no
timeout. Handlers must not wait on a call to the other side from the
dispatcher thread; an adapter delivers events from its own thread.

Core side, ``StdioLink``: spawns the adapter in its own process group with the
environment it is given (credentials reach it there, never through argv or a
protocol message), answers its ``events`` requests and ``health``
notifications through the bound ``AdapterHost``, keeps stderr as diagnostics
only, and on ``close`` closes stdin (the adapter's stop signal), waits, then
signals the process group and reaps it (docs/DEFENSIVE_PATTERNS.md, Dispose
must reach quiescence). Adapter side, ``serve``: runs a ``ProtocolAdapter`` on
stdin/stdout until stdin closes.

Entry points: ``StdioLink``, ``serve``. Dependencies: ``contract`` only.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from typing import Any, BinaryIO

from lib.channels.protocol.contract import ERRORS, METHODS, ProtocolError

_CLOSED = 'The channel adapter binding closed'


class _Peer:
    """One JSON-RPC 2.0 endpoint over a pair of byte streams."""

    def __init__(self, reader: BinaryIO, writer: BinaryIO, handler: Callable[[str, dict], Any], *, name: str):
        self._reader, self._writer, self._handler = reader, writer, handler
        self._pending: dict[int, Future] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        self._inbox: queue.Queue = queue.Queue()
        self._threads = [threading.Thread(target=self._read, name=f'{name}-reader', daemon=True),
                         threading.Thread(target=self._dispatch, name=f'{name}-dispatch', daemon=True)]

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def join(self, timeout: float | None = None) -> None:
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout)

    def _write(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8')
        with self._write_lock:
            self._writer.write(data)
            self._writer.flush()

    def _answer(self, request_id: Any, *, result: Any = None, error: ProtocolError | None = None) -> None:
        message: dict[str, Any] = {'jsonrpc': '2.0', 'id': request_id}
        if error is None:
            message['result'] = result
        else:
            message['error'] = {'code': error.code, 'message': str(error)}
            if error.data:
                message['error']['data'] = error.data
        try:
            self._write(message)
        except (OSError, ValueError):
            pass  # the other side is gone; its pending request fails with the binding

    def request(self, method: str, params: dict[str, Any]) -> Any:
        future: Future = Future()
        with self._lock:
            if self._closed:
                raise ProtocolError(ERRORS['internalError'], _CLOSED)
            request_id = next(self._ids)
            self._pending[request_id] = future
        try:
            self._write({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        except (OSError, ValueError) as error:
            with self._lock:
                self._pending.pop(request_id, None)
            raise ProtocolError(ERRORS['internalError'], _CLOSED) from error
        return future.result()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        try:
            self._write({'jsonrpc': '2.0', 'method': method, 'params': params})
        except (OSError, ValueError) as error:
            raise ProtocolError(ERRORS['internalError'], _CLOSED) from error

    def _read(self) -> None:
        try:
            for raw in self._reader:
                try:
                    message = json.loads(raw.decode('utf-8'))
                except (UnicodeDecodeError, ValueError):
                    self._answer(None, error=ProtocolError(ERRORS['parseError'], 'A line is not JSON'))
                    continue
                if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                    self._answer(None, error=ProtocolError(ERRORS['invalidRequest'], 'Not a JSON-RPC 2.0 object'))
                elif 'method' in message:
                    self._inbox.put(message)
                else:
                    with self._lock:
                        future = self._pending.pop(message.get('id'), None)
                    if future is None:
                        continue
                    error = message.get('error')
                    if isinstance(error, dict):
                        future.set_exception(ProtocolError(int(error.get('code') or ERRORS['internalError']),
                                                           str(error.get('message') or ''),
                                                           data=error.get('data') or None))
                    else:
                        future.set_result(message.get('result'))
        finally:
            with self._lock:
                self._closed = True
                pending, self._pending = list(self._pending.values()), {}
            for future in pending:
                future.set_exception(ProtocolError(ERRORS['internalError'], _CLOSED))
            self._inbox.put(None)

    def _dispatch(self) -> None:
        while True:
            message = self._inbox.get()
            if message is None:
                return
            request_id = message.get('id')
            params = message.get('params')
            try:
                if not isinstance(params, dict):
                    raise ProtocolError(ERRORS['invalidParams'], 'params must be an object')
                result = self._handler(str(message['method']), params)
            except ProtocolError as error:
                if request_id is not None:
                    self._answer(request_id, error=error)
                continue
            except Exception as error:
                if request_id is not None:
                    self._answer(request_id, error=ProtocolError(ERRORS['internalError'], type(error).__name__))
                continue
            if request_id is not None:
                self._answer(request_id, result=result)


class StdioLink:
    """Core side of the stdio binding: one adapter child process."""

    def __init__(self, argv: list[str], *, env: Mapping[str, str] | None = None, cwd: str | None = None):
        self._proc = subprocess.Popen(argv, cwd=cwd, env=dict(env) if env is not None else None,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      start_new_session=True)
        self._host = None
        # Diagnostics only: an adapter never writes message content or credentials to stderr.
        self.stderr_tail = ''
        self._peer = _Peer(self._proc.stdout, self._proc.stdin, self._handle, name='channel-adapter')
        self._stderr = threading.Thread(target=self._drain, name='channel-adapter-stderr', daemon=True)
        self._peer.start()
        self._stderr.start()

    def bind(self, host) -> None:
        self._host = host

    def _handle(self, method: str, params: dict[str, Any]) -> Any:
        spec = METHODS.get(method)
        if spec is None or spec['direction'] != 'adapter_to_core':
            raise ProtocolError(ERRORS['methodNotFound'], f'The core has no method {method!r}')
        if self._host is None:
            raise ProtocolError(ERRORS['notInitialized'], 'No core endpoint is bound')
        result = getattr(self._host, method)(params)
        return result if spec['kind'] == 'request' else None

    def _drain(self) -> None:
        for raw in self._proc.stderr:
            self.stderr_tail = raw.decode('utf-8', 'replace').strip() or self.stderr_tail

    def request(self, method: str, params: dict[str, Any]) -> Any:
        return self._peer.request(method, params)

    @property
    def returncode(self) -> int | None:
        return self._proc.poll()

    def _signal(self, sig: int) -> None:
        try:
            os.killpg(self._proc.pid, sig)
        except ProcessLookupError:
            pass  # the group already exited

    def close(self, timeout: float) -> bool:
        """Close stdin (the adapter's stop signal), then escalate; always reap."""
        try:
            self._proc.stdin.close()
        except OSError:
            pass  # the pipe is already broken: the child is gone or going
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._signal(signal.SIGTERM)
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._signal(signal.SIGKILL)
                self._proc.wait()
        self._peer.join(timeout)
        self._stderr.join(timeout)
        return self._proc.poll() is not None


class _CoreProxy:
    """The adapter's ``CoreLink`` over the stdio binding."""

    def __init__(self, peer: _Peer):
        self._peer = peer

    def events(self, batch: dict[str, Any]) -> dict[str, Any]:
        return self._peer.request('events', batch)

    def health(self, health: dict[str, Any]) -> None:
        self._peer.notify('health', health)


def serve(adapter, *, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None,
          extra_methods: Mapping[str, Callable[[dict], Any]] | None = None) -> int:
    """Run ``adapter`` (a ``ProtocolAdapter``) on stdin/stdout until stdin closes.

    stdout carries protocol lines only, so an adapter logs to stderr.
    ``extra_methods`` answers further core-to-adapter requests (a test platform's controls).
    """
    def handle(method: str, params: dict[str, Any]) -> Any:
        spec = METHODS.get(method)
        if spec is not None and spec['direction'] == 'core_to_adapter':
            return getattr(adapter, method)(params)
        if extra_methods and method in extra_methods:
            return extra_methods[method](params)
        raise ProtocolError(ERRORS['methodNotFound'], f'The adapter has no method {method!r}')

    peer = _Peer(stdin or sys.stdin.buffer, stdout or sys.stdout.buffer, handle, name='channel-adapter-serve')
    adapter.attach(_CoreProxy(peer))
    peer.start()
    peer.join()
    return 0


__all__ = ['StdioLink', 'serve']
