"""Length-prefixed ``orjson`` framing for ``storage.v1``."""

from __future__ import annotations

import math
import re
import socket
import struct
from typing import Any, Callable, Mapping

import orjson

from lib.storage.errors import StorageError


PROTOCOL_VERSION = 'storage.v1'
# Must fit the largest legitimate conversation document: the production
# authority holds transcripts up to ~46 MiB, and every checkpoint sync ships
# the full document. The historical 8 MiB cap silently rejected those writes
# (import + live sync). 64 MiB leaves headroom without dropping the anti-abuse
# bound (loopback + token-authenticated; rpc slots cap concurrency).
MAX_FRAME_BYTES = 64 * 1024 * 1024
_HEADER = struct.Struct('!I')
_OPERATION = re.compile(r'^[a-z][a-z0-9_.:-]{0,191}$')


def validate_finite_json_numbers(value: Any) -> None:
    """Reject values that JSON cannot represent without silent coercion."""
    pending = [value]
    visited_containers: set[int] = set()
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise StorageError(
                'database_protocol_error',
                'Storage value contains a non-finite JSON number',
            )
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in visited_containers:
                continue
            visited_containers.add(identity)
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in visited_containers:
                continue
            visited_containers.add(identity)
            pending.extend(item)


def _materialize_json_mappings(value: Any) -> Any:
    """Copy ``Mapping`` views (e.g. ``MappingProxyType`` from shared caches)
    into plain ``dict``; ``orjson`` rejects non-dict mappings. Clean
    containers pass through unchanged (copy-on-write) so full-transcript
    frames only pay the scan."""
    if isinstance(value, dict):
        rebuilt: dict[Any, Any] | None = None
        for key, item in value.items():
            converted = _materialize_json_mappings(item)
            if converted is not item:
                if rebuilt is None:
                    rebuilt = dict(value)
                rebuilt[key] = converted
        return value if rebuilt is None else rebuilt
    if isinstance(value, Mapping):
        return {key: _materialize_json_mappings(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        rebuilt_list: list[Any] | None = None
        for index, item in enumerate(value):
            converted = _materialize_json_mappings(item)
            if converted is not item:
                if rebuilt_list is None:
                    rebuilt_list = list(value)
                rebuilt_list[index] = converted
        return value if rebuilt_list is None else rebuilt_list
    return value


def canonical_json(value: Any) -> bytes:
    validate_finite_json_numbers(value)
    return orjson.dumps(_materialize_json_mappings(value), option=orjson.OPT_SORT_KEYS)


def validate_operation(operation: Any) -> str:
    value = str(operation or '')
    if not _OPERATION.fullmatch(value):
        raise StorageError(
            'database_protocol_error', 'Invalid storage operation name')
    return value


def encode_frame(message: Mapping[str, Any]) -> bytes:
    validate_finite_json_numbers(message)
    try:
        body = orjson.dumps(_materialize_json_mappings(message))
    except (TypeError, RecursionError, orjson.JSONEncodeError) as exc:
        raise StorageError(
            'database_protocol_error', 'Storage frame is not serializable') from exc
    if not body or len(body) > MAX_FRAME_BYTES:
        raise StorageError(
            'database_protocol_error', 'Storage frame exceeds the size limit')
    return _HEADER.pack(len(body)) + body


def _recv_exact(sock: socket.socket, size: int) -> bytearray:
    # Allocate the declared frame once. The previous chunk-list + join path
    # retained both copies at the join boundary (92 MiB for a fragmented
    # 46 MiB production-sized frame) and copied the entire payload again.
    buffer = bytearray(size)
    view = memoryview(buffer)
    offset = 0
    try:
        while offset < size:
            received = sock.recv_into(view[offset:], size - offset)
            if not received:
                # EOF before a complete frame means the PEER went away — an
                # overload close, a crash, or a supervised restart — which is
                # transport unavailability, not a framing violation.  The
                # previous 'database_protocol_error' classification was
                # non-retryable, so a transient blip killed idempotent reads
                # that the client retry loop exists to absorb (2026-08-19: 181
                # SSE streams died on a sidecar capacity/restart window).
                raise StorageError(
                    'database_unavailable',
                    'Storage connection closed mid-frame', True, 100)
            offset += received
    finally:
        view.release()
    return buffer


def recv_frame(
    sock: socket.socket,
    *,
    before_payload: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    raw_size = _recv_exact(sock, _HEADER.size)
    (size,) = _HEADER.unpack(raw_size)
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise StorageError(
            'database_protocol_error', 'Invalid storage frame length')
    if before_payload is not None:
        before_payload(size)
    try:
        value = orjson.loads(_recv_exact(sock, size))
    except orjson.JSONDecodeError as exc:
        raise StorageError(
            'database_protocol_error', 'Invalid storage frame JSON') from exc
    if not isinstance(value, dict):
        raise StorageError(
            'database_protocol_error', 'Storage frame must be an object')
    return value


def send_frame(sock: socket.socket, message: Mapping[str, Any]) -> int:
    frame = encode_frame(message)
    sock.sendall(frame)
    return len(frame)


__all__ = [
    'MAX_FRAME_BYTES', 'PROTOCOL_VERSION', 'canonical_json', 'encode_frame',
    'recv_frame', 'send_frame', 'validate_finite_json_numbers',
    'validate_operation',
]
