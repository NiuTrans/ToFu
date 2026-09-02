"""Private task-event compression remains lossless, bounded, and compatible."""

from __future__ import annotations

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar.task_event_codec import (
    COMPRESSED_TASK_EVENT_MAGIC,
    TASK_EVENT_COMPRESSION_MIN_BYTES,
    decode_task_event_payload,
    encode_task_event_payload,
)


pytestmark = pytest.mark.unit


def test_small_task_event_keeps_legacy_json_bytes():
    raw = orjson.dumps({"type": "delta", "content": "small"})

    assert encode_task_event_payload(raw) is raw
    assert decode_task_event_payload(raw) == raw


def test_large_task_event_compresses_and_round_trips_losslessly():
    raw = orjson.dumps({
        "type": "messages_snapshot",
        "messages": [{"role": "user", "content": "repeatable " * 20_000}],
    })

    encoded = encode_task_event_payload(raw)

    assert len(raw) >= TASK_EVENT_COMPRESSION_MIN_BYTES
    assert encoded.startswith(COMPRESSED_TASK_EVENT_MAGIC)
    assert len(encoded) < len(raw) // 10
    assert decode_task_event_payload(encoded) == raw
    assert decode_task_event_payload(memoryview(encoded)) == raw


def test_unhelpful_compression_keeps_legacy_bytes(monkeypatch):
    import lib.storage_sidecar.task_event_codec as codec

    raw = b'x' * TASK_EVENT_COMPRESSION_MIN_BYTES
    monkeypatch.setattr(
        codec.zlib, 'compress', lambda value, level: b'z' * len(value))

    assert codec.encode_task_event_payload(raw) is raw


def test_decoded_event_budget_rejects_oversize_input(monkeypatch):
    import lib.storage_sidecar.task_event_codec as codec

    monkeypatch.setattr(codec, 'MAX_DECODED_TASK_EVENT_BYTES', 10)

    with pytest.raises(StorageError) as raised:
        codec.encode_task_event_payload(b'x' * 11)

    assert raised.value.code == 'database_protocol_error'


@pytest.mark.parametrize("mutate", [
    lambda encoded: encoded[:-1],
    lambda encoded: encoded + b"trailing-data",
    lambda encoded: encoded[:len(COMPRESSED_TASK_EVENT_MAGIC)] + b"\x00",
])
def test_corrupt_compressed_task_event_fails_closed(mutate):
    raw = orjson.dumps({"content": "compressible " * 10_000})
    encoded = encode_task_event_payload(raw)
    assert encoded.startswith(COMPRESSED_TASK_EVENT_MAGIC)

    with pytest.raises(StorageError) as raised:
        decode_task_event_payload(mutate(encoded))

    assert raised.value.code == "database_integrity"


def test_unknown_task_event_codec_fails_closed():
    with pytest.raises(StorageError) as raised:
        decode_task_event_payload(b"tofu.task-event.zstd.v2\x00payload")

    assert raised.value.code == "database_integrity"
