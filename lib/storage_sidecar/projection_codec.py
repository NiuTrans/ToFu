"""Lossless private storage codec for turn projection payload duplication.

Responsibility
--------------
Turn projections intentionally expose both canonical ``segments`` and richer
``toolRounds`` for compatibility and recovery.  Tool segments therefore carry
copies of their tool arguments and result content.  This module removes only
copies that are byte-for-byte equal while a projection is at rest, records
explicit versioned references, and restores the public projection before any
semantic operation consumes it.

Entry points are :func:`encode_projection_for_storage` and
:func:`decode_projection_from_storage`.  The codec is backend-neutral and has
no database, filesystem, or application-service dependency.
"""

from __future__ import annotations

from typing import Any

import orjson


STORAGE_PROJECTION_CODEC_KEY = "_tofuStorageProjectionCodec"
STORAGE_PROJECTION_CODEC_VERSION = 1
# A reference is one-to-one: the removed JSON value still exists once in the
# matching tool round. Even before the explicit reference envelope is counted,
# hydrating can therefore add no more than the encoded document's byte size.
STORAGE_PROJECTION_MAX_HYDRATION_RATIO = 2


class ProjectionCodecError(ValueError):
    """The private projection reference envelope is malformed or incomplete."""


def _segment_tool_call_id(segment: dict[str, Any]) -> str:
    """Return the public tool-call identity used by current segment schemas."""
    value = segment.get("id") or segment.get("toolCallId")
    return value if isinstance(value, str) else ""


def _unique_tool_rounds_by_id(
    tool_rounds: list[Any],
) -> dict[str, dict[str, Any]]:
    """Index unambiguous tool rounds; duplicate identities are never interned."""
    indexed: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for candidate in tool_rounds:
        if not isinstance(candidate, dict):
            continue
        tool_call_id = candidate.get("toolCallId")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            continue
        if tool_call_id in indexed:
            duplicate_ids.add(tool_call_id)
            continue
        indexed[tool_call_id] = candidate
    for tool_call_id in duplicate_ids:
        indexed.pop(tool_call_id, None)
    return indexed


def _is_projection_shape(value: Any) -> bool:
    """Recognize only the controlled assistant-turn projection shape."""
    return (
        isinstance(value, dict)
        and isinstance(value.get("segments"), list)
        and isinstance(value.get("toolRounds"), list)
    )


def _unique_segment_tool_call_ids(segments: list[Any]) -> set[str]:
    """Return segment identities that occur exactly once in the projection."""
    counts: dict[str, int] = {}
    for candidate in segments:
        if not isinstance(candidate, dict):
            continue
        tool_call_id = _segment_tool_call_id(candidate)
        if tool_call_id:
            counts[tool_call_id] = counts.get(tool_call_id, 0) + 1
    return {
        tool_call_id for tool_call_id, count in counts.items() if count == 1
    }


def encode_projection_for_storage(projection: Any) -> Any:
    """Return a losslessly interned storage copy of one public projection.

    The input and all untouched nested values retain their original identity.
    A segment field is removed only when the matching, uniquely identified
    ``toolRounds`` field exists and compares equal.  Consequently partial,
    compacted, malformed, or future projection variants stay verbatim.
    """
    if not _is_projection_shape(projection):
        return projection
    if STORAGE_PROJECTION_CODEC_KEY in projection:
        # Public/caller projections never own this private namespace. Existing
        # database values pass through _load first, so accepting a marker here
        # could persist an unvalidated or future-version reference envelope.
        raise _codec_error("public projection uses the reserved codec key")

    segments = projection["segments"]
    tool_rounds_by_id = _unique_tool_rounds_by_id(projection["toolRounds"])
    unique_segment_ids = _unique_segment_tool_call_ids(segments)
    encoded_segments: list[Any] | None = None
    references: list[dict[str, Any]] = []

    for segment_index, candidate in enumerate(segments):
        if not isinstance(candidate, dict):
            continue
        tool_call_id = _segment_tool_call_id(candidate)
        if tool_call_id not in unique_segment_ids:
            continue
        tool_round = tool_rounds_by_id.get(tool_call_id)
        if tool_round is None:
            continue

        input_from_tool_round = (
            "input" in candidate
            and "toolArgs" in tool_round
            and candidate["input"] == tool_round["toolArgs"]
        )
        result = candidate.get("result")
        result_content_from_tool_round = (
            isinstance(result, dict)
            and "content" in result
            and "toolContent" in tool_round
            and result["content"] == tool_round["toolContent"]
        )
        if not input_from_tool_round and not result_content_from_tool_round:
            continue

        if encoded_segments is None:
            encoded_segments = list(segments)
        encoded_segment = dict(candidate)
        reference: dict[str, Any] = {
            "segmentIndex": segment_index,
            "toolCallId": tool_call_id,
        }
        if input_from_tool_round:
            encoded_segment.pop("input", None)
            reference["inputFromToolRound"] = True
        if result_content_from_tool_round:
            encoded_result = dict(result)
            encoded_result.pop("content", None)
            encoded_segment["result"] = encoded_result
            reference["resultContentFromToolRound"] = True
        encoded_segments[segment_index] = encoded_segment
        references.append(reference)

    if not references or encoded_segments is None:
        return projection

    encoded_projection = dict(projection)
    encoded_projection["segments"] = encoded_segments
    encoded_projection[STORAGE_PROJECTION_CODEC_KEY] = {
        "version": STORAGE_PROJECTION_CODEC_VERSION,
        "segmentToolRoundReferences": references,
    }
    return encoded_projection


def _codec_error(reason: str) -> ProjectionCodecError:
    return ProjectionCodecError(f"invalid stored projection codec: {reason}")


def decode_projection_from_storage(projection: Any) -> Any:
    """Hydrate one encoded projection, failing closed on invalid references."""
    if not isinstance(projection, dict):
        return projection
    codec = projection.get(STORAGE_PROJECTION_CODEC_KEY)
    if codec is None:
        return projection
    if not _is_projection_shape(projection):
        raise _codec_error("projection shape is missing")
    if not isinstance(codec, dict):
        raise _codec_error("codec envelope is not an object")
    version = codec.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != STORAGE_PROJECTION_CODEC_VERSION
    ):
        raise _codec_error("unsupported codec version")
    references = codec.get("segmentToolRoundReferences")
    segments = projection["segments"]
    if (
        not isinstance(references, list)
        or not references
        or len(references) > len(segments)
    ):
        raise _codec_error("reference list is invalid")

    tool_rounds_by_id = _unique_tool_rounds_by_id(projection["toolRounds"])
    decoded_segments = list(segments)
    referenced_segment_indexes: set[int] = set()
    referenced_tool_call_ids: set[str] = set()

    for reference in references:
        if not isinstance(reference, dict):
            raise _codec_error("reference is not an object")
        segment_index = reference.get("segmentIndex")
        if (
            not isinstance(segment_index, int)
            or isinstance(segment_index, bool)
            or segment_index < 0
            or segment_index >= len(segments)
            or segment_index in referenced_segment_indexes
        ):
            raise _codec_error("segment index is invalid")
        referenced_segment_indexes.add(segment_index)

        tool_call_id = reference.get("toolCallId")
        if (
            not isinstance(tool_call_id, str)
            or not tool_call_id
            or tool_call_id in referenced_tool_call_ids
        ):
            raise _codec_error("tool-call identity is invalid")
        referenced_tool_call_ids.add(tool_call_id)
        candidate = decoded_segments[segment_index]
        if (
            not isinstance(candidate, dict)
            or _segment_tool_call_id(candidate) != tool_call_id
        ):
            raise _codec_error("segment identity does not match reference")
        tool_round = tool_rounds_by_id.get(tool_call_id)
        if tool_round is None:
            raise _codec_error("referenced tool round is absent or ambiguous")

        restore_input = reference.get("inputFromToolRound") is True
        restore_result_content = (
            reference.get("resultContentFromToolRound") is True
        )
        if not restore_input and not restore_result_content:
            raise _codec_error("reference contains no restorable field")

        decoded_segment = dict(candidate)
        if restore_input and "input" not in decoded_segment:
            if "toolArgs" not in tool_round:
                raise _codec_error("referenced tool arguments are absent")
            decoded_segment["input"] = tool_round["toolArgs"]
        if restore_result_content:
            result = decoded_segment.get("result")
            if not isinstance(result, dict):
                raise _codec_error("segment result is not an object")
            decoded_result = dict(result)
            if "content" not in decoded_result:
                if "toolContent" not in tool_round:
                    raise _codec_error("referenced tool result is absent")
                decoded_result["content"] = tool_round["toolContent"]
            decoded_segment["result"] = dict(sorted(decoded_result.items()))
        # _dump has always stored sorted object keys. Preserve its previous
        # public decode order as well as semantic equality.
        decoded_segments[segment_index] = dict(sorted(decoded_segment.items()))

    decoded_projection = dict(projection)
    decoded_projection.pop(STORAGE_PROJECTION_CODEC_KEY, None)
    decoded_projection["segments"] = decoded_segments
    return dict(sorted(decoded_projection.items()))


def projection_hydration_byte_upper_bound(value: Any) -> int:
    """Return a backend-neutral upper bound for one hydrated projection.

    SQLite may expose JSON storage as bytes/text while PostgreSQL JSONB is
    already a dict. Counting ``len(dict)`` would count keys rather than bytes;
    serialize that representation canonically. A codec marker applies the
    proven one-to-one hydration ratio. A marker-looking string inside legacy
    JSON can only make this estimate more conservative.
    """
    if value is None:
        return 0
    if isinstance(value, memoryview):
        encoded = value.tobytes()
        has_codec = STORAGE_PROJECTION_CODEC_KEY.encode() in encoded
    elif isinstance(value, bytes):
        encoded = value
        has_codec = STORAGE_PROJECTION_CODEC_KEY.encode() in encoded
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
        has_codec = STORAGE_PROJECTION_CODEC_KEY in value
    else:
        try:
            encoded = orjson.dumps(value, option=orjson.OPT_SORT_KEYS)
        except (TypeError, orjson.JSONEncodeError) as exc:
            raise ProjectionCodecError(
                "stored projection is not serializable"
            ) from exc
        has_codec = (
            isinstance(value, dict)
            and STORAGE_PROJECTION_CODEC_KEY in value
        )
    ratio = STORAGE_PROJECTION_MAX_HYDRATION_RATIO if has_codec else 1
    return len(encoded) * ratio


__all__ = [
    "ProjectionCodecError",
    "STORAGE_PROJECTION_CODEC_KEY",
    "STORAGE_PROJECTION_MAX_HYDRATION_RATIO",
    "STORAGE_PROJECTION_CODEC_VERSION",
    "decode_projection_from_storage",
    "encode_projection_for_storage",
    "projection_hydration_byte_upper_bound",
]
