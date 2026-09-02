"""Orchestration run/event and swarm-session operation handlers plus shared row codecs."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

import orjson

from lib.log import get_logger
from lib.orchestration.run_status import (
    TERMINAL_RUN_STATUSES,
    VALID_RUN_STATUSES,
    is_run_status,
    is_terminal_run_status,
)
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session


logger = get_logger(__name__)


from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _integer,
    _load,
    _required_text,
)

_RUN_STATUSES = VALID_RUN_STATUSES
_TERMINAL_RUN_STATUSES = TERMINAL_RUN_STATUSES
_TERMINAL_STATUS_SQL = ", ".join("?" for _ in TERMINAL_RUN_STATUSES)


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
    row = session.fetch_one(
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
    return {"changed": bool(count)}


def _orchestration_run_retire(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id, tenant_id = _run_owner(payload)
    error = payload.get("error")
    error_text = error if isinstance(error, str) else _json_text(error)
    now = int(time.time() * 1000)
    count = session.execute(
        "UPDATE orchestration_runs SET status = 'error', final = '', "
        "error = ?, updated_at = ?, finished_at = CASE "
        "WHEN finished_at = 0 THEN ? ELSE finished_at END "
        "WHERE user_id = ? AND tenant_id = ? "
        f"AND status NOT IN ({_TERMINAL_STATUS_SQL})",
        (error_text, now, now, user_id, tenant_id, *TERMINAL_RUN_STATUSES),
    )
    return {"retired": count}


def _orchestration_run_retire_all(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    """Settle process-orphaned runs across owners during server startup."""
    error = payload.get("error")
    error_text = error if isinstance(error, str) else _json_text(error)
    now = int(time.time() * 1000)
    count = session.execute(
        "UPDATE orchestration_runs SET status = 'error', final = '', "
        "error = ?, updated_at = ?, finished_at = CASE "
        "WHEN finished_at = 0 THEN ? ELSE finished_at END "
        f"WHERE status NOT IN ({_TERMINAL_STATUS_SQL})",
        (error_text, now, now, *TERMINAL_RUN_STATUSES),
    )
    return {"retired": count}


def _orchestration_event_append(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, "run_id", 200)
    user_id, tenant_id = _run_owner(payload)
    sequence = _integer(payload, "sequence", minimum=0)
    event = payload.get("event")
    if not isinstance(event, Mapping):
        raise StorageError("database_protocol_error", "Invalid orchestration event")
    encoded = _json_text(dict(event))
    if session.fetch_one(
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
    rows = session.fetch_all(
        "SELECT seq, payload FROM orchestration_run_events "
        "WHERE run_id = ? AND user_id = ? AND tenant_id = ? AND seq >= ? "
        "ORDER BY seq LIMIT 2000",
        (run_id, user_id, tenant_id, requested),
    )
    events = []
    for row in rows:
        event = _load(row["payload"])
        if not isinstance(event, dict):
            raise StorageError(
                "database_integrity", "Durable orchestration event is not an object"
            )
        event.setdefault("seq", int(row["seq"]))
        events.append(event)
    next_cursor = boundary
    if len(events) >= 2000:
        next_cursor = min(boundary, int(events[-1]["seq"]) + 1)
    return {
        "events": events,
        "next_cursor": next_cursor,
        "cursor_reset": False,
        "caught_up": next_cursor >= boundary,
    }


def _orchestration_run_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    run_id = _required_text(payload, "run_id", 200)
    user_id, tenant_id = _run_owner(payload)
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
            _optional_text(
                payload, "status", default="running", maximum=64, scope="swarm"
            ),
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
        "UPDATE swarm_sessions SET status = 'terminated', updated_at = ? "
        "WHERE swarm_key = ?",
        (_integer(payload, "now_ms", minimum=0), swarm_key),
    )
    return {"changed": bool(count)}


def _swarm_session_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key = _required_text(payload, "swarm_key", 512)
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
    )
    if delivered is None:
        session.execute(
            "INSERT INTO swarm_agents("
            "swarm_key, agent_id, role, objective, status, messages_json, "
            "result_json, rounds_used, delivered, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(swarm_key, agent_id) DO UPDATE SET "
            "role = excluded.role, objective = excluded.objective, "
            "status = excluded.status, messages_json = excluded.messages_json, "
            "result_json = excluded.result_json, "
            "rounds_used = excluded.rounds_used, updated_at = excluded.updated_at",
            values,
        )
    else:
        session.execute(
            "INSERT INTO swarm_agents("
            "swarm_key, agent_id, role, objective, status, messages_json, "
            "result_json, rounds_used, delivered, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(swarm_key, agent_id) DO UPDATE SET "
            "role = excluded.role, objective = excluded.objective, "
            "status = excluded.status, messages_json = excluded.messages_json, "
            "result_json = excluded.result_json, rounds_used = excluded.rounds_used, "
            "delivered = excluded.delivered, updated_at = excluded.updated_at",
            values,
        )
    return {"saved": True}


def _swarm_agents_mark_delivered(session: Session, payload: Mapping[str, Any]) -> Any:
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
    changed = 0
    for agent_id in dict.fromkeys(agent_ids):
        changed += session.execute(
            "UPDATE swarm_agents SET delivered = 1 "
            "WHERE swarm_key = ? AND agent_id = ?",
            (swarm_key, agent_id),
        )
    return {"changed": changed}


def _swarm_session_get(session: Session, payload: Mapping[str, Any]) -> Any:
    swarm_key = _required_text(payload, "swarm_key", 512)
    item = session.fetch_one(
        "SELECT swarm_key, conv_id, task_id, status, specs_json, config_json, "
        "created_at, updated_at FROM swarm_sessions WHERE swarm_key = ?",
        (swarm_key,),
    )
    if item is None:
        return None
    agents = session.fetch_all(
        "SELECT agent_id, role, objective, status, messages_json, result_json, "
        "rounds_used, delivered, updated_at FROM swarm_agents "
        "WHERE swarm_key = ? ORDER BY agent_id",
        (swarm_key,),
    )
    specs = _load(item["specs_json"])
    config = _load(item["config_json"])
    if not isinstance(specs, list) or not isinstance(config, dict):
        raise StorageError(
            "database_integrity", "Durable swarm session JSON is invalid"
        )
    decoded_agents = []
    for agent in agents:
        messages = _load(agent["messages_json"])
        result = _load(agent["result_json"])
        if not isinstance(messages, list) or not isinstance(result, dict):
            raise StorageError(
                "database_integrity", "Durable swarm agent JSON is invalid"
            )
        decoded_agents.append(
            {
                "agent_id": agent["agent_id"],
                "role": agent["role"] or "",
                "objective": agent["objective"] or "",
                "status": agent["status"] or "pending",
                "messages": messages,
                "result": result,
                "rounds_used": int(agent["rounds_used"] or 0),
                "delivered": bool(agent["delivered"]),
                "updated_at": int(agent["updated_at"] or 0),
            }
        )
    return {
        "swarm_key": item["swarm_key"],
        "conv_id": item["conv_id"] or "",
        "task_id": item["task_id"] or "",
        "status": item["status"] or "running",
        "specs": specs,
        "config": config,
        "created_at": int(item["created_at"] or 0),
        "updated_at": int(item["updated_at"] or 0),
        "agents": decoded_agents,
    }


def _swarm_resumable_list(session: Session, _payload: Mapping[str, Any]) -> Any:
    sessions = session.fetch_all(
        "SELECT swarm_key, conv_id, task_id, status, specs_json, config_json "
        "FROM swarm_sessions ORDER BY updated_at DESC, swarm_key"
    )
    agents = session.fetch_all(
        "SELECT swarm_key, agent_id, role, objective, status, messages_json, "
        "result_json, rounds_used, delivered FROM swarm_agents "
        "ORDER BY swarm_key, agent_id"
    )
    by_session: dict[str, list[Mapping[str, Any]]] = {}
    for agent in agents:
        by_session.setdefault(str(agent["swarm_key"]), []).append(agent)
    result = []
    for item in sessions:
        decoded_agents = []
        resumable = False
        for agent in by_session.get(str(item["swarm_key"]), []):
            status = str(agent["status"] or "pending")
            delivered = bool(agent["delivered"])
            resumable = resumable or status in _SWARM_NONTERMINAL
            resumable = resumable or (status == "completed" and not delivered)
            messages = _load(agent["messages_json"])
            agent_result = _load(agent["result_json"])
            if not isinstance(messages, list) or not isinstance(agent_result, dict):
                raise StorageError(
                    "database_integrity", "Durable swarm agent JSON is invalid"
                )
            decoded_agents.append(
                {
                    "agent_id": agent["agent_id"],
                    "role": agent["role"] or "",
                    "objective": agent["objective"] or "",
                    "status": status,
                    "messages": messages,
                    "result": agent_result,
                    "rounds_used": int(agent["rounds_used"] or 0),
                    "delivered": delivered,
                }
            )
        if not resumable:
            continue
        specs = _load(item["specs_json"])
        config = _load(item["config_json"])
        if not isinstance(specs, list) or not isinstance(config, dict):
            raise StorageError(
                "database_integrity", "Durable swarm session JSON is invalid"
            )
        result.append(
            {
                "swarm_key": item["swarm_key"],
                "conv_id": item["conv_id"] or "",
                "task_id": item["task_id"] or "",
                "status": item["status"] or "running",
                "specs": specs,
                "config": config,
                "agents": decoded_agents,
            }
        )
    return result
