"""Record, event-log, and rate-limit storage operation handlers."""

from __future__ import annotations

from collections.abc import Mapping
import re
import time
from typing import Any

import orjson

from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage_projection import (
    project_event_usage_for_storage,
    project_task_result_metadata_for_storage,
)
from lib.storage_sidecar.adapters.base import Session
from lib.storage_sidecar.projection_codec import ProjectionCodecError
from lib.storage_sidecar.task_result_field_codec import (
    decode_task_result_fields_from_storage,
    encode_task_result_fields_for_storage,
)
from lib.task_result_checkpoint_contract import (
    CONVERSATION_CACHE_PREFIX_HWM_SETTING,
    CONVERSATION_LAST_TURN_CACHE_READ_SETTING,
    TASK_RESULT_CACHE_FACT_MAXIMUM,
    TASK_RESULT_CACHE_PREFIX_HWM_FIELD,
    TASK_RESULT_CACHE_SETTINGS_CONTRACT,
    TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
    TASK_RESULT_LAST_TURN_CACHE_READ_FIELD,
)
from lib.storage_sidecar.task_event_codec import (
    decode_task_event_payload,
    encode_task_event_payload,
    task_event_decoded_size,
)


logger = get_logger(__name__)

_LEGACY_BLANK_EVENT_RECOVERY_MAX_ROWS = 100
_LEGACY_BLANK_EVENT_RECOVERY_PAYLOAD_BYTES = 4 * 1024 * 1024
_LEGACY_OPAQUE_EVENT_KIND = "__tofu_legacy_opaque__"
_RATE_LIMIT_PRUNE_BATCH = 256
_COST_EXPERIMENT_SCAN_PAGE_ROWS = 8
_COST_EXPERIMENT_SCAN_MAX_ROWS = 256


from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _expected_version,
    _integer,
    _load,
    _required_text,
)


def _encoded_task_result_document(value: Any) -> bytes:
    """Encode one public task result or reject private/malformed input."""
    try:
        return encode_task_result_fields_for_storage(value).stored_document
    except ProjectionCodecError as exc:
        raise StorageError(
            "database_protocol_error", "Task result storage encoding is invalid"
        ) from exc


def _hydrated_task_result_value(
    value: Any,
    *,
    fields: set[str] | None = None,
) -> Any:
    """Hydrate all or selected private fields at a semantic read boundary."""
    try:
        return decode_task_result_fields_from_storage(value, fields=fields)
    except ProjectionCodecError as exc:
        raise StorageError(
            "database_integrity", "Stored task result field encoding is invalid"
        ) from exc


def _load_record_value(namespace: str, raw_value: Any) -> Any:
    value = _load(raw_value)
    return (
        _hydrated_task_result_value(value)
        if namespace == "task_results"
        else value
    )


def _record_get(session: Session, payload: Mapping[str, Any]) -> Any:
    namespace = _required_text(payload, "namespace", 128)
    key = _required_text(payload, "key")
    row = session.fetch_one(
        "SELECT value_json, version, updated_at_ms FROM storage_records "
        "WHERE namespace = ? AND record_key = ?",
        (namespace, key),
    )
    if row is None:
        return None
    return {
        "value": _load_record_value(namespace, row["value_json"]),
        "version": int(row["version"]),
        "updated_at_ms": int(row["updated_at_ms"]),
    }


def _record_list(session: Session, payload: Mapping[str, Any]) -> Any:
    namespace = _required_text(payload, "namespace", 128)
    prefix = payload.get("prefix", "")
    if not isinstance(prefix, str) or len(prefix) > 512:
        raise StorageError(
            "database_protocol_error", "Invalid prefix in storage request"
        )
    limit = _integer(payload, "limit", default=100, minimum=1, maximum=1000)
    rows = session.fetch_all(
        "SELECT record_key, value_json, version, updated_at_ms "
        "FROM storage_records WHERE namespace = ? AND record_key LIKE ? "
        "ORDER BY record_key LIMIT ?",
        (namespace, prefix + "%", limit),
    )
    return [
        {
            "key": row["record_key"],
            "value": _load_record_value(namespace, row["value_json"]),
            "version": int(row["version"]),
            "updated_at_ms": int(row["updated_at_ms"]),
        }
        for row in rows
    ]


_TASK_RESULT_SUMMARY_ORDERS = {
    "created_at_desc",
    "completed_at_asc",
    "updated_at_asc",
}


def _task_results_replay_get(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    """Return one owner-scoped compact replay projection.

    A task-result row can carry MiB of cumulative content, thinking and tool
    diagnostics. Cursor clients need only owner/status/clocks until their last
    page, so keep those heavy fields inside the Sidecar unless the caller
    explicitly requests the terminal payload.
    """
    key = _required_text(payload, "key")
    user_id = _integer(payload, "user_id", minimum=1)
    include_terminal_payload = payload.get(
        "include_terminal_payload", False)
    if not isinstance(include_terminal_payload, bool):
        raise StorageError(
            "database_protocol_error",
            "include_terminal_payload must be boolean",
        )
    include_metadata = payload.get("include_metadata", False)
    if not isinstance(include_metadata, bool):
        raise StorageError(
            "database_protocol_error", "include_metadata must be boolean")
    row = session.fetch_one(
        "SELECT value_json, version, updated_at_ms FROM storage_records "
        "WHERE namespace=? AND record_key=?",
        ("task_results", key),
    )
    if row is None:
        return None
    try:
        value = _load(row["value_json"])
        replay_fields = {"metadata", "error"}
        if include_terminal_payload:
            replay_fields.update({"content", "thinking"})
        value = _hydrated_task_result_value(value, fields=replay_fields)
    except (TypeError, ValueError, orjson.JSONDecodeError) as exc:
        raise StorageError(
            "database_integrity", "Stored task result is invalid") from exc
    if not isinstance(value, Mapping):
        raise StorageError(
            "database_integrity", "Stored task result is not an object")
    raw_user_id = value.get("user_id")
    try:
        row_user_id = (
            0 if isinstance(raw_user_id, bool) else int(raw_user_id or 0))
    except (TypeError, ValueError, OverflowError):
        return None
    # Owner filtering precedes every projection so foreign and missing rows
    # are indistinguishable at the application boundary.
    if row_user_id != user_id:
        return None
    stored_task_id = str(value.get("task_id") or key)
    if stored_task_id != key:
        raise StorageError(
            "database_integrity", "Stored task-result identity is invalid")

    metadata = value.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = orjson.loads(metadata)
        except (orjson.JSONDecodeError, ValueError):
            metadata = {}
    metadata = metadata if isinstance(metadata, Mapping) else {}
    replay_metadata = (
        dict(metadata) if include_metadata else {
            field: metadata[field]
            for field in ("finishReason", "model", "requestId")
            if field in metadata
        }
    )
    try:
        created_at = int(value.get("created_at") or 0)
        completed_at = int(value.get("completed_at") or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StorageError(
            "database_integrity", "Stored task-result clocks are invalid"
        ) from exc
    if isinstance(value.get("created_at"), bool) or isinstance(
        value.get("completed_at"), bool
    ):
        raise StorageError(
            "database_integrity", "Stored task-result clocks are invalid")
    result = {
        "task_id": key,
        "conv_id": str(value.get("conv_id") or ""),
        "user_id": row_user_id,
        "status": str(value.get("status") or "running"),
        "error": value.get("error"),
        "metadata": replay_metadata,
        "created_at": created_at,
        "completed_at": completed_at,
        "version": int(row["version"]),
        "updated_at_ms": int(row["updated_at_ms"]),
    }
    if include_terminal_payload:
        result["content"] = value.get("content") or ""
        result["thinking"] = value.get("thinking") or ""
    return result


def _task_results_summary_list(
    session: Session, payload: Mapping[str, Any]
) -> Any:
    """Return bounded task-result metadata without shipping heavy values.

    ``task_results`` values contain the complete answer, thinking trace, tool
    rounds, and metadata.  Listing 1,000 raw records can therefore exceed the
    64 MiB storage frame even when the caller only needs status/identity
    fields (startup recovery, orphan detection, abort fencing, and the request
    inspector all did exactly that).

    Scan in small primary-key pages so the sidecar never materializes the
    former 1,000-value response.  Filters are checked after canonical JSON
    decoding to stay backend-neutral (SQLite BLOB and PostgreSQL BYTEA use the
    same operation contract).  ``scan_limit`` preserves a hard work bound;
    ``capped`` makes truncation explicit instead of silently pretending the
    result is exhaustive.
    """
    status = payload.get("status")
    if status is not None and (
        not isinstance(status, str) or not status or len(status) > 64
    ):
        raise StorageError(
            "database_protocol_error", "Invalid status in storage request"
        )
    conv_id = payload.get("conv_id")
    if conv_id is not None and (
        not isinstance(conv_id, str) or not conv_id or len(conv_id) > 512
    ):
        raise StorageError(
            "database_protocol_error", "Invalid conv_id in storage request"
        )
    user_id = payload.get("user_id")
    if user_id is not None:
        user_id = _integer(payload, "user_id", minimum=1)
    completed_before_ms = payload.get("completed_before_ms")
    if completed_before_ms is not None:
        completed_before_ms = _integer(
            payload, "completed_before_ms", minimum=0
        )
    limit = _integer(payload, "limit", default=100, minimum=1, maximum=1000)
    scan_limit = _integer(
        payload, "scan_limit", default=1000, minimum=1, maximum=10_000
    )
    order_by = str(payload.get("order_by") or "created_at_desc")
    if order_by not in _TASK_RESULT_SUMMARY_ORDERS:
        raise StorageError(
            "database_protocol_error", "Invalid task-result summary order"
        )

    # Eight rows keeps peak materialization bounded even when individual task
    # results approach the protocol's per-record size ceiling.
    page_size = 8
    after_key = payload.get("after_key", "")
    if not isinstance(after_key, str) or len(after_key) > 1024:
        raise StorageError(
            "database_protocol_error", "Invalid task recovery cursor"
        )
    scanned = 0
    invalid = 0
    summaries: list[dict[str, Any]] = []
    exhausted = False
    while scanned < scan_limit:
        rows = session.fetch_all(
            "SELECT record_key, value_json, version, updated_at_ms "
            "FROM storage_records WHERE namespace=? AND record_key>? "
            "ORDER BY record_key LIMIT ?",
            ("task_results", after_key, min(page_size, scan_limit - scanned)),
        )
        if not rows:
            exhausted = True
            break
        for row in rows:
            after_key = str(row["record_key"])
            scanned += 1
            try:
                value = _load(row["value_json"])
            except (TypeError, ValueError, orjson.JSONDecodeError):
                invalid += 1
                continue
            if not isinstance(value, Mapping):
                invalid += 1
                continue
            row_status = str(value.get("status") or "")
            row_conv_id = str(value.get("conv_id") or "")
            try:
                row_user_id = int(value.get("user_id") or 0)
            except (TypeError, ValueError, OverflowError):
                invalid += 1
                continue
            try:
                created_at = int(value.get("created_at") or 0)
                completed_at = int(value.get("completed_at") or 0)
            except (TypeError, ValueError, OverflowError):
                invalid += 1
                continue
            if status is not None and row_status != status:
                continue
            if conv_id is not None and row_conv_id != conv_id:
                continue
            if user_id is not None and row_user_id != user_id:
                continue
            if (
                completed_before_ms is not None
                and (not completed_at or completed_at >= completed_before_ms)
            ):
                continue
            summaries.append(
                {
                    "key": after_key,
                    "task_id": str(value.get("task_id") or after_key),
                    "conv_id": row_conv_id,
                    "user_id": row_user_id,
                    "status": row_status,
                    "created_at": created_at,
                    "completed_at": completed_at,
                    "version": int(row["version"]),
                    "updated_at_ms": int(row["updated_at_ms"]),
                }
            )

    if order_by == "created_at_desc":
        summaries.sort(
            key=lambda item: (item["created_at"], item["key"]), reverse=True
        )
    elif order_by == "completed_at_asc":
        summaries.sort(key=lambda item: (item["completed_at"], item["key"]))
    else:
        summaries.sort(key=lambda item: (item["updated_at_ms"], item["key"]))
    return {
        "records": summaries[:limit],
        "scanned": scanned,
        "invalid": invalid,
        "capped": not exhausted,
    }


def _project_cost_experiment_candidate(
    row: Mapping[str, Any],
    *,
    cutoff: int,
    experiment_id: str,
    experiment_id_bytes: bytes | None,
) -> tuple[dict[str, Any] | None, bool]:
    """Return one compact exact-match projection and its invalid-row bit."""
    try:
        if int(row.get("updated_at_ms") or 0) < cutoff:
            return None, False
        raw_value = row["value_json"]
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, True
    if isinstance(raw_value, str):
        if experiment_id not in raw_value:
            return None, False
    else:
        try:
            if (
                experiment_id_bytes is not None
                and experiment_id_bytes not in bytes(raw_value)
            ):
                return None, False
        except (TypeError, ValueError):
            return None, True
    try:
        value = _load(raw_value)
        value = _hydrated_task_result_value(value, fields={"metadata"})
    except (
        TypeError,
        ValueError,
        orjson.JSONDecodeError,
        StorageError,
    ):
        return None, True
    if not isinstance(value, dict):
        return None, True
    completed_at = value.get("completed_at")
    if (
        not isinstance(completed_at, int)
        or isinstance(completed_at, bool)
        or completed_at < cutoff
    ):
        return None, False
    metadata = value.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = orjson.loads(metadata)
        except (orjson.JSONDecodeError, ValueError):
            return None, True
    outcome = (
        (metadata or {}).get("costExperiment")
        if isinstance(metadata, dict)
        else None
    )
    if not isinstance(outcome, dict) or not outcome:
        return None, False
    outcome_experiment_id = str(
        outcome.get("experimentId") or outcome.get("experiment_id") or ""
    )
    if outcome_experiment_id != experiment_id:
        return None, False
    conv_id = str(value.get("conv_id") or "")
    return {
        "task_id": str(row["record_key"]),
        "conv_id": conv_id,
        "completed_at": int(completed_at),
        "outcome": outcome,
    }, False


def _task_results_cost_experiment_scan(
    session: Session, payload: Mapping[str, Any]
) -> Any:
    """Compact A/B-outcome projection over the ``task_results`` namespace.

    The cost-experiment report must NEVER ride ``record.list`` over this
    namespace: each value carries full content/thinking/tool blobs (MiB
    apiece), so a listing frame would blow the 64 MiB cap exactly like the
    retired ``conversation.list(include_messages=True)`` scan did.  Only the
    tiny outcome dict crosses the wire; heavy fields never leave the store.

    Rows whose conversation no longer exists (orphan recovery rows) or that
    belong to another user are excluded, matching the visibility fence the
    legacy task_results↔conversations join enforces.
    """
    cutoff = _integer(payload, "completed_at_gte", minimum=0)
    limit = _integer(payload, "limit", default=5000, minimum=1, maximum=10000)
    scan_limit = _integer(
        payload,
        "scan_limit",
        default=256,
        minimum=1,
        maximum=_COST_EXPERIMENT_SCAN_MAX_ROWS,
    )
    user_id = _integer(payload, "user_id", minimum=1)
    experiment_id = _required_text(payload, "experiment_id", 128)
    after_key = payload.get("after_key", "")
    if not isinstance(after_key, str) or len(after_key) > 1024:
        raise StorageError(
            "database_protocol_error", "Invalid cost-experiment scan cursor"
        )

    # ``storage_records`` deliberately remains a generic, backend-neutral
    # document table. Historical task rows predate the tiny top-level
    # experiment projection, so an exact SQL JSON predicate still reads and
    # parses every multi-kilobyte task result. More importantly, one SQLite
    # deadline interrupt loses all scan progress and the read retry restarts at
    # the first 177 MiB page. Walk the existing (namespace, record_key) primary
    # key instead: each RPC advances an explicit cursor and each fetch
    # materializes at most eight BLOBs. The route-level repository resumes up
    # to its independent hard row budget.
    parsed: list[dict[str, Any]] = []
    invalid = 0
    conv_ids: set[str] = set()
    try:
        experiment_id_bytes = experiment_id.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        # The public experiment contract is ASCII. A direct semantic-storage
        # caller may still carry an older Unicode id; preserve correctness by
        # disabling only the cheap byte prefilter for that compatibility case.
        experiment_id_bytes = None
    scanned = 0
    exhausted = False
    while scanned < scan_limit:
        page_limit = min(
            _COST_EXPERIMENT_SCAN_PAGE_ROWS,
            scan_limit - scanned,
        )
        page = session.fetch_all(
            "SELECT record_key, value_json, updated_at_ms "
            "FROM storage_records "
            "WHERE namespace = ? AND record_key > ? "
            "ORDER BY record_key LIMIT ?",
            ("task_results", after_key, page_limit),
        )
        if not page:
            exhausted = True
            break
        for row in page:
            after_key = str(row["record_key"])
            scanned += 1
            candidate, row_invalid = _project_cost_experiment_candidate(
                row,
                cutoff=cutoff,
                experiment_id=experiment_id,
                experiment_id_bytes=experiment_id_bytes,
            )
            invalid += int(row_invalid)
            if candidate is None:
                continue
            parsed.append(candidate)
            conv_id = candidate["conv_id"]
            if conv_id:
                conv_ids.add(conv_id)
        if len(page) < page_limit:
            exhausted = True
            break
    if not exhausted:
        # A full final page is ambiguous: it may end exactly at the source
        # boundary. Probe only the next primary key (never its BLOB) so an
        # exact 10,000-row repository scan is not falsely marked truncated.
        exhausted = not bool(session.fetch_all(
            "SELECT record_key FROM storage_records "
            "WHERE namespace = ? AND record_key > ? "
            "ORDER BY record_key LIMIT 1",
            ("task_results", after_key),
        ))
    owned: set[str] = set()
    for chunk_start in range(0, len(conv_ids), 500):
        chunk = sorted(conv_ids)[chunk_start : chunk_start + 500]
        marks = ",".join("?" for _ in chunk)
        owners = session.fetch_all(
            "SELECT id FROM storage_conversations "
            f"WHERE user_id = ? AND id IN ({marks})",
            (user_id, *chunk),
        )
        owned.update(str(item["id"]) for item in owners)
    records = [
        item for item in parsed
        if item["conv_id"] and item["conv_id"] in owned
    ]
    records.sort(
        key=lambda item: (item["completed_at"], item["task_id"]),
        reverse=True,
    )
    return {
        "records": records[:limit],
        "invalid": invalid,
        "scanned": scanned,
        "capped": len(records) > limit,
        "exhausted": exhausted,
        "next_cursor": "" if exhausted else after_key,
    }


def _project_task_result_experiment(value: Any) -> Any:
    """Project task-result metadata and its queryable experiment ID.

    The manager normally strips private wire diagnostics before issuing a
    checkpoint.  Repeating the pure projection at the storage authority keeps
    generic ``record.put`` callers and future producers from turning those
    multi-kilobyte diagnostics into durable state.
    """
    if not isinstance(value, Mapping):
        return value
    projected = dict(value)
    raw_metadata = projected.get("metadata")
    metadata = raw_metadata
    metadata_was_text = isinstance(metadata, str)
    if metadata_was_text:
        try:
            metadata = orjson.loads(metadata)
        except (orjson.JSONDecodeError, ValueError):
            metadata = None
    if isinstance(metadata, Mapping):
        metadata = dict(metadata)
        clean_metadata = project_task_result_metadata_for_storage(metadata)
        if clean_metadata is not metadata:
            projected["metadata"] = (
                orjson.dumps(clean_metadata).decode("utf-8")
                if metadata_was_text
                else clean_metadata
            )
        metadata = clean_metadata
    outcome = (
        metadata.get("costExperiment") if isinstance(metadata, Mapping) else None
    )
    experiment_id = (
        str(outcome.get("experimentId") or outcome.get("experiment_id") or "")
        if isinstance(outcome, Mapping)
        else ""
    )
    if experiment_id:
        projected["cost_experiment_id"] = experiment_id[:128]
    else:
        projected.pop("cost_experiment_id", None)
    return projected


def _record_put(session: Session, payload: Mapping[str, Any]) -> Any:
    namespace = _required_text(payload, "namespace", 128)
    key = _required_text(payload, "key")
    value = payload.get("value")
    if namespace == "task_results":
        value = _project_task_result_experiment(value)
    encoded = (
        _encoded_task_result_document(value)
        if namespace == "task_results"
        else _dump(value)
    )
    now = int(time.time() * 1000)
    # Serialize the read-version-then-upsert CAS per record.  SQLite's single
    # writer already provides this exclusion; PostgreSQL serializes two
    # concurrent writers on the same key so only one can observe a stale
    # expected_version.
    session.lock_key("record", f"{namespace}:{key}")
    current = session.fetch_one(
        "SELECT version FROM storage_records WHERE namespace = ? AND record_key = ?",
        (namespace, key),
    )
    expected = _expected_version(payload)
    actual = int(current["version"]) if current else 0
    if expected is not None and expected != actual:
        raise StorageError("database_conflict", "Storage record version conflict")
    version = actual + 1
    session.execute(
        "INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(namespace, record_key) DO UPDATE SET "
        "value_json = excluded.value_json, version = excluded.version, "
        "updated_at_ms = excluded.updated_at_ms",
        (namespace, key, encoded, version, now),
    )
    return {"key": key, "version": version, "updated_at_ms": now}


def _task_results_checkpoint(session: Session, payload: Mapping[str, Any]) -> Any:
    """CAS one task snapshot with natural ambiguous-ACK replay semantics.

    Task projections are rewritten at every streaming checkpoint.  Routing
    those writes through generic ``record.put`` created one permanent command
    receipt per checkpoint (203k receipts in seven production days).  This
    domain operation requires a witnessed version and treats an identical
    current value as the successful replay of the earlier commit.  A stale
    DIFFERENT snapshot still conflicts, so an old retry can never roll a
    newer terminal/checkpoint state backward.
    """
    key = _required_text(payload, "key")
    value = payload.get("value")
    if not isinstance(value, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid task result checkpoint"
        )
    guard_contract = payload.get("guard_contract")
    if guard_contract not in (None, TASK_RESULT_CHECKPOINT_GUARD_CONTRACT):
        raise StorageError(
            "database_protocol_error",
            "Invalid task result checkpoint guard contract",
        )
    guarded = guard_contract == TASK_RESULT_CHECKPOINT_GUARD_CONTRACT
    require_parent = payload.get("require_parent", False)
    if guarded and not isinstance(require_parent, bool):
        raise StorageError(
            "database_protocol_error",
            "Invalid task result parent requirement",
        )
    if not guarded and "require_parent" in payload:
        raise StorageError(
            "database_protocol_error",
            "Task result parent requirement needs a guard contract",
        )
    expected = _expected_version(payload)
    if expected is None:
        raise StorageError(
            "database_protocol_error",
            "task_results.checkpoint requires expected_version",
        )
    checkpoint_value = _project_task_result_experiment(value)
    if not isinstance(checkpoint_value, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid projected task result"
        )
    checkpoint_value = dict(checkpoint_value)

    cache_settings_contract = payload.get("cache_settings_contract")
    cache_prefix_hwm_present = (
        TASK_RESULT_CACHE_PREFIX_HWM_FIELD in checkpoint_value
    )
    last_turn_cache_read_present = (
        TASK_RESULT_LAST_TURN_CACHE_READ_FIELD in checkpoint_value
    )
    cache_settings_requested = (
        cache_prefix_hwm_present or last_turn_cache_read_present
    )
    if cache_settings_contract not in (
        None, TASK_RESULT_CACHE_SETTINGS_CONTRACT,
    ):
        raise StorageError(
            "database_protocol_error",
            "Invalid task result cache settings contract",
        )
    if cache_settings_requested:
        if (cache_settings_contract != TASK_RESULT_CACHE_SETTINGS_CONTRACT
                or not guarded or require_parent is not True):
            raise StorageError(
                "database_protocol_error",
                "Task result cache settings need a guarded parent",
            )
    elif cache_settings_contract is not None:
        raise StorageError(
            "database_protocol_error",
            "Task result cache settings contract has no facts",
        )

    def cache_fact(field: str, present: bool) -> int | None:
        if not present:
            return None
        candidate = checkpoint_value.get(field)
        if (isinstance(candidate, bool)
                or not isinstance(candidate, int)
                or candidate < 1
                or candidate > TASK_RESULT_CACHE_FACT_MAXIMUM):
            raise StorageError(
                "database_protocol_error",
                f"Invalid task result cache fact: {field}",
            )
        return candidate

    requested_cache_prefix_hwm = cache_fact(
        TASK_RESULT_CACHE_PREFIX_HWM_FIELD, cache_prefix_hwm_present,
    )
    requested_last_turn_cache_read = cache_fact(
        TASK_RESULT_LAST_TURN_CACHE_READ_FIELD,
        last_turn_cache_read_present,
    )

    def guarded_result(
        *,
        owned: bool,
        version: int = 0,
        updated_at_ms: int = 0,
        cache_settings_committed: bool = False,
        cache_settings_values: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "key": key,
            "version": version,
            "updated_at_ms": updated_at_ms,
            "owned": owned,
            "guard_contract": TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
        }
        if cache_settings_requested:
            result.update({
                "cache_settings_contract": (
                    TASK_RESULT_CACHE_SETTINGS_CONTRACT
                ),
                "cache_settings_committed": cache_settings_committed,
            })
            if cache_settings_committed and cache_settings_values:
                result.update(cache_settings_values)
        return result

    incoming_user_id = 0
    conversation_id = ""
    parent_settings: dict[str, Any] | None = None
    if guarded:
        raw_user_id = checkpoint_value.get("user_id")
        if (isinstance(raw_user_id, bool)
                or not isinstance(raw_user_id, int)
                or raw_user_id < 1):
            raise StorageError(
                "database_protocol_error",
                "Guarded task result checkpoint requires a valid owner",
            )
        incoming_user_id = raw_user_id
        if checkpoint_value.get("task_id") != key:
            raise StorageError(
                "database_protocol_error",
                "Guarded task result checkpoint identity is invalid",
            )
        if require_parent:
            conversation_id = checkpoint_value.get("conv_id")
            if (not isinstance(conversation_id, str)
                    or not conversation_id
                    or len(conversation_id) > 512):
                raise StorageError(
                    "database_protocol_error",
                    "Guarded task result checkpoint requires a parent",
                )
            # Share the conversation lifecycle lock used by delete/purge.
            # A plain parent SELECT inside the same transaction is not enough
            # under PostgreSQL READ COMMITTED: a concurrent purge could delete
            # the header after this check but before the checkpoint write.
            session.lock_key(
                "conversation", f"{incoming_user_id}:{conversation_id}",
            )
            selected_parent_columns = (
                "settings_json" if cache_settings_requested else "1 AS present"
            )
            parent = session.fetch_one(
                f"SELECT {selected_parent_columns} FROM storage_conversations "
                "WHERE id=? AND user_id=?",
                (conversation_id, incoming_user_id),
            )
            if parent is None:
                return guarded_result(owned=False)
            if cache_settings_requested:
                loaded_settings = _load(parent["settings_json"]) or {}
                if not isinstance(loaded_settings, Mapping):
                    raise StorageError(
                        "database_integrity",
                        "Conversation settings are malformed",
                    )
                parent_settings = dict(loaded_settings)
    # Match task_results.abort's per-key lock so concurrent checkpoint CAS
    # writers serialize before reading the witnessed version (PostgreSQL).
    # SQLite treats lock_key as a no-op: its single writer already serializes.
    session.lock_key("task_result", key)
    current = session.fetch_one(
        "SELECT value_json, version, updated_at_ms FROM storage_records "
        "WHERE namespace = ? AND record_key = ?",
        ("task_results", key),
    )
    actual = int(current["version"]) if current else 0
    current_value: Mapping[str, Any] | None = None
    if guarded and current is not None:
        decoded_current = _load(current["value_json"])
        if not isinstance(decoded_current, Mapping):
            raise StorageError(
                "database_integrity", "Task result checkpoint is malformed"
            )
        current_value = decoded_current
        raw_current_user_id = current_value.get("user_id")
        if (isinstance(raw_current_user_id, bool)
                or not isinstance(raw_current_user_id, int)):
            raise StorageError(
                "database_integrity",
                "Task result checkpoint owner is malformed",
            )
        if raw_current_user_id != incoming_user_id:
            # A foreign task id is intentionally indistinguishable from a
            # missing/fenced task id.
            return guarded_result(owned=False)
        current_status = current_value.get("status")
        incoming_status = checkpoint_value.get("status")
        if current_status == "interrupted" or (
            incoming_status in ("pending", "running")
            and current_status not in (None, "pending", "running")
        ):
            return guarded_result(owned=False)
        for tombstone_key in ("abort_requested_at", "abort_source"):
            if tombstone_key in current_value:
                checkpoint_value[tombstone_key] = current_value[tombstone_key]

    encoded = _encoded_task_result_document(checkpoint_value)

    def commit_cache_settings(*, ambiguous_replay: bool) -> dict[str, int]:
        """Merge requested facts while the conversation lock is held.

        HWM is monotonic and therefore safe to repair on every replay.  The
        last-read fact is LWW only for a NEW task checkpoint.  On an identical
        task replay, a different stored value may belong to a newer task whose
        transaction committed after the ambiguous ACK; never regress it.
        """
        if not cache_settings_requested or parent_settings is None:
            return {}
        changed = False
        authoritative: dict[str, int] = {}
        if requested_cache_prefix_hwm is not None:
            current_hwm = parent_settings.get(
                CONVERSATION_CACHE_PREFIX_HWM_SETTING,
            )
            current_hwm = (
                current_hwm
                if isinstance(current_hwm, int)
                and not isinstance(current_hwm, bool)
                and 0 < current_hwm <= TASK_RESULT_CACHE_FACT_MAXIMUM
                else 0
            )
            merged_hwm = max(current_hwm, requested_cache_prefix_hwm)
            if merged_hwm != current_hwm:
                parent_settings[
                    CONVERSATION_CACHE_PREFIX_HWM_SETTING
                ] = merged_hwm
                changed = True
            authoritative[TASK_RESULT_CACHE_PREFIX_HWM_FIELD] = merged_hwm
        if requested_last_turn_cache_read is not None:
            current_last_read = parent_settings.get(
                CONVERSATION_LAST_TURN_CACHE_READ_SETTING,
            )
            current_last_read = (
                current_last_read
                if isinstance(current_last_read, int)
                and not isinstance(current_last_read, bool)
                and 0 < current_last_read <= TASK_RESULT_CACHE_FACT_MAXIMUM
                else 0
            )
            if not ambiguous_replay or current_last_read == 0:
                if current_last_read != requested_last_turn_cache_read:
                    parent_settings[
                        CONVERSATION_LAST_TURN_CACHE_READ_SETTING
                    ] = requested_last_turn_cache_read
                    changed = True
                current_last_read = requested_last_turn_cache_read
            authoritative[
                TASK_RESULT_LAST_TURN_CACHE_READ_FIELD
            ] = current_last_read
        if changed:
            session.execute(
                "UPDATE storage_conversations SET settings_json=? "
                "WHERE id=? AND user_id=?",
                (_dump(parent_settings), conversation_id, incoming_user_id),
            )
        return authoritative

    if current is not None:
        current_encoded = current["value_json"]
        if isinstance(current_encoded, memoryview):
            current_encoded = bytes(current_encoded)
        semantic_replay = False
        if current_encoded != encoded and expected != actual:
            decoded_for_replay = _hydrated_task_result_value(
                _load(current["value_json"])
            )
            semantic_replay = (
                _dump(decoded_for_replay) == _dump(checkpoint_value)
            )
        if current_encoded == encoded or semantic_replay:
            if guarded:
                cache_settings_values = commit_cache_settings(
                    ambiguous_replay=True,
                )
                return guarded_result(
                    owned=True,
                    version=actual,
                    updated_at_ms=int(current["updated_at_ms"]),
                    cache_settings_committed=cache_settings_requested,
                    cache_settings_values=cache_settings_values,
                )
            return {
                "key": key,
                "version": actual,
                "updated_at_ms": int(current["updated_at_ms"]),
            }
    if expected != actual:
        raise StorageError("database_conflict", "Storage record version conflict")
    now = int(time.time() * 1000)
    version = actual + 1
    session.execute(
        "INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(namespace, record_key) DO UPDATE SET "
        "value_json = excluded.value_json, version = excluded.version, "
        "updated_at_ms = excluded.updated_at_ms",
        ("task_results", key, encoded, version, now),
    )
    if guarded:
        cache_settings_values = commit_cache_settings(
            ambiguous_replay=False,
        )
        return guarded_result(
            owned=True,
            version=version,
            updated_at_ms=now,
            cache_settings_committed=cache_settings_requested,
            cache_settings_values=cache_settings_values,
        )
    return {"key": key, "version": version, "updated_at_ms": now}


def _task_results_abort(session: Session, payload: Mapping[str, Any]) -> Any:
    """Atomically signal one pending/running task owned by the requester."""
    key = _required_text(payload, "task_id")
    user_id = _integer(payload, "user_id", minimum=1)
    source = _required_text(payload, "source", 128)
    session.lock_key("task_result", key)
    row = session.fetch_one(
        "SELECT value_json, version FROM storage_records "
        "WHERE namespace=? AND record_key=?",
        ("task_results", key),
    )
    if row is None:
        return {"signaled": False, "changed": False}
    try:
        value = _load(row["value_json"])
        record_user_id = int(value.get("user_id") or 0)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise StorageError(
            "database_integrity", "Task result ownership metadata is invalid"
        ) from exc
    # A foreign id is intentionally indistinguishable from a missing id.
    if (record_user_id != user_id
            or value.get("status") not in ("pending", "running")):
        return {"signaled": False, "changed": False}
    if value.get("abort_requested_at"):
        return {"signaled": True, "changed": False}
    updated = dict(value)
    requested_at = int(time.time() * 1000)
    updated["abort_requested_at"] = requested_at
    updated["abort_source"] = source
    version = int(row["version"]) + 1
    session.execute(
        "UPDATE storage_records SET value_json=?, version=?, updated_at_ms=? "
        "WHERE namespace=? AND record_key=?",
        (_dump(updated), version, requested_at, "task_results", key),
    )
    return {
        "signaled": True,
        "changed": True,
        "version": version,
        "requested_at_ms": requested_at,
    }


def _task_results_abort_requested(
    session: Session, payload: Mapping[str, Any]
) -> Any:
    """Read an abort signal without exposing a foreign task record."""
    key = _required_text(payload, "task_id")
    user_id = _integer(payload, "user_id", minimum=1)
    row = session.fetch_one(
        "SELECT value_json FROM storage_records "
        "WHERE namespace=? AND record_key=?",
        ("task_results", key),
    )
    if row is None:
        return {"requested": False}
    try:
        value = _load(row["value_json"])
        record_user_id = int(value.get("user_id") or 0)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise StorageError(
            "database_integrity", "Task result ownership metadata is invalid"
        ) from exc
    return {
        "requested": bool(
            record_user_id == user_id and value.get("abort_requested_at")
        )
    }


def _task_results_recover_running(
    session: Session, payload: Mapping[str, Any]
) -> Any:
    """Settle a bounded page of orphaned live task snapshots after restart.

    Task snapshots are a read model for task inspection and transport replay;
    they never write conversation projections.  Turn recovery is owned by the
    separate ``turn.recover`` transaction.  Keeping those two authorities
    independent removes the old task-result → whole-transcript merge race.
    """
    reason = str(payload.get("interrupted_reason") or "server_restart")
    if reason not in {"server_restart", "process_killed", "manual_restart"}:
        raise StorageError(
            "database_protocol_error", "Invalid task recovery reason"
        )
    max_rows = _integer(payload, "max_rows", default=32, minimum=1, maximum=500)
    scan_limit = _integer(
        payload, "scan_limit", default=10_000, minimum=1, maximum=100_000
    )
    now = int(time.time() * 1000)
    after_key = ""
    scanned = 0
    recovered: list[dict[str, str]] = []
    exhausted = False

    # Small pages bound peak memory even when one snapshot contains MiB of
    # content/tool state. Repeated calls naturally skip rows already settled.
    while scanned < scan_limit and len(recovered) < max_rows:
        rows = session.fetch_all(
            "SELECT record_key, value_json, version FROM storage_records "
            "WHERE namespace=? AND record_key>? ORDER BY record_key LIMIT ?",
            ("task_results", after_key, min(8, scan_limit - scanned)),
        )
        if not rows:
            exhausted = True
            break
        for row in rows:
            after_key = str(row["record_key"])
            scanned += 1
            try:
                value = _load(row["value_json"])
            except (TypeError, ValueError, orjson.JSONDecodeError):
                continue
            if (
                not isinstance(value, Mapping)
                or value.get("status") not in {"pending", "running"}
            ):
                continue
            document = dict(value)
            document["status"] = "interrupted"
            document["interruptedReason"] = reason
            document["completed_at"] = int(document.get("completed_at") or now)
            version = int(row["version"]) + 1
            session.execute(
                "UPDATE storage_records SET value_json=?, version=?, "
                "updated_at_ms=? WHERE namespace=? AND record_key=? AND version=?",
                (
                    _dump(document), version, now, "task_results",
                    after_key, int(row["version"]),
                ),
            )
            recovered.append({
                "taskId": str(document.get("task_id") or after_key),
                "conversationId": str(document.get("conv_id") or ""),
            })
            if len(recovered) >= max_rows:
                break

    return {
        "recovered": recovered,
        "scanned": scanned,
        "nextKey": after_key,
        # A full page means another call must verify the remaining keyspace;
        # returning a conservative true is harmless because settlement is
        # naturally idempotent.
        "remaining": not exhausted,
    }


def _record_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    namespace = _required_text(payload, "namespace", 128)
    key = _required_text(payload, "key")
    expected = _expected_version(payload)
    if expected is None:
        count = session.execute(
            "DELETE FROM storage_records WHERE namespace = ? AND record_key = ?",
            (namespace, key),
        )
    else:
        count = session.execute(
            "DELETE FROM storage_records "
            "WHERE namespace = ? AND record_key = ? AND version = ?",
            (namespace, key, expected),
        )
        if not count:
            current = session.fetch_one(
                "SELECT version FROM storage_records "
                "WHERE namespace = ? AND record_key = ?",
                (namespace, key),
            )
            if current is not None:
                raise StorageError(
                    "database_conflict", "Storage record version conflict"
                )
    return {"deleted": bool(count)}


def _append_event_row(session: Session, payload: Mapping[str, Any]) -> dict[str, Any]:
    from lib.task_event_contract import TASK_STREAM_KIND

    task_id = _required_text(payload, "task_id")
    sequence = payload.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise StorageError("database_protocol_error", "Invalid event sequence")
    # The manager normally projects its live object before crossing the RPC
    # boundary. Repeat the pure projection at the storage authority so an
    # importer, older producer, or future atomic carrier cannot persist the
    # private ``_wire_*`` graphs that made historical round-usage rows grow
    # with the complete prompt once per model round. The helper is copy-on-
    # change, so ordinary already-projected events retain their fast path.
    event = project_event_usage_for_storage(payload.get("event"))
    raw_encoded = _dump(event)
    encoded = encode_task_event_payload(raw_encoded)
    stream_kind = str(payload.get("stream_kind") or TASK_STREAM_KIND)
    if stream_kind != TASK_STREAM_KIND:
        raise StorageError(
            "database_protocol_error",
            "event.append only accepts task streams; Project Brain uses its "
            "transactional semantic command boundary",
        )
    event_type = (
        str(event.get("type") or "")[:128]
        if isinstance(event, Mapping) else ""
    )
    event_kind = (
        str(event.get("kind") or "")[:128]
        if isinstance(event, Mapping) else ""
    )
    now = int(time.time() * 1000)
    count = session.execute(
        "INSERT INTO storage_events(task_id, sequence, stream_kind, "
        "event_type, event_kind, event_json, created_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(task_id, sequence) DO NOTHING",
        (task_id, sequence, stream_kind, event_type, event_kind, encoded, now),
    )
    if not count:
        row = session.fetch_one(
            "SELECT event_json FROM storage_events WHERE task_id = ? AND sequence = ?",
            (task_id, sequence),
        )
        existing = (
            None
            if row is None
            else _dump(_load(decode_task_event_payload(row["event_json"])))
        )
        if existing != raw_encoded:
            raise StorageError(
                "database_conflict", "Event sequence has a conflicting payload"
            )
    return {"inserted": bool(count), "task_id": task_id, "sequence": sequence}


def _event_append(session: Session, payload: Mapping[str, Any]) -> Any:
    return _append_event_row(session, payload)


def _event_append_batch(session: Session, payload: Mapping[str, Any]) -> Any:
    events = payload.get("events")
    if not isinstance(events, list) or not events or len(events) > 500:
        raise StorageError("database_protocol_error", "Invalid event batch")
    results = []
    for event in events:
        if not isinstance(event, Mapping):
            raise StorageError("database_protocol_error", "Invalid event batch item")
        results.append(_append_event_row(session, event))
    return {
        "results": results,
        "inserted": sum(1 for item in results if item["inserted"]),
        "deduplicated": sum(1 for item in results if not item["inserted"]),
    }


# Literal characters only — a GLOB prefix must never become a pattern.
_EVENT_TYPE_PREFIX_SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}$")


def _event_type_filter(payload: Mapping[str, Any]) -> tuple[str, tuple]:
    """Optional storage-level event-type filter for ``event.list``.

    ``types`` are exact event types; ``type_prefixes`` are LITERAL prefixes
    (matched as ``event_type GLOB '<prefix>*'``). A task's event log is
    dominated by streaming rows (per-token deltas, phase lines, tool
    progress); a metadata reader such as the Request Inspector fold only
    needs the few structural rows per round, and filtering in SQL means the
    noise rows are never JSON-decoded or transferred. Callers must still
    re-check types consumer-side — the filter is a bandwidth/decoding
    optimization, not an authorization boundary.
    """
    types = payload.get("types") or []
    prefixes = payload.get("type_prefixes") or []
    if not isinstance(types, (list, tuple)) or len(types) > 64:
        raise StorageError(
            "database_protocol_error", "Invalid types in storage request")
    if not isinstance(prefixes, (list, tuple)) or len(prefixes) > 16:
        raise StorageError(
            "database_protocol_error",
            "Invalid type_prefixes in storage request")
    for value in types:
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise StorageError(
                "database_protocol_error", "Invalid types in storage request")
    clauses = []
    params: list = []
    if types:
        clauses.append("event_type IN (" + ",".join("?" for _ in types) + ")")
        params.extend(types)
    for prefix in prefixes:
        if not isinstance(prefix, str) or not _EVENT_TYPE_PREFIX_SAFE.match(
                prefix):
            raise StorageError(
                "database_protocol_error",
                "Invalid type_prefixes in storage request")
        clauses.append("event_type GLOB ?")
        params.append(prefix + "*")
    if not clauses:
        return "", ()
    return " AND (" + " OR ".join(clauses) + ")", tuple(params)


def _event_list(session: Session, payload: Mapping[str, Any]) -> Any:
    task_id = _required_text(payload, "task_id")
    after = _integer(payload, "after_sequence", default=-1, minimum=-1)
    limit = _integer(payload, "limit", default=500, minimum=1, maximum=1000)
    type_clause, type_params = _event_type_filter(payload)
    rows = session.fetch_all(
        "SELECT sequence, event_json, created_at_ms FROM storage_events "
        "WHERE task_id = ? AND sequence > ?" + type_clause +
        " ORDER BY sequence LIMIT ?",
        (task_id, after, *type_params, limit),
    )
    return [
        {
            "sequence": int(row["sequence"]),
            "event": _load(decode_task_event_payload(row["event_json"])),
            "created_at_ms": int(row["created_at_ms"]),
        }
        for row in rows
    ]


def _event_latest(session: Session, payload: Mapping[str, Any]) -> Any:
    task_id = _required_text(payload, "task_id")
    row = session.fetch_one(
        "SELECT sequence, event_json, created_at_ms FROM storage_events "
        "WHERE task_id = ? ORDER BY sequence DESC LIMIT 1",
        (task_id,),
    )
    if row is None:
        return None
    return {
        "sequence": int(row["sequence"]),
        "event": _load(decode_task_event_payload(row["event_json"])),
        "created_at_ms": int(row["created_at_ms"]),
    }


def _event_bounds(session: Session, payload: Mapping[str, Any]) -> Any:
    """Return exact replay-window bounds without materializing event bodies."""
    task_id = _required_text(payload, "task_id")
    row = session.fetch_one(
        "SELECT COUNT(*) AS retained_count, MIN(sequence) AS base_cursor, "
        "MAX(sequence) AS latest_sequence FROM storage_events WHERE task_id = ?",
        (task_id,),
    )
    retained_count = int(row["retained_count"] or 0) if row else 0
    if retained_count <= 0:
        return {
            "retained_count": 0,
            "base_cursor": 0,
            "next_cursor": 0,
        }
    base_cursor = max(0, int(row["base_cursor"]))
    latest_sequence = max(base_cursor, int(row["latest_sequence"]))
    return {
        "retained_count": retained_count,
        "base_cursor": base_cursor,
        "next_cursor": latest_sequence + 1,
    }


def _event_inspector_summary(session: Session, payload: Mapping[str, Any]) -> Any:
    """Return exact structural counts for roots and their swarm children.

    The Request Inspector needs compact existence/count metadata, never every
    payload. Keeping this fold in the Sidecar avoids N raw event pages per
    task and makes child discovery an indexed key-range query.
    """
    from lib.orchestration_message_compat import LEGACY_FLOW_EVENT_PREFIX
    from lib.task_event_contract import STRUCTURAL_EVENT_TYPES, TASK_STREAM_KIND

    raw_task_ids = payload.get("task_ids")
    if not isinstance(raw_task_ids, list) or not raw_task_ids:
        raise StorageError("database_protocol_error", "task_ids must be non-empty")
    if len(raw_task_ids) > 100:
        raise StorageError("database_protocol_error", "too many task_ids")
    task_ids = tuple(_required_text({"task_id": value}, "task_id")
                     for value in raw_task_ids)
    root_placeholders = ",".join("?" for _ in task_ids)
    ranges = []
    range_params: list[str] = []
    for task_id in task_ids:
        prefix = f"{task_id}#agent:"
        ranges.append("(task_id >= ? AND task_id < ?)")
        range_params.extend((prefix, f"{task_id}#agent;"))
    ownership_predicate = (
        f"task_id IN ({root_placeholders}) OR " + " OR ".join(ranges))
    structural_types = tuple(sorted(STRUCTURAL_EVENT_TYPES))
    structural_placeholders = ",".join("?" for _ in structural_types)
    rows = session.fetch_all(
        "SELECT task_id, "
        "SUM(CASE WHEN event_type='messages_snapshot' "
        "AND event_kind='request' THEN 1 ELSE 0 END) AS request_count, "
        "SUM(CASE WHEN event_type='messages_snapshot' "
        "AND event_kind='state' THEN 1 ELSE 0 END) AS state_count, "
        "SUM(CASE WHEN event_type='messages_snapshot' "
        "AND event_kind='' THEN 1 ELSE 0 END) AS legacy_count, "
        "COUNT(*) AS event_count, MIN(created_at_ms) AS first_event_at_ms "
        "FROM storage_events WHERE stream_kind=? AND ("
        + ownership_predicate + ") AND (event_type IN ("
        + structural_placeholders + ") OR event_type LIKE ?) "
        "GROUP BY task_id ORDER BY task_id",
        (TASK_STREAM_KIND, *task_ids, *range_params, *structural_types,
         f'{LEGACY_FLOW_EVENT_PREFIX}%'),
    )
    return {"records": [{
        "task_id": str(row["task_id"]),
        "request_count": int(row["request_count"] or 0),
        "state_count": int(row["state_count"] or 0),
        "legacy_count": int(row["legacy_count"] or 0),
        "event_count": int(row["event_count"] or 0),
        "first_event_at_ms": int(row["first_event_at_ms"] or 0),
    } for row in rows]}


def _delete_legacy_event_type_page(
    session: Session,
    *,
    event_type: str,
    cutoff: int,
    limit: int,
) -> int:
    from lib.task_event_contract import TASK_STREAM_KIND

    return session.execute(
        "DELETE FROM storage_events WHERE (task_id, sequence) IN ("
        "SELECT task_id, sequence FROM storage_events "
        "WHERE stream_kind = ? AND event_type = ? "
        "AND created_at_ms < ? ORDER BY created_at_ms LIMIT ?)",
        (TASK_STREAM_KIND, event_type, cutoff, limit),
    )


def _recover_legacy_blank_event_page(
    session: Session,
    *,
    cutoff: int,
    limit: int,
) -> Any | None:
    """Classify one bounded page written before typed event metadata existed.

    Blank types are conservatively structural, so old streaming deltas would
    otherwise survive the 30-day structural horizon. Selection uses the same
    structural/legacy age index as retention, while a separate stored-payload
    budget bounds materialization. Expired streaming rows are deleted; valid
    structural rows receive only their recovered metadata. Opaque JSON keeps
    its payload and blank type, with an internal kind marker preventing one
    malformed oldest row from starving later recoverable rows.
    """
    from lib.task_event_contract import STRUCTURAL_EVENT_TYPES, TASK_STREAM_KIND

    payload_length = (
        "length(event_json)"
        if session.backend == "sqlite" else "octet_length(event_json)"
    )
    candidate_limit = min(
        int(limit), _LEGACY_BLANK_EVENT_RECOVERY_MAX_ROWS)
    candidates = session.fetch_all(
        "SELECT task_id, sequence, " + payload_length + " AS payload_bytes "
        "FROM storage_events WHERE stream_kind = ? AND event_type = '' "
        "AND event_kind <> ? AND created_at_ms < ? "
        "ORDER BY created_at_ms LIMIT ?",
        (TASK_STREAM_KIND, _LEGACY_OPAQUE_EVENT_KIND, cutoff, candidate_limit),
    )
    selected: list[Mapping[str, Any]] = []
    oversize_keys: list[tuple[str, int]] = []
    oversize_stored_bytes = 0
    selected_payload_bytes = 0
    for row in candidates:
        payload_bytes = max(0, int(row["payload_bytes"] or 0))
        key = (str(row["task_id"]), int(row["sequence"]))
        if payload_bytes > _LEGACY_BLANK_EVENT_RECOVERY_PAYLOAD_BYTES:
            # Retain an individually oversized legacy diagnostic under the
            # conservative structural horizon, but never materialize it on the
            # maintenance writer. Marking it opaque lets later rows progress.
            oversize_keys.append(key)
            oversize_stored_bytes += payload_bytes
            continue
        if (selected and selected_payload_bytes + payload_bytes
                > _LEGACY_BLANK_EVENT_RECOVERY_PAYLOAD_BYTES):
            break
        selected.append(row)
        selected_payload_bytes += payload_bytes
        if (selected_payload_bytes
                >= _LEGACY_BLANK_EVENT_RECOVERY_PAYLOAD_BYTES):
            break
    if not selected and not oversize_keys:
        return None

    payload_rows = []
    if selected:
        key_placeholders = ",".join("(?, ?)" for _ in selected)
        key_params = tuple(
            value
            for row in selected
            for value in (str(row["task_id"]), int(row["sequence"]))
        )
        payload_rows = session.fetch_all(
            "SELECT task_id, sequence, event_json FROM storage_events WHERE "
            f"(task_id, sequence) IN ({key_placeholders})",
            key_params,
        )
    payloads = {
        (str(row["task_id"]), int(row["sequence"])): row["event_json"]
        for row in payload_rows
    }
    selected_payload_sizes = {
        (str(row["task_id"]), int(row["sequence"])): max(
            0, int(row["payload_bytes"] or 0)
        )
        for row in selected
    }
    delete_keys: list[tuple[str, int]] = []
    structural_updates: list[tuple[str, str, str, int]] = []
    opaque_keys: list[tuple[str, int]] = list(oversize_keys)
    oversize_opaque_count = len(oversize_keys)
    for row in selected:
        key = (str(row["task_id"]), int(row["sequence"]))
        if key not in payloads:
            raise StorageError(
                "database_integrity",
                "Legacy blank task event disappeared during classification",
            )
        stored_payload = payloads[key]
        if (task_event_decoded_size(stored_payload)
                > _LEGACY_BLANK_EVENT_RECOVERY_PAYLOAD_BYTES):
            opaque_keys.append(key)
            oversize_opaque_count += 1
            oversize_stored_bytes += selected_payload_sizes[key]
            continue
        decoded = decode_task_event_payload(stored_payload)
        try:
            event = orjson.loads(decoded)
        except orjson.JSONDecodeError:
            event = None
        recovered_type = (
            str(event.get("type") or "")[:128]
            if isinstance(event, Mapping) else ""
        )
        recovered_kind = (
            str(event.get("kind") or "")[:128]
            if isinstance(event, Mapping) else ""
        )
        if not recovered_type:
            opaque_keys.append(key)
        elif recovered_type in STRUCTURAL_EVENT_TYPES:
            structural_updates.append(
                (recovered_type, recovered_kind, key[0], key[1]))
        else:
            delete_keys.append(key)

    deleted = 0
    if delete_keys:
        delete_placeholders = ",".join("(?, ?)" for _ in delete_keys)
        delete_params = tuple(value for key in delete_keys for value in key)
        deleted = session.execute(
            "DELETE FROM storage_events WHERE (task_id, sequence) IN ("
            + delete_placeholders + ")",
            delete_params,
        )
        if int(deleted) != len(delete_keys):
            raise StorageError(
                "database_integrity",
                "Legacy blank task-event delete count mismatched",
            )

    updated = 0
    for recovered_type, recovered_kind, task_id, sequence in structural_updates:
        updated += session.execute(
            "UPDATE storage_events SET event_type = ?, event_kind = ? "
            "WHERE task_id = ? AND sequence = ? AND event_type = ''",
            (recovered_type, recovered_kind, task_id, sequence),
        )
    if opaque_keys:
        opaque_placeholders = ",".join("(?, ?)" for _ in opaque_keys)
        opaque_params = tuple(value for key in opaque_keys for value in key)
        updated += session.execute(
            "UPDATE storage_events SET event_kind = ? "
            "WHERE event_type = '' AND (task_id, sequence) IN ("
            + opaque_placeholders + ")",
            (_LEGACY_OPAQUE_EVENT_KIND, *opaque_params),
        )
    expected_updates = len(structural_updates) + len(opaque_keys)
    if int(updated) != expected_updates:
        raise StorageError(
            "database_integrity",
            "Legacy blank task-event classification count mismatched",
        )
    return {
        "deleted": int(deleted),
        "classified": len(selected) + len(oversize_keys),
        "recovered_types": len(delete_keys) + len(structural_updates),
        "opaque": len(opaque_keys),
        "payload_bytes": selected_payload_bytes,
        "oversize_opaque": oversize_opaque_count,
        "oversize_stored_bytes": oversize_stored_bytes,
        # A separately committed follow-up must establish that the blank page
        # and any typed backlog are both drained.
        "has_more": True,
        "index_mode": "legacy_blank_type_recovery",
    }


def _legacy_index_event_prune(
    session: Session,
    *,
    cutoff: int,
    limit: int,
    legacy_recovery_limit: int,
    retention_class: str,
    required_index: str,
) -> Any:
    """Prune one exact type without pre-scanning the whole legacy index.

    The v1 index is ordered by ``(stream_kind, event_type, created_at_ms)``.
    It cannot safely answer the v2 tier-wide age query, so streaming retention
    advances one exact type at a time. Discovery and deletion are deliberately
    interleaved: once one deletable page is found, the transaction returns
    instead of paying up to 64 additional B-tree seeks while holding the sole
    writer. Empty probes remain bounded by the same explicit type ceiling.
    """
    from lib.task_event_contract import STRUCTURAL_EVENT_TYPES, TASK_STREAM_KIND
    from lib.storage_sidecar.schema import (
        LEGACY_TASK_EVENT_RETENTION_TYPE_LIMIT,
    )

    if retention_class == "structural":
        for event_type in ["", *sorted(STRUCTURAL_EVENT_TYPES)]:
            deleted = _delete_legacy_event_type_page(
                session, event_type=event_type, cutoff=cutoff, limit=limit)
            if deleted:
                return {
                    "deleted": int(deleted),
                    "has_more": True,
                    "index_mode": "legacy_exact_type",
                }
        return {
            "deleted": 0,
            "has_more": False,
            "index_mode": "legacy_exact_type",
        }

    cursor = ""
    type_discovery_exhausted = False
    for _ in range(LEGACY_TASK_EVENT_RETENTION_TYPE_LIMIT):
        row = session.fetch_one(
            "SELECT event_type FROM storage_events "
            "WHERE stream_kind = ? AND event_type > ? "
            "ORDER BY event_type LIMIT 1",
            (TASK_STREAM_KIND, cursor),
        )
        if row is None:
            type_discovery_exhausted = True
            break
        event_type = str(row["event_type"] or "")
        if not event_type or event_type <= cursor:
            raise StorageError(
                "database_integrity",
                "Legacy event-retention index returned a non-advancing type",
            )
        cursor = event_type
        if event_type in STRUCTURAL_EVENT_TYPES:
            continue

        # Interleave discovery with the exact-type delete. Returning as soon
        # as one page lands is the compatibility path's load-bearing latency
        # bound on large established authorities.
        deleted = _delete_legacy_event_type_page(
            session, event_type=event_type, cutoff=cutoff, limit=limit)
        if deleted:
            # One compatibility command intentionally handles one type. Tell
            # the maintenance pager to make another separately committed call
            # even when this type's tail was smaller than ``limit``; the next
            # call either advances to another type or proves the tier drained.
            return {
                "deleted": int(deleted),
                "has_more": True,
                "index_mode": "legacy_exact_type",
            }

    if not type_discovery_exhausted:
        overflow = session.fetch_one(
            "SELECT event_type FROM storage_events "
            "WHERE stream_kind = ? AND event_type > ? "
            "ORDER BY event_type LIMIT 1",
            (TASK_STREAM_KIND, cursor),
        )
        if overflow is not None:
            return {
                "deleted": 0,
                "deferred": True,
                "reason": "legacy_index_event_type_limit",
                "required_index": required_index,
            }

    recovered = _recover_legacy_blank_event_page(
        session, cutoff=cutoff, limit=legacy_recovery_limit)
    if recovered is not None:
        return recovered
    return {
        "deleted": 0,
        "has_more": False,
        "index_mode": "legacy_exact_type",
    }


def _event_prune(session: Session, payload: Mapping[str, Any]) -> Any:
    from lib.storage_sidecar.schema import (
        LEGACY_TASK_EVENT_RETENTION_INDEX_NAME,
        TASK_EVENT_RETENTION_SPECS,
    )

    cutoff = _integer(payload, "created_before_ms", minimum=0)
    limit = _integer(payload, "limit", default=100, minimum=1, maximum=1000)
    retention_class = _required_text(payload, "retention_class", 32)
    retention_spec = TASK_EVENT_RETENTION_SPECS.get(retention_class)
    if retention_spec is None:
        raise StorageError(
            "database_protocol_error", "Invalid event retention class")
    required_index, tier_predicate = retention_spec
    if not session.index_exists(required_index):
        # The compatibility index orders by ``event_type`` before age.  Even
        # though the legacy helper bounds deleted rows, finding those rows can
        # still walk a huge payload-bearing event table once per event type.
        # On an established multi-GiB SQLite authority that held the sole
        # writer for seconds at a time and starved interactive turn.create /
        # turn.attempt.claim commands.  A row limit is not an execution-time
        # budget, so online maintenance must require the age-leading partial
        # index and fail closed before issuing any legacy scan.
        #
        # Keep the legacy helper above for explicit offline repair/forensics;
        # the live sidecar never invokes it.  The maintenance owner disables
        # this tier for the process and startup already reports the missing
        # offline-maintenance prerequisite.
        legacy_index_present = session.index_exists(
            LEGACY_TASK_EVENT_RETENTION_INDEX_NAME
        )
        return {
            "deleted": 0,
            "deferred": True,
            "reason": (
                "legacy_index_offline_required"
                if legacy_index_present
                else "missing_index"
            ),
            "required_index": required_index,
        }
    # One statement riding the age-leading retention index, not a SELECT round
    # trip plus N primary-key deletes: on a slow-fsync filesystem the
    # per-statement overhead of the old loop dominated the writer's time
    # budget. The index order also makes an empty sweep an O(log N) age range
    # probe instead of a scan/sort across every task event type.
    deleted = session.execute(
        "DELETE FROM storage_events WHERE (task_id, sequence) IN ("
        "SELECT task_id, sequence FROM storage_events "
        f"WHERE {tier_predicate} "
        "AND created_at_ms < ? ORDER BY created_at_ms LIMIT ?)",
        (cutoff, limit),
    )
    deleted_count = int(deleted or 0)
    return {
        "deleted": deleted_count,
        "has_more": deleted_count >= limit,
        "index_mode": "tier_partial_v2",
    }


def _rate_limit_record_and_check(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    endpoint = _required_text(payload, "endpoint", 256)
    client_key = _required_text(payload, "client_key", 512)
    event_id = _required_text(payload, "event_id", 200)
    limit = _integer(payload, "limit", minimum=1, maximum=1_000_000)
    per_seconds = _integer(payload, "per_seconds", minimum=1, maximum=7 * 24 * 60 * 60)
    now = int(time.time() * 1000)
    window_start = now - per_seconds * 1000
    expires_at = now + per_seconds * 1000
    pruned = int(session.execute(
        "DELETE FROM storage_rate_limit_events WHERE event_id IN ("
        "SELECT event_id FROM storage_rate_limit_events "
        "WHERE expires_at_ms <= ? ORDER BY expires_at_ms, event_id LIMIT ?)",
        (now, _RATE_LIMIT_PRUNE_BATCH),
    ) or 0)
    # PostgreSQL TEXT cannot carry NUL bytes.  A length prefix preserves an
    # unambiguous composite bucket key for both adapters.
    session.lock_key("rate_limit_bucket", f"{len(endpoint)}:{endpoint}{client_key}")
    row = session.fetch_one(
        "SELECT COUNT(*) AS event_count FROM storage_rate_limit_events "
        "WHERE endpoint = ? AND client_key = ? AND occurred_at_ms >= ?",
        (endpoint, client_key, window_start),
    )
    current = int(row["event_count"]) if row else 0
    if current >= limit:
        return {"allowed": False, "count": current, "pruned": pruned}
    session.execute(
        "INSERT INTO storage_rate_limit_events("
        "event_id, endpoint, client_key, occurred_at_ms, expires_at_ms) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_id, endpoint, client_key, now, expires_at),
    )
    return {"allowed": True, "count": current + 1, "pruned": pruned}
