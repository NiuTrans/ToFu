"""Read-only, revision-fenced conversation diagnostics without projection hydration.

Entry point: ``_conversation_diagnostic`` (conversation domain). SQL returns only
metadata, bounded patch heads, or byte slices of one inline/checkpoint JSON base.
The client joins bytes before UTF-8/JSON/storage-codec decoding and patch replay.
Dialect-specific byte/JSON expressions belong here, never in API/repository code.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from typing import Any

from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters.base import Session
from lib.storage_authority_sql.operations_pkg._common import _dump, _load
from lib.storage_authority_sql.operations_pkg._conversations import _conversation_document
from lib.storage_authority_sql.operations_pkg._turns_core import _turn_public_metadata
from lib.storage_authority_sql.turn_projection_head import (
    PROJECTION_HEAD_MAX_PATCH_BYTES,
    PROJECTION_HEAD_MAX_PATCHES,
    TurnProjectionHeadState,
    projection_head_state,
)
from lib.turn_projection_patch import PROJECTION_PATCH_VERSION


_CHUNK_BYTES = 65536
_METADATA_BYTES = 1024 * 1024
_EVENT_ROWS = PROJECTION_HEAD_MAX_PATCHES * 4 + 1
# JSONB text adds at most one space per compact separator. It also expands
# finite binary64 exponents: the worst short token, 5e-324 (6 bytes), becomes
# 326 decimal bytes. Strings/integers do not expand; 64x bounds both effects.
# At most 64 MiB can cross the driver for a head; canonical admission and
# returned patch bytes stay at 1 MiB. Smaller heads get a proportional budget.
_POSTGRES_PATCH_TEXT_EXPANSION = 64
_TURN_COLUMNS = (
    "turn_id,conversation_id,user_id,presentation_id,lane_id,parent_turn_id,"
    "ordinal,actor,kind,run_id,status,current_attempt_id,projection_revision,"
    "projection_checkpoint_revision,projection_materialized_revision,"
    "projection_patch_count,projection_patch_bytes,created_at,updated_at"
)


def _invalid(message: str) -> StorageError:
    return StorageError("database_invalid_payload", message)


def _integrity(message: str) -> StorageError:
    return StorageError("database_integrity", message)


def _payload(payload: Mapping[str, Any]) -> tuple[str, int, str]:
    if not isinstance(payload, Mapping) or set(payload) - {
        "conv_id", "user_id", "kind", "revision", "index", "offset", "limit", "source_token",
    }:
        raise _invalid("Invalid conversation diagnostic payload")
    conv_id, user_id, kind = (
        payload.get("conv_id"), payload.get("user_id"), payload.get("kind"))
    if not isinstance(conv_id, str) or not conv_id.strip():
        raise _invalid("Invalid diagnostic conversation identity")
    if (type(user_id) is not int or not 1 <= user_id <= 2**63 - 1
            or not isinstance(kind, str) or kind not in {"header", "turn", "chunk"}):
        raise _invalid("Invalid diagnostic owner or kind")
    for field in ("revision", "index", "offset", "limit"):
        if field not in payload:
            continue
        value = payload[field]
        if type(value) is not int or value < (1 if field == "limit" else 0):
            raise _invalid(f"Invalid diagnostic {field}")
        if field == "limit" and value > _CHUNK_BYTES:
            raise _invalid("Diagnostic chunk limit exceeds 65536 bytes")
    if kind != "header" and "index" not in payload:
        raise _invalid("Diagnostic turn index is required")
    if kind == "header" and set(payload) & {"index", "offset", "limit", "source_token"}:
        raise _invalid("Invalid fields for diagnostic header")
    if kind == "turn" and set(payload) & {"offset", "limit", "source_token"}:
        raise _invalid("Invalid fields for diagnostic turn")
    if kind == "chunk":
        token = payload.get("source_token")
        if (not isinstance(token, str) or len(token) != 64
                or any(character not in "0123456789abcdef" for character in token)):
            raise _invalid("Diagnostic chunk requires a 64-lowercase-hex source_token")
    return conv_id, user_id, kind


def _bytes(session: Session, expression: str) -> str:
    # JSONDOC is SQLite text/blob or PostgreSQL jsonb. PostgreSQL's server-side
    # text representation is the source, not a driver-decoded/reserialized dict.
    if session.backend == "postgres":
        return f"convert_to(({expression})::text, 'UTF8')"
    return f"CAST(({expression}) AS BLOB)"


def _json_type(session: Session, expression: str) -> str:
    if session.backend == "postgres":
        return f"jsonb_typeof({expression})"
    text = f"CAST(({expression}) AS TEXT)"
    return f"CASE WHEN json_valid({text}) THEN json_type({text}) ELSE 'invalid' END"


def _bounded_json(session: Session, column: str) -> str:
    raw = _bytes(session, column)
    return f"CASE WHEN length({raw}) <= {_METADATA_BYTES} THEN {raw} END"


def _object(raw: Any, label: str) -> dict[str, Any]:
    if raw is None:
        raise _integrity(f"Diagnostic {label} exceeds its metadata budget")
    try:
        value = _load(raw)
    except (ValueError, TypeError) as exc:
        raise _integrity(f"Diagnostic {label} is malformed") from exc
    if not isinstance(value, Mapping):
        raise _integrity(f"Diagnostic {label} must be an object")
    return dict(value)


def _fence(session: Session, conv_id: str, user_id: int, revision: int) -> bool:
    # PostgreSQL's default READ COMMITTED snapshot is per statement. A final
    # revision witness rejects mixed reads without locks, writes, or a new
    # transaction-isolation policy. SQLite already uses a read transaction.
    row = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if row is None:
        return False
    if row["rev"] != revision:
        raise StorageError("database_conflict", "Conversation diagnostic revision changed")
    return True


def _legacy_count(session: Session, conv_id: str, user_id: int) -> int:
    if session.backend == "postgres":
        count = (
            "CASE WHEN jsonb_typeof(messages_json)='array' "
            "THEN jsonb_array_length(messages_json) ELSE -1 END")
    else:
        text = "CAST(messages_json AS TEXT)"
        count = (
            f"CASE WHEN json_valid({text}) THEN CASE WHEN json_type({text})='array' "
            f"THEN json_array_length({text}) ELSE -1 END ELSE -1 END")
    row = session.fetch_one(
        f"SELECT {count} AS count FROM storage_conversations WHERE id=? AND user_id=?",
        (conv_id, user_id),
    )
    if row is None:
        raise StorageError("database_conflict", "Conversation disappeared during diagnostic")
    if row["count"] < 0:
        raise _integrity("Legacy conversation messages are malformed")
    return int(row["count"])


def _row_version(alias: str) -> str:
    # Only an intra-request READ COMMITTED witness, never a durable generation:
    # transaction IDs can wrap and backup/restore can reuse physical identities.
    return f"{alias}.xmin::text || ':' || {alias}.ctid::text"


def _source_token(
    turn: Mapping[str, Any], source: Mapping[str, Any], patches: list[dict[str, Any]],
    revision: int, index: int,
) -> str:
    # SQL has no restore-proof source generation. Normal writes increment rev,
    # but restore derives it from an archived header, not an identity lifetime.
    # Bind actual bytes, not length/timestamps/rowid/xmin or a process-local cache.
    # Byte-identical replacement with identical metadata is deliberately safe to
    # reuse; unlike native envelopes, SQL cannot distinguish such generations.
    envelope = [
        "tofu.sql-diagnostic-source/v1", revision, index,
        [turn[column] for column in _TURN_COLUMNS.split(",")],
        turn["settlement_json"], patches, source["total_bytes"],
        source.get("declared_bytes"), source.get("checkpoint_updated_at"),
        source["source_digest"],
    ]
    return hashlib.sha256(_dump(envelope)).hexdigest()


def _same_source(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    for key in ("total_bytes", "declared_bytes", "checkpoint_updated_at", "source_version"):
        if before.get(key) != after.get(key):
            raise StorageError("database_conflict", "Diagnostic projection source changed")


def _source(
    session: Session, turn: Mapping[str, Any], head: TurnProjectionHeadState,
    *, validate: bool, digest: bool = False,
    offset: int | None = None, limit: int = _CHUNK_BYTES,
) -> dict[str, Any]:
    checkpoint = head.checkpoint_active
    expression = "cp.projection_json" if checkpoint else "t.projection_json"
    raw = _bytes(session, expression)
    columns = [f"length({raw}) AS total_bytes"]

    source_alias = "cp" if checkpoint else "t"
    if session.backend == "postgres":
        columns.extend((
            f"{_row_version('t')} AS turn_version",
            f"{_row_version(source_alias)} AS source_version",
        ))
        if digest:
            # The exact same byte expression supplies both digest and slices.
            # PostgreSQL hashes server-side: only 64 hex bytes cross the driver.
            columns.append(f"encode(sha256({raw}), 'hex') AS source_digest")
    elif digest:
        columns.append(f"{source_alias}.rowid AS source_rowid")
    if validate:
        # Validate inside SQL; never select or decode the large JSON base in
        # Python. Codec and base-dependent patch validation also run at the
        # reconstructing client, after the complete source bytes are joined.
        columns.append(f"{_json_type(session, expression)} AS source_type")
    if checkpoint:
        columns.extend(("cp.projection_bytes AS declared_bytes", "cp.updated_at AS checkpoint_updated_at"))
    params: tuple[Any, ...] = ()
    if offset is not None:
        columns.append(f"substr({raw}, ?, ?) AS data")
        params = (offset + 1, limit)
    join = (
        " LEFT JOIN storage_turn_projection_checkpoints cp "
        "ON cp.turn_id=t.turn_id AND cp.conversation_id=t.conversation_id "
        "AND cp.user_id=t.user_id AND cp.attempt_id=COALESCE(t.current_attempt_id,'') "
        "AND cp.projection_revision=t.projection_checkpoint_revision"
        if checkpoint else "")
    row = session.fetch_one(
        "SELECT " + ",".join(columns) + " FROM storage_conversation_turns t" + join
        + " WHERE t.turn_id=? AND t.conversation_id=? AND t.user_id=? "
        "AND t.projection_revision=?",
        params + (
            turn["turn_id"], turn["conversation_id"], turn["user_id"],
            turn["projection_revision"],
        ),
    )
    if row is None:
        raise StorageError("database_conflict", "Diagnostic turn revision changed")

    if session.backend == "postgres" and row["turn_version"] != turn["row_version"]:
        raise StorageError("database_conflict", "Diagnostic turn source changed")
    size = row["total_bytes"]
    if type(size) is not int or size < 2:
        raise _integrity("Diagnostic projection base is missing or empty")
    if validate and row["source_type"] != "object":
        raise _integrity("Diagnostic projection base must be a valid JSON object")
    if checkpoint:
        declared = row["declared_bytes"]
        if type(declared) is not int or declared < 1:
            raise _integrity("Diagnostic checkpoint byte evidence is invalid")
        # JSONB canonical whitespace differs from the writer's original JSON.
        # Its wire source length is measured above, never the declared length.
        if session.backend != "postgres" and declared != size:
            raise _integrity("Diagnostic checkpoint byte evidence is inconsistent")
    if digest and session.backend == "sqlite":
        # SQLiteSession.query pins a read snapshot. blobopen streams TEXT/BLOB
        # without a full driver value or a Python JSON decode; both source tables
        # have rowids. Each request rehashes: a cached digest cannot safely detect
        # equal-size, equal-revision offline restore/replacement on SQLite.
        table = "storage_turn_projection_checkpoints" if checkpoint else "storage_conversation_turns"
        hasher = hashlib.sha256()
        with session.connection.blobopen(table, "projection_json", row["source_rowid"], readonly=True) as blob:
            if len(blob) != size:
                raise StorageError("database_conflict", "Diagnostic projection source changed")
            for position in range(0, size, _CHUNK_BYTES):
                block = blob.read(min(_CHUNK_BYTES, size - position))
                if len(block) != min(_CHUNK_BYTES, size - position):
                    raise _integrity("Diagnostic projection digest is incomplete")
                hasher.update(block)
                if position % _METADATA_BYTES == 0:
                    # blob.read bypasses the adapter's 1000-opcode deadline
                    # callback. This bounded probe runs >1000 VM instructions
                    # once per MiB (a SELECT 1 alone cannot ensure a callback).
                    session.fetch_one(
                        "SELECT count(*) AS digest_progress FROM ("
                        "WITH RECURSIVE ticks(n) AS (VALUES(0) UNION ALL "
                        "SELECT n+1 FROM ticks WHERE n<64) SELECT n FROM ticks)")
        row["source_digest"] = hasher.hexdigest()
    return row


def _validate_patch(patch: Mapping[str, Any], revision: int) -> None:
    for field, expected in (
        ("version", PROJECTION_PATCH_VERSION),
        ("baseRevision", revision - 1), ("targetRevision", revision),
    ):
        if type(patch.get(field)) is not int or patch[field] != expected:
            raise _integrity("Diagnostic projection patch chain is misbased or unsupported")
    operations = patch.get("operations")
    if not isinstance(operations, list):
        raise _integrity("Diagnostic projection patch operations are malformed")
    for operation in operations:
        if not isinstance(operation, Mapping):
            raise _integrity("Diagnostic projection patch operation is malformed")
        path = operation.get("path")
        if not isinstance(path, list) or not all(
            isinstance(part, str) or (type(part) is int and part >= 0) for part in path
        ):
            raise _integrity("Diagnostic projection patch path is malformed")
        op = operation.get("op")
        if not isinstance(op, str) or op not in {"set", "remove", "append_text", "append", "truncate"}:
            raise _integrity("Diagnostic projection patch operation is unsupported")
        if ((op == "set" and ("value" not in operation or (
                    not path and not isinstance(operation["value"], Mapping))))
                or (op == "remove" and not path)
                or (op == "append_text" and not isinstance(operation.get("value"), str))
                or (op == "append" and not isinstance(operation.get("value"), list))
                or (op == "truncate" and (
                    type(operation.get("length")) is not int or operation["length"] < 0))):
            raise _integrity("Diagnostic projection patch value is malformed")


def _patches(
    session: Session, turn: Mapping[str, Any], head: TurnProjectionHeadState,
) -> list[dict[str, Any]]:
    if not head.active:
        return []
    base = head.materialized_revision
    revision = turn["projection_revision"]
    if base is None or revision - base != head.patch_count or not turn["current_attempt_id"]:
        raise _integrity("Diagnostic projection patch revisions are inconsistent")
    if session.backend == "postgres":
        patch = "payload_json->'payload'->'projectionPatch'"
        patch_type = f"jsonb_typeof({patch})"
    else:
        payload = "CAST(payload_json AS TEXT)"
        patch = (
            f"CASE WHEN json_valid({payload}) THEN "
            f"json_extract({payload}, '$.payload.projectionPatch') END")
        patch_type = (
            f"CASE WHEN json_valid({payload}) THEN "
            f"json_type({payload}, '$.payload.projectionPatch') END")
    raw = _bytes(session, patch)
    where = (
        " FROM storage_attempt_events WHERE attempt_id=? AND conversation_id=? "
        "AND turn_id=? AND projection_revision>? AND projection_revision<=?")
    params = (
        turn["current_attempt_id"], turn["conversation_id"], turn["turn_id"], base, revision)
    version = f",{_row_version('storage_attempt_events')} AS event_version" if session.backend == "postgres" else ""
    candidate_sql = (
        "SELECT sequence,projection_revision" + version + ","
        + _json_type(session, "payload_json") + " AS envelope_type,"
        + patch_type + f" AS patch_type,length({raw}) AS patch_bytes"
        + where + " ORDER BY sequence LIMIT ?")
    candidates = session.fetch_all(candidate_sql, params + (_EVENT_ROWS,))
    if len(candidates) >= _EVENT_ROWS:
        raise _integrity("Diagnostic projection patch event range is ambiguous")
    selected: dict[int, Mapping[str, Any]] = {}
    # The head metadata has already passed the canonical 1 MiB admission gate.
    # Scale by its declared size, not the global max, so small forged heads
    # cannot cause a large payload fetch. Recheck canonical evidence below.
    physical_byte_budget = head.patch_bytes * (
        _POSTGRES_PATCH_TEXT_EXPANSION if session.backend == "postgres" else 1)
    physical_bytes = 0
    for row in candidates:
        if row["envelope_type"] != "object":
            raise _integrity("Diagnostic projection event envelope is malformed")
        if row["patch_type"] is None:
            continue
        if row["patch_type"] != "object":
            raise _integrity("Diagnostic projection patch must be an object")
        target, size = row["projection_revision"], row["patch_bytes"]
        if target in selected:
            raise _integrity("Diagnostic projection revision has duplicate patches")
        if type(target) is not int or type(size) is not int or size < 1:
            raise _integrity("Diagnostic projection patch byte evidence is invalid")
        physical_bytes += size
        if physical_bytes > physical_byte_budget:
            raise _integrity("Diagnostic projection patch head exceeds its physical byte budget")
        selected[target] = row
    if set(selected) != set(range(base + 1, revision + 1)):
        raise _integrity("Diagnostic projection patch chain has a gap")
    result = []
    canonical_bytes = 0
    for target in range(base + 1, revision + 1):
        candidate = selected[target]
        # Length gate remains on the actual payload read, not only the prior
        # metadata query. A malformed huge event is never delivered to Python.
        version_where = (
            f" AND {_row_version('storage_attempt_events')}=?" if session.backend == "postgres" else "")
        version_params = (candidate["event_version"],) if version_where else ()
        row = session.fetch_one(
            f"SELECT {raw} AS patch_source" + where
            + f" AND sequence=? AND projection_revision=? AND length({raw})=?" + version_where,
            params + (candidate["sequence"], target, candidate["patch_bytes"]) + version_params,
        )
        if row is None:
            raise StorageError("database_conflict", "Diagnostic projection patch source changed")
        decoded = _object(row["patch_source"], "projection patch")
        _validate_patch(decoded, target)
        canonical_bytes += len(_dump(decoded))
        if canonical_bytes > PROJECTION_HEAD_MAX_PATCH_BYTES:
            raise _integrity("Diagnostic projection patch head exceeds its canonical byte budget")
        result.append({"projectionRevision": target, "projectionPatch": decoded})
    if session.backend == "postgres" and session.fetch_all(candidate_sql, params + (_EVENT_ROWS,)) != candidates:
        raise StorageError("database_conflict", "Diagnostic projection patch source changed")
    if canonical_bytes != head.patch_bytes:
        raise _integrity("Diagnostic projection patch byte evidence is inconsistent")
    return result


def _read(
    session: Session, payload: Mapping[str, Any], header: Mapping[str, Any], kind: str,
) -> dict[str, Any]:
    conv_id, user_id, revision = header["id"], header["user_id"], header["rev"]
    counts = session.fetch_one(
        "SELECT COUNT(*) AS total_count FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? AND lane_id='main'", (conv_id, user_id))
    total = int(counts["total_count"])
    native = total > 0 or session.fetch_one(
        "SELECT 1 AS present FROM storage_conversation_turns "
        "WHERE conversation_id=? AND user_id=? LIMIT 1", (conv_id, user_id)) is not None
    if not native:
        total = _legacy_count(session, conv_id, user_id)
        native = total == 0
    if kind == "header":
        settings = _object(header["settings_json"], "conversation settings")
        document = _conversation_document({**header, "settings_json": settings}, include_messages=False)
        return {"revision": revision, "totalCount": total, "document": document, "turnNative": native}
    if not native:
        raise StorageError("database_unsupported", "Legacy messages have no bounded diagnostic turn source")
    index = payload["index"]
    if index >= total:
        raise _invalid("Diagnostic turn index is outside the main lane")
    settlement = "," + _bounded_json(session, "settlement_json") + " AS settlement_json"
    if session.backend == "postgres":
        settlement += f",{_row_version('storage_conversation_turns')} AS row_version"
    turn = session.fetch_one(
        "SELECT " + _TURN_COLUMNS + settlement
        + " FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? "
        "AND lane_id='main' ORDER BY ordinal LIMIT 1 OFFSET ?", (conv_id, user_id, index))
    if turn is None:
        raise StorageError("database_conflict", "Diagnostic turn rank changed")
    head = projection_head_state(turn)
    if type(turn["projection_revision"]) is not int or turn["projection_revision"] < 0:
        raise _integrity("Diagnostic turn projection revision is invalid")
    if head.active and turn["projection_revision"] - head.materialized_revision != head.patch_count:
        raise _integrity("Diagnostic projection patch revisions are inconsistent")
    turn["settlement_json"] = _object(turn["settlement_json"], "turn settlement")
    source = _source(session, turn, head, validate=True, digest=True)
    size = source["total_bytes"]
    patches = _patches(session, turn, head)
    token = _source_token(turn, source, patches, revision, index)
    if kind == "turn":
        if session.backend == "postgres":
            # The source, metadata and bounded patch head must coexist, even
            # when a replacement commits without advancing conversation rev.
            _same_source(source, _source(session, turn, head, validate=False))
        return {
            "revision": revision, "index": index, "totalCount": total,
            "turn": _turn_public_metadata(turn),
            "source": {"format": "projection", "totalBytes": size, "token": token},
            "patches": patches,
        }
    if payload["source_token"] != token:
        raise StorageError("database_conflict", "Diagnostic projection source token changed")
    offset = payload.get("offset", 0)
    if offset > size:
        raise _invalid("Diagnostic chunk offset exceeds the source length")
    chunk = _source(
        session, turn, head, validate=False, offset=offset, limit=payload.get("limit", _CHUNK_BYTES))
    _same_source(source, chunk)
    data = bytes(chunk["data"])
    end = offset + len(data)
    if len(data) != min(payload.get("limit", _CHUNK_BYTES), size - offset):
        raise _integrity("Diagnostic projection chunk is incomplete")
    return {
        "revision": revision, "index": index, "offset": offset, "totalBytes": size, "sourceToken": token,
        "data": base64.b64encode(data).decode("ascii"),
        "nextOffset": end if end < size else None,
    }


def _conversation_diagnostic(session: Session, payload: Mapping[str, Any]) -> Any:
    conv_id, user_id, kind = _payload(payload)
    metadata = (
        ",title,created_at_ms,updated_at_ms,msg_count,"
        + _bounded_json(session, "settings_json") + " AS settings_json"
        if kind == "header" else "")
    header = session.fetch_one(
        "SELECT id,user_id,rev" + metadata
        + " FROM storage_conversations WHERE id=? AND user_id=?", (conv_id, user_id))
    if header is None:
        return None
    revision = header["rev"]
    if type(revision) is not int or revision < 0:
        raise _integrity("Diagnostic conversation revision is invalid")
    if ((kind != "header" and "revision" not in payload)
            or ("revision" in payload and payload["revision"] != revision)):
        raise StorageError("database_conflict", "Diagnostic requires the exact conversation revision")
    try:
        result = _read(session, payload, header, kind)
    except StorageError:
        if not _fence(session, conv_id, user_id, revision):
            return None
        raise
    return result if _fence(session, conv_id, user_id, revision) else None


__all__ = ["_conversation_diagnostic"]
