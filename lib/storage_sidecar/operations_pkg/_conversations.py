"""Conversation document and transcript operation handlers."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping
import time
from typing import Any

import orjson


from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session


logger = get_logger(__name__)


_ACTIVITY_DATE_BATCH_SIZE = 64
_ACTIVITY_ARCHIVE_BATCH_SIZE = 4
_ACTIVITY_DATE_MAX_INTERVALS = 366


# Reverse-scanning JSON in Python only wins when the selected suffix is small.
# Beyond this code-unit budget, orjson's authoritative full decoder is both
# faster and already the compatibility fallback.  The cap also bounds corrupt
# or highly skewed archives whose final message is unexpectedly enormous.
_ARCHIVED_TAIL_SCAN_CODE_UNIT_BUDGET = 128 * 1024
_TITLE_FILTER_SCAN_PAGE_SIZE = 512


from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)
from lib.storage_sidecar.operations_pkg._turns import (
    _mark_conversation_search_projection_dirty,
    _turn_public,
)
from lib.storage_sidecar.archived_message_codec import (
    decode_archived_message_sequence_from_storage,
)
from lib.storage_sidecar.projection_codec import ProjectionCodecError
from lib.storage_sidecar.turn_projection_head import (
    discard_projection_cache_for_row,
    projection_from_turn_row,
    projection_head_state,
)

_CONVERSATION_METADATA = frozenset(
    {
        "title",
        "created_at",
        "updated_at",
        "settings",
        "search_text",
    }
)

from lib.storage_sidecar.operations_pkg._conversation_clone import (
    conversation_clone,
    settings_without_live_runtime,
)


def _conversation_identity(payload: Mapping[str, Any]) -> tuple[str, int]:
    conv_id = _required_text(payload, "conv_id", 256)
    user_id = _integer(payload, "user_id", minimum=1)
    return conv_id, user_id


def _conversation_document(
    row: Mapping[str, Any],
    *,
    include_messages: bool = True,
    messages: list[dict[str, Any]] | None = None,
    settings_keys: list[str] | None = None,
) -> dict[str, Any]:
    messages = list(messages or []) if include_messages else []
    settings = _load(row["settings_json"]) or {}
    if not isinstance(messages, list) or not isinstance(settings, Mapping):
        raise StorageError("database_integrity", "Conversation document is malformed")
    if settings_keys is not None:
        settings = {key: settings[key] for key in settings_keys if key in settings}
    return {
        "metadata": {
            "id": str(row["id"]),
            "user_id": int(row["user_id"]),
            "title": str(row["title"] or ""),
            "created_at": int(row["created_at_ms"]),
            "updated_at": int(row["updated_at_ms"]),
            "settings": settings,
            "msg_count": int(row["msg_count"]),
            # Compatibility placeholder only. Search is an independently
            # rebuilt projection; the frozen header copy is never authority
            # and must not ride full transcript RPCs.
            "search_text": "",
            "rev": int(row["rev"]),
        },
        "messages": messages if include_messages else [],
        "source": "sidecar",
    }


def _turn_actor_to_legacy_role(actor: str) -> str:
    """Mirror of the client's ``messageRole`` (turn-projection.ts)."""
    return "user" if actor in ("human", "critic", "virtual_user") else "assistant"


def _with_legacy_compaction_fields(projection: dict[str, Any]) -> dict[str, Any]:
    """Project canonical compaction metadata onto the read-only v1 overlay."""
    compaction = projection.get("compaction")
    if not isinstance(compaction, Mapping) \
            or compaction.get("blockId") != "compaction":
        return projection
    result = dict(projection)
    result["_isCompactionSummary"] = True
    archive_id = compaction.get("archiveId")
    if archive_id not in (None, ""):
        result["_compactionArchiveId"] = str(archive_id)
    estimated = compaction.get("estimatedPromptTokens")
    if isinstance(estimated, (int, float)) and not isinstance(estimated, bool):
        result["_estimatedPromptTokens"] = max(0, int(estimated))
    folded = compaction.get("foldedToolRounds")
    if isinstance(folded, (int, float)) and not isinstance(folded, bool):
        result["_foldedToolRounds"] = max(0, int(folded))
    marker_names = {
        "archiveId": "archiveId",
        "conversationId": "convId",
        "trigger": "trigger",
        "timestamp": "ts",
        "tokensBefore": "tokensBefore",
        "tokensAfter": "tokensAfter",
        "messagesBefore": "msgsBefore",
        "messagesAfter": "msgsAfter",
        "reductionPercent": "reductionPct",
        "foldedToolRounds": "foldedToolRounds",
    }
    marker = {
        legacy_name: compaction[canonical_name]
        for canonical_name, legacy_name in marker_names.items()
        if compaction.get(canonical_name) not in (None, "")
    }
    if marker:
        result["_compactions"] = [marker]
    return result


def _turn_to_legacy_message(turn: Mapping[str, Any]) -> dict[str, Any]:
    """One canonical turn row → one legacy v1 message document.

    Mirrors the client's own ``turnToLegacyMessage``
    (frontend/src/core/turn-projection.ts) field-for-field, so a v1 body read
    and a v2 hydration agree on the same conversation byte-for-byte — the v1
    view is a DERIVED VIEW of the turn authority, never a second copy.
    ``_commandPending`` is client-runtime-only and stays None server-side.
    """
    projection = dict(turn.get("projection") or {})
    projection.pop("role", None)
    projection = _with_legacy_compaction_fields(projection)
    created_at = int(turn.get("createdAt") or 0)
    return {
        **projection,
        "role": _turn_actor_to_legacy_role(str(turn.get("actor") or "")),
        "_turnId": turn.get("turnId"),
        "_attemptId": turn.get("currentAttemptId"),
        "_turnActor": turn.get("actor"),
        "_turnKind": turn.get("kind"),
        "_turnLaneId": turn.get("laneId") or "main",
        "_turnStatus": turn.get("status"),
        "_turnSettlement": turn.get("settlement") or {},
        "_commandPending": None,
        "_projectionRevision": int(turn.get("projectionRevision") or 0),
        "timestamp": projection.get("timestamp") or created_at,
    }


def _derive_turn_messages(session: Session, conv_id: str, user_id: Any) -> list:
    """Project canonical main-lane turns into the HTTP message view."""
    rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND lane_id='main' "
        "ORDER BY ordinal",
        (conv_id, user_id),
    )
    return [_turn_to_legacy_message(_turn_public(session, row)) for row in rows]


def _message_window_bounds(
    total_count: int,
    window: int,
    before_sequence: int | None,
) -> tuple[int, int]:
    end = (
        total_count
        if before_sequence is None
        else max(0, min(before_sequence, total_count))
    )
    return max(0, end - window), end


def _derive_turn_message_window(
    session: Session,
    conv_id: str,
    user_id: int,
    *,
    window: int,
    before_sequence: int | None,
) -> tuple[list[dict[str, Any]], int, int, int]:
    count_row = session.fetch_one(
        "SELECT COUNT(*) AS c FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND lane_id='main'",
        (conv_id, user_id),
    )
    total_count = int(count_row["c"]) if count_row else 0
    start, end = _message_window_bounds(
        total_count, window, before_sequence
    )
    if start == end:
        return [], total_count, start, end
    rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND lane_id='main' "
        "ORDER BY ordinal LIMIT ? OFFSET ?",
        (conv_id, user_id, end - start, start),
    )
    return (
        [_turn_to_legacy_message(_turn_public(session, row)) for row in rows],
        total_count,
        start,
        end,
    )


def _archived_conversation_messages(raw: Any) -> list[dict[str, Any]]:
    """Decode one frozen pre-turn transcript for read compatibility only.

    New writes never target ``messages_json``.  A non-empty value remains the
    only durable transcript for conversations imported before the turn-native
    cutover, so ignoring it would project intact history as an empty chat.
    """
    messages = _load(raw) or []
    if not isinstance(messages, list) or not all(
        isinstance(message, Mapping) for message in messages
    ):
        raise StorageError(
            "database_integrity", "Archived conversation transcript is malformed"
        )
    try:
        return decode_archived_message_sequence_from_storage(
            [dict(message) for message in messages]
        )
    except ProjectionCodecError as exc:
        raise StorageError(
            "database_integrity",
            "Archived conversation projection encoding is invalid",
        ) from exc


def _json_character(raw: str | bytes, index: int) -> str:
    """Return one ASCII JSON syntax character without copying the archive."""
    value = raw[index]
    return chr(value) if isinstance(value, int) else value


def _json_quote_is_escaped(raw: str | bytes, quote_index: int) -> bool:
    """Whether a reverse-scanned quote has an odd backslash prefix."""
    backslashes = 0
    index = quote_index - 1
    while index >= 0 and _json_character(raw, index) == "\\":
        backslashes += 1
        index -= 1
    return bool(backslashes % 2)


def _json_array_tail_bounds(
    raw: str | bytes,
    count: int,
    *,
    scan_code_unit_budget: int | None = None,
) -> tuple[list[tuple[int, int]], bool] | None:
    """Locate the last ``count`` top-level array values without decoding all.

    JSON strings may contain commas, braces, escaped quotes, or arbitrary
    Unicode; the reverse scanner treats only syntax outside strings as
    structure. ``has_more`` says an earlier top-level value exists.
    """
    if count < 1 or (
        scan_code_unit_budget is not None and scan_code_unit_budget < 1
    ):
        return None
    scan_origin = len(raw) - 1
    scan_floor = (
        max(-1, scan_origin - scan_code_unit_budget)
        if scan_code_unit_budget is not None
        else -1
    )
    end = len(raw) - 1
    while end >= 0 and _json_character(raw, end).isspace():
        if end <= scan_floor:
            return None
        end -= 1
    if end < 0 or _json_character(raw, end) != "]":
        return None
    value_end = end
    index = end - 1
    while index >= 0 and _json_character(raw, index).isspace():
        if index <= scan_floor:
            return None
        index -= 1
    if index >= 0 and _json_character(raw, index) == "[":
        return ([], False)

    depth = 0
    in_string = False
    reverse_bounds: list[tuple[int, int]] = []
    while index >= 0:
        if index <= scan_floor:
            return None
        character = _json_character(raw, index)
        if in_string:
            if character == '"' and not _json_quote_is_escaped(raw, index):
                in_string = False
            index -= 1
            continue
        if character == '"':
            in_string = True
        elif character in "}]":
            depth += 1
        elif character in "{[":
            if depth:
                depth -= 1
            elif character == "[":
                value_start = index + 1
                while (
                    value_start < value_end
                    and _json_character(raw, value_start).isspace()
                ):
                    value_start += 1
                reverse_bounds.append((value_start, value_end))
                return (list(reversed(reverse_bounds)), False)
            else:
                return None
        elif character == "," and depth == 0:
            value_start = index + 1
            while (
                value_start < value_end
                and _json_character(raw, value_start).isspace()
            ):
                value_start += 1
            reverse_bounds.append((value_start, value_end))
            if len(reverse_bounds) >= count:
                return (list(reversed(reverse_bounds)), True)
            value_end = index
            while (
                value_end > 0
                and _json_character(raw, value_end - 1).isspace()
            ):
                if value_end - 1 <= scan_floor:
                    return None
                value_end -= 1
        index -= 1
    return None


def _json_array_head_bounds(
    raw: str | bytes,
    count: int,
    *,
    scan_code_unit_budget: int | None = None,
) -> tuple[list[tuple[int, int]], bool] | None:
    """Locate the first ``count`` top-level array values with bounded work."""
    if count < 1 or (
        scan_code_unit_budget is not None and scan_code_unit_budget < 1
    ):
        return None
    index = 0
    scan_ceiling = (
        min(len(raw), scan_code_unit_budget)
        if scan_code_unit_budget is not None
        else len(raw)
    )
    while index < len(raw) and _json_character(raw, index).isspace():
        if index >= scan_ceiling:
            return None
        index += 1
    if index >= len(raw) or _json_character(raw, index) != "[":
        return None
    index += 1
    while index < len(raw) and _json_character(raw, index).isspace():
        if index >= scan_ceiling:
            return None
        index += 1
    if index < len(raw) and _json_character(raw, index) == "]":
        return ([], False)

    value_start = index
    depth = 0
    in_string = False
    escaped = False
    bounds: list[tuple[int, int]] = []
    while index < len(raw):
        if index >= scan_ceiling:
            return None
        character = _json_character(raw, index)
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
        elif character in "]}":
            if depth:
                depth -= 1
            elif character == "]":
                value_end = index
                while (
                    value_end > value_start
                    and _json_character(raw, value_end - 1).isspace()
                ):
                    value_end -= 1
                bounds.append((value_start, value_end))
                return bounds, False
            else:
                return None
        elif character == "," and depth == 0:
            value_end = index
            while (
                value_end > value_start
                and _json_character(raw, value_end - 1).isspace()
            ):
                value_end -= 1
            bounds.append((value_start, value_end))
            if len(bounds) >= count:
                return bounds, True
            index += 1
            while index < len(raw) and _json_character(raw, index).isspace():
                if index >= scan_ceiling:
                    return None
                index += 1
            value_start = index
            continue
        index += 1
    return None


def _load_archived_window_fragments(
    raw: str | bytes,
    bounds: list[tuple[int, int]],
) -> list[dict[str, Any]] | None:
    fragments = [raw[left:right] for left, right in bounds]
    if isinstance(raw, bytes):
        encoded_window = b"[" + b",".join(fragments) + b"]"
    else:
        encoded_window = "[" + ",".join(fragments) + "]"
    try:
        candidates = orjson.loads(encoded_window)
    except orjson.JSONDecodeError:
        return None
    if not isinstance(candidates, list) or not all(
        isinstance(message, Mapping) for message in candidates
    ):
        return None
    return [dict(message) for message in candidates]


def _archived_conversation_tail_window(
    raw: Any,
    *,
    window: int,
    expected_count: int,
) -> tuple[list[dict[str, Any]], int, int, int] | None:
    """Decode a frozen archive's tail without materializing its old prefix.

    The durable ``msg_count`` determines the requested absolute window. Basic
    suffix-shape checks guard stale counts; any ambiguity returns ``None`` so
    the caller preserves the full legacy decoder as the authority fallback.
    """
    if expected_count < 0:
        return None
    start, end = _message_window_bounds(expected_count, window, None)
    wanted = end - start
    if isinstance(raw, list):
        if len(raw) != expected_count:
            return None
        candidates = raw[start:end]
    elif isinstance(raw, (str, bytes)):
        if wanted == 0:
            bounds = _json_array_tail_bounds(raw, 1)
            if bounds != ([], False):
                return None
            candidates = []
        else:
            # Average-size projection avoids entering the Python scanner when
            # the requested suffix is predictably large.  The scanner's own
            # cap catches skewed archives where only the last values are huge.
            if (
                expected_count > 0
                and len(raw) * wanted
                > _ARCHIVED_TAIL_SCAN_CODE_UNIT_BUDGET * expected_count
            ):
                return None
            located = _json_array_tail_bounds(
                raw,
                wanted,
                scan_code_unit_budget=(
                    _ARCHIVED_TAIL_SCAN_CODE_UNIT_BUDGET
                ),
            )
            if located is None:
                return None
            bounds, has_more = located
            if len(bounds) != wanted or has_more != (expected_count > wanted):
                return None
            candidates = _load_archived_window_fragments(raw, bounds)
            if candidates is None:
                return None
    else:
        return None
    if not isinstance(candidates, list) or not all(
        isinstance(message, Mapping) for message in candidates
    ):
        return None
    try:
        decoded = decode_archived_message_sequence_from_storage(
            [dict(message) for message in candidates]
        )
    except ProjectionCodecError:
        return None
    return decoded, expected_count, start, end


def _archived_conversation_head_window(
    raw: Any,
    *,
    window: int,
    before_sequence: int,
    expected_count: int,
) -> tuple[list[dict[str, Any]], int, int, int] | None:
    """Decode a frozen archive's first page without materializing its tail."""
    if expected_count < 0:
        return None
    start, end = _message_window_bounds(
        expected_count, window, before_sequence
    )
    wanted = end - start
    if start != 0:
        return None
    if isinstance(raw, list):
        if len(raw) != expected_count:
            return None
        candidates = raw[:end]
    elif isinstance(raw, (str, bytes)):
        if wanted == 0:
            located = _json_array_head_bounds(raw, 1)
            if located != ([], False):
                return None
            candidates = []
        else:
            if (
                expected_count > 0
                and len(raw) * wanted
                > _ARCHIVED_TAIL_SCAN_CODE_UNIT_BUDGET * expected_count
            ):
                return None
            located = _json_array_head_bounds(
                raw,
                wanted,
                scan_code_unit_budget=(
                    _ARCHIVED_TAIL_SCAN_CODE_UNIT_BUDGET
                ),
            )
            if located is None:
                return None
            bounds, has_more = located
            if len(bounds) != wanted or has_more != (expected_count > wanted):
                return None
            candidates = _load_archived_window_fragments(raw, bounds)
            if candidates is None:
                return None
    else:
        return None
    if not isinstance(candidates, list) or not all(
        isinstance(message, Mapping) for message in candidates
    ):
        return None
    try:
        decoded = decode_archived_message_sequence_from_storage(
            [dict(message) for message in candidates]
        )
    except ProjectionCodecError:
        return None
    return decoded, expected_count, start, end


def _conversation_get(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _conversation_identity(payload)
    include_messages = bool(payload.get("derive_messages", True))
    message_window = payload.get("message_window", 0)
    if (
        isinstance(message_window, bool)
        or not isinstance(message_window, int)
        or not 0 <= message_window <= 500
    ):
        raise StorageError(
            "database_protocol_error", "Invalid conversation message window"
        )
    before_sequence = payload.get("before_sequence")
    if before_sequence is not None and (
        isinstance(before_sequence, bool)
        or not isinstance(before_sequence, int)
    ):
        raise StorageError(
            "database_protocol_error", "Invalid conversation message cursor"
        )
    if before_sequence is not None and not message_window:
        raise StorageError(
            "database_protocol_error",
            "Conversation message cursor requires a message window",
        )
    if not include_messages and (message_window or before_sequence is not None):
        raise StorageError(
            "database_protocol_error",
            "Conversation message window requires transcript projection",
        )

    row = session.fetch_one(
        "SELECT id, user_id, title, created_at_ms, "
        "updated_at_ms, settings_json, msg_count, rev "
        "FROM storage_conversations WHERE id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    if row is None:
        return None

    messages: list[dict[str, Any]] = []
    message_page = None
    if include_messages:
        if message_window:
            messages, total_count, start, end = _derive_turn_message_window(
                session,
                conv_id,
                user_id,
                window=message_window,
                before_sequence=before_sequence,
            )
            if total_count:
                row["msg_count"] = total_count
            else:
                archive = session.fetch_one(
                    "SELECT messages_json FROM storage_conversations "
                    "WHERE id=? AND user_id=?",
                    (conv_id, user_id),
                )
                if archive is None:
                    raise StorageError(
                        "database_integrity", "Conversation archive disappeared"
                    )
                archived_window = (
                    _archived_conversation_tail_window(
                        archive["messages_json"],
                        window=message_window,
                        expected_count=int(row["msg_count"]),
                    )
                    if before_sequence is None
                    else _archived_conversation_head_window(
                        archive["messages_json"],
                        window=message_window,
                        before_sequence=before_sequence,
                        expected_count=int(row["msg_count"]),
                    )
                )
                if archived_window is not None:
                    messages, total_count, start, end = archived_window
                else:
                    archived = _archived_conversation_messages(
                        archive["messages_json"]
                    )
                    total_count = len(archived)
                    start, end = _message_window_bounds(
                        total_count, message_window, before_sequence
                    )
                    messages = archived[start:end]
                row["msg_count"] = total_count
            message_page = {
                "total_count": total_count,
                "start": start,
                "end": end,
            }
        else:
            messages = _derive_turn_messages(session, conv_id, user_id)
            if messages:
                row["msg_count"] = len(messages)
            else:
                archive = session.fetch_one(
                    "SELECT messages_json FROM storage_conversations "
                    "WHERE id=? AND user_id=?",
                    (conv_id, user_id),
                )
                if archive is None:
                    raise StorageError(
                        "database_integrity", "Conversation archive disappeared"
                    )
                messages = _archived_conversation_messages(
                    archive["messages_json"]
                )
                row["msg_count"] = len(messages)
    else:
        _backfill_turn_message_counts(session, [row])
    document = _conversation_document(
        row,
        include_messages=include_messages,
        messages=messages,
    )
    if message_page is not None:
        document["message_page"] = message_page
    return document


def _backfill_turn_message_counts(session: Session, rows: list[dict]) -> None:
    """Prefer exact turn counts while preserving pre-turn archive counts."""
    if not rows:
        return
    identities = [
        (str(row["id"]), int(row["user_id"])) for row in rows
    ]
    where = " OR ".join(
        "(conversation_id=? AND user_id=?)" for _ in identities)
    params = tuple(value for identity in identities for value in identity)
    counts = session.fetch_all(
        "SELECT conversation_id AS cid, user_id, count(*) AS n "
        "FROM storage_conversation_turns "
        "WHERE (" + where + ") AND lane_id = 'main' "
        "GROUP BY conversation_id,user_id",
        params,
    )
    by_identity = {
        (str(item["cid"]), int(item["user_id"])): int(item["n"])
        for item in counts
    }
    for row in rows:
        count = by_identity.get((str(row["id"]), int(row["user_id"])))
        if count is not None:
            row["msg_count"] = count


def _conversation_catalog_page(
    session: Session,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return one bounded sidebar page plus its snapshot-consistent total.

    Count and page reads execute inside the Sidecar query transaction.  The
    owner/folder predicates therefore describe one authority snapshot while
    the route avoids loading and projecting every conversation merely to
    return its newest page.
    """
    user_id = _integer(payload, "user_id", minimum=1)
    if payload.get("include_messages", False) is not False:
        raise StorageError(
            "database_protocol_error",
            "Conversation catalog pages cannot include transcripts",
        )
    if payload.get("order_by", "updated_at_desc") != "updated_at_desc":
        raise StorageError(
            "database_protocol_error", "Invalid conversation catalog ordering"
        )
    limit = _integer(
        payload, "limit", default=500, minimum=1, maximum=1000
    )
    settings_keys = payload.get("settings_keys")
    if settings_keys is not None:
        if not isinstance(settings_keys, list) or not all(
            isinstance(key, str) and key for key in settings_keys
        ):
            raise StorageError(
                "database_protocol_error",
                "Invalid conversation settings projection",
            )
        settings_keys = list(dict.fromkeys(settings_keys))[:64]

    folder_id = (
        _required_text(payload, "folder_id", 512)
        if "folder_id" in payload
        else None
    )
    before_updated_at = payload.get("before_updated_at")
    if before_updated_at is not None and (
        not isinstance(before_updated_at, int)
        or isinstance(before_updated_at, bool)
        or before_updated_at < 0
    ):
        raise StorageError(
            "database_protocol_error",
            "Invalid conversation catalog cursor timestamp",
        )
    before_id = payload.get("before_id", "")
    if not isinstance(before_id, str) or len(before_id) > 256:
        raise StorageError(
            "database_protocol_error",
            "Invalid conversation catalog cursor id",
        )
    if before_updated_at is None and before_id:
        raise StorageError(
            "database_protocol_error",
            "Conversation catalog cursor id requires a timestamp",
        )

    base_where = ["user_id = ?"]
    base_params: list[Any] = [user_id]
    if folder_id is not None:
        folder_expression = (
            "settings_json ->> 'folderId'"
            if session.backend == "postgres"
            else "json_extract(settings_json, '$.folderId')"
        )
        base_where.append(f"{folder_expression} = ?")
        base_params.append(folder_id)

    count_row = session.fetch_one(
        "SELECT COUNT(*) AS c FROM storage_conversations WHERE "
        + " AND ".join(base_where),
        tuple(base_params),
    )
    total_count = int(count_row["c"]) if count_row else 0

    page_where = list(base_where)
    page_params = list(base_params)
    if before_updated_at is not None:
        page_where.append(
            "(updated_at_ms < ? OR (updated_at_ms = ? AND id < ?))"
        )
        page_params.extend([before_updated_at, before_updated_at, before_id])
    rows = session.fetch_all(
        "SELECT id, user_id, title, created_at_ms, updated_at_ms, "
        "settings_json, msg_count, rev FROM storage_conversations WHERE "
        + " AND ".join(page_where)
        + " ORDER BY updated_at_ms DESC, id DESC LIMIT ?",
        tuple(page_params + [limit + 1]),
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    _backfill_turn_message_counts(session, rows)
    return {
        "items": [
            _conversation_document(
                row,
                include_messages=False,
                settings_keys=settings_keys,
            )
            for row in rows
        ],
        "total_count": total_count,
        "has_more": has_more,
    }


def _conversation_title_match_ids(
    session: Session,
    *,
    where: list[str],
    params: list[Any],
    order_by: str,
    title_contains: str,
    limit: int,
) -> list[str]:
    """Return an ordered, bounded title match without hauling metadata.

    Python ``str.lower`` is the established conversation-reference contract.
    SQLite's built-in ``lower`` only handles ASCII, while PostgreSQL behavior
    depends on database collation.  Scan tiny keyset pages inside the storage
    authority so both backends preserve the same Unicode semantics and only
    matching rows proceed to the heavier document projection.
    """
    if limit <= 0:
        return []
    needle = title_contains.lower()
    matched_ids: list[str] = []
    cursor_updated_at: int | None = None
    cursor_id = ""
    while len(matched_ids) < limit:
        page_where = list(where)
        page_params = list(params)
        if cursor_updated_at is not None:
            page_where.append(
                "(updated_at_ms < ? OR (updated_at_ms = ? AND id < ?))"
            )
            page_params.extend(
                [cursor_updated_at, cursor_updated_at, cursor_id]
            )
        elif cursor_id:
            page_where.append("id > ?")
            page_params.append(cursor_id)
        rows = session.fetch_all(
            "SELECT id,title,updated_at_ms FROM storage_conversations WHERE "
            + " AND ".join(page_where or ["1 = 1"])
            + (
                " ORDER BY updated_at_ms DESC, id DESC LIMIT ?"
                if order_by == "updated_at_desc"
                else " ORDER BY id ASC LIMIT ?"
            ),
            tuple(page_params + [_TITLE_FILTER_SCAN_PAGE_SIZE]),
        )
        if not rows:
            break
        for row in rows:
            if needle in str(row["title"] or "").lower():
                matched_ids.append(str(row["id"]))
                if len(matched_ids) >= limit:
                    break
        if len(rows) < _TITLE_FILTER_SCAN_PAGE_SIZE:
            break
        last_row = rows[-1]
        cursor_id = str(last_row["id"])
        if order_by == "updated_at_desc":
            cursor_updated_at = int(last_row["updated_at_ms"])
    return matched_ids


def _conversation_list(session: Session, payload: Mapping[str, Any]) -> Any:
    catalog_page = payload.get("catalog_page", False)
    if not isinstance(catalog_page, bool):
        raise StorageError(
            "database_protocol_error", "Invalid conversation catalog mode"
        )
    if catalog_page:
        return _conversation_catalog_page(session, payload)

    user_id = _integer(payload, "user_id", minimum=1)
    project_path = (
        _required_text(payload, "project_path", 4096)
        if "project_path" in payload
        else None
    )
    title_contains = (
        _required_text(payload, "title_contains", 512)
        if "title_contains" in payload
        else None
    )
    ids = payload.get("ids")
    if ids is not None:
        if not isinstance(ids, list):
            raise StorageError(
                "database_protocol_error", "Invalid conversation id filter"
            )
        ids = list(dict.fromkeys(str(item) for item in ids if item))
        if not ids:
            return []
    updated_at_gte = payload.get("updated_at_gte")
    updated_at_gt = payload.get("updated_at_gt")
    created_at_lt = payload.get("created_at_lt")
    for value in (updated_at_gte, updated_at_gt, created_at_lt):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise StorageError(
                "database_protocol_error", "Invalid conversation time filter"
            )
    order_by = payload.get("order_by", "updated_at_desc")
    order_sql = {
        "updated_at_desc": "updated_at_ms DESC, id DESC",
        "id_asc": "id ASC",
    }.get(order_by)
    if order_sql is None:
        raise StorageError("database_protocol_error", "Invalid conversation ordering")
    include_messages = payload.get("include_messages", True)
    if not isinstance(include_messages, bool):
        raise StorageError(
            "database_protocol_error", "Invalid conversation message projection"
        )
    settings_keys = payload.get("settings_keys")
    if settings_keys is not None:
        if not isinstance(settings_keys, list) or not all(
            isinstance(key, str) and key for key in settings_keys
        ):
            raise StorageError(
                "database_protocol_error", "Invalid conversation settings projection"
            )
        settings_keys = list(dict.fromkeys(settings_keys))[:64]
    metadata_only = not include_messages
    default_limit = 1000
    for value in (
        updated_at_gte,
        updated_at_gt,
        created_at_lt,
        ids,
        project_path,
        title_contains,
    ):
        if value is not None:
            metadata_only = False
            break
    if metadata_only:
        # The sidebar poll is metadata-only (every consumer reads
        # ``document['metadata']``). Without a bound it recovers the complete
        # personal history after a browser cache loss, and the projection
        # below skips the GiB-scale transcript archive entirely.
        default_limit = 10000
    limit = _integer(payload, "limit", default=default_limit, minimum=0, maximum=10000)
    where: list[str] = []
    params: list[Any] = []
    if user_id is not None:
        where.append("user_id = ?")
        params.append(user_id)
    if project_path is not None:
        project_path_expression = (
            "settings_json ->> 'projectPath'"
            if session.backend == "postgres"
            else "json_extract(settings_json, '$.projectPath')"
        )
        where.append(f"{project_path_expression} = ?")
        params.append(project_path)
    if ids is not None:
        where.append("id IN (%s)" % ",".join("?" for _ in ids))
        params.extend(ids)
    if updated_at_gte is not None:
        where.append("updated_at_ms >= ?")
        params.append(updated_at_gte)
    if updated_at_gt is not None:
        where.append("updated_at_ms > ?")
        params.append(updated_at_gt)
    if created_at_lt is not None:
        where.append("created_at_ms < ?")
        params.append(created_at_lt)
    if title_contains is not None:
        title_match_ids = _conversation_title_match_ids(
            session,
            where=where,
            params=params,
            order_by=order_by,
            title_contains=title_contains,
            limit=limit,
        )
        if not title_match_ids:
            return []
        where.append("id IN (%s)" % ",".join("?" for _ in title_match_ids))
        params.extend(title_match_ids)
    if not where:
        where.append("1 = 1")
    # Metadata-only listings never touch turn projections or search fragments.
    settings_projection = (
        "'{}' AS settings_json" if settings_keys == [] else "settings_json"
    )
    projection = (
        "id, user_id, title, messages_json, created_at_ms, "
        f"updated_at_ms, {settings_projection}, msg_count, rev "
        if include_messages
        else "id, user_id, title, created_at_ms, "
        f"updated_at_ms, {settings_projection}, msg_count, rev "
    )
    rows = session.fetch_all(
        "SELECT "
        + projection
        + "FROM storage_conversations WHERE "
        + " AND ".join(where)
        + f" ORDER BY {order_sql} LIMIT ?",
        tuple(params + [limit]),
    )
    _backfill_turn_message_counts(session, rows)
    if include_messages:
        _derive_turn_messages_bulk(session, rows)
    return [
        _conversation_document(
            row,
            include_messages=include_messages,
            messages=row.get("_projected_messages"),
            settings_keys=settings_keys,
        )
        for row in rows
    ]


def _derive_turn_messages_bulk(session: Session, rows: list[dict]) -> None:
    """Project turns, falling back to frozen pre-cutover transcript archives."""
    if not rows:
        return
    identities = [
        (str(row["id"]), int(row["user_id"])) for row in rows
    ]
    where = " OR ".join(
        "(conversation_id=? AND user_id=?)" for _ in identities)
    params = tuple(value for identity in identities for value in identity)
    turn_rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns "
        "WHERE (" + where + ") AND lane_id = 'main' "
        "ORDER BY user_id,conversation_id,ordinal",
        params,
    )
    by_identity: dict[tuple[str, int], list[dict]] = {}
    for turn_row in turn_rows:
        identity = (
            str(turn_row["conversation_id"]), int(turn_row["user_id"])
        )
        by_identity.setdefault(identity, []).append(turn_row)
    for row in rows:
        identity = (str(row["id"]), int(row["user_id"]))
        projected = [
            _turn_to_legacy_message(_turn_public(session, turn_row))
            for turn_row in by_identity.get(identity, [])
        ]
        row["_projected_messages"] = (
            projected
            if projected
            else _archived_conversation_messages(row.get("messages_json"))
        )


def _activity_timestamp(value: Any) -> int:
    """Mirror the report reader's tolerant timestamp coercion."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _activity_interval(
    timestamp_ms: int,
    boundaries_ms: list[int],
) -> int | None:
    if timestamp_ms < boundaries_ms[0] or timestamp_ms >= boundaries_ms[-1]:
        return None
    index = bisect_right(boundaries_ms, timestamp_ms) - 1
    return index if 0 <= index < len(boundaries_ms) - 1 else None


def _conversation_activity_dates(
    session: Session,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Count owner conversations active in explicit calendar intervals.

    Candidate headers are selected first. Turn-native transcripts project only
    their timestamp scalar in bounded ID batches; frozen pre-Turn archives load
    four at a time and decode one at a time. The response therefore grows with
    the number of calendar intervals, never with transcript bytes.
    """
    user_id = _integer(payload, "user_id", minimum=1)
    updated_at_gte = payload.get("updated_at_gte")
    created_at_lt = payload.get("created_at_lt")
    for value in (updated_at_gte, created_at_lt):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise StorageError(
                "database_protocol_error", "Invalid conversation time filter"
            )
    if updated_at_gte is None:
        raise StorageError(
            "database_protocol_error", "updated_at_gte is required"
        )
    boundaries = payload.get("day_boundaries_ms")
    if (
        not isinstance(boundaries, list)
        or not 2 <= len(boundaries) <= _ACTIVITY_DATE_MAX_INTERVALS + 1
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in boundaries
        )
        or any(left >= right for left, right in zip(boundaries, boundaries[1:]))
    ):
        raise StorageError(
            "database_protocol_error", "Invalid activity date boundaries"
        )
    boundaries_ms = list(boundaries)
    limit = _integer(payload, "limit", default=10_000, minimum=1, maximum=10_000)
    where = ["user_id = ?", "updated_at_ms >= ?"]
    params: list[Any] = [user_id, updated_at_gte]
    if created_at_lt is not None:
        where.append("created_at_ms < ?")
        params.append(created_at_lt)
    candidates = session.fetch_all(
        "SELECT id,created_at_ms,updated_at_ms "
        "FROM storage_conversations WHERE " + " AND ".join(where)
        + " ORDER BY updated_at_ms DESC,id DESC LIMIT ?",
        tuple(params + [limit]),
    )
    counts = [0] * (len(boundaries_ms) - 1)
    projection_document_expression = (
        "COALESCE(cp.projection_json,t.projection_json)")
    timestamp_expression = (
        f"{projection_document_expression} -> 'timestamp'"
        if session.backend == "postgres"
        else f"json_extract({projection_document_expression}, '$.timestamp')"
    )
    for start in range(0, len(candidates), _ACTIVITY_DATE_BATCH_SIZE):
        batch = candidates[start:start + _ACTIVITY_DATE_BATCH_SIZE]
        by_id = {str(row["id"]): row for row in batch}
        ids = list(by_id)
        placeholders = ",".join("?" for _ in ids)
        turn_rows = session.fetch_all(
            "SELECT t.turn_id,t.conversation_id,t.conversation_id AS cid,t.user_id,"
            "t.current_attempt_id,t.status,t.projection_revision,"
            "t.projection_checkpoint_revision,"
            "t.projection_materialized_revision,t.projection_patch_count,"
            "t.projection_patch_bytes,cp.turn_id AS checkpoint_turn_id,"
            "t.created_at AS turn_created_at,"
            f"{timestamp_expression} AS projection_timestamp "
            "FROM storage_conversation_turns AS t LEFT JOIN "
            "storage_turn_projection_checkpoints AS cp ON "
            "cp.turn_id=t.turn_id AND cp.conversation_id=t.conversation_id "
            "AND cp.user_id=t.user_id AND cp.attempt_id=t.current_attempt_id "
            "AND cp.projection_revision=t.projection_checkpoint_revision "
            "WHERE t.user_id=? AND t.lane_id='main' "
            "AND t.conversation_id IN ("
            + placeholders
            + ") ORDER BY t.conversation_id,t.ordinal",
            tuple([user_id, *ids]),
        )
        turn_backed: set[str] = set()
        active_by_id: dict[str, set[int]] = {
            conversation_id: set() for conversation_id in ids
        }
        for turn in turn_rows:
            conversation_id = str(turn["cid"])
            header = by_id.get(conversation_id)
            if header is None:
                continue
            turn_backed.add(conversation_id)
            projected = turn.get("projection_timestamp")
            has_external_projection = (
                turn.get("projection_checkpoint_revision") is not None
                or turn.get("projection_materialized_revision") is not None
                or bool(turn.get("projection_patch_count"))
                or bool(turn.get("projection_patch_bytes"))
            )
            if has_external_projection:
                head = projection_head_state(turn)
                if (
                    head.checkpoint_active
                    and turn.get("checkpoint_turn_id") is None
                ):
                    raise StorageError(
                        "database_integrity",
                        "Turn projection checkpoint is missing",
                    )
                if head.active:
                    projected = projection_from_turn_row(
                        session, turn).get("timestamp")
            message_value = (
                projected if projected else turn.get("turn_created_at")
            )
            fallback = _activity_timestamp(
                header.get("updated_at_ms") or header.get("created_at_ms") or 0
            )
            timestamp_ms = _activity_timestamp(message_value) or fallback
            interval = _activity_interval(timestamp_ms, boundaries_ms)
            if interval is not None:
                active_by_id[conversation_id].add(interval)
        legacy_ids = [
            conversation_id
            for conversation_id in ids
            if conversation_id not in turn_backed
        ]
        for legacy_start in range(
            0, len(legacy_ids), _ACTIVITY_ARCHIVE_BATCH_SIZE
        ):
            archive_ids = legacy_ids[
                legacy_start:legacy_start + _ACTIVITY_ARCHIVE_BATCH_SIZE
            ]
            archive_placeholders = ",".join("?" for _ in archive_ids)
            archives = session.fetch_all(
                "SELECT id,messages_json FROM storage_conversations "
                "WHERE user_id=? AND id IN ("
                + archive_placeholders
                + ") ORDER BY id",
                tuple([user_id, *archive_ids]),
            )
            by_archive_id = {str(row["id"]): row for row in archives}
            if set(by_archive_id) != set(archive_ids):
                raise StorageError(
                    "database_integrity", "Conversation archive disappeared"
                )
            for conversation_id in archive_ids:
                header = by_id[conversation_id]
                fallback = _activity_timestamp(
                    header.get("updated_at_ms")
                    or header.get("created_at_ms")
                    or 0
                )
                for message in _archived_conversation_messages(
                    by_archive_id[conversation_id].get("messages_json")
                ):
                    timestamp_ms = _activity_timestamp(
                        message.get("timestamp", 0)
                    ) or fallback
                    interval = _activity_interval(timestamp_ms, boundaries_ms)
                    if interval is not None:
                        active_by_id[conversation_id].add(interval)
        for active_intervals in active_by_id.values():
            for interval in active_intervals:
                counts[interval] += 1
    return {"candidate_count": len(candidates), "counts": counts}


def _conversation_count(session: Session, payload: Mapping[str, Any]) -> Any:
    """Authoritative conversation count for a user, without touching blobs.

    The sidebar needs the REAL total (not the bounded window length) so the
    browser can distinguish "list is a recent page" from "list is complete"
    before it prunes locally-cached conversations. A bare ``COUNT(*)`` over
    the indexed ``user_id`` keeps the poll cheap.
    """
    user_id = _integer(payload, "user_id", minimum=1)
    where = "WHERE user_id = ?"
    params = (user_id,)
    row = session.fetch_one(
        "SELECT COUNT(*) AS c FROM storage_conversations " + where, params
    )
    return {"count": int(row["c"]) if row else 0}


def _conversation_search_op(session: Session, payload: Mapping[str, Any]) -> Any:
    """Search canonical settled main-lane turn fragments."""
    query_raw = payload.get("query")
    if not isinstance(query_raw, str):
        raise StorageError(
            "database_protocol_error", "Invalid conversation search query"
        )
    query = query_raw.strip().lower()
    user_id = _integer(payload, "user_id", minimum=1)
    limit = _integer(payload, "limit", default=50, minimum=1, maximum=200)
    radius = _integer(payload, "snippet_radius", default=40, minimum=0, maximum=400)
    if len(query) < 2:
        return []
    turn_head = "lower(substr(s.search_text, 1, 10000))"

    def _like(term: str) -> str:
        return (
            "%"
            + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )

    def _match_clause(terms: list[str]) -> tuple[str, list[Any]]:
        likes = [_like(term) for term in terms]
        turn_matches = " AND ".join(
            "EXISTS (SELECT 1 FROM storage_search_turns AS s "
            "WHERE s.conversation_id=c.id AND s.user_id=c.user_id "
            "AND s.lane_id='main' AND "
            f"{turn_head} LIKE ? ESCAPE '\\')"
            for _ in terms
        )
        return turn_matches, likes

    def _fetch(
        terms: list[str], excluded_ids: list[str], fetch_limit: int
    ) -> list[dict[str, Any]]:
        clauses, params = _match_clause(terms)
        if excluded_ids:
            clauses += " AND c.id NOT IN (" + ",".join(
                "?" for _ in excluded_ids) + ")"
            params.extend(excluded_ids)
        return session.fetch_all(
            "SELECT c.id FROM storage_search_conversations AS c "
            "WHERE c.user_id = ? AND "
            + clauses
            + " ORDER BY c.updated_at_ms DESC, c.id DESC LIMIT ?",
            tuple([user_id, *params, fetch_limit]),
        )

    rows = _fetch([query], [], limit)
    words = query.split()
    if len(rows) < limit and len(words) > 1:
        found = [row["id"] for row in rows]
        rows.extend(_fetch(words, found, limit - len(rows)))

    width = 2 * radius + len(query)
    items = []
    for row in rows:
        snippet_terms = [query]
        if words and words[0] != query:
            snippet_terms.append(words[0])
        snippet_where = " OR ".join(
            f"{turn_head} LIKE ? ESCAPE '\\'" for _ in snippet_terms)
        fragment = session.fetch_one(
            "SELECT substr(s.search_text, 1, 10000) AS head "
            "FROM storage_search_turns AS s "
            "WHERE s.conversation_id=? AND s.user_id=? "
            "AND s.lane_id='main' AND (" + snippet_where + ") "
            "ORDER BY CASE WHEN " + turn_head
            + " LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END, s.ordinal LIMIT 1",
            tuple([
                row["id"], user_id,
                *[_like(term) for term in snippet_terms],
                _like(query),
            ]),
        )
        head = str(fragment["head"] or "") if fragment is not None else ""
        lowered = head.lower()
        pos = lowered.find(query)
        if pos < 0 and words:
            pos = lowered.find(words[0])
        snippet = ""
        if pos >= 0 and radius > 0:
            snippet = head[max(0, pos - radius) : pos - radius + width]
            snippet = snippet.replace("\n", " ").strip()
            if snippet:
                snippet = "…" + snippet + "…"
        items.append({"id": row["id"], "snippet": snippet})
    return items


def _conversation_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise StorageError("database_protocol_error", "Invalid conversation metadata")
    unknown = set(metadata) - _CONVERSATION_METADATA
    if unknown:
        raise StorageError(
            "database_protocol_error",
            f"Unsupported conversation metadata: {sorted(unknown)}",
        )
    result = dict(metadata)
    if "settings" in result and not isinstance(result["settings"], (str, Mapping)):
        raise StorageError("database_protocol_error", "Invalid conversation settings")
    return result


def _conversation_create(session: Session, payload: Mapping[str, Any]) -> Any:
    """Create an owner-scoped conversation header without a transcript.

    Turn commands are the only way to add transcript content. Keeping header
    creation separate makes empty conversations explicit without reopening a
    whole-document writer.
    """
    conv_id, user_id = _conversation_identity(payload)
    settings = payload.get("settings") or {}
    if not isinstance(settings, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid conversation settings")
    title = payload.get("title") or "New Chat"
    if not isinstance(title, str):
        raise StorageError("database_protocol_error", "Invalid conversation title")
    now = int(time.time() * 1000)
    created_at = _integer(payload, "created_at", default=now, minimum=0)
    updated_at = _integer(payload, "updated_at", default=created_at, minimum=0)
    session.lock_key("conversation", f"{user_id}:{conv_id}")
    active_id = session.fetch_one(
        "SELECT 1 AS present FROM storage_conversations WHERE id=?",
        (conv_id,),
    )
    trashed_id = session.fetch_one(
        "SELECT 1 AS present FROM storage_conversation_trash "
        "WHERE conversation_id=?",
        (conv_id,),
    )
    if active_id is not None or trashed_id is not None:
        raise StorageError("database_conflict", "Conversation already exists")
    session.execute(
        "INSERT INTO storage_conversations "
        "(id,user_id,title,messages_json,created_at_ms,updated_at_ms,"
        "settings_json,msg_count,search_text,rev) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0)",
        (
            conv_id,
            user_id,
            title[:500],
            _dump([]),
            created_at,
            updated_at,
            _dump(dict(settings)),
            "",
        ),
    )
    _mark_conversation_search_projection_dirty(session, conv_id, user_id)
    return {"applied": True, "rev": 0}


def _conversation_settings_update(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id = _conversation_identity(payload)
    updates = payload.get("updates")
    if not isinstance(updates, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid conversation settings updates"
        )
    replace = payload.get("replace", False)
    if not isinstance(replace, bool):
        raise StorageError(
            "database_protocol_error", "Invalid conversation settings replace flag"
        )
    expected_settings = payload.get("expected_settings")
    if replace and not isinstance(expected_settings, Mapping):
        raise StorageError(
            "database_protocol_error",
            "Replacing conversation settings requires expected_settings",
        )

    session.lock_key("conversation", f"{user_id}:{conv_id}")
    row = session.fetch_one(
        "SELECT settings_json, rev FROM storage_conversations "
        "WHERE id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    if row is None:
        return {"applied": False, "missing": True, "rev": None}
    actual = int(row["rev"])
    expected = payload.get("expected_rev")
    if expected is not None:
        expected = _integer(payload, "expected_rev", minimum=0)
        if expected != actual:
            return {"applied": False, "missing": False, "rev": actual}
    settings = _load(row["settings_json"]) or {}
    if not isinstance(settings, Mapping):
        raise StorageError("database_integrity", "Conversation settings are malformed")
    current = dict(settings)
    if replace:
        # Settings-only changes intentionally do not advance transcript rev.
        # Compare the complete settings snapshot under the storage lock instead:
        # this is a real cross-process CAS, supports key deletion, and cannot
        # clobber a concurrent mutation that happened after the caller's read.
        if current != dict(expected_settings):
            return {
                "applied": False,
                "missing": False,
                "conflict": True,
                "rev": actual,
            }
        merged = dict(updates)
    else:
        merged = current
        merged.update(dict(updates))
    session.execute(
        "UPDATE storage_conversations SET settings_json = ? "
        "WHERE id = ? AND user_id = ?",
        (_dump(merged), conv_id, user_id),
    )
    return {
        "applied": True,
        "missing": False,
        "conflict": False,
        "rev": actual,
    }


def _conversation_metadata_update(session: Session, payload: Mapping[str, Any]) -> Any:
    """Update scalar conversation metadata without touching transcript state.

    Transcript revisions describe turn mutations. Renames must therefore not
    replay an archived message array or advance ``rev`` merely to change a
    label. This operation is the sole metadata write boundary for that case.
    """
    conv_id, user_id = _conversation_identity(payload)
    updates = payload.get("updates")
    if not isinstance(updates, Mapping) or not updates:
        raise StorageError(
            "database_protocol_error", "Invalid conversation metadata updates"
        )
    unknown = set(updates) - {"title", "updated_at"}
    if unknown:
        raise StorageError(
            "database_protocol_error",
            f"Unsupported conversation metadata updates: {sorted(unknown)}",
        )
    title = updates.get("title")
    if title is not None and not isinstance(title, str):
        raise StorageError("database_protocol_error", "Invalid conversation title")
    updated_at = updates.get("updated_at")
    if updated_at is not None and (
        isinstance(updated_at, bool) or not isinstance(updated_at, int)
        or updated_at < 0
    ):
        raise StorageError("database_protocol_error", "Invalid update timestamp")

    session.lock_key("conversation", f"{user_id}:{conv_id}")
    row = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if row is None:
        return {"applied": False, "missing": True, "rev": None}
    assignments: list[str] = []
    values: list[Any] = []
    if title is not None:
        assignments.append("title=?")
        values.append(title)
    if updated_at is not None:
        assignments.append("updated_at_ms=?")
        values.append(updated_at)
    session.execute(
        "UPDATE storage_conversations SET " + ",".join(assignments)
        + " WHERE id=? AND user_id=?",
        tuple([*values, conv_id, user_id]),
    )
    _mark_conversation_search_projection_dirty(session, conv_id, user_id)
    return {"applied": True, "missing": False, "rev": int(row["rev"])}


def _drop_active_conversation_rows(
    session: Session,
    conv_id: str,
    user_id: int,
    *,
    retain_recoverable_rows: bool,
) -> bool:
    """Remove the live authority and every executable child row.

    Search fragments and compaction archives are immutable derived/history
    rows.  A recoverable delete keeps them detached until restore or retention
    purge; a permanent purge removes them as well.  No active reader can reach
    either while the conversation header and turns are absent.
    """
    _mark_conversation_search_projection_dirty(session, conv_id, user_id)
    cached_turns = session.fetch_all(
        "SELECT turn_id,conversation_id,user_id,current_attempt_id "
        "FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? "
        "AND current_attempt_id IS NOT NULL",
        (conv_id, user_id),
    )
    for cached_turn in cached_turns:
        discard_projection_cache_for_row(session, cached_turn)
    timer_ids = session.fetch_all(
        "SELECT id FROM storage_timers WHERE conv_id=? AND user_id=?",
        (conv_id, user_id),
    )
    for timer in timer_ids:
        session.execute(
            "DELETE FROM storage_timer_poll_log WHERE timer_id=?", (timer["id"],)
        )
    session.execute(
        "DELETE FROM storage_timers WHERE conv_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_queue_items WHERE conv_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_autopilot_markers WHERE conv_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "UPDATE storage_scheduled_tasks SET enabled=0 "
        "WHERE user_id=? AND (target_conv_id=? OR source_conv_id=?)",
        (user_id, conv_id, conv_id),
    )
    session.execute(
        "DELETE FROM storage_attempt_events WHERE attempt_id IN ("
        "SELECT a.attempt_id FROM storage_generation_attempts AS a "
        "JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id "
        "WHERE t.conversation_id=? AND t.user_id=?)",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_raw_archives "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_generation_attempts WHERE turn_id IN ("
        "SELECT turn_id FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=?)",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_turn_projection_checkpoints "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    if not retain_recoverable_rows:
        session.execute(
            "DELETE FROM storage_compaction_archives "
            "WHERE conversation_id=? AND user_id=?",
            (conv_id, user_id),
        )
    session.execute(
        "DELETE FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_turn_tombstones "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_conversation_changes "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_conversation_sync_heads "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    deleted = session.execute(
        "DELETE FROM storage_conversations WHERE id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    return bool(deleted)


def _purge_conversation_identity(
    session: Session, conv_id: str, user_id: int,
) -> bool:
    active = _drop_active_conversation_rows(
        session,
        conv_id,
        user_id,
        retain_recoverable_rows=False,
    )
    session.execute(
        "DELETE FROM storage_compaction_archives "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    trashed_turns = session.execute(
        "DELETE FROM storage_conversation_trash_turns "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    trashed_header = session.execute(
        "DELETE FROM storage_conversation_trash "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    return bool(active or trashed_turns or trashed_header)


def _materialize_external_turn_projections_for_trash(
    session: Session,
    conv_id: str,
    user_id: int,
) -> None:
    """Fold only checkpoint/head Turns into self-contained trash rows."""
    rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND ("
        "projection_checkpoint_revision IS NOT NULL OR "
        "projection_materialized_revision IS NOT NULL OR "
        "projection_patch_count<>0 OR projection_patch_bytes<>0) "
        "ORDER BY turn_id",
        (conv_id, user_id),
    )
    for row in rows:
        projection = projection_from_turn_row(session, row)
        changed = session.execute(
            "UPDATE storage_conversation_trash_turns SET projection_json=? "
            "WHERE conversation_id=? AND user_id=? AND turn_id=?",
            (_dump(projection), conv_id, user_id, str(row["turn_id"])),
        )
        if changed != 1:
            raise StorageError(
                "database_integrity",
                "Turn disappeared while its trash projection was materialized",
            )


def _conversation_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    """Atomically move one live conversation into the recoverable trash.

    The active header and turns disappear in the same transaction, so every
    existing read/write path naturally treats the conversation as absent and
    late executor frames cannot resurrect it.  Trash rows are normalized turn
    records, not a second live transcript implementation.
    """
    conv_id, user_id = _conversation_identity(payload)
    session.lock_key("conversation", f"{user_id}:{conv_id}")
    header = session.fetch_one(
        "SELECT title,messages_json,created_at_ms,updated_at_ms,settings_json,"
        "msg_count,rev "
        "FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if header is None:
        already_deleted = session.fetch_one(
            "SELECT 1 AS present FROM storage_conversation_trash "
            "WHERE conversation_id=? AND user_id=?",
            (conv_id, user_id),
        ) is not None
        return {"deleted": False, "alreadyDeleted": already_deleted}

    now = int(time.time() * 1000)
    count_row = session.fetch_one(
        "SELECT COUNT(*) AS c FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND lane_id='main'",
        (conv_id, user_id),
    )
    main_count = int(count_row["c"] or 0) if count_row else 0
    # Turn-native conversations keep msg_count=0 as a placeholder because
    # their transcript lives in storage_conversation_turns.  Pre-turn
    # archives are the reverse: msg_count is the only sidebar-visible size
    # for the frozen messages_json transcript, so preserve it verbatim when
    # non-zero instead of overwriting it with the (zero) turn count.
    archived_count = int(header["msg_count"] or 0)
    trash_msg_count = archived_count or main_count
    session.execute(
        "DELETE FROM storage_conversation_trash_turns "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_conversation_trash "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "INSERT INTO storage_conversation_trash("
        "conversation_id,user_id,title,messages_json,created_at_ms,"
        "updated_at_ms,settings_json,msg_count,rev,deleted_at_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            conv_id,
            user_id,
            str(header["title"] or "New Chat"),
            # Pre-turn conversations keep their ONLY durable transcript in
            # messages_json; dropping it here would make restore return an
            # empty chat.  Pass the raw bytes through unchanged so SQLite
            # keeps the JSONDOC BLOB (``str(b'...')`` would corrupt it).
            header["messages_json"] or _dump([]),
            int(header["created_at_ms"] or 0),
            int(header["updated_at_ms"] or 0),
            _dump(settings_without_live_runtime(header["settings_json"])),
            trash_msg_count,
            int(header["rev"] or 0),
            now,
        ),
    )
    session.execute(
        "INSERT INTO storage_conversation_trash_turns("
        "conversation_id,user_id,turn_id,lane_id,parent_turn_id,ordinal,actor,"
        "kind,run_id,status,projection_json,projection_revision,"
        "settlement_json,created_at,updated_at) "
        "SELECT conversation_id,user_id,turn_id,lane_id,parent_turn_id,ordinal,"
        "actor,kind,run_id,status,projection_json,projection_revision,"
        "settlement_json,created_at,updated_at "
        "FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    _materialize_external_turn_projections_for_trash(
        session, conv_id, user_id)
    session.execute(
        "UPDATE storage_conversation_trash_turns "
        "SET status='interrupted',settlement_json=?,updated_at=? "
        "WHERE conversation_id=? AND user_id=? "
        "AND status IN ('pending','running')",
        (
            _dump({
                "outcome": "interrupted",
                "cause": "conversation_deleted",
                "resumeOptions": [],
            }),
            now,
            conv_id,
            user_id,
        ),
    )
    deleted = _drop_active_conversation_rows(
        session,
        conv_id,
        user_id,
        retain_recoverable_rows=True,
    )
    if not deleted:
        raise StorageError(
            "database_integrity", "Conversation disappeared during delete"
        )
    return {"deleted": True, "recoverable": True, "deletedAt": now}


def _conversation_restore(session: Session, payload: Mapping[str, Any]) -> Any:
    """Restore one trashed conversation without reviving executable work."""
    conv_id, user_id = _conversation_identity(payload)
    session.lock_key("conversation", f"{user_id}:{conv_id}")
    if session.fetch_one(
        "SELECT 1 AS present FROM storage_conversations WHERE id=?", (conv_id,)
    ) is not None:
        return {"restored": False, "conflict": True, "missing": False}
    header = session.fetch_one(
        "SELECT * FROM storage_conversation_trash "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    if header is None:
        return {"restored": False, "conflict": False, "missing": True}
    restored_revision = int(header["rev"] or 0) + 1
    settings = settings_without_live_runtime(header["settings_json"])
    session.execute(
        "INSERT INTO storage_conversations("
        "id,user_id,title,messages_json,created_at_ms,updated_at_ms,"
        "settings_json,msg_count,search_text,rev) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            conv_id,
            user_id,
            str(header["title"] or "New Chat"),
            header["messages_json"] or _dump([]),
            int(header["created_at_ms"] or 0),
            int(header["updated_at_ms"] or 0),
            _dump(settings),
            int(header["msg_count"] or 0),
            "",
            restored_revision,
        ),
    )
    session.execute(
        "INSERT INTO storage_conversation_turns("
        "turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,actor,"
        "kind,run_id,status,current_attempt_id,projection_json,"
        "projection_revision,settlement_json,created_at,updated_at) "
        "SELECT turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,"
        "actor,kind,run_id,status,NULL,projection_json,projection_revision,"
        "settlement_json,created_at,updated_at "
        "FROM storage_conversation_trash_turns "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    restored_turns = session.fetch_all(
        "SELECT turn_id FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? ORDER BY lane_id,ordinal",
        (conv_id, user_id),
    )
    _mark_conversation_search_projection_dirty(session, conv_id, user_id)
    session.execute(
        "DELETE FROM storage_conversation_trash_turns "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    session.execute(
        "DELETE FROM storage_conversation_trash "
        "WHERE conversation_id=? AND user_id=?",
        (conv_id, user_id),
    )
    return {
        "restored": True,
        "conflict": False,
        "missing": False,
        "rev": restored_revision,
        "turnCount": len(restored_turns),
    }


def _conversation_clone(session: Session, payload: Mapping[str, Any]) -> Any:
    """Create an inert snapshot even while the source is generating."""
    source_id, user_id = _conversation_identity(payload)
    return conversation_clone(
        session,
        payload,
        source_id=source_id,
        user_id=user_id,
    )


def _conversation_purge(session: Session, payload: Mapping[str, Any]) -> Any:
    """Permanently remove one active or trashed conversation (no HTTP route)."""
    conv_id, user_id = _conversation_identity(payload)
    session.lock_key("conversation", f"{user_id}:{conv_id}")
    return {"purged": _purge_conversation_identity(session, conv_id, user_id)}


def _conversation_trash_prune(session: Session, payload: Mapping[str, Any]) -> Any:
    """Purge a bounded oldest-first page after the recovery horizon."""
    deleted_before = _integer(payload, "deleted_before_ms", minimum=1)
    maximum = _integer(
        payload, "max_conversations", default=4, minimum=1, maximum=64
    )
    rows = session.fetch_all(
        "SELECT conversation_id,user_id FROM storage_conversation_trash "
        "WHERE deleted_at_ms<? "
        "ORDER BY deleted_at_ms,conversation_id,user_id LIMIT ?",
        (deleted_before, maximum),
    )
    purged = 0
    for row in rows:
        conv_id = str(row["conversation_id"])
        user_id = int(row["user_id"])
        session.lock_key("conversation", f"{user_id}:{conv_id}")
        purged += int(_purge_conversation_identity(session, conv_id, user_id))
    remaining = session.fetch_one(
        "SELECT 1 AS present FROM storage_conversation_trash "
        "WHERE deleted_at_ms<? LIMIT 1",
        (deleted_before,),
    ) is not None
    return {"purgedConversations": purged, "remaining": remaining}
