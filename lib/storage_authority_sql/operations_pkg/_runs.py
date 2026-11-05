"""Orchestration run/event and swarm-session operation handlers plus shared row codecs."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

import orjson

from lib.goal_runs.contract import (
    GOAL_RUN_CREATED_BY,
    GOAL_RUN_FORMAT,
    GOAL_RUN_ORCHESTRATION_PREFIX,
    GOAL_RUN_TERMINAL_STATUSES,
    MAX_GOAL_OBJECTIVE_CHARS,
    goal_orchestration_id,
    goal_run_policy,
    goal_status_from_storage,
    is_valid_goal_transition,
    storage_status_for_goal_status,
)
from lib.identity import require_user_id
from lib.log import get_logger
from lib.orchestration.run_status import (
    CONTINUITY_ELECTION_CLOSED_EVENT_TYPES, CONTINUITY_RESTART_ERROR,
    TERMINAL_RUN_STATUSES,
    VALID_RUN_STATUSES,
    is_run_status,
    is_terminal_run_status,
)
from lib.swarm.persistence_contract import (
    SWARM_CHECKPOINT_MARKER_RESULT_FIELDS,
    SWARM_CHECKPOINT_MARKER_TEXT_CHARACTERS,
    SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS,
    SWARM_SESSION_STATUS_RUNNING,
    SWARM_SESSION_STATUS_TERMINATED,
    SWARM_SESSION_WRITABLE_STATUSES,
)
from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters.base import Session
from lib.storage_authority_sql.adapters.text_size import utf8_length_expression
from lib.storage.protocol import MAX_FRAME_BYTES


logger = get_logger(__name__)


from lib.storage_authority_sql.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)

_RUN_STATUSES = VALID_RUN_STATUSES
_TERMINAL_RUN_STATUSES = TERMINAL_RUN_STATUSES
_TERMINAL_STATUS_SQL = ", ".join("?" for _ in TERMINAL_RUN_STATUSES)

# Durable-storage hygiene ceiling for the full goal stop report carried on a
# transition event.  Far beyond any real VU verdict — the report is never
# truncated in the product sense; this only bounds pathological model output.
_GOAL_STOP_REPORT_MAX_CHARS = 200_000
_ORCHESTRATION_EVENT_PAGE_BYTES = MAX_FRAME_BYTES // 8


def _json_text(value: Any) -> str:
    return _dump(value).decode("utf-8")


def _run_owner(payload: Mapping[str, Any]) -> tuple[int, str]:
    tenant_id = payload.get("tenant_id", "")
    if not isinstance(tenant_id, str) or len(tenant_id) > 256:
        raise StorageError(
            "database_protocol_error", "Invalid tenant_id in orchestration request")
    return _integer(payload, "user_id", minimum=1), tenant_id.strip()


def _run_status(payload: Mapping[str, Any], *, optional: bool = False) -> str:
    value = payload.get("status", "")
    if optional and value == "":
        return ""
    if not is_run_status(value):
        raise StorageError(
            "database_protocol_error", "Invalid orchestration run status"
        )
    return value


def _decode_run_error(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        return _load(value)
    except orjson.JSONDecodeError as exc:
        logger.debug("[StorageSidecar] preserving undecodable run error: %s", exc)
        return str(value)


def _run_row(row: Mapping[str, Any], *, detail: bool) -> dict[str, Any]:
    status = str(row["status"] or "pending")
    result = {
        "id": row["id"],
        "orch_id": row["orch_id"] or "",
        "name": row["name"] or "",
        "status": status,
        "terminal": is_terminal_run_status(status),
        "final": row["final"] or "",
        "error": _decode_run_error(row["error"]),
        "created_by": row["created_by"] or "",
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
        "finished_at": int(row["finished_at"] or 0),
    }
    if detail:
        definition = _load(row["definition"])
        if not isinstance(definition, dict):
            raise StorageError(
                "database_integrity",
                "Durable orchestration definition is not an object",
            )
        result["definition"] = definition
        result["input"] = row["input"] or ""
    return result


def _orchestration_run_create(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, "run_id", 200)
    user_id, tenant_id = _run_owner(payload)
    definition = payload.get("definition")
    if not isinstance(definition, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid orchestration definition"
        )
    now = int(time.time() * 1000)
    session.execute(
        "INSERT INTO orchestration_runs("
        "id, user_id, tenant_id, orch_id, name, definition, input, status, "
        "created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            user_id,
            tenant_id,
            str(payload.get("orch_id") or ""),
            str(payload.get("name") or ""),
            _json_text(dict(definition)),
            str(payload.get("input") or ""),
            "pending",
            str(payload.get("created_by") or ""),
            now,
            now,
        ),
    )
    return {"created": True}


def _orchestration_run_get(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, "run_id", 200)
    user_id, tenant_id = _run_owner(payload)
    row = session.fetch_one(
        "SELECT id, orch_id, name, definition, input, status, final, error, "
        "created_by, created_at, updated_at, finished_at "
        "FROM orchestration_runs WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (run_id, user_id, tenant_id),
    )
    return _run_row(row, detail=True) if row else None


def _orchestration_run_list(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id, tenant_id = _run_owner(payload)
    status = payload.get("status", "")
    orch_id = payload.get("orch_id", "")
    if status and not is_run_status(status):
        raise StorageError(
            "database_protocol_error", "Invalid orchestration run status"
        )
    if not isinstance(status, str) or not isinstance(orch_id, str):
        raise StorageError(
            "database_protocol_error", "Invalid orchestration run filter"
        )
    limit = _integer(payload, "limit", default=50, minimum=1, maximum=200)
    rows = session.fetch_all(
        "SELECT id, orch_id, name, status, final, error, created_by, "
        "created_at, updated_at, finished_at FROM orchestration_runs "
        "WHERE user_id = ? AND tenant_id = ? "
        "AND (? = '' OR status = ?) AND (? = '' OR orch_id = ?) "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (user_id, tenant_id, status, status, orch_id, orch_id, limit),
    )
    return [_run_row(row, detail=False) for row in rows]


def _orchestration_run_update(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, "run_id", 200)
    user_id, tenant_id = _run_owner(payload)
    status = _run_status(payload)
    now = int(time.time() * 1000)
    final = payload.get("final")
    error_present = "error" in payload
    error = payload.get("error")
    row = session.fetch_one_for_update(
        "SELECT status, final, error, finished_at FROM orchestration_runs "
        "WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (run_id, user_id, tenant_id),
    )
    if row is None:
        return {"changed": False}
    if is_terminal_run_status(row["status"]) and row["status"] != status:
        return {"changed": False}
    next_final = row["final"] if final is None else str(final)
    if not error_present:
        next_error = row["error"]
    elif isinstance(error, str):
        next_error = error
    else:
        next_error = _json_text(error)
    finished = int(row["finished_at"] or 0)
    if is_terminal_run_status(status):
        finished = finished or now
    else:
        finished = 0
    count = session.execute(
        "UPDATE orchestration_runs SET status = ?, final = ?, error = ?, "
        "updated_at = ?, finished_at = ? "
        "WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (status, next_final, next_error, now, finished,
         run_id, user_id, tenant_id),
    )
    if count and error_present:
        session.execute(
            "UPDATE orchestration_runs SET continuity_pending = 0 "
            "WHERE id = ? AND user_id = ? AND tenant_id = ?", (run_id, user_id, tenant_id))
    return {"changed": bool(count)}


def _retire_orchestration_runs(session, payload, *, owner=None):
    where = f"status NOT IN ({_TERMINAL_STATUS_SQL})"
    args = list(TERMINAL_RUN_STATUSES)
    if owner is not None:
        where += " AND user_id = ? AND tenant_id = ?"
        args.extend(owner)
    rows = session.fetch_all(
        "SELECT id, user_id, tenant_id, status, created_by FROM orchestration_runs "
        f"WHERE {where} LIMIT 1001", tuple(args))
    if len(rows) > 1000:
        raise StorageError('database_resource_exhausted', 'Run retirement exceeds its bound')
    error = payload.get('error')
    encoded = error if isinstance(error, str) else _json_text(error)
    now, retired, goals, candidates = int(time.time() * 1000), 0, [], []
    for row in rows:
        boundary = (row['id'], row['user_id'], row['tenant_id'])
        if row['status'] == 'paused':
            last = session.fetch_one(
                "SELECT payload FROM orchestration_run_events "
                "WHERE run_id = ? AND user_id = ? AND tenant_id = ? ORDER BY seq DESC LIMIT 1",
                boundary)
            event = _load(last['payload']) if last else {}
            if ((event.get('type') == 'human_request'
                 and isinstance(event.get('continuation'), dict))
                    or event.get('continuation_expired') is True):
                continue
        changed = session.execute(
            "UPDATE orchestration_runs SET status = 'error', final = '', error = ?, "
            "updated_at = ?, finished_at = CASE WHEN finished_at = 0 THEN ? ELSE finished_at END "
            "WHERE id = ? AND user_id = ? AND tenant_id = ?",
            (encoded, now, now, *boundary))
        retired += changed
        if (changed and owner is None and row['created_by'] == GOAL_RUN_CREATED_BY
                and isinstance(error, Mapping)
                and all(error.get(key) == value for key, value in CONTINUITY_RESTART_ERROR.items())):
            session.execute(
                "UPDATE orchestration_runs SET continuity_pending = 1 "
                "WHERE id = ? AND user_id = ? AND tenant_id = ?", boundary)
        if changed:
            # Boot-continuity election (docs/RUN_CONTINUITY.md D2): the pass
            # resumes exactly the runs settled in this boot generation.
            candidates.append({
                'run_id': row['id'],
                'user_id': row['user_id'],
                'tenant_id': row['tenant_id'],
                'created_by': row['created_by'] or '',
            })
        if row['created_by'] == GOAL_RUN_CREATED_BY:
            goals.append(row)
    _append_interrupted_goal_transitions(session, goals, error=error)
    if owner is None:
        # Indexed durable election survives loss of the process-local boot slot.
        # This is process authority; public lookups retain their owner boundary.
        pending = session.fetch_all(
            "SELECT id, user_id, tenant_id, created_by FROM orchestration_runs "
            "WHERE continuity_pending = 1 ORDER BY finished_at, id LIMIT 1000", ())
        descriptors = {row['id']: {
            'run_id': row['id'], 'user_id': row['user_id'],
            'tenant_id': row['tenant_id'], 'created_by': row['created_by'],
        } for row in pending}
        # Fresh non-Goal retirements keep their explicit-resume descriptors.
        for candidate in candidates:
            if len(descriptors) >= 1000:
                break
            descriptors.setdefault(candidate['run_id'], candidate)
        candidates = list(descriptors.values())
    return {'retired': retired, 'candidates': candidates}


def _orchestration_run_retire(session: Session, payload: Mapping[str, Any]) -> Any:
    return _retire_orchestration_runs(session, payload, owner=_run_owner(payload))


def _orchestration_run_retire_all(session: Session, payload: Mapping[str, Any]) -> Any:
    return _retire_orchestration_runs(session, payload)


def _orchestration_event_append(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, "run_id", 200)
    user_id, tenant_id = _run_owner(payload)
    sequence = _integer(payload, "sequence", minimum=0)
    event = payload.get("event")
    if not isinstance(event, Mapping):
        raise StorageError("database_protocol_error", "Invalid orchestration event")
    encoded = _json_text(dict(event))
    if session.fetch_one_for_update(
        "SELECT 1 AS present FROM orchestration_runs "
        "WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (run_id, user_id, tenant_id),
    ) is None:
        raise StorageError(
            "database_not_found", "Orchestration run does not exist")
    inserted = session.execute(
        "INSERT INTO orchestration_run_events("
        "run_id, user_id, tenant_id, seq, type, node_id, payload, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id, user_id, tenant_id, seq) DO NOTHING",
        (
            run_id,
            user_id,
            tenant_id,
            sequence,
            str(event.get("type") or ""),
            str(event.get("node_id") or ""),
            encoded,
            int(time.time() * 1000),
        ),
    )
    if not inserted:
        row = session.fetch_one(
            "SELECT payload FROM orchestration_run_events "
            "WHERE run_id = ? AND user_id = ? AND tenant_id = ? AND seq = ?",
            (run_id, user_id, tenant_id, sequence),
        )
        existing = None if row is None else _json_text(_load(row["payload"]))
        if existing != encoded:
            raise StorageError(
                "database_conflict",
                "Orchestration event sequence has a conflicting payload",
            )
    if event.get('type') in CONTINUITY_ELECTION_CLOSED_EVENT_TYPES:
        session.execute(
            "UPDATE orchestration_runs SET continuity_pending = 0 "
            "WHERE id = ? AND user_id = ? AND tenant_id = ?", (run_id, user_id, tenant_id))
    return {"inserted": bool(inserted), "accepted": True}


def _orchestration_event_project(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id, tenant_id = _run_owner(payload)
    status = _run_status(payload, optional=True)
    if is_terminal_run_status(status):
        raise StorageError(
            "database_protocol_error",
            "Terminal orchestration status requires an explicit transition",
        )
    append = _orchestration_event_append(session, payload)
    if not append["inserted"]:
        return {"projected": True, "inserted": False}
    run_id = str(payload["run_id"])
    now = int(time.time() * 1000)
    if status:
        count = session.execute(
            "UPDATE orchestration_runs SET status = ?, updated_at = ?, "
            "finished_at = 0 WHERE id = ? AND user_id = ? AND tenant_id = ? "
            "AND status NOT IN "
            f"({_TERMINAL_STATUS_SQL})",
            (status, now, run_id, user_id, tenant_id, *TERMINAL_RUN_STATUSES),
        )
    else:
        count = session.execute(
            "UPDATE orchestration_runs SET updated_at = ? "
            "WHERE id = ? AND user_id = ? AND tenant_id = ? "
            f"AND status NOT IN ({_TERMINAL_STATUS_SQL})",
            (now, run_id, user_id, tenant_id, *TERMINAL_RUN_STATUSES),
        )
    if not count:
        raise StorageError(
            "database_conflict", "Orchestration run header rejected event projection"
        )
    return {"projected": True, "inserted": True}


def _orchestration_event_page(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, "run_id", 200)
    user_id, tenant_id = _run_owner(payload)
    requested = _integer(payload, "cursor", default=0, minimum=0)
    boundary_row = session.fetch_one(
        "SELECT COALESCE(MAX(seq) + 1, 0) AS next_cursor "
        "FROM orchestration_run_events "
        "WHERE run_id = ? AND user_id = ? AND tenant_id = ?",
        (run_id, user_id, tenant_id),
    )
    boundary = int(boundary_row["next_cursor"] or 0) if boundary_row else 0
    if requested > boundary:
        return {
            "events": [],
            "next_cursor": boundary,
            "cursor_reset": True,
            "caught_up": True,
        }
    lengths = session.fetch_all(
        f"SELECT seq, {utf8_length_expression(session.backend, 'payload')} AS payload_bytes "
        "FROM orchestration_run_events "
        "WHERE run_id = ? AND user_id = ? AND tenant_id = ? AND seq >= ? "
        "ORDER BY seq LIMIT 2000",
        (run_id, user_id, tenant_id, requested),
    )
    count, total_bytes = 0, 1024
    for row in lengths:
        # Account for the sequence field added to historical event payloads.
        size = int(row['payload_bytes']) + 32
        if total_bytes + size > _ORCHESTRATION_EVENT_PAGE_BYTES:
            if count == 0:
                raise StorageError('database_protocol_error',
                                   'Orchestration event exceeds the replay page byte boundary')
            break
        count += 1
        total_bytes += size
    rows = session.fetch_all(
        "SELECT seq, payload FROM orchestration_run_events "
        "WHERE run_id = ? AND user_id = ? AND tenant_id = ? AND seq >= ? "
        "ORDER BY seq LIMIT ?",
        (run_id, user_id, tenant_id, requested, count),
    ) if count else []
    events = []
    for row in rows:
        event = _load(row["payload"])
        if not isinstance(event, dict):
            raise StorageError(
                "database_integrity", "Durable orchestration event is not an object"
            )
        event.setdefault("seq", int(row["seq"]))
        events.append(event)
    next_cursor = min(boundary, int(rows[-1]['seq']) + 1) if rows else boundary
    return {
        "events": events,
        "next_cursor": next_cursor,
        "cursor_reset": False,
        "caught_up": next_cursor >= boundary,
    }


def _orchestration_run_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, "run_id", 200)
    user_id, tenant_id = _run_owner(payload)
    # Serialize event deletion with transaction-scoped recovery observations.
    session.fetch_one_for_update(
        "SELECT id FROM orchestration_runs WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (run_id, user_id, tenant_id),
    )
    session.execute(
        "DELETE FROM orchestration_run_events "
        "WHERE run_id = ? AND user_id = ? AND tenant_id = ?",
        (run_id, user_id, tenant_id),
    )
    count = session.execute(
        "DELETE FROM orchestration_runs "
        "WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (run_id, user_id, tenant_id),
    )
    return {"deleted": bool(count)}


def _goal_event_append(
    session: Session,
    *,
    run_id: str,
    user_id: int,
    tenant_id: str,
    event: Mapping[str, Any],
) -> int:
    """Append the next GoalRun event inside the caller's transaction."""
    # The run row fences MAX(seq), ordinary event appends and native claims.
    if session.fetch_one_for_update(
        "SELECT id FROM orchestration_runs WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (run_id, user_id, tenant_id),
    ) is None:
        raise StorageError('database_not_found', 'Orchestration run does not exist')
    row = session.fetch_one(
        "SELECT COALESCE(MAX(seq) + 1, 0) AS next_sequence "
        "FROM orchestration_run_events WHERE run_id = ? AND user_id = ? "
        "AND tenant_id = ?",
        (run_id, user_id, tenant_id),
    )
    sequence = int(row["next_sequence"] or 0) if row else 0
    document = dict(event)
    document.setdefault("format", GOAL_RUN_FORMAT)
    session.execute(
        "INSERT INTO orchestration_run_events("
        "run_id, user_id, tenant_id, seq, type, node_id, payload, ts) "
        "VALUES (?, ?, ?, ?, ?, '', ?, ?)",
        (
            run_id,
            user_id,
            tenant_id,
            sequence,
            str(document.get("type") or ""),
            _json_text(document),
            int(time.time() * 1000),
        ),
    )
    return sequence


def _append_interrupted_goal_transitions(
    session: Session,
    rows: list[Mapping[str, Any]],
    *,
    error: Any,
) -> None:
    """Keep startup retirement typed for every affected GoalRun."""
    error_projection: Any = (
        dict(error) if isinstance(error, Mapping) else str(error or '')
    )
    for row in rows:
        _goal_event_append(
            session,
            run_id=str(row['id']),
            user_id=int(row['user_id']),
            tenant_id=str(row['tenant_id'] or ''),
            event={
                'type': 'goal_run_transition',
                'status': 'failed',
                'reason': 'worker_lost',
                'outcome': {'error': error_projection},
            },
        )


def _goal_last_event(
    session: Session,
    *,
    run_id: str,
    user_id: int,
    tenant_id: str,
) -> dict[str, Any]:
    row = session.fetch_one(
        "SELECT payload FROM orchestration_run_events "
        "WHERE run_id = ? AND user_id = ? AND tenant_id = ? "
        "AND type IN ('goal_run_started', 'goal_run_transition') "
        "ORDER BY seq DESC LIMIT 1",
        (run_id, user_id, tenant_id),
    )
    if row is None:
        return {}
    decoded = _load(row["payload"])
    return decoded if isinstance(decoded, dict) else {}


def _goal_started_event(
    session: Session,
    *,
    run_id: str,
    user_id: int,
    tenant_id: str,
) -> dict[str, Any]:
    row = session.fetch_one(
        "SELECT payload FROM orchestration_run_events "
        "WHERE run_id = ? AND user_id = ? AND tenant_id = ? "
        "AND type = 'goal_run_started' ORDER BY seq LIMIT 1",
        (run_id, user_id, tenant_id),
    )
    if row is None:
        return {}
    decoded = _load(row['payload'])
    return decoded if isinstance(decoded, dict) else {}


def _goal_run_projection(
    session: Session,
    *,
    run_id: str,
    user_id: int,
    tenant_id: str,
) -> dict[str, Any] | None:
    row = session.fetch_one(
        "SELECT id, orch_id, name, definition, input, status, final, error, "
        "created_by, created_at, updated_at, finished_at "
        "FROM orchestration_runs WHERE id = ? AND user_id = ? AND tenant_id = ? "
        "AND created_by = ?",
        (run_id, user_id, tenant_id, GOAL_RUN_CREATED_BY),
    )
    if row is None:
        return None
    event = _goal_last_event(
        session, run_id=run_id, user_id=user_id, tenant_id=tenant_id)
    started = _goal_started_event(
        session, run_id=run_id, user_id=user_id, tenant_id=tenant_id)
    physical_status = str(row["status"] or "pending")
    status = str(event.get("status") or goal_status_from_storage(physical_status))
    orch_id = str(row["orch_id"] or "")
    conversation_id = (
        orch_id.removeprefix(GOAL_RUN_ORCHESTRATION_PREFIX)
        if orch_id.startswith(GOAL_RUN_ORCHESTRATION_PREFIX) else ''
    )
    return {
        "format": GOAL_RUN_FORMAT,
        "runId": row["id"],
        "conversationId": conversation_id,
        "objective": row["input"] or "",
        "status": status,
        "reason": str(event.get("reason") or ""),
        "policy": (
            dict(started.get('policy'))
            if isinstance(started.get('policy'), Mapping) else {}
        ),
        "final": str(row['final'] or ''),
        "outcome": (
            dict(event.get('outcome'))
            if isinstance(event.get('outcome'), Mapping) else {}
        ),
        # Full terminal stop report (excerpt lives in conversation settings).
        "stopReport": str(event.get('stopReport') or ''),
        "storageStatus": physical_status,
        "terminal": status in GOAL_RUN_TERMINAL_STATUSES,
        "createdAt": int(row["created_at"] or 0),
        "updatedAt": int(row["updated_at"] or 0),
        "finishedAt": int(row["finished_at"] or 0),
    }


def _goal_run_get(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, 'run_id', 200)
    user_id, tenant_id = _run_owner(payload)
    return _goal_run_projection(
        session, run_id=run_id, user_id=user_id, tenant_id=tenant_id)


def _goal_run_latest(session: Session, payload: Mapping[str, Any]) -> Any:
    conversation_id = _required_text(payload, 'conversation_id', 256)
    user_id, tenant_id = _run_owner(payload)
    row = session.fetch_one(
        'SELECT id FROM orchestration_runs '
        'WHERE user_id = ? AND tenant_id = ? AND orch_id = ? '
        'AND created_by = ? ORDER BY created_at DESC, id DESC LIMIT 1',
        (
            user_id,
            tenant_id,
            goal_orchestration_id(conversation_id),
            GOAL_RUN_CREATED_BY,
        ),
    )
    if row is None:
        return None
    return _goal_run_projection(
        session,
        run_id=str(row['id']),
        user_id=user_id,
        tenant_id=tenant_id,
    )


def _goal_run_start(session: Session, payload: Mapping[str, Any]) -> Any:
    """Atomically supersede any prior active goal and start exactly one run."""
    run_id = _required_text(payload, "run_id", 200)
    conversation_id = _required_text(payload, "conversation_id", 256)
    objective = _required_text(
        payload, "objective", MAX_GOAL_OBJECTIVE_CHARS)
    user_id, tenant_id = _run_owner(payload)
    definition = payload.get("definition")
    policy = payload.get("policy")
    if not isinstance(definition, Mapping) or not isinstance(policy, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid GoalRun definition or policy")
    if dict(policy) != goal_run_policy():
        raise StorageError(
            'database_protocol_error',
            'GoalRun policy does not match the canonical contract',
        )
    orch_id = goal_orchestration_id(conversation_id)
    existing = session.fetch_one(
        "SELECT id, orch_id, input, definition, created_by "
        "FROM orchestration_runs "
        "WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (run_id, user_id, tenant_id),
    )
    if existing is not None:
        if (
            existing["created_by"] != GOAL_RUN_CREATED_BY
            or existing["orch_id"] != orch_id
            or existing["input"] != objective
            or _load(existing['definition']) != dict(definition)
        ):
            raise StorageError(
                "database_conflict", "GoalRun id belongs to another launch")
        started = _goal_started_event(
            session, run_id=run_id, user_id=user_id, tenant_id=tenant_id)
        if started.get('policy') != dict(policy):
            raise StorageError(
                'database_conflict', 'GoalRun id has a different policy')
        return {
            "created": False,
            "supersededRunIds": [],
            "run": _goal_run_projection(
                session, run_id=run_id, user_id=user_id, tenant_id=tenant_id),
        }

    active_rows = session.fetch_all(
        "SELECT id FROM orchestration_runs WHERE user_id = ? AND tenant_id = ? "
        "AND orch_id = ? AND created_by = ? "
        f"AND status NOT IN ({_TERMINAL_STATUS_SQL}) "
        "ORDER BY created_at, id",
        (user_id, tenant_id, orch_id, GOAL_RUN_CREATED_BY,
         *TERMINAL_RUN_STATUSES),
    )
    now = int(time.time() * 1000)
    superseded: list[str] = []
    for active in active_rows:
        prior_run_id = str(active["id"])
        cancellation = {
            "format": GOAL_RUN_FORMAT,
            "kind": "goal_run_cancelled",
            "reason": "superseded_by_new_goal",
            "supersededByRunId": run_id,
        }
        changed = session.execute(
            "UPDATE orchestration_runs SET status = 'aborted', error = ?, "
            "updated_at = ?, finished_at = CASE WHEN finished_at = 0 THEN ? "
            "ELSE finished_at END WHERE id = ? AND user_id = ? AND tenant_id = ? "
            f"AND status NOT IN ({_TERMINAL_STATUS_SQL})",
            (_json_text(cancellation), now, now, prior_run_id, user_id,
             tenant_id, *TERMINAL_RUN_STATUSES),
        )
        if not changed:
            continue
        _goal_event_append(
            session,
            run_id=prior_run_id,
            user_id=user_id,
            tenant_id=tenant_id,
            event={
                "type": "goal_run_transition",
                "status": "cancelled",
                "reason": "superseded_by_new_goal",
                "supersededByRunId": run_id,
            },
        )
        superseded.append(prior_run_id)

    session.execute(
        "INSERT INTO orchestration_runs("
        "id, user_id, tenant_id, orch_id, name, definition, input, status, "
        "created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)",
        (
            run_id,
            user_id,
            tenant_id,
            orch_id,
            "Goal Mode",
            _json_text(dict(definition)),
            objective,
            GOAL_RUN_CREATED_BY,
            now,
            now,
        ),
    )
    _goal_event_append(
        session,
        run_id=run_id,
        user_id=user_id,
        tenant_id=tenant_id,
        event={
            "type": "goal_run_started",
            "status": "active",
            "reason": "started",
            "conversationId": conversation_id,
            "objective": objective,
            "policy": dict(policy),
        },
    )
    return {
        "created": True,
        "supersededRunIds": superseded,
        "run": _goal_run_projection(
            session, run_id=run_id, user_id=user_id, tenant_id=tenant_id),
    }


def _goal_run_transition(session: Session, payload: Mapping[str, Any]) -> Any:
    """Atomically append one typed terminal transition and settle its header."""
    run_id = _required_text(payload, "run_id", 200)
    user_id, tenant_id = _run_owner(payload)
    status = payload.get("status")
    reason = payload.get("reason")
    if not is_valid_goal_transition(status, reason):
        raise StorageError(
            "database_protocol_error",
            "Invalid GoalRun terminal status/reason pair",
        )
    final = payload.get("final", "")
    outcome = payload.get("outcome", {})
    # The full (machine-token-stripped) terminal VU verdict.  Persisted on
    # the transition event so the fold record in conversation settings can
    # stay a bounded excerpt while the report itself is never truncated.
    stop_report = _optional_text(
        payload, "stop_report", maximum=_GOAL_STOP_REPORT_MAX_CHARS,
        scope="goal run")
    if not isinstance(final, str) or not isinstance(outcome, Mapping):
        raise StorageError(
            "database_protocol_error", "Invalid GoalRun transition payload")
    row = session.fetch_one_for_update(
        "SELECT status, final, created_by FROM orchestration_runs "
        "WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (run_id, user_id, tenant_id),
    )
    if row is None or row["created_by"] != GOAL_RUN_CREATED_BY:
        return {"transitioned": False, "run": None}
    current_event = _goal_last_event(
        session, run_id=run_id, user_id=user_id, tenant_id=tenant_id)
    current_goal_status = str(
        current_event.get("status") or goal_status_from_storage(row["status"]))
    if is_terminal_run_status(str(row["status"] or "")):
        if (
            current_goal_status != status
            or current_event.get('reason') != reason
            or str(row['final'] or '') != final
            or current_event.get('outcome', {}) != dict(outcome)
            or str(current_event.get('stopReport') or '') != stop_report
        ):
            raise StorageError(
                "database_conflict", "GoalRun terminal meaning is immutable")
        return {
            "transitioned": False,
            "run": _goal_run_projection(
                session, run_id=run_id, user_id=user_id, tenant_id=tenant_id),
        }

    physical_status = storage_status_for_goal_status(str(status))
    now = int(time.time() * 1000)
    error_value = ""
    if status != "completed":
        error_value = _json_text({
            "format": GOAL_RUN_FORMAT,
            "kind": f"goal_run_{status}",
            "reason": reason,
        })
    changed = session.execute(
        "UPDATE orchestration_runs SET status = ?, final = ?, error = ?, "
        "updated_at = ?, finished_at = CASE WHEN finished_at = 0 THEN ? "
        "ELSE finished_at END WHERE id = ? AND user_id = ? AND tenant_id = ? "
        f"AND status NOT IN ({_TERMINAL_STATUS_SQL})",
        (physical_status, final, error_value, now, now, run_id, user_id,
         tenant_id, *TERMINAL_RUN_STATUSES),
    )
    if not changed:
        raise StorageError(
            "database_conflict", "GoalRun transition lost its active fence")
    _goal_event_append(
        session,
        run_id=run_id,
        user_id=user_id,
        tenant_id=tenant_id,
        event={
            "type": "goal_run_transition",
            "status": status,
            "reason": reason,
            "outcome": dict(outcome),
            **({"stopReport": stop_report} if stop_report else {}),
        },
    )
    return {
        "transitioned": True,
        "run": _goal_run_projection(
            session, run_id=run_id, user_id=user_id, tenant_id=tenant_id),
    }


_SWARM_NONTERMINAL = frozenset({"pending", "running", "retrying"})


def _swarm_json(value: Any, expected: type, field: str) -> str:
    if not isinstance(value, expected):
        raise StorageError("database_protocol_error", f"Invalid swarm {field}")
    return _json_text(value)


def _optional_text(
    payload: Mapping[str, Any],
    field: str,
    *,
    default: str = "",
    maximum: int = 4096,
    scope: str = "storage",
) -> str:
    value = payload.get(field, default)
    if not isinstance(value, str) or len(value) > maximum:
        raise StorageError("database_protocol_error", f"Invalid {scope} {field}")
    return value


def _swarm_session_save(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key = _required_text(payload, "swarm_key", 512)
    specs = payload.get("specs")
    config = payload.get("config")
    specs_json = _swarm_json(specs, list, "specs")
    if not isinstance(config, Mapping):
        raise StorageError("database_protocol_error", "Invalid swarm config")
    config_json = _json_text(dict(config))
    now = _integer(payload, "now_ms", minimum=0)
    status = _optional_text(
        payload, "status", default="running", maximum=64, scope="swarm"
    )
    if status not in SWARM_SESSION_WRITABLE_STATUSES:
        raise StorageError(
            "database_protocol_error", "Invalid swarm session status"
        )
    session.lock_key("swarm.session", swarm_key)
    session.execute(
        "INSERT INTO swarm_sessions("
        "swarm_key, conv_id, task_id, status, specs_json, config_json, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(swarm_key) DO UPDATE SET "
        "conv_id = excluded.conv_id, task_id = excluded.task_id, "
        "status = excluded.status, specs_json = excluded.specs_json, "
        "config_json = excluded.config_json, updated_at = excluded.updated_at",
        (
            swarm_key,
            _optional_text(payload, "conv_id", scope="swarm"),
            _optional_text(payload, "task_id", scope="swarm"),
            status,
            specs_json,
            config_json,
            now,
            now,
        ),
    )
    return {"saved": True}


def _swarm_session_terminate(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key = _required_text(payload, "swarm_key", 512)
    count = session.execute(
        "UPDATE swarm_sessions SET status = ?, updated_at = ? "
        "WHERE swarm_key = ? AND status = ?",
        (
            SWARM_SESSION_STATUS_TERMINATED,
            _integer(payload, "now_ms", minimum=0),
            swarm_key,
            SWARM_SESSION_STATUS_RUNNING,
        ),
    )
    return {"changed": bool(count)}


def _swarm_session_quarantine_ownerless(
    session: Session, payload: Mapping[str, Any]
) -> Any:
    """Quarantine one invalid legacy session without deleting child evidence.

    Ownership is re-checked inside this transaction. If a concurrent repair
    supplied a valid owner after startup discovery, the mutation becomes a
    clean no-op instead of quarantining repaired work.
    """
    swarm_key = _required_text(payload, "swarm_key", 512)
    now_ms = _integer(payload, "now_ms", minimum=0)
    session.lock_key("swarm.session", swarm_key)
    item = session.fetch_one(
        "SELECT status, config_json FROM swarm_sessions WHERE swarm_key = ?",
        (swarm_key,),
    )
    if item is None:
        return {"changed": False}
    config = _load(item["config_json"])
    if not isinstance(config, dict):
        raise StorageError(
            "database_integrity", "Durable swarm session config is invalid"
        )
    try:
        require_user_id(config.get("user_id"), context="durable swarm owner")
    except ValueError:
        pass
    else:
        return {"changed": False}
    current_status = str(item["status"] or "running")
    if current_status == SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS:
        return {"changed": False}
    count = session.execute(
        "UPDATE swarm_sessions SET status = ?, updated_at = ? "
        "WHERE swarm_key = ? AND status = ? AND config_json = ?",
        (
            SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS,
            now_ms,
            swarm_key,
            current_status,
            item["config_json"],
        ),
    )
    return {"changed": bool(count)}


def _swarm_session_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key = _required_text(payload, "swarm_key", 512)
    session.execute(
        "DELETE FROM swarm_agent_checkpoint_markers WHERE swarm_key = ?", (swarm_key,))
    session.execute("DELETE FROM swarm_agents WHERE swarm_key = ?", (swarm_key,))
    count = session.execute(
        "DELETE FROM swarm_sessions WHERE swarm_key = ?", (swarm_key,)
    )
    return {"deleted": bool(count)}


def _swarm_agent_save(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key = _required_text(payload, "swarm_key", 512)
    agent_id = _required_text(payload, "agent_id", 512)
    messages_json = _swarm_json(payload.get("messages"), list, "messages")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise StorageError("database_protocol_error", "Invalid swarm result")
    result_json = _json_text(dict(result))
    rounds_used = _integer(
        payload, "rounds_used", default=0, minimum=0, maximum=1_000_000
    )
    now = _integer(payload, "now_ms", minimum=0)
    delivered = payload.get("delivered")
    if delivered is not None and not isinstance(delivered, bool):
        raise StorageError("database_protocol_error", "Invalid swarm delivered flag")
    checkpoint_id = _optional_text(payload, "checkpoint_id", maximum=512, scope="swarm")
    # PostgreSQL TEXT rejects NUL bytes; a length-prefixed composite key is
    # unambiguous on both backends and safe for advisory-lock hashing.
    session.lock_key("swarm.agent", f"{len(swarm_key)}:{swarm_key}{agent_id}")
    values = (
        swarm_key,
        agent_id,
        _optional_text(payload, "role", scope="swarm"),
        _optional_text(payload, "objective", maximum=100_000, scope="swarm"),
        _optional_text(payload, "status", default="pending", maximum=64, scope="swarm"),
        messages_json,
        result_json,
        rounds_used,
        int(bool(delivered)),
        now,
        checkpoint_id,
    )
    if delivered is None:
        session.execute(
            "INSERT INTO swarm_agents("
            "swarm_key, agent_id, role, objective, status, messages_json, "
            "result_json, rounds_used, delivered, updated_at, checkpoint_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(swarm_key, agent_id) DO UPDATE SET "
            "role = excluded.role, objective = excluded.objective, "
            "status = excluded.status, messages_json = excluded.messages_json, "
            "result_json = excluded.result_json, "
            "rounds_used = excluded.rounds_used, updated_at = excluded.updated_at, "
            "checkpoint_id = excluded.checkpoint_id",
            values,
        )
    else:
        session.execute(
            "INSERT INTO swarm_agents("
            "swarm_key, agent_id, role, objective, status, messages_json, "
            "result_json, rounds_used, delivered, updated_at, checkpoint_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(swarm_key, agent_id) DO UPDATE SET "
            "role = excluded.role, objective = excluded.objective, "
            "status = excluded.status, messages_json = excluded.messages_json, "
            "result_json = excluded.result_json, rounds_used = excluded.rounds_used, "
            "delivered = excluded.delivered, updated_at = excluded.updated_at, "
            "checkpoint_id = excluded.checkpoint_id",
            values,
        )
    # A full checkpoint supersedes every earlier compact marker.
    session.execute(
        "DELETE FROM swarm_agent_checkpoint_markers WHERE swarm_key = ? AND agent_id = ?",
        (swarm_key, agent_id),
    )
    # Echoing the id acknowledges it as the base of a later swarm.agent.append.
    return {"saved": True, "checkpoint_id": checkpoint_id} if checkpoint_id else {"saved": True}


def _swarm_agent_append(session: Session, payload: Mapping[str, Any]) -> Any:
    """Append new messages to the exact checkpoint this writer last wrote.

    The base is that checkpoint's id and message count. A missing agent, any
    other write since (a different checkpoint_id) or a different count changes
    nothing and answers ``appended: false``; the writer then saves in full. On
    success the row equals a full save of the extended transcript: result,
    status, rounds and time are replaced and the compact marker retires.
    """
    swarm_key = _required_text(payload, "swarm_key", 512)
    agent_id = _required_text(payload, "agent_id", 512)
    base_checkpoint_id = _required_text(payload, "base_checkpoint_id", 512)
    checkpoint_id = _required_text(payload, "checkpoint_id", 512)
    base_message_count = _integer(payload, "base_message_count", minimum=0)
    appended = payload.get("messages")
    if not isinstance(appended, list):
        raise StorageError("database_protocol_error", "Invalid swarm messages")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise StorageError("database_protocol_error", "Invalid swarm result")
    result_json = _json_text(dict(result))
    rounds_used = _integer(
        payload, "rounds_used", default=0, minimum=0, maximum=1_000_000
    )
    now = _integer(payload, "now_ms", minimum=0)
    status = _optional_text(payload, "status", default="pending", maximum=64, scope="swarm")
    session.lock_key("swarm.agent", f"{len(swarm_key)}:{swarm_key}{agent_id}")
    row = session.fetch_one(
        "SELECT role, objective, messages_json, checkpoint_id FROM swarm_agents "
        "WHERE swarm_key = ? AND agent_id = ?",
        (swarm_key, agent_id),
    )
    if row is None or row["checkpoint_id"] != base_checkpoint_id:
        return {"appended": False}
    messages = _load(row["messages_json"])
    if not isinstance(messages, list):
        raise StorageError("database_integrity", "Durable swarm agent JSON is invalid")
    if len(messages) != base_message_count:
        return {"appended": False}
    messages.extend(appended)
    messages_json = _dump(messages).decode("utf-8")
    # A full save of this transcript must fit one storage frame and every
    # agent page must still carry it, so appends stop at the same page bound.
    row_bytes = sum(len(text.encode("utf-8")) for text in (
        agent_id, row["role"] or "", row["objective"] or "", messages_json, result_json))
    if row_bytes + _SWARM_AGENT_FIELD_OVERHEAD_BYTES > _SWARM_AGENT_PAGE_BYTES:
        raise StorageError("database_protocol_error", "Storage frame exceeds the size limit")
    session.execute(
        "UPDATE swarm_agents SET status = ?, messages_json = ?, result_json = ?, "
        "rounds_used = ?, updated_at = ?, checkpoint_id = ? "
        "WHERE swarm_key = ? AND agent_id = ?",
        (status, messages_json, result_json, rounds_used, now, checkpoint_id,
         swarm_key, agent_id),
    )
    session.execute(
        "DELETE FROM swarm_agent_checkpoint_markers WHERE swarm_key = ? AND agent_id = ?",
        (swarm_key, agent_id),
    )
    return {"appended": True}


def _swarm_checkpoint_marker(value: Any) -> dict[str, Any]:
    if (not isinstance(value, Mapping) or not value
            or not set(value) <= set(SWARM_CHECKPOINT_MARKER_RESULT_FIELDS)):
        raise StorageError("database_protocol_error", "Invalid swarm checkpoint marker")
    for item in value.values():
        if isinstance(item, str):
            if len(item) > SWARM_CHECKPOINT_MARKER_TEXT_CHARACTERS:
                raise StorageError(
                    "database_protocol_error", "Invalid swarm checkpoint marker")
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise StorageError("database_protocol_error", "Invalid swarm checkpoint marker")
    return dict(value)


def _swarm_agent_checkpoint_mark(session: Session, payload: Mapping[str, Any]) -> Any:
    """Overlay compact checkpoint fields on one agent's last full checkpoint.

    Constant size: the marker row never reads or rewrites the messages/result
    bodies. Reads apply it over the full row; the next ``swarm.agent.save``
    retires it. Without a full checkpoint there is nothing to mark.
    """
    swarm_key = _required_text(payload, "swarm_key", 512)
    agent_id = _required_text(payload, "agent_id", 512)
    marker = _swarm_checkpoint_marker(payload.get("result"))
    rounds_used = _integer(payload, "rounds_used", minimum=0, maximum=1_000_000)
    now = _integer(payload, "now_ms", minimum=0)
    session.lock_key("swarm.agent", f"{len(swarm_key)}:{swarm_key}{agent_id}")
    if session.fetch_one(
            "SELECT 1 AS present FROM swarm_agents WHERE swarm_key = ? AND agent_id = ?",
            (swarm_key, agent_id)) is None:
        return {"marked": False}
    current = session.fetch_one(
        "SELECT result_json FROM swarm_agent_checkpoint_markers "
        "WHERE swarm_key = ? AND agent_id = ?",
        (swarm_key, agent_id),
    )
    if current is not None:
        previous = _load(current["result_json"])
        if not isinstance(previous, dict):
            raise StorageError(
                "database_integrity", "Durable swarm checkpoint marker is invalid")
        marker = {**previous, **marker}
    session.execute(
        "INSERT INTO swarm_agent_checkpoint_markers("
        "swarm_key, agent_id, result_json, rounds_used, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(swarm_key, agent_id) DO UPDATE SET "
        "result_json = excluded.result_json, rounds_used = excluded.rounds_used, "
        "updated_at = excluded.updated_at",
        (swarm_key, agent_id, _json_text(marker), rounds_used, now),
    )
    return {"marked": True}


def _swarm_delivery_ids(payload: Mapping[str, Any]) -> tuple[str, list[str]]:
    swarm_key = _required_text(payload, "swarm_key", 512)
    agent_ids = payload.get("agent_ids")
    if (
        not isinstance(agent_ids, list)
        or len(agent_ids) > 1000
        or any(
            not isinstance(item, str) or not item or len(item) > 512
            for item in agent_ids
        )
    ):
        raise StorageError("database_protocol_error", "Invalid swarm agent_ids")
    return swarm_key, list(dict.fromkeys(agent_ids))


def _swarm_agents_acknowledge_results(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key, _ = _swarm_delivery_ids(payload)
    task_id = _required_text(payload, "task_id", 4096)
    # Share the replacement writer's lock, including PostgreSQL's read/ACK
    # window. A process-local session check cannot fence another writer.
    session.lock_key("swarm.session", swarm_key)
    row = session.fetch_one("SELECT task_id FROM swarm_sessions WHERE swarm_key = ?",
                            (swarm_key,))
    if row is None or row['task_id'] != task_id:
        return {"acknowledged": False, "changed": 0}
    return {"acknowledged": True, **_swarm_agents_mark_delivered(session, payload)}


def _swarm_agents_mark_delivered(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key, agent_ids = _swarm_delivery_ids(payload)
    changed = 0
    for agent_id in agent_ids:
        changed += session.execute(
            "UPDATE swarm_agents SET delivered = 1 "
            "WHERE swarm_key = ? AND agent_id = ?",
            (swarm_key, agent_id),
        )
    return {"changed": changed}


def _swarm_marked_state(agent: Mapping[str, Any]) -> tuple[dict[str, Any], int, int]:
    """Compact marker overlay (result fields, rounds, updated_at) or none."""
    if agent["marker_result_json"] is None:
        return {}, int(agent["rounds_used"] or 0), int(agent["updated_at"] or 0)
    marker = _load(agent["marker_result_json"])
    if not isinstance(marker, dict):
        raise StorageError("database_integrity", "Durable swarm checkpoint marker is invalid")
    return (marker, int(agent["marker_rounds_used"] or 0),
            int(agent["marker_updated_at"] or 0))


def _swarm_agent_row(agent: Mapping[str, Any]) -> dict[str, Any]:
    messages = _load(agent["messages_json"])
    result = _load(agent["result_json"])
    if not isinstance(messages, list) or not isinstance(result, dict):
        raise StorageError(
            "database_integrity", "Durable swarm agent JSON is invalid"
        )
    marker, rounds_used, updated_at = _swarm_marked_state(agent)
    return {
        "agent_id": agent["agent_id"],
        "role": agent["role"] or "",
        "objective": agent["objective"] or "",
        "status": agent["status"] or "pending",
        "messages": messages,
        "result": {**result, **marker},
        "rounds_used": rounds_used,
        "delivered": bool(agent["delivered"]),
        "updated_at": updated_at,
    }


_SWARM_MARKER_COLUMNS = (
    "m.result_json AS marker_result_json, m.rounds_used AS marker_rounds_used, "
    "m.updated_at AS marker_updated_at"
)
_SWARM_MARKER_JOIN = (
    "LEFT JOIN swarm_agent_checkpoint_markers AS m "
    "ON m.swarm_key = a.swarm_key AND m.agent_id = a.agent_id"
)
_SWARM_AGENT_SELECT = (
    "SELECT a.agent_id AS agent_id, a.role AS role, a.objective AS objective, "
    "a.status AS status, a.messages_json AS messages_json, "
    "a.result_json AS result_json, a.rounds_used AS rounds_used, "
    "a.delivered AS delivered, a.updated_at AS updated_at, "
    f"{_SWARM_MARKER_COLUMNS} FROM swarm_agents AS a {_SWARM_MARKER_JOIN}"
)


def _swarm_session_get(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key = _required_text(payload, "swarm_key", 512)
    include_agents = payload.get("include_agents", True)
    if not isinstance(include_agents, bool):
        raise StorageError(
            "database_protocol_error", "Invalid swarm include_agents flag"
        )
    item = session.fetch_one(
        "SELECT swarm_key, conv_id, task_id, status, specs_json, config_json, "
        "created_at, updated_at FROM swarm_sessions WHERE swarm_key = ?",
        (swarm_key,),
    )
    if item is None:
        return None
    specs = _load(item["specs_json"])
    config = _load(item["config_json"])
    if not isinstance(specs, list) or not isinstance(config, dict):
        raise StorageError(
            "database_integrity", "Durable swarm session JSON is invalid"
        )
    detail = {
        "swarm_key": item["swarm_key"],
        "conv_id": item["conv_id"] or "",
        "task_id": item["task_id"] or "",
        "status": item["status"] or "running",
        "specs": specs,
        "config": config,
        "created_at": int(item["created_at"] or 0),
        "updated_at": int(item["updated_at"] or 0),
    }
    if include_agents:
        detail["agents"] = [
            _swarm_agent_row(agent)
            for agent in session.fetch_all(
                f"{_SWARM_AGENT_SELECT} WHERE a.swarm_key = ? ORDER BY a.agent_id",
                (swarm_key,),
            )
        ]
    return detail


def _swarm_agent_state_get(session: Session, payload: Mapping[str, Any]) -> Any:
    """Session header without specs/config plus one agent's compact state.

    Exact-run message consumers fence on this every model round, so it never
    reads checkpoint bodies: its cost is independent of retained history.
    """
    swarm_key = _required_text(payload, "swarm_key", 512)
    agent_id = _required_text(payload, "agent_id", 512)
    item = session.fetch_one(
        "SELECT swarm_key, conv_id, task_id, status, created_at, updated_at "
        "FROM swarm_sessions WHERE swarm_key = ?",
        (swarm_key,),
    )
    if item is None:
        return None
    agent = session.fetch_one(
        "SELECT a.agent_id AS agent_id, a.status AS status, a.rounds_used AS rounds_used, "
        "a.delivered AS delivered, a.updated_at AS updated_at, "
        f"{_SWARM_MARKER_COLUMNS} FROM swarm_agents AS a {_SWARM_MARKER_JOIN} "
        "WHERE a.swarm_key = ? AND a.agent_id = ?",
        (swarm_key, agent_id),
    )
    _, rounds_used, updated_at = (_swarm_marked_state(agent) if agent is not None
                                  else ({}, 0, 0))
    return {
        "swarm_key": item["swarm_key"],
        "conv_id": item["conv_id"] or "",
        "task_id": item["task_id"] or "",
        "status": item["status"] or "running",
        "created_at": int(item["created_at"] or 0),
        "updated_at": int(item["updated_at"] or 0),
        "agent": None if agent is None else {
            "agent_id": agent["agent_id"],
            "status": agent["status"] or "pending",
            "rounds_used": rounds_used,
            "delivered": bool(agent["delivered"]),
            "updated_at": updated_at,
        },
    }


# The native swarm stream ceiling (contracts/tofudb_ir_v1.json swarm_sessions
# maximumResponseBytes) less the page envelope. One checkpoint always fits.
_SWARM_AGENT_PAGE_BYTES = MAX_FRAME_BYTES - 4096 - 4096
_SWARM_AGENT_FIELD_OVERHEAD_BYTES = 256


def _swarm_agents_page(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key = _required_text(payload, "swarm_key", 512)
    after = _optional_text(payload, "after_agent_id", maximum=512, scope="swarm")
    size = " + ".join(
        utf8_length_expression(session.backend, column)
        for column in ("agent_id", "role", "objective", "messages_json", "result_json")
    )
    remaining = session.fetch_all(
        f"SELECT agent_id, {size} AS row_bytes FROM swarm_agents "
        "WHERE swarm_key = ? AND agent_id > ? ORDER BY agent_id",
        (swarm_key, after),
    )
    marker_size = utf8_length_expression(session.backend, "result_json")
    marker_bytes = {
        row["agent_id"]: int(row["marker_bytes"])
        for row in session.fetch_all(
            f"SELECT agent_id, {marker_size} AS marker_bytes "
            "FROM swarm_agent_checkpoint_markers WHERE swarm_key = ? AND agent_id > ?",
            (swarm_key, after),
        )
    }
    count, page_bytes = 0, 0
    for row in remaining:
        row_bytes = (int(row["row_bytes"]) + marker_bytes.get(row["agent_id"], 0)
                     + _SWARM_AGENT_FIELD_OVERHEAD_BYTES)
        if count and page_bytes + row_bytes > _SWARM_AGENT_PAGE_BYTES:
            break
        count += 1
        page_bytes += row_bytes
    agents = [
        _swarm_agent_row(agent)
        for agent in session.fetch_all(
            f"{_SWARM_AGENT_SELECT} WHERE a.swarm_key = ? AND a.agent_id > ? "
            "ORDER BY a.agent_id LIMIT ?",
            (swarm_key, after, count),
        )
    ] if count else []
    return {
        "agents": agents,
        "next_after_agent_id": (
            agents[-1]["agent_id"] if count < len(remaining) else None
        ),
    }


def _swarm_resumable_list(session: Session, _payload: Mapping[str, Any]) -> Any:
    """Keys only: each session is read separately, so history never overflows."""
    normal_statuses = tuple(sorted(SWARM_SESSION_WRITABLE_STATUSES))
    nonterminal_statuses = tuple(sorted(_SWARM_NONTERMINAL))
    rows = session.fetch_all(
        "SELECT s.swarm_key AS swarm_key, s.updated_at AS updated_at "
        "FROM swarm_sessions AS s WHERE s.status IN (?, ?) AND EXISTS ("
        "SELECT 1 FROM swarm_agents AS candidate "
        "WHERE candidate.swarm_key = s.swarm_key AND ("
        "candidate.status IN (?, ?, ?) OR "
        "(candidate.status = ? AND candidate.delivered = 0))) "
        "ORDER BY s.updated_at DESC, s.swarm_key",
        (*normal_statuses, *nonterminal_statuses, "completed"),
    )
    return [
        {"swarm_key": row["swarm_key"], "updated_at": int(row["updated_at"] or 0)}
        for row in rows
    ]
