"""``conversation.turn_usage``: one conversation's per-Turn usage projection.

The daily-report calendar prices each main-lane message's ``usage``. Until
2026-11-08 it hydrated whole transcripts for that and held every candidate's
content in the API host. This query returns only the usage-bearing fields of
the main-lane Turns whose time falls in ``[timestamp_gte, timestamp_lt)``, so
its response grows with priced Turns, never with transcript bytes. The Turn's
``usage`` stays the per-Turn aggregate of its ``apiRounds``; neither is
rewritten here.

Turn time is the activity time ``conversation.activity_dates`` counts: the
projection ``timestamp`` read as an integer exactly like the native engine's
``tolerant_activity_timestamp``, else the Turn's creation time. Turn rows are
preselected from that scalar and only the selected Turns are materialized. A
frozen pre-Turn archive keeps raw message timestamps: a usage row is omitted
only when its timestamp is a non-zero integer outside the window, so the
caller can still place untimestamped rows by their transcript position.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters.base import Session
from lib.storage_authority_sql.operations_pkg._common import (
    _integer,
    _load,
    _required_text,
)
from lib.storage_authority_sql.operations_pkg._conversations import (
    _ACTIVITY_DATE_BATCH_SIZE,
    _archived_conversation_messages,
)
from lib.storage_authority_sql.turn_projection_head import (
    projection_from_turn_row,
    projection_head_state,
)

USAGE_ROW_FIELDS = ("model", "preset", "effort", "provider_id", "providerId")
_SETTINGS_FIELDS = ("model", "preset", "effort")
_MAX_TITLE_FALLBACK_CHARACTERS = 256
_INTEGER_TEXT = re.compile(r"[+-]?[0-9]+")
_I128_MIN, _I128_MAX = -(2 ** 127), 2 ** 127 - 1


def tolerant_turn_timestamp(value: Any) -> int:
    """Port of tofu-db ``tolerant_activity_timestamp`` (0 = no usable time)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value if _I128_MIN <= value <= _I128_MAX else 0
    if isinstance(value, float):
        if not math.isfinite(value):
            return 0
        # Rust ``f64 as i128`` truncates and saturates.
        return max(_I128_MIN, min(_I128_MAX, int(value)))
    if isinstance(value, str):
        text = value.strip()
        if not _INTEGER_TEXT.fullmatch(text):
            return 0
        parsed = int(text)
        return parsed if _I128_MIN <= parsed <= _I128_MAX else 0
    return 0


def _usage_row(message: Mapping[str, Any], index: int,
               timestamp: Any, *, has_timestamp: bool) -> dict[str, Any] | None:
    usage = message.get("usage")
    if not isinstance(usage, Mapping) or not usage:
        return None
    row: dict[str, Any] = {"index": index, "usage": dict(usage)}
    if has_timestamp:
        row["timestamp"] = timestamp
    row.update({field: message[field] for field in USAGE_ROW_FIELDS
                if field in message})
    return row


def _title_fallback(message: Any, characters: int) -> str:
    content = message.get("content") if isinstance(message, Mapping) else None
    return content[:characters] if isinstance(content, str) else ""


def _selected_turn_rows(session: Session, conv_id: str, user_id: int,
                        start: int, end: int) -> tuple[int, list[tuple[int, str]]]:
    """Return the main-lane count and the ``(index, turn_id)`` in the window."""
    document = "COALESCE(cp.projection_json,t.projection_json)"
    timestamp = (f"{document} -> 'timestamp'" if session.backend == "postgres"
                 else f"json_extract({document}, '$.timestamp')")
    turns = session.fetch_all(
        "SELECT t.turn_id,t.conversation_id,t.user_id,t.current_attempt_id,"
        "t.status,t.projection_revision,t.projection_checkpoint_revision,"
        "t.projection_materialized_revision,t.projection_patch_count,"
        "t.projection_patch_bytes,cp.turn_id AS checkpoint_turn_id,"
        f"t.created_at AS turn_created_at,{timestamp} AS projection_timestamp "
        "FROM storage_conversation_turns AS t LEFT JOIN "
        "storage_turn_projection_checkpoints AS cp ON "
        "cp.turn_id=t.turn_id AND cp.conversation_id=t.conversation_id "
        "AND cp.user_id=t.user_id AND cp.attempt_id=t.current_attempt_id "
        "AND cp.projection_revision=t.projection_checkpoint_revision "
        "WHERE t.user_id=? AND t.conversation_id=? AND t.lane_id='main' "
        "ORDER BY t.ordinal",
        (user_id, conv_id),
    )
    selected = []
    for index, turn in enumerate(turns):
        projected = turn.get("projection_timestamp")
        if (turn.get("projection_checkpoint_revision") is not None
                or turn.get("projection_materialized_revision") is not None
                or turn.get("projection_patch_count")
                or turn.get("projection_patch_bytes")):
            head = projection_head_state(turn)
            if head.checkpoint_active and turn.get("checkpoint_turn_id") is None:
                raise StorageError(
                    "database_integrity", "Turn projection checkpoint is missing")
            if head.active:
                projected = projection_from_turn_row(session, turn).get("timestamp")
        turn_time = (tolerant_turn_timestamp(projected)
                     or int(turn.get("turn_created_at") or 0))
        if not turn_time or start <= turn_time < end:
            selected.append((index, str(turn["turn_id"])))
    return len(turns), selected


def _turn_usage_rows(session: Session, conv_id: str, user_id: int,
                     start: int, end: int,
                     selected: list[tuple[int, str]]) -> list[dict[str, Any]]:
    rows = []
    for batch_start in range(0, len(selected), _ACTIVITY_DATE_BATCH_SIZE):
        batch = selected[batch_start:batch_start + _ACTIVITY_DATE_BATCH_SIZE]
        by_id = {
            str(row["turn_id"]): row for row in session.fetch_all(
                "SELECT * FROM storage_conversation_turns WHERE user_id=? "
                "AND conversation_id=? AND lane_id='main' AND turn_id IN ("
                + ",".join("?" for _ in batch) + ")",
                (user_id, conv_id, *(turn_id for _index, turn_id in batch)),
            )
        }
        for index, turn_id in batch:
            turn = by_id.get(turn_id)
            if turn is None:
                raise StorageError(
                    "database_integrity", "Turn disappeared during a usage read")
            projection = projection_from_turn_row(session, turn)
            turn_time = (tolerant_turn_timestamp(projection.get("timestamp"))
                         or int(turn["created_at"] or 0))
            if turn_time and not start <= turn_time < end:
                continue
            row = _usage_row(projection, index, turn_time, has_timestamp=True)
            if row is not None:
                rows.append(row)
    return rows


def _first_turn_fallback(session: Session, conv_id: str, user_id: int,
                         characters: int) -> str:
    first = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE user_id=? "
        "AND conversation_id=? AND lane_id='main' ORDER BY ordinal LIMIT 1",
        (user_id, conv_id),
    )
    if first is None:
        return ""
    return _title_fallback(projection_from_turn_row(session, first), characters)


def _archive_usage(session: Session, conv_id: str, user_id: int, start: int,
                   end: int, characters: int) -> tuple[int, str, list[dict]]:
    archive = session.fetch_one(
        "SELECT messages_json FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if archive is None:
        raise StorageError(
            "database_integrity", "Conversation archive disappeared")
    messages = _archived_conversation_messages(archive["messages_json"])
    rows = []
    for index, message in enumerate(messages):
        raw = message.get("timestamp")
        if (isinstance(raw, int) and not isinstance(raw, bool) and raw
                and not start <= raw < end):
            continue
        row = _usage_row(message, index, raw,
                         has_timestamp="timestamp" in message)
        if row is not None:
            rows.append(row)
    fallback = (_title_fallback(messages[0], characters)
                if messages and characters else "")
    return len(messages), fallback, rows


def _window(payload: Mapping[str, Any]) -> tuple[int, int, int]:
    start, end = payload.get("timestamp_gte"), payload.get("timestamp_lt")
    if any(isinstance(value, bool) or not isinstance(value, int)
           or not -(2 ** 63) <= value < 2 ** 63 for value in (start, end)) \
            or start >= end:
        raise StorageError(
            "database_protocol_error", "Invalid conversation usage window")
    characters = _integer(payload, "title_fallback_characters", default=0,
                          minimum=0, maximum=_MAX_TITLE_FALLBACK_CHARACTERS)
    return start, end, characters


def _conversation_turn_usage(session: Session, payload: Mapping[str, Any]) -> Any:
    """Return one owner conversation's usage rows in a turn-time window."""
    conv_id = _required_text(payload, "conv_id", 256)
    user_id = _integer(payload, "user_id", minimum=1)
    start, end, characters = _window(payload)
    header = session.fetch_one(
        "SELECT id,title,created_at_ms,updated_at_ms,settings_json "
        "FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if header is None:
        return None
    settings = _load(header["settings_json"]) or {}
    if not isinstance(settings, Mapping):
        raise StorageError("database_integrity", "Conversation document is malformed")
    title = str(header["title"] or "")
    message_count, selected = _selected_turn_rows(
        session, conv_id, user_id, start, end)
    if message_count:
        turns = _turn_usage_rows(session, conv_id, user_id, start, end, selected)
        fallback = (_first_turn_fallback(session, conv_id, user_id, characters)
                    if characters and not title else "")
    else:
        message_count, fallback, turns = _archive_usage(
            session, conv_id, user_id, start, end,
            characters if not title else 0)
    return {
        "metadata": {
            "id": str(header["id"]),
            "title": title,
            "created_at": int(header["created_at_ms"]),
            "updated_at": int(header["updated_at_ms"]),
            "settings": {field: settings[field] for field in _SETTINGS_FIELDS
                         if field in settings},
        },
        "message_count": message_count,
        "title_fallback": fallback,
        "turns": turns,
    }
