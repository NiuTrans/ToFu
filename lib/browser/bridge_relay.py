"""Private loopback relay from the resident execution host to the browser bridge owner.

Problem: in the managed split (``TOFU_PROCESS_ROLE=api`` + ``execution``) the
extension long-polls the API process, so the process-local queue/registry in
``lib.browser.queue._state`` lives there, while task tools and context
composition run in the execution process, whose registry is always empty.
Every model saw "no usable extension" and every browser command would have
been enqueued where no extension ever polls.

Decision: the API process stays the single bridge owner (it terminates the
extension's HTTP polls). It publishes a private endpoint record beside the
manager's other process records (``<project>/data/browser-bridge-relay.json``,
mode 0600, rotating bearer token); the execution process selects that owner
and ``lib.browser.queue`` forwards its owner-scoped worker verbs over it.

Alternatives considered: moving the registry into the execution host would
need the API to forward every poll and every status/panel read the other way;
a durable shared registry would turn transient liveness into stored state.

Protocol (``FORMAT``): one loopback connection per call; a network-order u32
byte length precedes one UTF-8 JSON object, both directions. Request
``{format, token, method, params}``; response ``{format, result, error}``.
The caller sends nothing after its request, so the owner treats EOF as the
caller's cancellation and withdraws a queued command exactly like a local
``cancel_event``. There is no retry: a lost response is reported as an
unreachable owner, never replayed.

Entry points: ``configure_bridge_owner`` / ``stop_bridge_owner`` (process
services), ``selected_relay`` (queue facade), ``RelayServer``, ``RelayClient``.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import struct
import threading
import time
from pathlib import Path

from lib.log import get_logger
from lib.tasks_pkg.generated_command_control import ACKNOWLEDGEMENT_SECONDS

logger = get_logger(__name__)

FORMAT = 'tofu.browser-bridge-relay/v1'
RECORD_NAME = 'browser-bridge-relay.json'
# ACKNOWLEDGEMENT_SECONDS: non-blocking verbs answer from in-memory state, so
# they reuse the existing private execution-control acknowledgement contract
# instead of a new timeout. Blocking verbs add their own declared wait.

_LENGTH = struct.Struct('!I')


class RelayUnavailable(RuntimeError):
    """The browser bridge owner (API process) gave no acknowledged answer."""


def record_path(project: str | os.PathLike) -> Path:
    return Path(project) / 'data' / RECORD_NAME


def _encode(value) -> bytes:
    data = json.dumps(value, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return _LENGTH.pack(len(data)) + data


def _read_exact(connection, count, deadline=None, cancel_event=None) -> bytes:
    from lib.browser.queue._dispatch import CANCEL_OBSERVATION_SECONDS
    chunks = bytearray()
    while len(chunks) < count:
        if cancel_event is not None and cancel_event.is_set():
            raise _Cancelled()
        wait = None
        if deadline is not None:
            wait = deadline - time.monotonic()
            if wait <= 0:
                raise TimeoutError('Browser bridge owner did not answer in time')
        if cancel_event is not None:
            wait = CANCEL_OBSERVATION_SECONDS if wait is None else min(
                wait, CANCEL_OBSERVATION_SECONDS)
        connection.settimeout(wait)
        try:
            chunk = connection.recv(count - len(chunks))
        except TimeoutError:
            continue
        if not chunk:
            raise OSError('Browser bridge relay connection closed')
        chunks.extend(chunk)
    return bytes(chunks)


class _Cancelled(Exception):
    pass


# ── Owner side (API process) ────────────────────────────────────────────

def _method_table():
    from lib.browser.queue import _dispatch, _registry

    def connected_clients(params, cancel):
        owner = params.get('owner_user_id')
        return _registry.get_connected_clients(
            owner_user_id=None if owner is None else str(owner))

    def extension_connected(params, cancel):
        return _registry.is_extension_connected(
            params['client_id'], owner_user_id=params['owner_user_id'])

    def client_owner(params, cancel):
        return _registry.client_owner_user_id(params['client_id'])

    def credit_hub_quota(params, cancel):
        return _registry.get_credit_hub_quota(owner_user_id=params['owner_user_id'])

    def wait_connected(params, cancel):
        return _registry.wait_for_connected_clients(
            owner_user_id=params['owner_user_id'], client_id=params.get('client_id'),
            timeout=float(params['timeout']), cancellation=cancel)

    def send_command(params, cancel):
        result, error = _dispatch.send_browser_command(
            params['cmd_type'], params.get('params') or {}, params['timeout'],
            client_id=params['client_id'], owner_user_id=params['owner_user_id'],
            cancel_event=cancel)
        return [result, error]

    def client_protocol(params, cancel):
        from lib.browser.protocol import client_protocol as local_client_protocol
        return local_client_protocol(params['client_id'])

    return {
        'client_protocol': client_protocol,
        'connected_clients': connected_clients,
        'extension_connected': extension_connected,
        'client_owner': client_owner,
        'credit_hub_quota': credit_hub_quota,
        'wait_connected': wait_connected,
        'send_command': send_command,
    }


class RelayServer:
    """Loopback listener owned by the API process; one thread per call.

    Calls borrow a thread for as long as the forwarded browser command waits,
    exactly like the local tool thread they replace. Close withdraws
    admission, cancels entered calls and joins them.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._token = secrets.token_hex(32)
        self._methods = _method_table()
        self._listener = None
        self._accept_thread = None
        self._lock = threading.Lock()
        self._calls: dict[threading.Thread, threading.Event] = {}
        self._closing = threading.Event()

    def start(self) -> dict:
        from lib.json_store import write_json_atomic
        if self._listener is not None:
            raise RuntimeError('Browser bridge relay already has a lifecycle')
        self._listener = socket.create_server(('127.0.0.1', 0))
        port = self._listener.getsockname()[1]
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name='browser-bridge-relay-accept', daemon=True)
        self._accept_thread.start()
        record = {'format': FORMAT, 'host': '127.0.0.1', 'port': port,
                  'pid': os.getpid(), 'token': self._token}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(str(self._path), record, mode=0o600)
        logger.info('[BrowserRelay] bridge owner listening on 127.0.0.1:%d', port)
        return {'host': '127.0.0.1', 'port': port}

    def _accept_loop(self):
        while not self._closing.is_set():
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            cancel = threading.Event()
            thread = threading.Thread(target=self._serve, args=(connection, cancel),
                                      name='browser-bridge-relay-call', daemon=True)
            with self._lock:
                if self._closing.is_set():
                    connection.close()
                    return
                self._calls[thread] = cancel
            thread.start()

    def _serve(self, connection, cancel):
        watcher = None
        try:
            deadline = time.monotonic() + ACKNOWLEDGEMENT_SECONDS
            length = _LENGTH.unpack(_read_exact(connection, _LENGTH.size, deadline))[0]
            request = json.loads(_read_exact(connection, length, deadline))
            if not (isinstance(request, dict) and request.get('format') == FORMAT
                    and isinstance(request.get('token'), str)
                    and secrets.compare_digest(request['token'], self._token)
                    and request.get('method') in self._methods
                    and isinstance(request.get('params'), dict)):
                response = {'format': FORMAT, 'result': None, 'error': 'relay_refused'}
            else:
                watcher = threading.Thread(target=self._watch_peer, args=(connection, cancel),
                                           name='browser-bridge-relay-peer', daemon=True)
                watcher.start()
                try:
                    result = self._methods[request['method']](request['params'], cancel)
                    response = {'format': FORMAT, 'result': result, 'error': None}
                except (KeyError, TypeError, ValueError) as exc:
                    response = {'format': FORMAT, 'result': None,
                                'error': f'relay_bad_request: {type(exc).__name__}'}
            connection.settimeout(ACKNOWLEDGEMENT_SECONDS)
            connection.sendall(_encode(response))
        except (OSError, ValueError, TypeError, struct.error) as exc:
            logger.info('[BrowserRelay] call ended without a response: %s', type(exc).__name__)
        except Exception:
            logger.exception('[BrowserRelay] call failed')
        finally:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
            if watcher is not None:
                watcher.join()
            with self._lock:
                self._calls.pop(threading.current_thread(), None)

    @staticmethod
    def _watch_peer(connection, cancel):
        # The caller writes nothing after its request: any readable byte, EOF
        # or error means it left, so its forwarded wait must be withdrawn.
        try:
            connection.settimeout(None)
            connection.recv(1)
        except OSError:
            pass
        cancel.set()

    def close(self, timeout: float | None = None) -> bool:
        self._closing.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        with self._lock:
            calls = dict(self._calls)
        for cancel in calls.values():
            cancel.set()
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in calls:
            thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        if self._accept_thread is not None:
            self._accept_thread.join(
                None if deadline is None else max(0.0, deadline - time.monotonic()))
        try:
            current = json.loads(self._path.read_text(encoding='utf-8'))
            if current.get('token') == self._token:
                self._path.unlink()
        except (OSError, ValueError):
            pass
        return not any(thread.is_alive() for thread in calls)


# ── Caller side (execution process) ─────────────────────────────────────

class RelayClient:
    """Forwards one queue verb per connection to the published bridge owner."""

    def __init__(self, path: Path):
        self._path = Path(path)

    def _endpoint(self) -> dict:
        try:
            record = json.loads(self._path.read_text(encoding='utf-8'))
        except FileNotFoundError as exc:
            raise RelayUnavailable(
                'The browser bridge owner (API process) has not published its relay') from exc
        except (OSError, ValueError) as exc:
            raise RelayUnavailable('The browser bridge relay record is unreadable') from exc
        if (not isinstance(record, dict) or record.get('format') != FORMAT
                or record.get('host') != '127.0.0.1'
                or type(record.get('port')) is not int or not 0 < record['port'] < 65536
                or not isinstance(record.get('token'), str)):
            raise RelayUnavailable('The browser bridge relay record is invalid')
        return record

    def call(self, method: str, params: dict, *, wait_seconds: float = 0.0,
             cancel_event=None):
        from tofu_db._loopback import create_connection
        endpoint = self._endpoint()
        deadline = time.monotonic() + max(0.0, float(wait_seconds)) + ACKNOWLEDGEMENT_SECONDS
        request = {'format': FORMAT, 'token': endpoint['token'],
                   'method': method, 'params': params}
        try:
            with create_connection((endpoint['host'], endpoint['port']),
                                   ACKNOWLEDGEMENT_SECONDS) as connection:
                connection.settimeout(ACKNOWLEDGEMENT_SECONDS)
                connection.sendall(_encode(request))
                length = _LENGTH.unpack(_read_exact(
                    connection, _LENGTH.size, deadline, cancel_event))[0]
                response = json.loads(_read_exact(connection, length, deadline, cancel_event))
        except _Cancelled:
            return _CANCELLED
        except (OSError, ValueError, TypeError, struct.error) as exc:
            raise RelayUnavailable(
                f'The browser bridge owner (API process) is unreachable: {type(exc).__name__}'
            ) from exc
        if (not isinstance(response, dict) or response.get('format') != FORMAT
                or response.get('error') is not None):
            raise RelayUnavailable(
                'The browser bridge owner refused the relay call: '
                f'{(response or {}).get("error") if isinstance(response, dict) else "malformed"}')
        return response.get('result')


_CANCELLED = object()
_selection_lock = threading.Lock()
_selected_client: RelayClient | None = None
_owned_server: RelayServer | None = None


def is_cancelled(value) -> bool:
    return value is _CANCELLED


def selected_relay() -> RelayClient | None:
    """The remote bridge owner selected for this process, or None (local owner)."""
    return _selected_client


def configure_bridge_owner(*, process_role: str, environ) -> str:
    """Select this process's browser bridge owner from its process role.

    ``api`` publishes the relay for the queue it owns; ``execution`` forwards
    to it; ``all`` (and embedders) keep the local in-process owner.
    """
    global _selected_client, _owned_server
    project = (environ.get('TOFU_PROJECT_PATH') or '').strip()
    with _selection_lock:
        if process_role == 'api':
            if not project:
                raise RuntimeError('A separate API needs its explicit project to publish '
                                   'the browser bridge relay')
            if _owned_server is None:
                server = RelayServer(record_path(project))
                server.start()
                _owned_server = server
            return 'owner'
        if process_role == 'execution':
            if not project:
                raise RuntimeError('Resident execution needs its explicit project to reach '
                                   'the browser bridge owner')
            _selected_client = RelayClient(record_path(project))
            return 'relay'
        return 'local'


def stop_bridge_owner(timeout: float = 2.0) -> bool:
    global _selected_client, _owned_server
    with _selection_lock:
        server, _owned_server = _owned_server, None
        _selected_client = None
    return True if server is None else server.close(timeout)
