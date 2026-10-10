"""Standalone storage.v2 wire client for the Tofu-DB daemon.

Public transport owner for application adapters and independent harnesses.
No application imports, process launch, database paths, or automatic replay.
An optional orjson encoder accelerates compatible large JSON payloads.

Protocol source of truth: ``contracts/storage_v2.json`` and
``packages/tofu-db/src/protocol.rs`` / ``daemon.rs`` / ``server.rs``.

Frame layout: u32 big-endian body length, canonical flat MessagePack body,
u32 big-endian CRC32C (Castagnoli) over the body.  Message maps use strictly
increasing numeric field IDs; unknown fields are rejected by the peer.

Large semantic requests and successful responses use the bounded chunk streams
defined by the same contract; callers still observe one request/response pair.
"""

from __future__ import annotations

from .generated_v2 import COMMITTED_EVENTS_CONTRACT, MAX_COMMIT_NOTICE_KEYS

import hashlib
import json
import re
import select
import socket

from ._loopback import create_connection
import struct
from dataclasses import dataclass
from typing import Any, Iterable

from .generated_contract import STORAGE_SCHEMA_VERSION

try:
    import orjson as _fast_json
except ImportError:
    _fast_json = None


# ── Contract constants (contracts/storage_v2.json) ──────────────────────
PROTOCOL_VERSION = 2
KIND_HELLO = 1
KIND_REQUEST = 2
KIND_RESPONSE = 3
KIND_BLOB_CHUNK = 4
KIND_RESPONSE_CHUNK = 5

FIELD_KIND = 0
FIELD_PROTOCOL_VERSION = 1
FIELD_CORRELATION_ID = 2
FIELD_DEADLINE_UNIX_MS = 3
FIELD_OWNER_ID = 4
FIELD_TENANT_ID = 5
FIELD_COMMAND_ID = 6
FIELD_SCHEMA_ID = 7
FIELD_OPERATION = 8
FIELD_PAYLOAD = 9
FIELD_STATUS = 10
FIELD_ERROR_CODE = 11
FIELD_ERROR_MESSAGE = 12
FIELD_RETRYABLE = 13
FIELD_STREAM_ID = 14
FIELD_CHUNK_INDEX = 15
FIELD_FINAL = 16
FIELD_MINIMUM_VERSION = 17
FIELD_MAXIMUM_VERSION = 18
FIELD_SCHEMA_IDS = 19
FIELD_AUTH_TOKEN = 20
FIELD_TOTAL_PAYLOAD_BYTES = 21

MAX_FRAME_BODY_BYTES = 8 * 1024 * 1024
MAX_PAYLOAD_BYTES = 8_384_512
MAX_BLOB_CHUNK_BYTES = 1024 * 1024
MAX_STREAMED_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_STREAMED_CHUNKS = 64
AUTH_TOKEN_BYTES = 32

# packages/tofu-db/src/daemon.rs AUTH_TOKEN_DOMAIN
_AUTH_TOKEN_DOMAIN = b"tofu.storage.v2.auth-token\0"

class StorageV2Error(Exception):
    """One classified storage.v2 failure response."""

    def __init__(self, status: int, code: str, message: str, retryable: bool):
        super().__init__(f"storage.v2 {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable


class StorageV2ProtocolError(Exception):
    """The peer violated framing, checksum, or session rules."""


class StorageV2ConnectionClosedError(StorageV2ProtocolError):
    """The peer closed the connection at a frame boundary.

    Distinct from a mid-frame truncation: no byte of the awaited frame was
    consumed, so the local frame parser is still synchronized and the failure
    is a transport-lifetime event (idle reaping, daemon restart), never a
    framing violation. Callers may classify it as an availability failure and
    replay on a fresh session instead of a fail-fast protocol error.
    """


class StorageV2AdmissionRejected(Exception):
    """The local process response-byte budget rejected this response.

    A caller-supplied legacy admission hook explicitly declined a body.
    The built-in process buffer owner instead selects anonymous file overflow
    without queueing or policy rejection. This type preserves external hook
    compatibility and classified transport failure behavior.
    """


# The optional encoder accelerates large text payloads already common in the
# application. A bounded shape walk admits only values with byte-identical
# stdlib/orjson encodings. Floats, custom objects, non-string keys and excessive
# depth/width use the original encoder, including its original failure behavior.
_JSON_ACCELERATOR_MIN_TEXT_CHARACTERS = 8192
_JSON_ACCELERATOR_MAX_VALUES = 4096
_JSON_ACCELERATOR_MAX_DEPTH = 32
# Rejection-only probe: common metadata floats make the exact-number fast path
# ineligible. Inspect bounded response edges before allocating a decoded tree.
_JSON_NUMBER_PREFLIGHT_EDGE_BYTES = 8192
_JSON_FLOAT_MEMBER = re.compile(
    br'(?<!\\)"[ \t\r\n]*:[ \t\r\n]*-?(?:0|[1-9][0-9]*)'
    br'(?:\.[0-9]+(?:[eE][+-]?[0-9]+)?|[eE][+-]?[0-9]+)'
    br'(?=[ \t\r\n,}\]])'
)


def _has_float_member_near_response_edges(payload: bytes | bytearray) -> bool:
    """Decline a speculative decoder; never admit or validate a response.

    Escaped quotes in JSON text are skipped. A missed float, array value, or
    ambiguous slice still crosses the existing decoded-value compatibility
    check. A false positive only selects the original stdlib parser.
    """
    bound = _JSON_NUMBER_PREFLIGHT_EDGE_BYTES
    if _JSON_FLOAT_MEMBER.search(payload[:bound]) is not None:
        return True
    return len(payload) > bound and _JSON_FLOAT_MEMBER.search(payload[-bound:]) is not None


def _stdlib_payload_bytes(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _payload_bytes(payload: Any) -> bytes:
    if _fast_json is None or type(payload) is not dict:
        return _stdlib_payload_bytes(payload)
    remaining = _JSON_ACCELERATOR_MAX_VALUES
    text_characters = 0

    def compatible(value: Any, depth: int) -> bool:
        nonlocal remaining, text_characters
        remaining -= 1
        if remaining < 0 or depth > _JSON_ACCELERATOR_MAX_DEPTH:
            return False
        kind = type(value)
        if kind is str:
            text_characters += len(value)
            return True
        if value is None or kind is bool:
            return True
        if kind is int:
            return -(1 << 63) <= value < (1 << 64)
        if kind is dict:
            return all(type(key) is str and compatible(item, depth + 1)
                       for key, item in value.items())
        if kind is list or kind is tuple:
            return all(compatible(item, depth + 1) for item in value)
        return False

    if compatible(payload, 0) and text_characters >= _JSON_ACCELERATOR_MIN_TEXT_CHARACTERS:
        try:
            return _fast_json.dumps(payload)
        except _fast_json.JSONEncodeError:
            # In particular, preserve stdlib's handling of unpaired surrogates.
            pass
    return _stdlib_payload_bytes(payload)


def _response_json(payload: bytes | bytearray) -> Any:
    """Decode large responses without a full UTF-8 copy, preserving stdlib numbers.

    orjson may coerce integers beyond 64 bits to float. A bounded edge probe
    declines known float-bearing responses before speculative decoding. Any
    float, deep shape, or rejected encoding still takes the original parser;
    no user number is rounded
    merely to enable this optional optimization.
    """
    if (_fast_json is not None
            and len(payload) >= _JSON_ACCELERATOR_MIN_TEXT_CHARACTERS
            and not _has_float_member_near_response_edges(payload)):
        try:
            decoded = _fast_json.loads(payload)
        except _fast_json.JSONDecodeError:
            pass
        else:
            remaining = _JSON_ACCELERATOR_MAX_VALUES

            def compatible(value: Any, depth: int) -> bool:
                nonlocal remaining
                remaining -= 1
                if remaining < 0 or depth > _JSON_ACCELERATOR_MAX_DEPTH:
                    return False
                kind = type(value)
                if kind is float:
                    return False
                if kind is dict:
                    return all(compatible(item, depth + 1) for item in value.values())
                if kind is list:
                    return all(compatible(item, depth + 1) for item in value)
                return True

            if compatible(decoded, 0):
                return decoded
            del decoded  # Do not retain two large trees during the fallback parse.
    return json.loads(payload.decode("utf-8"))


# ── CRC32C (Castagnoli), slicing-by-8, pure Python ─────────────────────
def _build_crc32c_table() -> tuple[tuple[int, ...], ...]:
    polynomial = 0x82F63B78
    table = []
    row = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ polynomial if crc & 1 else crc >> 1
        row.append(crc)
    table.append(row)
    for _ in range(7):
        previous = table[-1]
        table.append([(value >> 8) ^ table[0][value & 0xFF] for value in previous])
    return tuple(tuple(row) for row in table)


_CRC32C_TABLE = _build_crc32c_table()


def crc32c(data: bytes, crc: int = 0) -> int:
    # Slicing-by-8: identical result to the per-byte table loop, ~8x fewer
    # Python-level steps. Frame checksums dominate large-response reads.
    crc ^= 0xFFFFFFFF
    length = len(data)
    offset = 0
    while offset + 8 <= length:
        word = int.from_bytes(data[offset:offset + 8], 'little') ^ crc
        crc = (
            _CRC32C_TABLE[7][word & 0xFF]
            ^ _CRC32C_TABLE[6][(word >> 8) & 0xFF]
            ^ _CRC32C_TABLE[5][(word >> 16) & 0xFF]
            ^ _CRC32C_TABLE[4][(word >> 24) & 0xFF]
            ^ _CRC32C_TABLE[3][(word >> 32) & 0xFF]
            ^ _CRC32C_TABLE[2][(word >> 40) & 0xFF]
            ^ _CRC32C_TABLE[1][(word >> 48) & 0xFF]
            ^ _CRC32C_TABLE[0][word >> 56]
        )
        offset += 8
    while offset < length:
        crc = (crc >> 8) ^ _CRC32C_TABLE[0][(crc ^ data[offset]) & 0xFF]
        offset += 1
    return crc ^ 0xFFFFFFFF


# Optional compiled accelerator: frame checksums dominate large-response
# reads (a sidebar page is tens of KiB; the pure-Python loop costs ~100ns
# per byte). A lazy, bounded worker may build a tiny CRC32C extension
# into an owner-checked, ABI-keyed cache and publish it atomically; the
# pure-Python path above remains the verified fallback, so this module still
# has no hard build dependency. The extension's result is checked against
# the reference implementation before adoption.
_NATIVE_CRC32C_SOURCE = r'''
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <string.h>

static uint32_t crc32c_table[8][256];
static int crc32c_table_ready = 0;

static void crc32c_build_table(void) {
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t crc = i;
        for (int k = 0; k < 8; k++) {
            crc = (crc & 1) ? (crc >> 1) ^ 0x82F63B78u : crc >> 1;
        }
        crc32c_table[0][i] = crc;
    }
    for (int k = 1; k < 8; k++) {
        for (uint32_t i = 0; i < 256; i++) {
            crc32c_table[k][i] =
                (crc32c_table[k - 1][i] >> 8)
                ^ crc32c_table[0][crc32c_table[k - 1][i] & 0xFF];
        }
    }
    crc32c_table_ready = 1;
}

static PyObject *crc32c_native(PyObject *self, PyObject *args) {
    Py_buffer view;
    unsigned int crc = 0;
    if (!PyArg_ParseTuple(args, "y*|I:crc32c", &view, &crc)) {
        return NULL;
    }
    if (!crc32c_table_ready) {
        crc32c_build_table();
    }
    const unsigned char *p = (const unsigned char *)view.buf;
    Py_ssize_t n = view.len;
    Py_ssize_t i = 0;
    crc ^= 0xFFFFFFFFu;
    while (i + 8 <= n) {
        uint64_t word;
        memcpy(&word, p + i, 8);
        word ^= crc;
        crc = crc32c_table[7][word & 0xFF]
            ^ crc32c_table[6][(word >> 8) & 0xFF]
            ^ crc32c_table[5][(word >> 16) & 0xFF]
            ^ crc32c_table[4][(word >> 24) & 0xFF]
            ^ crc32c_table[3][(word >> 32) & 0xFF]
            ^ crc32c_table[2][(word >> 40) & 0xFF]
            ^ crc32c_table[1][(word >> 48) & 0xFF]
            ^ crc32c_table[0][word >> 56];
        i += 8;
    }
    while (i < n) {
        crc = (crc >> 8) ^ crc32c_table[0][(crc ^ p[i]) & 0xFF];
        i++;
    }
    PyBuffer_Release(&view);
    return PyLong_FromUnsignedLong(crc ^ 0xFFFFFFFFu);
}

static PyMethodDef crc32c_methods[] = {
    {"crc32c", crc32c_native, METH_VARARGS, "CRC32C (Castagnoli)."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef crc32c_module = {
    PyModuleDef_HEAD_INIT, "_tofudb_crc32c_native", NULL, -1, crc32c_methods
};

PyMODINIT_FUNC PyInit__tofudb_crc32c_native(void) {
    return PyModule_Create(&crc32c_module);
}
'''


_crc32c_fallback = crc32c
_NATIVE_CRC32C = None


def crc32c(data: bytes, crc: int = 0) -> int:
    """Use a verified accelerator when ready; compilation never blocks callers."""
    global _NATIVE_CRC32C
    if _NATIVE_CRC32C is None:
        from ._crc32c_accelerator import accelerator
        _NATIVE_CRC32C = accelerator(_NATIVE_CRC32C_SOURCE, _crc32c_fallback)
    implementation = _NATIVE_CRC32C or _crc32c_fallback
    return implementation(data, crc)


def derive_auth_token(secret: bytes) -> bytes:
    """Derive the 32-byte Hello capability from the shared ASCII secret.

    Mirrors ``derive_auth_token`` in packages/tofu-db/src/daemon.rs; the
    daemon additionally requires 32..256 ASCII bytes for the secret itself.
    """
    if not 32 <= len(secret) <= 256:
        raise ValueError("storage.v2 secret must contain 32..256 bytes")
    digest = hashlib.sha256()
    digest.update(_AUTH_TOKEN_DOMAIN)
    digest.update(secret)
    return digest.digest()


# ── Canonical MessagePack subset encoder ────────────────────────────────
def _put_uint(out: bytearray, value: int) -> None:
    if value < 0:
        raise ValueError("storage.v2 only encodes non-negative integers")
    if value <= 0x7F:
        out.append(value)
    elif value <= 0xFF:
        out += b"\xcc" + bytes((value,))
    elif value <= 0xFFFF:
        out += b"\xcd" + struct.pack(">H", value)
    elif value <= 0xFFFFFFFF:
        out += b"\xce" + struct.pack(">I", value)
    else:
        out += b"\xcf" + struct.pack(">Q", value)


def _put_binary(out: bytearray, value: bytes) -> None:
    length = len(value)
    if length <= 0xFF:
        out += b"\xc4" + bytes((length,))
    elif length <= 0xFFFF:
        out += b"\xc5" + struct.pack(">H", length)
    else:
        out += b"\xc6" + struct.pack(">I", length)
    out += value


def _put_string(out: bytearray, value: str) -> None:
    encoded = value.encode("utf-8")
    length = len(encoded)
    if length <= 31:
        out.append(0xA0 | length)
    elif length <= 0xFF:
        out += b"\xd9" + bytes((length,))
    elif length <= 0xFFFF:
        out += b"\xda" + struct.pack(">H", length)
    else:
        out += b"\xdb" + struct.pack(">I", length)
    out += encoded


def _encode_field_value(out: bytearray, value: Any) -> None:
    if value is None:
        out.append(0xC0)
    elif isinstance(value, bool):
        out.append(0xC3 if value else 0xC2)
    elif isinstance(value, int):
        _put_uint(out, value)
    elif isinstance(value, str):
        _put_string(out, value)
    elif isinstance(value, (bytes, bytearray)):
        _put_binary(out, bytes(value))
    elif isinstance(value, (list, tuple)):
        length = len(value)
        if length <= 15:
            out.append(0x90 | length)
        else:
            out.append(0xDC)  # array 16; matches the Rust encoder
            out += struct.pack(">H", length)
        for item in value:
            _encode_field_value(out, item)
    else:
        raise TypeError(f"unsupported storage.v2 field value: {type(value)!r}")


def encode_message(fields: dict[int, Any]) -> bytes:
    """Encode one flat field map with strictly increasing numeric IDs."""
    ordered = sorted(fields)
    if ordered != list(fields):
        raise ValueError("storage.v2 fields must be added in increasing ID order")
    out = bytearray()
    out.append(0x80 | len(fields))
    for field in ordered:
        _put_uint(out, field)
        _encode_field_value(out, fields[field])
    if len(out) > MAX_FRAME_BODY_BYTES:
        raise ValueError("storage.v2 frame body exceeds 8 MiB")
    return bytes(out)


# ── MessagePack subset decoder (responses) ──────────────────────────────
class _Decoder:
    def __init__(self, data: bytes):
        self._data = data
        self._offset = 0

    def _take(self, count: int) -> bytes:
        chunk = self._data[self._offset : self._offset + count]
        if len(chunk) != count:
            raise StorageV2ProtocolError("truncated storage.v2 frame body")
        self._offset += count
        return chunk

    def value(self) -> Any:
        (marker,) = self._take(1)
        if marker <= 0x7F:
            return marker
        if 0x80 <= marker <= 0x8F:
            return self._map(marker & 0x0F)
        if 0x90 <= marker <= 0x9F:
            return [self.value() for _ in range(marker & 0x0F)]
        if 0xA0 <= marker <= 0xBF:
            return self._take(marker & 0x1F).decode("utf-8")
        if marker == 0xC0:
            return None
        if marker == 0xC2:
            return False
        if marker == 0xC3:
            return True
        if marker == 0xC4:
            return self._take(self._take(1)[0])
        if marker == 0xC5:
            return self._take(struct.unpack(">H", self._take(2))[0])
        if marker == 0xC6:
            return self._take(struct.unpack(">I", self._take(4))[0])
        if marker == 0xCC:
            return self._take(1)[0]
        if marker == 0xCD:
            return struct.unpack(">H", self._take(2))[0]
        if marker == 0xCE:
            return struct.unpack(">I", self._take(4))[0]
        if marker == 0xCF:
            return struct.unpack(">Q", self._take(8))[0]
        if marker == 0xD9:
            return self._take(self._take(1)[0]).decode("utf-8")
        if marker == 0xDA:
            return self._take(struct.unpack(">H", self._take(2))[0]).decode("utf-8")
        if marker == 0xDB:
            return self._take(struct.unpack(">I", self._take(4))[0]).decode("utf-8")
        if marker == 0xDC:
            return [self.value() for _ in range(struct.unpack(">H", self._take(2))[0])]
        raise StorageV2ProtocolError(f"unsupported storage.v2 marker 0x{marker:02x}")

    def _map(self, length: int) -> dict[int, Any]:
        result: dict[int, Any] = {}
        for _ in range(length):
            key = self.value()
            result[key] = self.value()
        return result

    def finished(self) -> bool:
        return self._offset == len(self._data)


def decode_message(body: bytes) -> dict[int, Any]:
    decoder = _Decoder(body)
    value = decoder.value()
    if not isinstance(value, dict) or not decoder.finished():
        raise StorageV2ProtocolError("storage.v2 frame body is not one flat map")
    return value


def pack_frame(body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + body + struct.pack(">I", crc32c(body))


def _read_exact(sock: socket.socket, count: int, *, frame_start: bool = False) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        piece = sock.recv(count - len(chunks))
        if not piece:
            if frame_start and not chunks:
                raise StorageV2ConnectionClosedError(
                    "storage.v2 peer closed the connection at a frame boundary")
            raise StorageV2ProtocolError("storage.v2 peer closed mid-frame")
        chunks += piece
    return bytes(chunks)


def recv_frame(
    sock: socket.socket,
    *,
    before_payload: Any = None,
) -> dict[int, Any]:
    (length,) = struct.unpack(">I", _read_exact(sock, 4, frame_start=True))
    if length == 0 or length > MAX_FRAME_BODY_BYTES:
        raise StorageV2ProtocolError(f"invalid storage.v2 body length {length}")
    reservation = before_payload(length) if before_payload is not None else None
    from ._frame_body import received_frame_body
    with received_frame_body(lambda count: _read_exact(sock, count), length, reservation) as body:
        (checksum,) = struct.unpack(">I", _read_exact(sock, 4))
        with memoryview(body) as view:
            actual_checksum = crc32c(view)
        if checksum != actual_checksum:
            raise StorageV2ProtocolError("storage.v2 frame checksum mismatch")
        return decode_message(body)


@dataclass
class StorageV2Response:
    """One decoded response, success or classified failure."""

    status: int
    schema_id: int
    payload: bytes | bytearray
    error_code: str | None
    error_message: str | None
    retryable: bool

    def json(self) -> Any:
        if not self.payload:
            return None
        return _response_json(self.payload)

    def value(self) -> Any:
        """Return the semantic result, unwrapping a private commit envelope.

        ``json()`` continues to expose the exact decoded wire payload for
        clients that deliver the bounded post-commit notifications.
        """
        result = self.json()
        if not isinstance(result, dict) or '_storageCommitContract' not in result:
            return result
        events = result.get('events')
        if (result.get('_storageCommitContract') != COMMITTED_EVENTS_CONTRACT
                or 'value' not in result or not isinstance(events, list)
                or len(events) > MAX_COMMIT_NOTICE_KEYS
                or not all(isinstance(event, dict) for event in events)):
            raise StorageV2ProtocolError('Malformed committed-event envelope')
        return result['value']

    def raise_for_error(self) -> None:
        if self.status != 0:
            raise StorageV2Error(
                self.status,
                self.error_code or "unknown",
                self.error_message or "",
                self.retryable,
            )


class StorageV2Session:
    """One authenticated storage.v2 connection (Hello exactly once)."""

    def __init__(
        self,
        host: str,
        port: int,
        secret: bytes,
        *,
        owner_id: int,
        tenant_id: int | None,
        schema_ids: Iterable[int] = (STORAGE_SCHEMA_VERSION,),
        timeout: float = 10.0,
        correlation_seed: int = 1,
        response_admission: Any = None,
    ):
        if (type(owner_id) is not int or not 0 < owner_id < 2**64
                or (tenant_id is not None and (
                    type(tenant_id) is not int or not 0 < tenant_id < 2**64))):
            raise ValueError("storage.v2 owner/tenant IDs must be positive uint64 integers")
        token = derive_auth_token(secret)
        schemas = sorted(set(schema_ids))
        if not schemas or any(type(value) is not int or not 0 < value < 2**32 for value in schemas):
            raise ValueError("storage.v2 requires positive schema IDs")
        self._owner_id = owner_id
        self._tenant_id = tenant_id
        self._counter = correlation_seed & 0xFFFFFFFF
        self._timeout = timeout
        # Optional duck-typed process response-byte budget (acquire/release/
        # observe_frame, e.g. lib/storage/frame_admission.py). ``None`` keeps
        # the historical standalone-transport behavior with per-response
        # caps only; application clients inject the shared process budget.
        self._response_admission = response_admission
        correlation = self._next_correlation()
        hello = encode_message({
            FIELD_KIND: KIND_HELLO,
            FIELD_CORRELATION_ID: correlation,
            FIELD_MINIMUM_VERSION: PROTOCOL_VERSION,
            FIELD_MAXIMUM_VERSION: PROTOCOL_VERSION,
            FIELD_SCHEMA_IDS: schemas,
            FIELD_AUTH_TOKEN: token,
        })
        self._sock = create_connection((host, port), timeout=timeout)
        try:
            # RPC frames/chunks must not wait for an ACK of the preceding
            # TCP segment. Nagle + delayed ACK otherwise adds ~40 ms stalls
            # when a request ends with a short segment under CPU contention.
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sock.settimeout(timeout)
            self._sock.sendall(pack_frame(hello))
            acknowledgement = recv_frame(self._sock)
            if (acknowledgement.get(FIELD_KIND) != KIND_RESPONSE
                    or acknowledgement.get(FIELD_CORRELATION_ID) != correlation):
                raise StorageV2ProtocolError("storage.v2 hello acknowledgement mismatch")
            status = acknowledgement.get(FIELD_STATUS, -1)
            if status != 0:
                raise StorageV2Error(
                    status,
                    acknowledgement.get(FIELD_ERROR_CODE) or "unknown",
                    acknowledgement.get(FIELD_ERROR_MESSAGE) or "",
                    bool(acknowledgement.get(FIELD_RETRYABLE)),
                )
            selected = acknowledgement.get(FIELD_SCHEMA_ID)
            if type(selected) is not int or selected not in schemas:
                raise StorageV2ProtocolError("storage.v2 hello selected an unoffered schema")
            self.schema_id = selected
        except BaseException:
            self.close()
            raise

    def set_timeout(self, timeout: float) -> None:
        """Adjust the socket operation timeout for subsequent requests.

        Pooled sessions are shared across callers with different per-attempt
        budgets: the wire deadline inside each request stays the daemon-side
        bound, while this socket timeout is the client-side transport bound
        for a wedged peer. Callers set it before every request on a reused
        session; it never changes protocol semantics.
        """
        self._timeout = max(0.001, float(timeout))
        self._sock.settimeout(self._timeout)

    def probe_liveness(self) -> bool:
        """Check idle receive readiness without changing socket configuration.

        EOF, reset and unsolicited bytes all make an idle session unusable:
        the daemon never speaks first. A zero-time readiness check detects
        each without receiving bytes or toggling blocking/timeout modes.
        poll avoids select's descriptor-number ceiling where available;
        platforms without poll use select on this one owned socket.
        """
        try:
            if self._sock.fileno() < 0:
                return False
            poll_factory = getattr(select, "poll", None)
            if poll_factory is not None:
                poller = poll_factory()
                poller.register(self._sock, select.POLLIN)
                return not poller.poll(0)
            readable, _, errors = select.select([self._sock], [], [self._sock], 0)
            return not readable and not errors
        except (OSError, ValueError):
            return False

    def _next_correlation(self) -> bytes:
        self._counter = (self._counter + 1) & 0xFFFFFFFF
        if self._counter == 0:
            self._counter = 1
        # Exactly 16 bytes, never all-zero (protocol validates both).
        return b"tofu-diff-" + struct.pack(">I", self._counter) + b"\x01\x01"

    def request(
        self,
        operation: str,
        payload: dict[str, Any] | None,
        *,
        command_id: str | None,
        deadline_unix_ms: int,
    ) -> StorageV2Response:
        correlation = self._next_correlation()
        payload_bytes = _payload_bytes(payload if payload is not None else {})
        if len(payload_bytes) > MAX_PAYLOAD_BYTES:
            if len(payload_bytes) > MAX_STREAMED_PAYLOAD_BYTES:
                raise ValueError("storage.v2 request payload exceeds its operation bound")
            stream_id = correlation
            chunk_count = (
                len(payload_bytes) + MAX_BLOB_CHUNK_BYTES - 1
            ) // MAX_BLOB_CHUNK_BYTES
            if chunk_count > MAX_STREAMED_CHUNKS:
                raise ValueError("storage.v2 request requires too many chunks")
            for index, offset in enumerate(
                range(0, len(payload_bytes), MAX_BLOB_CHUNK_BYTES)
            ):
                chunk = payload_bytes[offset : offset + MAX_BLOB_CHUNK_BYTES]
                chunk_body = encode_message(
                    {
                        FIELD_KIND: KIND_BLOB_CHUNK,
                        FIELD_PROTOCOL_VERSION: PROTOCOL_VERSION,
                        FIELD_CORRELATION_ID: correlation,
                        FIELD_DEADLINE_UNIX_MS: deadline_unix_ms,
                        FIELD_OWNER_ID: self._owner_id,
                        FIELD_TENANT_ID: self._tenant_id,
                        FIELD_COMMAND_ID: command_id,
                        FIELD_SCHEMA_ID: self.schema_id,
                        FIELD_PAYLOAD: chunk,
                        FIELD_STREAM_ID: stream_id,
                        FIELD_CHUNK_INDEX: index,
                        FIELD_FINAL: index + 1 == chunk_count,
                        FIELD_TOTAL_PAYLOAD_BYTES: len(payload_bytes),
                    }
                )
                self._sock.sendall(pack_frame(chunk_body))
            wire_payload = b""
        else:
            wire_payload = payload_bytes
        body = encode_message(
            {
                FIELD_KIND: KIND_REQUEST,
                FIELD_PROTOCOL_VERSION: PROTOCOL_VERSION,
                FIELD_CORRELATION_ID: correlation,
                FIELD_DEADLINE_UNIX_MS: deadline_unix_ms,
                FIELD_OWNER_ID: self._owner_id,
                FIELD_TENANT_ID: self._tenant_id,
                FIELD_COMMAND_ID: command_id,
                FIELD_SCHEMA_ID: self.schema_id,
                FIELD_OPERATION: operation,
                FIELD_PAYLOAD: wire_payload,
            }
        )
        self._sock.sendall(pack_frame(body))
        response_payload = bytearray()
        response_stream_id: bytes | None = None
        response_final = False
        response_chunks = 0
        admission = self._response_admission
        reserved_bytes = 0
        reservations = []

        def _admit_response_frame(frame_body_bytes: int) -> Any:
            nonlocal reserved_bytes
            if admission is None:
                return
            reserve_frame = getattr(admission, 'reserve_frame', None)
            if reserve_frame is not None:
                reservation = reserve_frame(frame_body_bytes)
                reservations.append(reservation)
                admission.observe_frame("response", frame_body_bytes)
                return reservation
            acquired = admission.acquire(
                frame_body_bytes,
                # Never a silent wait: the budget wait is bounded well
                # inside the wire deadline, then classified as a rejection.
                timeout_s=min(5.0, max(0.05, self._timeout)),
                # A command may already be durable when its response
                # arrives; drain completed command responses first, exactly
                # like the storage.v1 frame admission.
                response_priority=command_id is not None,
            )
            if not acquired:
                raise StorageV2AdmissionRejected(
                    "storage.v2 response byte budget exhausted "
                    f"({frame_body_bytes} bytes declined)")
            reserved_bytes += frame_body_bytes
            admission.observe_frame("response", frame_body_bytes)

        try:
            while True:
                message = recv_frame(
                    self._sock,
                    before_payload=_admit_response_frame if admission is not None else None)
                if message.get(FIELD_CORRELATION_ID) != correlation:
                    raise StorageV2ProtocolError("storage.v2 response correlation mismatch")
                if int(message.get(FIELD_SCHEMA_ID, 0)) != self.schema_id:
                    raise StorageV2ProtocolError("storage.v2 response schema mismatch")
                if message.get(FIELD_KIND) == KIND_RESPONSE_CHUNK:
                    if set(message) != {
                        FIELD_KIND,
                        FIELD_PROTOCOL_VERSION,
                        FIELD_CORRELATION_ID,
                        FIELD_SCHEMA_ID,
                        FIELD_PAYLOAD,
                        FIELD_STREAM_ID,
                        FIELD_CHUNK_INDEX,
                        FIELD_FINAL,
                    } or message.get(FIELD_PROTOCOL_VERSION) != PROTOCOL_VERSION:
                        raise StorageV2ProtocolError("invalid storage.v2 response chunk fields")
                    chunk = message.get(FIELD_PAYLOAD)
                    stream_id = message.get(FIELD_STREAM_ID)
                    if (
                        response_final
                        or not isinstance(chunk, bytes)
                        or not 0 < len(chunk) <= MAX_BLOB_CHUNK_BYTES
                        or not isinstance(stream_id, bytes)
                        or len(stream_id) != 16
                        or not any(stream_id)
                        or int(message.get(FIELD_CHUNK_INDEX, -1)) != response_chunks
                        or response_chunks >= MAX_STREAMED_CHUNKS
                        or len(response_payload) + len(chunk) > MAX_STREAMED_PAYLOAD_BYTES
                    ):
                        raise StorageV2ProtocolError("invalid storage.v2 response chunk sequence")
                    if response_stream_id is None:
                        response_stream_id = stream_id
                    elif stream_id != response_stream_id:
                        raise StorageV2ProtocolError("storage.v2 response stream identity changed")
                    response_payload += chunk
                    response_chunks += 1
                    response_final = bool(message.get(FIELD_FINAL))
                    continue
                if message.get(FIELD_KIND) != KIND_RESPONSE:
                    raise StorageV2ProtocolError("storage.v2 request answered by non-response")
                if set(message) != {
                    FIELD_KIND,
                    FIELD_PROTOCOL_VERSION,
                    FIELD_CORRELATION_ID,
                    FIELD_SCHEMA_ID,
                    FIELD_PAYLOAD,
                    FIELD_STATUS,
                    FIELD_ERROR_CODE,
                    FIELD_ERROR_MESSAGE,
                    FIELD_RETRYABLE,
                } or message.get(FIELD_PROTOCOL_VERSION) != PROTOCOL_VERSION:
                    raise StorageV2ProtocolError("invalid storage.v2 response fields")
                if response_chunks:
                    if (
                        not response_final
                        or int(message.get(FIELD_STATUS, -1)) != 0
                        or message.get(FIELD_PAYLOAD) not in (b"", None)
                        or message.get(FIELD_ERROR_CODE) is not None
                        or message.get(FIELD_ERROR_MESSAGE) is not None
                        or bool(message.get(FIELD_RETRYABLE))
                    ):
                        raise StorageV2ProtocolError("invalid storage.v2 streamed response terminator")
                    message[FIELD_PAYLOAD] = response_payload
                break
        finally:
            for reservation in reservations:
                reservation.close()
            if reserved_bytes:
                admission.release(reserved_bytes)
        return StorageV2Response(
            status=int(message[FIELD_STATUS]),
            schema_id=int(message[FIELD_SCHEMA_ID]),
            payload=message.get(FIELD_PAYLOAD) or b"",
            error_code=message.get(FIELD_ERROR_CODE),
            error_message=message.get(FIELD_ERROR_MESSAGE),
            retryable=bool(message.get(FIELD_RETRYABLE)),
        )

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "StorageV2Session":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
