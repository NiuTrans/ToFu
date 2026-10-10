"""Embedded in-process tofu-db transport (cffi over ``libtofu_db``).

Loads the same crate that builds the ``tofu-db`` daemon as a shared library
and executes storage.v2 operations through the embedded runtime
(``packages/tofu-db/src/embed.rs``), which mirrors ``serve`` minus the
listener: identical admission, read lanes, receipts, rotation parking, and
strict-mode WAL-barrier durability — with no loopback socket on the path.

This is a measurement/integration transport, not a second semantic
contract: ``payload`` is the exact JSON the wire client would send
(``lib.storage.v2_semantics.request_payload`` output), and the response is
the daemon's envelope ``{status, error_code, error_message, retryable,
payload}``.

The handle is Send+Sync; cffi releases the GIL around calls, so concurrent
threads execute in parallel exactly like daemon connections. One runtime
per authority open; :meth:`EmbeddedRuntime.close` performs the daemon's
graceful shutdown tail, and :meth:`EmbeddedRuntime.abandon` is the SIGKILL
approximation (drivers joined, WAL-window retirement skipped).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import cffi

_ffi = cffi.FFI()
_ffi.cdef(
    """
    void* tofudb_embed_open(const char*, uint64_t, int64_t, const char*,
                            uint8_t*, size_t, uint8_t*, size_t);
    intptr_t tofudb_embed_execute(void*, const uint8_t*, size_t, uint8_t*, size_t);
    uint32_t tofudb_embed_checkpoint_supported(void);
    intptr_t tofudb_embed_checkpoint_execute(void*, const uint8_t*, size_t, uint8_t*, size_t);
    intptr_t tofudb_embed_source_sha256(uint8_t*, size_t);
    void tofudb_embed_close(void*);
    void tofudb_embed_abandon(void*);
    """
)

try:
    import orjson as _fast_json
except ImportError:  # pragma: no cover - optional accelerator
    _fast_json = None

_RESPONSE_INITIAL_BYTES = 1 * 1024 * 1024
_RESPONSE_MAX_BYTES = 16 * 1024 * 1024
_ERROR_BUFFER_BYTES = 4096

# Response buffers are thread-local and grow-on-demand: a fresh
# ``_ffi.new("uint8_t[]", 1 MiB)`` per execute zero-fills the whole
# allocation (~23 µs on a 2026-09-23 profile), which dominated the
# fixed per-op cost of the embedded transport. Reuse keeps concurrent
# threads safe (each owns its buffer) and bounds retention at the
# largest response that thread has ever served (≤ _RESPONSE_MAX_BYTES).
_thread_state = threading.local()


def _response_buffer() -> tuple[Any, int]:
    buffer = getattr(_thread_state, "buffer", None)
    capacity = getattr(_thread_state, "capacity", 0)
    if buffer is None:
        buffer = _ffi.new("uint8_t[]", _RESPONSE_INITIAL_BYTES)
        _thread_state.buffer = buffer
        capacity = _RESPONSE_INITIAL_BYTES
        _thread_state.capacity = capacity
    return buffer, capacity


def _grow_response_buffer(needed: int) -> tuple[Any, int]:
    capacity = needed
    buffer = _ffi.new("uint8_t[]", capacity)
    _thread_state.buffer = buffer
    _thread_state.capacity = capacity
    return buffer, capacity


def _dumps(payload: Any) -> bytes:
    if _fast_json is not None:
        return _fast_json.dumps(payload)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _loads(data: bytes | memoryview) -> Any:
    if _fast_json is not None:
        return _fast_json.loads(data)
    return json.loads(bytes(data).decode("utf-8"))


def default_library_path(project_root: Path) -> Path:
    return (
        project_root
        / "packages/tofu-db/target/release/libtofu_db.so"
    )


class EmbeddedTransportError(Exception):
    """The transport itself failed (the engine's semantic error envelope
    always arrives as a normal response payload instead)."""


class EmbeddedRuntime:
    """One embedded authority handle; thread-safe, GIL-free calls."""

    def __init__(
        self,
        library_path: Path,
        data_dir: Path,
        *,
        owner_id: int,
        tenant_id: int | None,
        projection_dir: Path | None = None,
        secret: bytes,
    ):
        if not 32 <= len(secret) <= 256:
            raise ValueError("embedded secret must contain 32..256 bytes")
        self._handle = _ffi.NULL
        self._lifecycle = threading.Condition()
        self._active_calls = 0
        self._closing = False
        self._lib = _ffi.dlopen(str(library_path))
        error = _ffi.new("uint8_t[]", _ERROR_BUFFER_BYTES)
        secret_buffer = _ffi.new("uint8_t[]", len(secret))
        _ffi.memmove(secret_buffer, secret, len(secret))
        handle = self._lib.tofudb_embed_open(
            str(data_dir).encode("utf-8"),
            owner_id,
            tenant_id if tenant_id is not None else -1,
            (
                str(projection_dir).encode("utf-8")
                if projection_dir is not None
                else _ffi.NULL
            ),
            secret_buffer,
            len(secret),
            error,
            _ERROR_BUFFER_BYTES,
        )
        if handle == _ffi.NULL:
            message = bytes(_ffi.buffer(error)).split(b"\x00", 1)[0].decode(
                "utf-8", errors="replace"
            )
            raise EmbeddedTransportError(f"embedded open failed: {message}")
        self._handle = handle

    def build_identity(self) -> dict[str, str] | None:
        """Identity from the loaded library; old ABIs report unavailable."""
        try:
            identify = self._lib.tofudb_embed_source_sha256
        except AttributeError:
            return None
        out = _ffi.new("uint8_t[]", 64)
        if identify(out, 64) != 64:
            raise EmbeddedTransportError("invalid embedded build identity")
        return {"source_sha256": bytes(_ffi.buffer(out, 64)).decode("ascii")}

    @property
    def closed(self) -> bool:
        return self._handle == _ffi.NULL

    def execute_json(
        self,
        operation: str,
        payload: dict[str, Any] | None,
        *,
        command_id: str | None,
        deadline_unix_ms: int,
    ) -> dict[str, Any]:
        """Allow concurrent calls while retaining the native handle until each returns."""
        with self._lifecycle:
            if self._closing or self._handle == _ffi.NULL:
                raise EmbeddedTransportError("embedded runtime is closed")
            self._active_calls += 1
        try:
            return self._execute_json(
                operation, payload, command_id=command_id,
                deadline_unix_ms=deadline_unix_ms,
            )
        finally:
            with self._lifecycle:
                self._active_calls -= 1
                self._lifecycle.notify_all()

    def _execute_json(
        self,
        operation: str,
        payload: dict[str, Any] | None,
        *,
        command_id: str | None,
        deadline_unix_ms: int,
    ) -> dict[str, Any]:
        """One storage.v2 operation; returns the daemon response envelope."""
        if self._handle == _ffi.NULL:
            raise EmbeddedTransportError("embedded runtime is closed")
        request = _dumps(
            {
                "operation": operation,
                "payload": payload if payload is not None else {},
                "command_id": command_id,
                "deadline_unix_ms": deadline_unix_ms,
            }
        )
        out, capacity = _response_buffer()
        for _attempt in range(8):
            written = self._lib.tofudb_embed_execute(
                self._handle, request, len(request), out, capacity
            )
            if written >= 0:
                # Decode synchronously while this caller owns its thread-local
                # output. The returned JSON tree owns its strings and containers;
                # no view escapes into a later execute or native worker.
                return _loads(memoryview(_ffi.buffer(out, written)))
            needed = -written
            if needed <= 0 or needed > _RESPONSE_MAX_BYTES:
                raise EmbeddedTransportError(
                    f"embedded response framing failure ({written})"
                )
            out, capacity = _grow_response_buffer(needed)
        raise EmbeddedTransportError("embedded response exceeds 16 MiB")

    @property
    def checkpoint_supported(self) -> bool:
        """Read-only feature negotiation; old libraries remain usable for storage."""
        try:
            return self._lib.tofudb_embed_checkpoint_supported() == 1
        except AttributeError:
            return False

    def checkpoint_control(
        self, operation: str, payload: dict[str, Any], *,
        deadline_unix_ms: int, response_bytes: int,
    ) -> dict[str, Any]:
        """One owned journal command; buffer admission precedes lease effects.

        The caller supplies the snapshot bound plus generated headroom. This
        method never grows-and-replays a one-shot load or ambiguous mutation.
        Capability lifecycle and journal release remain explicit caller actions.
        """
        from .generated_checkpoint_control import FORMAT, OPERATIONS
        if (operation not in OPERATIONS or not isinstance(payload, dict)
                or type(response_bytes) is not int or not 0 < response_bytes <= _RESPONSE_MAX_BYTES
                or type(deadline_unix_ms) is not int or deadline_unix_ms <= 0):
            raise EmbeddedTransportError("invalid checkpoint control admission")
        with self._lifecycle:
            if self._closing or self._handle == _ffi.NULL:
                raise EmbeddedTransportError("embedded runtime is closed")
            if not self.checkpoint_supported:
                raise EmbeddedTransportError("native checkpoints are not enabled")
            self._active_calls += 1
        try:
            request = _dumps({"operation": operation, "payload": payload,
                              "deadline_unix_ms": deadline_unix_ms})
            out, capacity = _response_buffer()
            if capacity < response_bytes:
                out, capacity = _grow_response_buffer(response_bytes)
            written = self._lib.tofudb_embed_checkpoint_execute(
                self._handle, request, len(request), out, response_bytes)
            if written < 0 or written > response_bytes:
                raise EmbeddedTransportError("checkpoint response framing failed; command is not retried")
            result = _loads(memoryview(_ffi.buffer(out, written)))
            if (not isinstance(result, dict) or result.get('format') != FORMAT
                    or type(result.get('accepted')) is not bool):
                raise EmbeddedTransportError("checkpoint control refused: " + str(result.get('error_message', '') if isinstance(result, dict) else 'invalid response'))
            return result
        finally:
            with self._lifecycle:
                self._active_calls -= 1
                self._lifecycle.notify_all()

    def _stop(self, *, graceful: bool) -> None:
        with self._lifecycle:
            while self._closing:
                self._lifecycle.wait()
            if self._handle == _ffi.NULL:
                return
            self._closing = True
            while self._active_calls:
                self._lifecycle.wait()
            handle = self._handle
            self._handle = _ffi.NULL
        try:
            if graceful:
                self._lib.tofudb_embed_close(handle)
            else:
                self._lib.tofudb_embed_abandon(handle)
        finally:
            with self._lifecycle:
                self._closing = False
                self._lifecycle.notify_all()

    def close(self) -> None:
        self._stop(graceful=True)

    def abandon(self) -> None:
        """Wait for active callers, then stop without WAL-window retirement."""
        self._stop(graceful=False)

    def __enter__(self) -> "EmbeddedRuntime":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # best-effort; close()/abandon() are explicit
        try:
            self.close()
        except Exception:
            pass


__all__ = [
    "EmbeddedRuntime",
    "EmbeddedTransportError",
    "default_library_path",
]
