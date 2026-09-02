"""Bounded command-receipt compression and replay contracts."""

from __future__ import annotations

import hashlib

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar.receipt_codec import (
    COMPRESSED_RECEIPT_MAGIC,
    MAX_DECODED_RECEIPT_BYTES,
    MAX_STORED_RECEIPT_BYTES,
    decode_receipt_response,
    encode_receipt_response,
)


pytestmark = pytest.mark.unit


def test_small_receipts_keep_legacy_canonical_json_bytes():
    response = {"ok": True, "value": {"answer": 42}}
    expected = orjson.dumps(response, option=orjson.OPT_SORT_KEYS)

    encoded = encode_receipt_response(response)

    assert encoded == expected
    assert decode_receipt_response(encoded) == response
    assert decode_receipt_response(memoryview(encoded)) == response


def test_large_repeated_commit_response_is_compressed_and_exactly_replayed():
    projection = {"content": "projection-result-" * 20_000}
    response = {
        "value": {"turn": {"projection": projection}},
        "events": [{"event": {"payload": {"turns": [
            {"projection": projection},
        ]}}}],
    }

    encoded = encode_receipt_response(response)

    assert encoded.startswith(COMPRESSED_RECEIPT_MAGIC)
    assert len(encoded) <= MAX_STORED_RECEIPT_BYTES
    assert decode_receipt_response(encoded) == response


def test_incompressible_or_decoded_oversize_responses_keep_hard_limits():
    incompressible = "".join(
        hashlib.sha256(str(index).encode()).hexdigest()
        for index in range(5_000)
    )
    with pytest.raises(StorageError) as stored_limit:
        encode_receipt_response({"value": incompressible})
    assert stored_limit.value.code == "database_protocol_error"
    assert "too large for a receipt" in stored_limit.value.message

    with pytest.raises(StorageError) as decoded_limit:
        encode_receipt_response({"value": "x" * MAX_DECODED_RECEIPT_BYTES})
    assert decoded_limit.value.code == "database_protocol_error"
    assert "decoded receipt budget" in decoded_limit.value.message


def test_corrupt_or_unknown_compressed_receipts_fail_as_integrity_errors():
    encoded = bytearray(encode_receipt_response({
        "value": "compressible-value-" * 20_000,
    }))
    encoded[-1] ^= 0xFF

    for invalid in (
        bytes(encoded),
        b"tofu.receipt.future.v2\x00payload",
        COMPRESSED_RECEIPT_MAGIC + b"\x00\x00",
    ):
        with pytest.raises(StorageError) as raised:
            decode_receipt_response(invalid)
        assert raised.value.code == "database_integrity"
