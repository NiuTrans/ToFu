"""Heavy task-result fields compress losslessly within explicit budgets."""

from __future__ import annotations

import base64
from copy import deepcopy
import random
import zlib

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar import task_result_field_codec as codec


pytestmark = pytest.mark.unit


def _public_result() -> dict:
    return {
        "task_id": "task-codec",
        "status": "done",
        "segments": "segment result " * 20_000,
        "metadata": orjson.dumps({
            "toolSummary": "summary " * 20_000,
            "finishReason": "stop",
        }).decode(),
        "tool_rounds": "tool output " * 20_000,
        "content": "small result",
        "thinking": None,
        "error": None,
    }


def _valid_envelope() -> dict:
    encoding = codec.encode_task_result_fields_for_storage(_public_result())
    return deepcopy(encoding.stored_value["segments"])


def test_large_controlled_fields_compress_and_round_trip():
    value = _public_result()

    encoding = codec.encode_task_result_fields_for_storage(value)

    assert encoding.compressed_fields == (
        "segments", "metadata", "tool_rounds"
    )
    assert encoding.stored_document_bytes < len(orjson.dumps(value)) * 0.1
    assert encoding.stored_value["content"] == "small result"
    assert codec.decode_task_result_fields_from_storage(
        encoding.stored_value
    ) == value


def test_incompressible_field_remains_plain():
    entropy = random.Random(29).randbytes(40_000)
    value = {
        "task_id": "incompressible",
        "segments": base64.b85encode(entropy).decode("ascii"),
    }

    encoding = codec.encode_task_result_fields_for_storage(value)

    assert encoding.compressed_fields == ()
    assert encoding.stored_value is value


def test_offline_accept_stored_is_idempotent_and_public_injection_fails():
    first = codec.encode_task_result_fields_for_storage(_public_result())

    second = codec.encode_task_result_fields_for_storage(
        first.stored_value, accept_stored=True
    )

    assert second == first
    with pytest.raises(codec.TaskResultFieldCodecError):
        codec.encode_task_result_fields_for_storage(first.stored_value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 2),
        ("version", True),
        ("encoding", "zstd-base64"),
        ("decodedBytes", 1),
        ("decodedBytes", codec.MAX_DECODED_TASK_RESULT_FIELD_BYTES + 1),
        ("payload", "not base64!"),
    ],
)
def test_corrupt_or_future_envelope_fails_closed(field, value):
    stored = _valid_envelope()
    stored[codec.TASK_RESULT_FIELD_CODEC_KEY][field] = value

    with pytest.raises(codec.TaskResultFieldCodecError):
        codec.decode_task_result_fields_from_storage({"segments": stored})


def test_envelope_rejects_siblings_extra_fields_and_trailing_stream_data():
    sibling = _valid_envelope()
    sibling["public"] = True
    extra = _valid_envelope()
    extra[codec.TASK_RESULT_FIELD_CODEC_KEY]["future"] = 1
    trailing = _valid_envelope()
    envelope = trailing[codec.TASK_RESULT_FIELD_CODEC_KEY]
    compressed = base64.b64decode(envelope["payload"], validate=True)
    envelope["payload"] = base64.b64encode(compressed + b"trailing").decode()

    for stored in (sibling, extra, trailing):
        with pytest.raises(codec.TaskResultFieldCodecError):
            codec.decode_task_result_fields_from_storage({"segments": stored})


def test_payload_limit_applies_before_base64_decode(monkeypatch):
    stored = _valid_envelope()
    stored[codec.TASK_RESULT_FIELD_CODEC_KEY]["payload"] = "AAAA"
    monkeypatch.setattr(
        codec, "MAX_STORED_TASK_RESULT_FIELD_PAYLOAD_BYTES", 3
    )
    monkeypatch.setattr(
        codec.base64,
        "b64decode",
        lambda *_args, **_kwargs: pytest.fail(
            "oversize base64 must be rejected before decoding"
        ),
    )

    with pytest.raises(codec.TaskResultFieldCodecError):
        codec.decode_task_result_fields_from_storage({"segments": stored})


def test_declared_length_caps_zlib_output(monkeypatch):
    stored = _valid_envelope()
    envelope = stored[codec.TASK_RESULT_FIELD_CODEC_KEY]
    envelope["decodedBytes"] = codec.TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES
    monkeypatch.setattr(
        codec,
        "MAX_DECODED_TASK_RESULT_FIELD_BYTES",
        codec.TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES,
    )

    with pytest.raises(codec.TaskResultFieldCodecError):
        codec.decode_task_result_fields_from_storage({"segments": stored})


def test_selected_fields_share_one_decoded_byte_budget(monkeypatch):
    value = {
        "segments": "x" * 40_000,
        "metadata": "y" * 40_000,
    }
    stored = codec.encode_task_result_fields_for_storage(value).stored_value
    monkeypatch.setattr(codec, "MAX_DECODED_TASK_RESULT_FIELD_BYTES", 60_000)

    assert codec.decode_task_result_fields_from_storage(
        stored, fields={"segments"}
    )["segments"] == value["segments"]
    with pytest.raises(codec.TaskResultFieldCodecError):
        codec.decode_task_result_fields_from_storage(stored)


def test_selected_fields_share_one_stored_payload_budget(monkeypatch):
    value = {
        "segments": "x" * 40_000,
        "metadata": "y" * 40_000,
    }
    stored = codec.encode_task_result_fields_for_storage(value).stored_value
    key = codec.TASK_RESULT_FIELD_CODEC_KEY
    segment_bytes = len(stored["segments"][key]["payload"])
    metadata_bytes = len(stored["metadata"][key]["payload"])
    shared_limit = max(segment_bytes, metadata_bytes) + 1
    assert shared_limit < segment_bytes + metadata_bytes
    monkeypatch.setattr(
        codec, "MAX_STORED_TASK_RESULT_FIELD_PAYLOAD_BYTES", shared_limit
    )

    assert codec.decode_task_result_fields_from_storage(
        stored, fields={"segments"}
    )["segments"] == value["segments"]
    with pytest.raises(codec.TaskResultFieldCodecError):
        codec.decode_task_result_fields_from_storage(stored)


def test_selected_decode_does_not_touch_unrequested_corruption():
    encoding = codec.encode_task_result_fields_for_storage(_public_result())
    stored = deepcopy(encoding.stored_value)
    stored["segments"][codec.TASK_RESULT_FIELD_CODEC_KEY]["payload"] = "bad"

    metadata_only = codec.decode_task_result_fields_from_storage(
        stored, fields={"metadata"}
    )

    assert isinstance(metadata_only["metadata"], str)
    assert isinstance(metadata_only["segments"], dict)
    with pytest.raises(codec.TaskResultFieldCodecError):
        codec.decode_task_result_fields_from_storage(stored)


def test_non_finite_public_values_keep_protocol_rejection():
    with pytest.raises(StorageError) as raised:
        codec.encode_task_result_fields_for_storage({
            "segments": "small",
            "usage": float("nan"),
        })

    assert raised.value.code == "database_protocol_error"
