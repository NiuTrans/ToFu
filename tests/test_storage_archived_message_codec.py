"""Frozen transcript compression stays lossless, bounded, and page-readable."""

from __future__ import annotations

import base64
from copy import deepcopy
import random
import zlib

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar import archived_message_codec as codec
from lib.storage_sidecar.operations_pkg import _archives as archives
from lib.storage_sidecar.operations_pkg import _conversations as conversations


pytestmark = pytest.mark.unit


def _canonical(value) -> bytes:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def _compressible_messages() -> list[dict]:
    repeated = "archived tool result " * 20_000
    return [
        {"role": "user", "content": "small prefix"},
        {
            "role": "assistant",
            "content": "done",
            "segments": [
                {
                    "type": "tool_use",
                    "id": "archive-call",
                    "input": {"path": "large.txt"},
                    "result": {"content": repeated, "isError": False},
                }
            ],
            "toolRounds": [
                {
                    "toolCallId": "archive-call",
                    "toolArgs": {"path": "large.txt"},
                    "toolContent": repeated,
                }
            ],
        },
        {"role": "user", "content": "small tail"},
    ]


def _valid_envelope() -> dict:
    stored = codec.encode_archived_message_sequence_for_storage(
        [{"role": "assistant", "content": "compress me " * 20_000}]
    )
    assert codec.ARCHIVED_MESSAGE_CODEC_KEY in stored[0]
    return stored[0]


def test_large_messages_compress_individually_and_round_trip():
    messages = _compressible_messages()

    encoding = codec.encode_archived_message_sequence_with_metrics(messages)

    assert encoding.projection_encoded_messages == 1
    assert encoding.compressed_messages == 1
    assert codec.ARCHIVED_MESSAGE_CODEC_KEY not in encoding.stored_messages[0]
    assert codec.ARCHIVED_MESSAGE_CODEC_KEY in encoding.stored_messages[1]
    assert codec.ARCHIVED_MESSAGE_CODEC_KEY not in encoding.stored_messages[2]
    assert encoding.stored_document_bytes < len(_canonical(messages)) * 0.1
    assert _canonical(codec.decode_archived_message_sequence_from_storage(
        encoding.stored_messages
    )) == _canonical(messages)

    second = codec.encode_archived_message_sequence_with_metrics(
        encoding.stored_messages, accept_stored=True
    )
    assert second == encoding
    with pytest.raises(codec.ArchivedMessageCodecError):
        codec.encode_archived_message_sequence_for_storage(
            encoding.stored_messages
        )


def test_incompressible_message_keeps_plain_canonical_json():
    entropy = random.Random(17).randbytes(100_000)
    message = {
        "role": "assistant",
        "content": base64.b85encode(entropy).decode("ascii"),
    }

    encoding = codec.encode_archived_message_sequence_with_metrics([message])

    assert encoding.compressed_messages == 0
    assert encoding.stored_messages == [message]
    assert encoding.stored_document_bytes == len(_canonical([message]))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 2),
        ("version", True),
        ("encoding", "zstd-base64"),
        ("decodedBytes", 1),
        ("decodedBytes", codec.MAX_DECODED_ARCHIVED_MESSAGE_BYTES + 1),
        ("payload", "not base64!"),
    ],
)
def test_corrupt_or_future_envelope_fails_closed(field, value):
    stored = _valid_envelope()
    stored[codec.ARCHIVED_MESSAGE_CODEC_KEY][field] = value

    with pytest.raises(codec.ArchivedMessageCodecError):
        codec.decode_archived_message_sequence_from_storage([stored])


def test_envelope_rejects_siblings_extra_fields_and_trailing_stream_data():
    sibling = _valid_envelope()
    sibling["role"] = "assistant"
    extra = _valid_envelope()
    extra[codec.ARCHIVED_MESSAGE_CODEC_KEY]["future"] = 1
    trailing = _valid_envelope()
    envelope = trailing[codec.ARCHIVED_MESSAGE_CODEC_KEY]
    compressed = base64.b64decode(envelope["payload"], validate=True)
    envelope["payload"] = base64.b64encode(compressed + b"trailing").decode(
        "ascii"
    )

    for stored in (sibling, extra, trailing):
        with pytest.raises(codec.ArchivedMessageCodecError):
            codec.decode_archived_message_sequence_from_storage([stored])


def test_envelope_rejects_a_nested_private_message():
    stored = _valid_envelope()
    envelope = stored[codec.ARCHIVED_MESSAGE_CODEC_KEY]
    nested = _canonical({
        codec.ARCHIVED_MESSAGE_CODEC_KEY: {"version": 1},
        "padding": "x" * 70_000,
    })
    envelope["decodedBytes"] = len(nested)
    envelope["payload"] = base64.b64encode(
        zlib.compress(nested, level=1)
    ).decode("ascii")

    with pytest.raises(codec.ArchivedMessageCodecError):
        codec.decode_archived_message_sequence_from_storage([stored])


def test_payload_and_decompression_limits_apply_before_unbounded_work(
    monkeypatch,
):
    oversized_payload = _valid_envelope()
    oversized_payload[codec.ARCHIVED_MESSAGE_CODEC_KEY]["payload"] = "AAAA"
    monkeypatch.setattr(
        codec, "MAX_STORED_ARCHIVED_MESSAGE_PAYLOAD_BYTES", 3
    )
    monkeypatch.setattr(
        codec.base64,
        "b64decode",
        lambda *_args, **_kwargs: pytest.fail(
            "oversized base64 must be rejected before decoding"
        ),
    )
    with pytest.raises(codec.ArchivedMessageCodecError):
        codec.decode_archived_message_sequence_from_storage(
            [oversized_payload]
        )


def test_declared_length_caps_zlib_output(monkeypatch):
    stored = _valid_envelope()
    envelope = stored[codec.ARCHIVED_MESSAGE_CODEC_KEY]
    envelope["decodedBytes"] = codec.ARCHIVED_MESSAGE_COMPRESSION_MIN_BYTES
    monkeypatch.setattr(
        codec,
        "MAX_DECODED_ARCHIVED_MESSAGE_BYTES",
        codec.ARCHIVED_MESSAGE_COMPRESSION_MIN_BYTES,
    )

    with pytest.raises(codec.ArchivedMessageCodecError):
        codec.decode_archived_message_sequence_from_storage([stored])


def test_tail_window_decompresses_only_the_selected_message(monkeypatch):
    messages = [
        {"role": "assistant", "content": f"message-{index} " * 20_000}
        for index in range(4)
    ]
    stored = codec.encode_archived_message_sequence_for_storage(messages)
    assert all(codec.ARCHIVED_MESSAGE_CODEC_KEY in item for item in stored)
    raw = _canonical(stored)
    decoded_envelopes: list[dict] = []
    original_decode = codec._decode_compressed_message

    def _recording_decode(message):
        decoded_envelopes.append(message)
        return original_decode(message)

    monkeypatch.setattr(codec, "_decode_compressed_message", _recording_decode)

    result = conversations._archived_conversation_tail_window(
        raw, window=1, expected_count=len(messages)
    )

    assert result is not None
    selected, total, start, end = result
    assert selected == messages[-1:]
    assert (total, start, end) == (4, 3, 4)
    assert len(decoded_envelopes) == 1


def test_runtime_maps_compressed_corruption_to_storage_integrity():
    stored = deepcopy(_valid_envelope())
    stored[codec.ARCHIVED_MESSAGE_CODEC_KEY]["decodedBytes"] -= 1

    with pytest.raises(StorageError) as raised:
        conversations._archived_conversation_messages(_canonical([stored]))

    assert raised.value.code == "database_integrity"


def test_compaction_archive_runtime_codec_is_lossless_and_fail_closed():
    messages = _compressible_messages()

    stored = archives._encoded_messages(messages)

    assert len(stored) < len(_canonical(messages)) * 0.1
    assert _canonical(archives._decoded_messages(stored)) == _canonical(messages)
    invalid = deepcopy(_valid_envelope())
    invalid[codec.ARCHIVED_MESSAGE_CODEC_KEY]["decodedBytes"] -= 1
    with pytest.raises(StorageError) as raised:
        archives._decoded_messages(_canonical([invalid]))
    assert raised.value.code == "database_integrity"


def test_compaction_archive_runtime_rejects_non_finite_public_numbers():
    messages = [{"role": "assistant", "content": float("nan")}]

    with pytest.raises(StorageError) as raised:
        archives._encoded_messages(messages)

    assert raised.value.code == "database_protocol_error"
