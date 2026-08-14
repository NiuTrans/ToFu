"""Length-prefixed ``orjson`` framing for ``storage.v1``."""

from __future__ import annotations

import re
import socket
import struct
from typing import Any, Mapping

import orjson

from lib.storage.errors import StorageError


PROTOCOL_VERSION = 'storage.v1'
MAX_FRAME_BYTES = 8 * 1024 * 1024
_HEADER = struct.Struct('!I')
_OPERATION = re.compile(r'^[a-z][a-z0-9_.:-]{0,191}$')


def canonical_json(value: Any) -> bytes:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def validate_operation(operation: Any) -> str:
    value = str(operation or '')
    if not _OPERATION.fullmatch(value):
        raise StorageError(
            'database_protocol_error', 'Invalid storage operation name')
    return value


def encode_frame(message: Mapping[str, Any]) -> bytes:
    try:
        body = orjson.dumps(message)
    except (TypeError, orjson.JSONEncodeError) as exc:
        raise StorageError(
            'database_protocol_error', 'Storage frame is not serializable') from exc
    if not body or len(body) > MAX_FRAME_BYTES:
        raise StorageError(
            'database_protocol_error', 'Storage frame exceeds the size limit')
    return _HEADER.pack(len(body)) + body


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        block = sock.recv(remaining)
        if not block:
            raise StorageError(
                'database_protocol_error', 'Storage connection closed mid-frame')
        chunks.append(block)
        remaining -= len(block)
    return b''.join(chunks)


def recv_frame(sock: socket.socket) -> dict[str, Any]:
    raw_size = _recv_exact(sock, _HEADER.size)
    (size,) = _HEADER.unpack(raw_size)
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise StorageError(
            'database_protocol_error', 'Invalid storage frame length')
    try:
        value = orjson.loads(_recv_exact(sock, size))
    except orjson.JSONDecodeError as exc:
        raise StorageError(
            'database_protocol_error', 'Invalid storage frame JSON') from exc
    if not isinstance(value, dict):
        raise StorageError(
            'database_protocol_error', 'Storage frame must be an object')
    return value


def send_frame(sock: socket.socket, message: Mapping[str, Any]) -> None:
    sock.sendall(encode_frame(message))


__all__ = [
    'MAX_FRAME_BYTES', 'PROTOCOL_VERSION', 'canonical_json', 'encode_frame',
    'recv_frame', 'send_frame', 'validate_operation',
]
