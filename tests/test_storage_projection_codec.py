"""Lossless and bounded durable turn-projection codec contracts."""

from __future__ import annotations

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar.operations_pkg._common import _dump, _load
from lib.storage_sidecar.projection_codec import (
    ProjectionCodecError,
    STORAGE_PROJECTION_CODEC_KEY,
    STORAGE_PROJECTION_MAX_HYDRATION_RATIO,
    decode_projection_sequence_from_storage,
    encode_projection_sequence_for_storage,
    projection_hydration_byte_upper_bound,
)


pytestmark = pytest.mark.unit


def _projection(*, payload: str = "result payload") -> dict:
    return {
        "content": "done",
        "segments": [
            {"type": "text", "text": "done"},
            {
                "type": "tool_use",
                "id": "call-1",
                "name": "read_files",
                "input": '{"paths":["a.py"]}',
                "result": {"content": payload, "isError": False},
            },
        ],
        "toolRounds": [
            {
                "toolCallId": "call-1",
                "toolName": "read_files",
                "toolArgs": '{"paths":["a.py"]}',
                "toolContent": payload,
            },
        ],
    }


def test_common_json_codec_interns_exact_tool_payloads_and_hydrates_public_shape():
    projection = _projection(payload="0123456789" * 20_000)
    expected = orjson.dumps(projection, option=orjson.OPT_SORT_KEYS)

    stored_bytes = _dump(projection)
    stored = orjson.loads(stored_bytes)

    assert projection["segments"][1]["input"] == '{"paths":["a.py"]}'
    assert STORAGE_PROJECTION_CODEC_KEY not in projection
    assert STORAGE_PROJECTION_CODEC_KEY in stored
    assert "input" not in stored["segments"][1]
    assert "content" not in stored["segments"][1]["result"]
    assert len(stored_bytes) < len(expected) * 0.6
    assert len(expected) <= (
        len(stored_bytes) * STORAGE_PROJECTION_MAX_HYDRATION_RATIO
    )
    assert projection_hydration_byte_upper_bound(stored_bytes) >= len(expected)
    assert projection_hydration_byte_upper_bound(stored) >= len(expected)
    assert orjson.dumps(_load(stored_bytes), option=orjson.OPT_SORT_KEYS) == expected
    # PostgreSQL JSONB reaches _load already decoded rather than as bytes.
    assert orjson.dumps(_load(stored), option=orjson.OPT_SORT_KEYS) == expected


def test_codec_keeps_nonmatching_or_ambiguous_segment_payloads_verbatim():
    nonmatching = _projection()
    nonmatching["segments"][1]["result"]["content"] = "newer segment value"
    ambiguous = _projection()
    ambiguous["toolRounds"].append(dict(ambiguous["toolRounds"][0]))
    ambiguous_segments = _projection()
    ambiguous_segments["segments"].append(
        dict(ambiguous_segments["segments"][1])
    )

    nonmatching_stored = orjson.loads(_dump(nonmatching))
    ambiguous_stored = orjson.loads(_dump(ambiguous))
    ambiguous_segments_stored = orjson.loads(_dump(ambiguous_segments))

    # Exact input remains independently internable; the divergent result does
    # not. A duplicate toolCallId disables every reference for that segment.
    assert "input" not in nonmatching_stored["segments"][1]
    assert nonmatching_stored["segments"][1]["result"]["content"] == (
        "newer segment value"
    )
    assert STORAGE_PROJECTION_CODEC_KEY not in ambiguous_stored
    assert ambiguous_stored == ambiguous
    assert STORAGE_PROJECTION_CODEC_KEY not in ambiguous_segments_stored
    assert ambiguous_segments_stored == ambiguous_segments


@pytest.mark.parametrize(
    "mutate",
    [
        lambda stored: stored[STORAGE_PROJECTION_CODEC_KEY].update({"version": 2}),
        lambda stored: stored[STORAGE_PROJECTION_CODEC_KEY][
            "segmentToolRoundReferences"
        ][0].update({"toolCallId": "missing"}),
    ],
)
def test_codec_corruption_fails_closed_as_storage_integrity(mutate):
    stored = orjson.loads(_dump(_projection()))
    mutate(stored)

    with pytest.raises(StorageError) as raised:
        _load(orjson.dumps(stored))

    assert raised.value.code == "database_integrity"


def test_reserved_codec_key_is_rejected_at_the_public_write_boundary():
    projection = _projection()
    projection[STORAGE_PROJECTION_CODEC_KEY] = {
        "version": 99,
        "segmentToolRoundReferences": [],
    }

    with pytest.raises(StorageError) as raised:
        _dump(projection)

    assert raised.value.code == "database_protocol_error"


def test_hydration_budget_counts_postgres_jsonb_bytes_not_mapping_keys():
    plain_projection = {"content": "四字节🙂" * 10_000}
    canonical = orjson.dumps(
        plain_projection, option=orjson.OPT_SORT_KEYS
    )

    assert projection_hydration_byte_upper_bound(plain_projection) == len(
        canonical
    )
    assert projection_hydration_byte_upper_bound(canonical) == len(canonical)


def test_projection_sequence_compacts_archives_and_hydrates_shared_values():
    projections = [_projection(payload="archive payload " * 20_000)]
    canonical = orjson.dumps(projections, option=orjson.OPT_SORT_KEYS)

    stored = encode_projection_sequence_for_storage(projections)
    stored_bytes = orjson.dumps(stored, option=orjson.OPT_SORT_KEYS)
    hydrated = decode_projection_sequence_from_storage(stored)

    assert len(stored_bytes) < len(canonical) * 0.6
    assert orjson.dumps(hydrated, option=orjson.OPT_SORT_KEYS) == canonical
    assert (
        hydrated[0]["segments"][1]["result"]["content"]
        is hydrated[0]["toolRounds"][0]["toolContent"]
    )
    assert encode_projection_sequence_for_storage(
        stored, accept_stored=True
    ) == stored
    with pytest.raises(ProjectionCodecError):
        encode_projection_sequence_for_storage(stored)


def test_archived_conversation_codec_corruption_is_storage_integrity():
    from lib.storage_sidecar.operations_pkg._conversations import (
        _archived_conversation_messages,
    )

    stored = encode_projection_sequence_for_storage([_projection()])
    stored[0][STORAGE_PROJECTION_CODEC_KEY]["version"] = 99

    with pytest.raises(StorageError) as raised:
        _archived_conversation_messages(orjson.dumps(stored))

    assert raised.value.code == "database_integrity"
