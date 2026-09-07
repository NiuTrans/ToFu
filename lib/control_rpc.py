"""Bounded JSON-RPC control plane carried by the existing push WebSocket.

Responsibility: validate an explicit read-method allowlist, isolate blocking
handlers from the ASGI loop, correlate responses, and retain global capacity
until timed-out filesystem work really exits.  Entry point:
``ControlRpcSession.receive``.  The durable HTTP APIs remain the compatibility
authority; this module is a transport optimization, never a generic HTTP
tunnel.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
import threading
import time
from typing import Any, Protocol

import orjson

from lib.log import get_logger
from runtime_guards import deployment_resource_default


logger = get_logger('tofu.control_rpc')

JSON_RPC_VERSION = '2.0'
CANCEL_METHOD = '$/cancelRequest'
REQUEST_MAX_BYTES = 16 * 1024
RESPONSE_MAX_BYTES = 2 * 1024 * 1024
IN_FLIGHT_PER_CONNECTION = 4
REQUESTS_PER_MINUTE = 120

INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
OVERLOADED = -32001
TIMED_OUT = -32002
RESPONSE_TOO_LARGE = -32003
DOMAIN_ERROR = -32010
REQUEST_CANCELLED = -32800


def _bounded_workers() -> int:
    fallback = deployment_resource_default('TOFU_CONTROL_RPC_WORKERS')
    try:
        value = int(os.environ.get('TOFU_CONTROL_RPC_WORKERS') or fallback)
    except (TypeError, ValueError, OverflowError):
        value = fallback
    return max(1, min(64, value))


GLOBAL_WORKERS = _bounded_workers()
_GLOBAL_SLOTS = threading.BoundedSemaphore(GLOBAL_WORKERS)
_METRICS_LOCK = threading.Lock()
_METRICS = {
    'accepted': 0,
    'completed': 0,
    'cancelled': 0,
    'timed_out': 0,
    'overloaded': 0,
    'failed': 0,
    'response_bytes': 0,
    'active_workers': 0,
}


def _metric(name: str, amount: int = 1) -> None:
    with _METRICS_LOCK:
        _METRICS[name] = int(_METRICS.get(name, 0)) + amount


def control_rpc_metrics() -> dict[str, int]:
    """Return a bounded, low-cardinality process snapshot."""
    with _METRICS_LOCK:
        return {
            **_METRICS,
            'global_workers': GLOBAL_WORKERS,
            'in_flight_per_connection': IN_FLIGHT_PER_CONNECTION,
        }


class _RpcClient(Protocol):
    def enqueue_rpc(self, frame: dict[str, Any]) -> bool: ...


@dataclass(frozen=True, slots=True)
class RpcContext:
    user_id: str
    request_id: str


@dataclass(frozen=True, slots=True)
class RpcMethod:
    handler: Callable[[RpcContext, Mapping[str, Any]], Any]
    timeout_seconds: float


class RpcInvalidParams(ValueError):
    pass


class RpcDomainError(RuntimeError):
    def __init__(self, message: str, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.data = dict(data or {})


def _project_browse(
    _context: RpcContext, params: Mapping[str, Any],
) -> dict[str, Any]:
    unknown = set(params) - {'path', 'showHidden'}
    if unknown:
        raise RpcInvalidParams(
            'Unknown project.browse parameter: ' + sorted(unknown)[0])
    raw_path = params.get('path')
    if raw_path is not None and not isinstance(raw_path, str):
        raise RpcInvalidParams('path must be a string')
    path = str(raw_path or '').strip()
    if len(path) > 4096 or '\0' in path:
        raise RpcInvalidParams('path exceeds the project browse limit')
    show_hidden = params.get('showHidden', False)
    if not isinstance(show_hidden, bool):
        raise RpcInvalidParams('showHidden must be a boolean')

    from lib.project_mod import browse_directory

    result = browse_directory(path or None, show_hidden=show_hidden)
    if result.get('error'):
        raise RpcDomainError(str(result['error']), result)
    return {'ok': True, **result}


METHODS: Mapping[str, RpcMethod] = {
    'project.browse': RpcMethod(_project_browse, 10.0),
}


def _valid_id(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return -(2 ** 53) < value < 2 ** 53
    return isinstance(value, str) and 0 < len(value) <= 128


class ControlRpcSession:
    """One authenticated socket's bounded request/cancellation lifecycle."""

    def __init__(
        self,
        client: _RpcClient,
        *,
        user_id: int | str,
        request_id: str = '',
        methods: Mapping[str, RpcMethod] | None = None,
    ) -> None:
        self.client = client
        self.context = RpcContext(str(user_id), str(request_id or ''))
        self.methods = METHODS if methods is None else methods
        self._pending: dict[tuple[type, Any], asyncio.Task] = {}
        self._workers: set[asyncio.Task] = set()
        self._recent: deque[float] = deque()
        self._closing = False

    @staticmethod
    def _key(request_id: Any) -> tuple[type, Any]:
        return type(request_id), request_id

    def _send(self, frame: dict[str, Any]) -> bool:
        if self._closing:
            return False
        if self.client.enqueue_rpc(frame):
            return True
        logger.warning(
            '[ControlRPC] reliable response lane saturated; disconnecting '
            'slow client user=%s rid=%s',
            self.context.user_id,
            self.context.request_id,
        )
        return False

    def _error(
        self,
        request_id: Any,
        code: int,
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> bool:
        error: dict[str, Any] = {'code': code, 'message': message}
        if data:
            error['data'] = dict(data)
        return self._send({
            'jsonrpc': JSON_RPC_VERSION,
            'id': request_id if _valid_id(request_id) else None,
            'error': error,
        })

    def _within_rate(self, now: float) -> bool:
        cutoff = now - 60.0
        while self._recent and self._recent[0] <= cutoff:
            self._recent.popleft()
        if len(self._recent) >= REQUESTS_PER_MINUTE:
            return False
        self._recent.append(now)
        return True

    def receive(self, raw: Any) -> bool:
        """Consume one JSON-RPC frame; return False for legacy push frames."""
        if not isinstance(raw, Mapping) or 'jsonrpc' not in raw:
            return False
        request_id = raw.get('id')
        if raw.get('jsonrpc') != JSON_RPC_VERSION:
            self._error(request_id, INVALID_REQUEST, 'Invalid JSON-RPC version')
            return True
        try:
            if len(orjson.dumps(raw)) > REQUEST_MAX_BYTES:
                self._error(request_id, INVALID_REQUEST, 'Request is too large')
                return True
        except (TypeError, orjson.JSONEncodeError):
            self._error(request_id, INVALID_REQUEST, 'Request is not serializable')
            return True

        method = raw.get('method')
        if method == CANCEL_METHOD and 'id' not in raw:
            params = raw.get('params')
            cancel_id = params.get('id') if isinstance(params, Mapping) else None
            task = self._pending.get(self._key(cancel_id)) \
                if _valid_id(cancel_id) else None
            if task is not None and not task.done():
                task.cancel()
            return True

        if not _valid_id(request_id) or not isinstance(method, str) \
                or not method or len(method) > 128:
            self._error(request_id, INVALID_REQUEST, 'Invalid JSON-RPC request')
            return True
        if not self._within_rate(time.monotonic()):
            _metric('overloaded')
            self._error(request_id, OVERLOADED, 'Control RPC rate limit reached', {
                'reason': 'connection_rate_limit', 'retryable': True,
            })
            return True
        key = self._key(request_id)
        if key in self._pending:
            self._error(request_id, INVALID_REQUEST, 'Duplicate request id')
            return True
        # A timed-out/cancelled waiter may leave its blocking filesystem
        # thread alive. Count both client-visible waiters and the real worker
        # set so one socket cannot consume the entire process budget by
        # starting a new call after each timeout. Admitted work owns its
        # connection token until it actually finishes.
        if (len(self._pending) >= IN_FLIGHT_PER_CONNECTION
                or len(self._workers) >= IN_FLIGHT_PER_CONNECTION):
            _metric('overloaded')
            self._error(request_id, OVERLOADED, 'Too many in-flight requests', {
                'reason': 'connection_capacity', 'retryable': True,
            })
            return True
        spec = self.methods.get(method)
        if spec is None:
            self._error(request_id, METHOD_NOT_FOUND, 'Method not found')
            return True
        params = raw.get('params', {})
        if not isinstance(params, Mapping):
            self._error(request_id, INVALID_PARAMS, 'params must be an object')
            return True
        if not _GLOBAL_SLOTS.acquire(blocking=False):
            _metric('overloaded')
            self._error(request_id, OVERLOADED, 'Control RPC is at capacity', {
                'reason': 'global_capacity', 'retryable': True,
            })
            return True

        _metric('accepted')
        _metric('active_workers')
        try:
            worker = asyncio.create_task(
                asyncio.to_thread(spec.handler, self.context, dict(params)),
                name=f'control-rpc-worker:{method}',
            )
        except BaseException:
            _GLOBAL_SLOTS.release()
            _metric('active_workers', -1)
            raise
        self._workers.add(worker)

        def worker_done(done: asyncio.Task) -> None:
            self._workers.discard(done)
            _GLOBAL_SLOTS.release()
            _metric('active_workers', -1)
            if not done.cancelled():
                try:
                    done.exception()
                except BaseException as error:
                    logger.debug(
                        'Control RPC worker completion inspection failed: %s',
                        error,
                    )

        worker.add_done_callback(worker_done)
        try:
            task = asyncio.create_task(
                self._execute(request_id, method, spec, worker),
                name=f'control-rpc:{method}',
            )
        except BaseException:
            worker.cancel()
            raise
        self._pending[key] = task

        def request_done(done: asyncio.Task) -> None:
            if self._pending.get(key) is done:
                self._pending.pop(key, None)
            if not done.cancelled():
                try:
                    done.exception()
                except BaseException as error:
                    logger.debug(
                        'Control RPC request completion inspection failed: %s',
                        error,
                    )

        task.add_done_callback(request_done)
        return True

    async def _execute(
        self,
        request_id: Any,
        method_name: str,
        spec: RpcMethod,
        worker: asyncio.Task,
    ) -> None:
        try:
            result = await asyncio.wait_for(
                asyncio.shield(worker), timeout=spec.timeout_seconds)
            encoded = orjson.dumps(result)
            if len(encoded) > RESPONSE_MAX_BYTES:
                _metric('failed')
                self._error(
                    request_id,
                    RESPONSE_TOO_LARGE,
                    'Control RPC response exceeds its byte budget',
                )
                return
            if self._send({
                'jsonrpc': JSON_RPC_VERSION,
                'id': request_id,
                'result': result,
            }):
                _metric('completed')
                _metric('response_bytes', len(encoded))
        except TimeoutError:
            # ``shield`` is load-bearing: the filesystem thread still owns the
            # global slot until its callback observes real completion.
            _metric('timed_out')
            self._error(request_id, TIMED_OUT, 'Control RPC timed out', {
                'retryable': True,
            })
        except asyncio.CancelledError:
            _metric('cancelled')
            if not self._closing:
                self._error(request_id, REQUEST_CANCELLED, 'Request cancelled')
        except RpcInvalidParams as exc:
            _metric('failed')
            self._error(request_id, INVALID_PARAMS, str(exc))
        except RpcDomainError as exc:
            _metric('failed')
            self._error(request_id, DOMAIN_ERROR, str(exc), exc.data)
        except BaseException as exc:
            _metric('failed')
            logger.warning(
                '[ControlRPC] method failed method=%s user=%s rid=%s type=%s',
                method_name,
                self.context.user_id,
                self.context.request_id,
                type(exc).__name__,
            )
            self._error(request_id, INTERNAL_ERROR, 'Control RPC failed')

    async def close(self) -> None:
        """Cancel socket-owned waiters without pretending threads were killed."""
        self._closing = True
        tasks = list(self._pending.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()


__all__ = [
    'CANCEL_METHOD',
    'ControlRpcSession',
    'DOMAIN_ERROR',
    'GLOBAL_WORKERS',
    'IN_FLIGHT_PER_CONNECTION',
    'METHODS',
    'METHOD_NOT_FOUND',
    'OVERLOADED',
    'REQUEST_CANCELLED',
    'RpcContext',
    'RpcMethod',
    'TIMED_OUT',
    'control_rpc_metrics',
]
