"""Application-facing storage.v2 client over the embedded Tofu-DB transport.

This is :class:`lib.storage.v2_client.StorageV2Client` with its transport
session replaced by in-process calls into ``tofu_db.embedded.EmbeddedRuntime``
(the cffi handle over ``libtofu_db.so``; ``packages/tofu-db/src/embed.rs``).
The engine executes the SAME ``server::dispatch_one_request`` pipeline as the
framed daemon — identical admission, receipts, rotation parking, and
strict-mode WAL-barrier durability — so every classified-retry, deadline,
and committed-event rule in ``StorageV2Client._call``/``command`` applies
unchanged; this module only adapts the session/response boundary:

* one adapter session is a cheap facade over the shared runtime handle
  (Send+Sync, GIL released around calls), so the inherited bounded pool is a
  concurrency bound, not a connection amortizer;
* the daemon-shaped envelope ``{status, error_code, error_message,
  retryable, payload}`` maps onto the same ``StorageV2Response`` attributes
  the socket codec produces, including the embedded transport's own
  ``status == -1``/``embedded_transport_error`` leg, which classifies as
  retryable ``database_unavailable`` (the availability-failure posture a
  mid-shutdown caller sees on the socket path);
* there is NO socket: the loopback-only invariant is strengthened, and the
  runtime lifecycle (open/graceful ``close()``) belongs to the supervisor —
  ``close()`` here retires only the pool facade.
"""

from __future__ import annotations

from typing import Any

from lib.storage.errors import StorageError
from lib.storage.v2_client import StorageV2Client
from tofu_db.embedded import EmbeddedRuntime, EmbeddedTransportError


class _EmbeddedResponse:
    """``StorageV2Response``-shaped view of one daemon-shaped JSON envelope."""

    __slots__ = ('status', 'error_code', 'error_message', 'retryable', '_payload')

    def __init__(self, envelope: dict[str, Any]):
        self.status = int(envelope.get('status') or 0)
        self.error_code = envelope.get('error_code')
        self.error_message = envelope.get('error_message')
        self.retryable = bool(envelope.get('retryable'))
        self._payload = envelope.get('payload')

    def json(self) -> Any:
        return self._payload


class _EmbeddedSessionAdapter:
    """``StorageV2Session``-shaped facade over the shared runtime handle.

    The wire deadline travels inside the request (the engine answers
    ``deadline_elapsed`` exactly like the daemon), so ``set_timeout`` has no
    socket to configure. ``probe_liveness`` lets the inherited pool retire
    adapters after the runtime closes instead of lending out a dead handle.
    """

    __slots__ = ('_runtime',)

    def __init__(self, runtime: EmbeddedRuntime):
        self._runtime = runtime

    def set_timeout(self, _seconds: float) -> None:
        pass

    def probe_liveness(self) -> bool:
        return not self._runtime.closed

    def request(
        self,
        operation: str,
        body: dict[str, Any],
        *,
        command_id: str | None,
        deadline_unix_ms: int,
    ) -> _EmbeddedResponse:
        try:
            envelope = self._runtime.execute_json(
                operation,
                body,
                command_id=command_id,
                deadline_unix_ms=deadline_unix_ms,
            )
        except EmbeddedTransportError as exc:
            # Handle closed or response-framing failure: an availability
            # failure, not an engine verdict. Replay-safety stays with the
            # inherited _call discipline (commands fail fast without a
            # pre-dispatch proof; reads retry).
            raise StorageError(
                'database_unavailable',
                f'embedded storage authority is unavailable: {exc}',
                True,
                50,
            ) from exc
        response = _EmbeddedResponse(envelope)
        if response.status == -1:
            message = str(response.error_message or response.error_code)
            if 'exceeds the storage.v2 payload' in message:
                # The engine's request-size admission (embed.rs's
                # CappedPayloadWriter, embed_ffi.rs's envelope pre-check)
                # rejected the exact same bytes the framed daemon's
                # per-operation payload bound rejects. That is a
                # deterministic, caller-correctable boundary failure —
                # classifying it as retryable availability would re-issue
                # an undeliverable request until the deadline. Use the v1
                # sidecar's payload-cap code so every downstream
                # deterministic-overflow fence engages uniformly.
                raise StorageError(
                    'storage_payload_too_large',
                    f'embedded storage request rejected before dispatch: {message}',
                )
            # The FFI's own error leg (invalid request/handle failures the
            # engine could not dispatch). Classify like a transport failure:
            # retryable availability error, never an engine verdict.
            raise StorageError(
                'database_unavailable',
                'embedded storage transport failed: ' + message,
                True,
                50,
            )
        return response

    def close(self) -> None:
        # The supervisor owns the runtime lifecycle; the pool facade holds
        # no per-session resource.
        pass


class EmbeddedStorageV2Client(StorageV2Client):
    """``StorageV2Client`` whose sessions execute in-process.

    The ``(host, port)`` identity is inert — nothing is ever dialed; it only
    satisfies the inherited loopback-only constructor contract. The embedded
    runtime must outlive this client (the supervisor closes it after
    ``close()`` here drains the pool facade).
    """

    def __init__(
        self,
        runtime: EmbeddedRuntime,
        secret: bytes,
        *,
        owner_id: int,
        tenant_id: int | None,
        timeout: float = 5.0,
        read_attempts: int = 3,
        session_pool_size: int | None = None,
    ):
        self._embedded_runtime = runtime
        super().__init__(
            '127.0.0.1',
            0,
            secret,
            owner_id=owner_id,
            tenant_id=tenant_id,
            timeout=timeout,
            read_attempts=read_attempts,
            session_pool_size=session_pool_size,
        )

    def native_checkpoint_available(self, *, user_id: int) -> bool:
        """Read-only capability check on the selected authority; no fallback/open."""
        if type(user_id) is not int or user_id != self._owner_id:
            raise StorageError('authority_scope_mismatch', 'native checkpoint owner differs')
        with self._pool._condition:
            if self._pool._closed:
                raise StorageError('database_unavailable', 'native checkpoint client closed')
        return not self._embedded_runtime.closed and self._embedded_runtime.checkpoint_supported

    def native_checkpoint_control(self, operation: str, payload: dict[str, Any], *,
                                  user_id: int, deadline_unix_ms: int,
                                  response_bytes: int) -> dict[str, Any]:
        if not self.native_checkpoint_available(user_id=user_id):
            raise StorageError('database_unsupported', 'selected authority lacks native checkpoint control')
        try:
            return self._embedded_runtime.checkpoint_control(
                operation, payload, deadline_unix_ms=deadline_unix_ms,
                response_bytes=response_bytes)
        except EmbeddedTransportError as error:
            raise StorageError('database_unavailable', 'native checkpoint transport failed', True) from error

    @property
    def endpoint(self) -> tuple[str, int]:
        return ('embedded', 0)

    def _open_session(self, timeout: float | None = None) -> _EmbeddedSessionAdapter:
        return _EmbeddedSessionAdapter(self._embedded_runtime)


__all__ = ['EmbeddedStorageV2Client']
