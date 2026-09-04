"""Timer and scheduler operation handlers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any


from lib.log import get_logger
from lib.scheduler.contract import (
    DUE_CLAIM_INTERVAL_SECONDS,
    MAX_TASKS_PER_OWNER,
    timer_live_capacity,
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

_TIMER_COLUMNS = (
    "id",
    "user_id",
    "conv_id",
    "source_task_id",
    "check_instruction",
    "check_command",
    "continuation_message",
    "poll_interval",
    "max_polls",
    "poll_count",
    "status",
    "tools_config",
    "created_at",
    "updated_at",
    "triggered_at",
    "cancelled_at",
    "execution_task_id",
    "last_poll_at",
    "last_poll_decision",
    "last_poll_reason",
    "condition_kind",
    "condition_command",
    "condition_regex",
    "promotion_streak",
    "fallback_streak",
    "promoted_at",
    "origin",
)


def _timer_id(payload: Mapping[str, Any]) -> str:
    return _required_text(payload, "timer_id", 128)


def _timer_owner(payload: Mapping[str, Any]) -> int:
    return _integer(payload, "user_id", minimum=1)


def _timer_document(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {column: row[column] for column in _TIMER_COLUMNS}
    result["tools_config"] = _load(result["tools_config"]) or {}
    for column in (
        "poll_interval",
        "max_polls",
        "poll_count",
        "promotion_streak",
        "fallback_streak",
        "user_id",
    ):
        result[column] = int(result[column] or 0)
    return result


def _timer_get(session: Session, payload: Mapping[str, Any]) -> Any:
    row = session.fetch_one(
        "SELECT " + ", ".join(_TIMER_COLUMNS)
        + " FROM storage_timers WHERE id = ? AND user_id = ?",
        (_timer_id(payload), _timer_owner(payload)),
    )
    return None if row is None else _timer_document(row)


def _timer_list(session: Session, payload: Mapping[str, Any]) -> Any:
    limit = _integer(payload, "limit", default=50, minimum=1, maximum=200)
    user_id = _timer_owner(payload)
    rows = session.fetch_all(
        "SELECT "
        + ", ".join(_TIMER_COLUMNS)
        + " FROM storage_timers WHERE user_id=? "
        "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, created_at DESC LIMIT ?",
        (user_id, limit),
    )
    return [_timer_document(row) for row in rows]


def _timer_history(session: Session, payload: Mapping[str, Any]) -> Any:
    row = session.fetch_one(
        "SELECT 1 AS present FROM storage_timers WHERE user_id=? LIMIT 1",
        (_timer_owner(payload),),
    )
    return bool(row)


def _timer_active_list_all(session: Session, payload: Mapping[str, Any]) -> Any:
    """Internal scheduler feed across owners; never expose through a route."""
    limit = _integer(payload, "limit", default=200, minimum=1, maximum=2000)
    rows = session.fetch_all(
        "SELECT " + ", ".join(_TIMER_COLUMNS)
        + " FROM storage_timers WHERE status='active' "
        "ORDER BY created_at ASC LIMIT ?",
        (limit,),
    )
    return [_timer_document(row) for row in rows]


def _timer_active_count(session: Session, payload: Mapping[str, Any]) -> int:
    row = session.fetch_one(
        "SELECT COUNT(*) AS n FROM storage_timers "
        "WHERE user_id=? AND status='active'",
        (_timer_owner(payload),),
    )
    return int(row["n"] or 0)


def _timer_create(session: Session, payload: Mapping[str, Any]) -> Any:
    timer_id = _timer_id(payload)
    user_id = _integer(payload, "user_id", minimum=1)
    required = ("conv_id", "check_instruction", "continuation_message")
    for key in required:
        _required_text(payload, key, 20000 if key != "conv_id" else 256)
    # The durable row is the admission authority. Lock by owner so concurrent
    # creates cannot both observe the last free slot; this also prevents a
    # rejected create from leaving an unserviceable active row behind.
    session.lock_key("timer_active_capacity", str(user_id))
    active = session.fetch_one(
        "SELECT COUNT(*) AS n FROM storage_timers "
        "WHERE user_id=? AND status='active'",
        (user_id,),
    )
    capacity = timer_live_capacity()
    if int(active["n"] or 0) >= capacity:
        raise StorageError(
            "database_conflict",
            f"Active timer capacity reached ({capacity})",
        )
    session.lock_key("timer", timer_id)
    if session.fetch_one("SELECT 1 FROM storage_timers WHERE id = ?", (timer_id,)):
        raise StorageError("database_conflict", "Timer already exists")
    config = payload.get("tools_config", {})
    if not isinstance(config, Mapping):
        raise StorageError("database_protocol_error", "Invalid timer tools_config")
    fields = {key: payload.get(key, "") for key in _TIMER_COLUMNS}
    for key in ("poll_count", "promotion_streak", "fallback_streak"):
        fields[key] = int(payload.get(key, 0) or 0)
    fields.update(
        {
            "id": timer_id,
            "user_id": user_id,
            "poll_interval": _integer(payload, "poll_interval", default=60, minimum=10),
            "max_polls": _integer(payload, "max_polls", default=120, minimum=0),
            "tools_config": _dump(dict(config)),
            "status": "active",
        }
    )
    columns = ", ".join(_TIMER_COLUMNS)
    session.execute(
        f"INSERT INTO storage_timers ({columns}) VALUES ({', '.join('?' for _ in _TIMER_COLUMNS)})",
        tuple(fields[column] for column in _TIMER_COLUMNS),
    )
    return {
        "applied": True,
        "timer": _timer_document(
            session.fetch_one(
                "SELECT " + columns + " FROM storage_timers WHERE id = ?", (timer_id,)
            )
        ),
    }


def _timer_cancel(session: Session, payload: Mapping[str, Any]) -> Any:
    timer_id = _timer_id(payload)
    user_id = _timer_owner(payload)
    session.lock_key("timer", timer_id)
    now = payload.get("now") or ""
    changed = session.execute(
        "UPDATE storage_timers SET status='cancelled', cancelled_at=?, updated_at=? "
        "WHERE id=? AND user_id=? AND status='active'",
        (now, now, timer_id, user_id),
    )
    return {"changed": bool(changed)}


def _timer_update(session: Session, payload: Mapping[str, Any]) -> Any:
    timer_id = _timer_id(payload)
    user_id = _timer_owner(payload)
    allowed = {
        "poll_count",
        "last_poll_at",
        "last_poll_decision",
        "last_poll_reason",
        "status",
        "updated_at",
        "triggered_at",
        "execution_task_id",
        "cancelled_at",
        "promotion_streak",
        "fallback_streak",
        "promoted_at",
        "condition_kind",
    }
    values = {key: payload[key] for key in allowed if key in payload}
    if not values:
        raise StorageError("database_protocol_error", "Empty timer update")
    session.lock_key("timer", timer_id)
    assignments = ", ".join(f"{key} = ?" for key in values)
    expected_status = payload.get("expected_status")
    if expected_status is not None:
        expected_status = _required_text(payload, "expected_status", 32)
    where = " WHERE id = ? AND user_id = ?" + (
        " AND status = ?" if expected_status else ""
    )
    params = tuple(values.values()) + (timer_id, user_id)
    if expected_status:
        params += (expected_status,)
    changed = session.execute(f"UPDATE storage_timers SET {assignments}{where}", params)
    return {"changed": bool(changed)}


def _timer_poll_append(session: Session, payload: Mapping[str, Any]) -> Any:
    timer_id = _timer_id(payload)
    user_id = _timer_owner(payload)
    poll_id = str(payload.get("poll_id") or "")[:80]
    session.lock_key("timer", timer_id)
    if session.fetch_one(
        "SELECT 1 AS present FROM storage_timers WHERE id=? AND user_id=?",
        (timer_id, user_id),
    ) is None:
        raise StorageError("database_not_found", "Timer not found")
    session.lock_key("timer_poll_log", "global")
    if poll_id:
        existing = session.fetch_one(
            "SELECT id FROM storage_timer_poll_log WHERE timer_id=? AND poll_id=?",
            (timer_id, poll_id),
        )
        if existing is not None:
            return {"inserted": False, "id": int(existing["id"])}
    row = session.fetch_one(
        "SELECT COALESCE(MAX(id), 0) AS max_id FROM storage_timer_poll_log"
    )
    next_id = int(row["max_id"]) + 1
    session.execute(
        "INSERT INTO storage_timer_poll_log "
        "(id, timer_id, poll_time, decision, reason, check_output, tokens_used, model, poll_id, raw_output, tier, predicate_matched, llm_agreed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            next_id,
            timer_id,
            str(payload.get("poll_time") or ""),
            str(payload.get("decision") or "wait"),
            str(payload.get("reason") or "")[:500],
            str(payload.get("check_output") or "")[:5000],
            int(payload.get("tokens_used") or 0),
            str(payload.get("model") or "")[:120],
            poll_id,
            str(payload.get("raw_output") or "")[:5000],
            str(payload.get("tier") or "llm"),
            int(payload.get("predicate_matched", -1)),
            int(payload.get("llm_agreed", -1)),
        ),
    )
    return {"inserted": True, "id": next_id}


def _timer_poll_commit(session: Session, payload: Mapping[str, Any]) -> Any:
    """Atomically append one poll ledger row and advance watcher progress."""
    result = _timer_poll_append(session, payload)
    if not result["inserted"]:
        return {**result, "advanced": False}
    timer_id = _timer_id(payload)
    user_id = _timer_owner(payload)
    poll_time = _required_text(payload, "poll_time", 64)
    changed = session.execute(
        "UPDATE storage_timers SET poll_count=poll_count+1, "
        "last_poll_at=?, last_poll_decision=?, last_poll_reason=?, updated_at=? "
        "WHERE id=? AND user_id=?",
        (
            poll_time,
            str(payload.get("decision") or "wait"),
            str(payload.get("reason") or "")[:500],
            poll_time,
            timer_id,
            user_id,
        ),
    )
    if not changed:
        raise StorageError("database_not_found", "Timer not found")
    return {**result, "advanced": True}


def _timer_progress(session: Session, payload: Mapping[str, Any]) -> Any:
    """Atomically advance a poll that intentionally has no ledger row."""
    timer_id = _timer_id(payload)
    user_id = _timer_owner(payload)
    now = _required_text(payload, "poll_time", 64)
    session.lock_key("timer", timer_id)
    changed = session.execute(
        "UPDATE storage_timers SET poll_count=poll_count+1, "
        "last_poll_at=?, last_poll_decision=?, last_poll_reason=?, updated_at=? "
        "WHERE id=? AND user_id=? AND status='active'",
        (
            now,
            str(payload.get("decision") or "skipped"),
            str(payload.get("reason") or "")[:500],
            now,
            timer_id,
            user_id,
        ),
    )
    return {"changed": bool(changed)}


def _timer_poll_log(session: Session, payload: Mapping[str, Any]) -> Any:
    timer_id = _timer_id(payload)
    user_id = _timer_owner(payload)
    limit = _integer(payload, "limit", default=30, minimum=1, maximum=200)
    rows = session.fetch_all(
        "SELECT l.id, l.timer_id, l.poll_time, l.decision, l.reason, "
        "l.check_output, l.tokens_used, l.model, l.poll_id, l.raw_output, "
        "l.tier, l.predicate_matched, l.llm_agreed "
        "FROM storage_timer_poll_log AS l JOIN storage_timers AS t "
        "ON t.id=l.timer_id WHERE l.timer_id=? AND t.user_id=? "
        "ORDER BY l.poll_time DESC LIMIT ?",
        (timer_id, user_id, limit),
    )
    return [dict(row) for row in rows]

_SCHEDULER_COLUMNS = (
    "id",
    "user_id",
    "system_key",
    "name",
    "schedule",
    "task_type",
    "command",
    "description",
    "enabled",
    "notify_on_failure",
    "notify_on_success",
    "max_runtime",
    "last_run",
    "last_result",
    "last_status",
    "run_count",
    "fail_count",
    "created_at",
    "updated_at",
    "target_conv_id",
    "source_conv_id",
    "tools_config",
    "poll_count",
    "last_poll_at",
    "last_poll_decision",
    "last_poll_reason",
    "last_execution_at",
    "last_execution_task_id",
    "last_execution_status",
    "execution_count",
    "max_executions",
    "expires_at",
    "condition_kind",
    "condition_command",
    "condition_regex",
    "promotion_streak",
    "fallback_streak",
    "promoted_at",
)


_SCHEDULER_NUMERIC = frozenset(
    {
        "enabled",
        "notify_on_failure",
        "notify_on_success",
        "max_runtime",
        "run_count",
        "fail_count",
        "poll_count",
        "execution_count",
        "max_executions",
        "promotion_streak",
        "fallback_streak",
        "user_id",
    }
)


def _scheduler_task_id(payload: Mapping[str, Any]) -> str:
    if "task_id" in payload:
        return _required_text(payload, "task_id", 128)
    return _required_text(payload, "id", 128)


def _scheduler_owner(payload: Mapping[str, Any]) -> int:
    return _integer(payload, "user_id", minimum=1)


def _scheduler_document(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {column: row[column] for column in _SCHEDULER_COLUMNS}
    result["tools_config"] = _load(result["tools_config"]) or {}
    for key in _SCHEDULER_NUMERIC:
        result[key] = int(result[key] or 0)
    return result


def _scheduler_get(session: Session, payload: Mapping[str, Any]) -> Any:
    row = session.fetch_one(
        "SELECT "
        + ", ".join(_SCHEDULER_COLUMNS)
        + " FROM storage_scheduled_tasks WHERE id = ? AND user_id = ?",
        (_scheduler_task_id(payload), _scheduler_owner(payload)),
    )
    return None if row is None else _scheduler_document(row)


def _scheduler_list(session: Session, payload: Mapping[str, Any]) -> Any:
    limit = _integer(payload, "limit", default=1000, minimum=1, maximum=2000)
    enabled_only = payload.get("enabled_only", False)
    if not isinstance(enabled_only, bool):
        raise StorageError("database_protocol_error", "Invalid enabled_only")
    user_id = _scheduler_owner(payload)
    where = " WHERE user_id=?" + (" AND enabled = 1" if enabled_only else "")
    rows = session.fetch_all(
        "SELECT "
        + ", ".join(_SCHEDULER_COLUMNS)
        + " FROM storage_scheduled_tasks"
        + where
        + " ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )
    return [_scheduler_document(row) for row in rows]


def _scheduler_list_all(session: Session, payload: Mapping[str, Any]) -> Any:
    """Internal worker feed across owners; route adapters must never use it."""
    limit = _integer(payload, "limit", default=1000, minimum=1, maximum=2000)
    enabled_only = payload.get("enabled_only", False)
    if not isinstance(enabled_only, bool):
        raise StorageError("database_protocol_error", "Invalid enabled_only")
    where = " WHERE enabled=1" if enabled_only else ""
    rows = session.fetch_all(
        "SELECT " + ", ".join(_SCHEDULER_COLUMNS)
        + " FROM storage_scheduled_tasks" + where
        + " ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return [_scheduler_document(row) for row in rows]


def _scheduler_create(
    session: Session,
    payload: Mapping[str, Any],
    *,
    system_key: str = "",
) -> Any:
    task_id = _scheduler_task_id(payload)
    user_id = _scheduler_owner(payload)
    for key in ("name", "schedule", "command"):
        _required_text(payload, key, 20000 if key == "command" else 512)
    config = payload.get("tools_config", {})
    if not isinstance(config, Mapping):
        raise StorageError("database_protocol_error", "Invalid scheduler tools_config")
    session.lock_key("scheduler_tasks_capacity", str(user_id))
    count = session.fetch_one(
        "SELECT COUNT(*) AS n FROM storage_scheduled_tasks WHERE user_id=?",
        (user_id,),
    )
    if int(count["n"]) >= MAX_TASKS_PER_OWNER:
        raise StorageError("database_conflict", "Too many scheduled tasks")
    session.lock_key("scheduler_task", task_id)
    if session.fetch_one(
        "SELECT 1 FROM storage_scheduled_tasks WHERE id=?", (task_id,)
    ):
        raise StorageError("database_conflict", "Scheduled task already exists")
    values = {key: payload.get(key, "") for key in _SCHEDULER_COLUMNS}
    values.update(
        {
            "id": task_id,
            "user_id": user_id,
            # Only scheduler.task.ensure may assign this authority-owned key.
            # User-created/imported tasks cannot impersonate a built-in task.
            "system_key": system_key,
            "tools_config": _dump(dict(config)),
            "enabled": int(bool(payload.get("enabled", True))),
            "notify_on_failure": int(bool(payload.get("notify_on_failure", True))),
            "notify_on_success": int(bool(payload.get("notify_on_success", False))),
        }
    )
    for key in _SCHEDULER_NUMERIC - {
        "enabled",
        "notify_on_failure",
        "notify_on_success",
    }:
        values[key] = int(payload.get(key, 0) or 0)
    values["max_runtime"] = int(payload.get("max_runtime", 300) or 300)
    columns = ", ".join(_SCHEDULER_COLUMNS)
    session.execute(
        f"INSERT INTO storage_scheduled_tasks ({columns}) VALUES ({', '.join('?' for _ in _SCHEDULER_COLUMNS)})",
        tuple(values[key] for key in _SCHEDULER_COLUMNS),
    )
    row = session.fetch_one(
        "SELECT " + columns + " FROM storage_scheduled_tasks WHERE id=?", (task_id,)
    )
    return {"applied": True, "task": _scheduler_document(row)}


def _scheduler_ensure(session: Session, payload: Mapping[str, Any]) -> Any:
    """Create or refresh one owner-scoped built-in task by stable identity."""
    user_id = _scheduler_owner(payload)
    system_key = _required_text(payload, "system_key", 128)
    name = _required_text(payload, "name", 512)
    schedule = _required_text(payload, "schedule", 512)
    command = _required_text(payload, "command", 20000)
    task_type = _required_text(payload, "task_type", 128)
    config = payload.get("tools_config", {})
    if not isinstance(config, Mapping):
        raise StorageError("database_protocol_error", "Invalid scheduler tools_config")
    definition = {
        "name": name,
        "schedule": schedule,
        "task_type": task_type,
        "command": command,
        "description": str(payload.get("description") or "")[:20000],
        "notify_on_failure": int(bool(payload.get("notify_on_failure", True))),
        "notify_on_success": int(bool(payload.get("notify_on_success", False))),
        "max_runtime": _integer(
            payload, "max_runtime", default=300, minimum=1, maximum=86400
        ),
        "tools_config": dict(config),
        "condition_kind": str(payload.get("condition_kind") or "llm")[:64],
        "condition_command": str(payload.get("condition_command") or "")[:20000],
        "condition_regex": str(payload.get("condition_regex") or "")[:20000],
    }
    reconcile_enabled = payload.get("reconcile_enabled", False)
    if not isinstance(reconcile_enabled, bool):
        raise StorageError(
            "database_protocol_error", "Invalid reconcile_enabled")
    if reconcile_enabled:
        definition["enabled"] = int(bool(payload.get("enabled", True)))
    session.lock_key("scheduler_system_task", f"{user_id}:{system_key}")
    row = session.fetch_one(
        "SELECT " + ", ".join(_SCHEDULER_COLUMNS)
        + " FROM storage_scheduled_tasks WHERE user_id=? AND system_key=?",
        (user_id, system_key),
    )
    # Authorities created before ``system_key`` matched built-ins by their
    # exact display name. On upgrade, blindly creating the new keyed row leaves
    # both enabled; each due tick then executes the same maintenance side
    # effect twice. Reconcile inside this locked transaction. Prefer the oldest
    # legacy row so its durable identity/history and any external links remain
    # stable, retire the short-lived duplicate, then attach the machine key.
    legacy_rows = session.fetch_all(
        "SELECT " + ", ".join(_SCHEDULER_COLUMNS)
        + " FROM storage_scheduled_tasks "
        "WHERE user_id=? AND system_key='' AND name=? AND task_type=? "
        "ORDER BY created_at, id",
        (user_id, name, task_type),
    )
    if legacy_rows:
        keeper = legacy_rows[0]
        duplicates = list(legacy_rows[1:])
        if row is not None:
            duplicates.append(row)
        for duplicate in duplicates:
            duplicate_id = str(duplicate["id"])
            session.execute(
                "DELETE FROM storage_proactive_poll_log WHERE task_id=?",
                (duplicate_id,),
            )
            session.execute(
                "DELETE FROM storage_scheduled_tasks WHERE id=? AND user_id=?",
                (duplicate_id, user_id),
            )
        session.execute(
            "UPDATE storage_scheduled_tasks SET system_key=?, updated_at=? "
            "WHERE id=? AND user_id=? AND system_key=''",
            (
                system_key,
                str(payload.get("updated_at") or ""),
                str(keeper["id"]),
                user_id,
            ),
        )
        row = session.fetch_one(
            "SELECT " + ", ".join(_SCHEDULER_COLUMNS)
            + " FROM storage_scheduled_tasks WHERE id=? AND user_id=?",
            (str(keeper["id"]), user_id),
        )
    if row is not None:
        current = _scheduler_document(row)
        changed = {
            key: value for key, value in definition.items()
            if current[key] != value
        }
        if changed:
            if "tools_config" in changed:
                changed["tools_config"] = _dump(changed["tools_config"])
            changed["updated_at"] = str(payload.get("updated_at") or "")
            assignments = ", ".join(f"{key}=?" for key in changed)
            session.execute(
                f"UPDATE storage_scheduled_tasks SET {assignments} "
                "WHERE id=? AND user_id=? AND system_key=?",
                tuple(changed.values()) + (current["id"], user_id, system_key),
            )
            row = session.fetch_one(
                "SELECT " + ", ".join(_SCHEDULER_COLUMNS)
                + " FROM storage_scheduled_tasks WHERE id=? AND user_id=?",
                (current["id"], user_id),
            )
        return {
            "created": False,
            "updated": bool(changed),
            "task": _scheduler_document(row),
        }
    created = _scheduler_create(session, payload, system_key=system_key)
    return {"created": True, "task": created["task"]}


def _scheduler_update(session: Session, payload: Mapping[str, Any]) -> Any:
    task_id = _scheduler_task_id(payload)
    user_id = _scheduler_owner(payload)
    allowed = set(_SCHEDULER_COLUMNS) - {
        "id", "user_id", "system_key", "created_at", "tools_config"
    }
    updates = {key: payload[key] for key in allowed if key in payload}
    if "tools_config" in payload:
        if not isinstance(payload["tools_config"], Mapping):
            raise StorageError(
                "database_protocol_error", "Invalid scheduler tools_config"
            )
        updates["tools_config"] = _dump(dict(payload["tools_config"]))
    if not updates:
        raise StorageError("database_protocol_error", "Empty scheduler update")
    session.lock_key("scheduler_task", task_id)
    if "updated_at" not in updates:
        updates["updated_at"] = str(payload.get("now") or "")
    for key in _SCHEDULER_NUMERIC:
        if key in updates:
            updates[key] = int(updates[key])
    assignments = ", ".join(f"{key}=?" for key in updates)
    changed = session.execute(
        f"UPDATE storage_scheduled_tasks SET {assignments} "
        "WHERE id=? AND user_id=?",
        tuple(updates.values()) + (task_id, user_id),
    )
    return {"changed": bool(changed)}


def _scheduler_delete(session: Session, payload: Mapping[str, Any]) -> Any:
    task_id = _scheduler_task_id(payload)
    user_id = _scheduler_owner(payload)
    session.lock_key("scheduler_task", task_id)
    present = session.fetch_one(
        "SELECT 1 AS present FROM storage_scheduled_tasks "
        "WHERE id=? AND user_id=?",
        (task_id, user_id),
    )
    if present is None:
        return {"deleted": False}
    session.execute(
        "DELETE FROM storage_proactive_poll_log WHERE task_id=?", (task_id,)
    )
    session.execute(
        "DELETE FROM storage_scheduled_tasks WHERE id=? AND user_id=?",
        (task_id, user_id),
    )
    return {"deleted": True}


def _scheduler_record_result(session: Session, payload: Mapping[str, Any]) -> Any:
    task_id = _scheduler_task_id(payload)
    user_id = _scheduler_owner(payload)
    session.lock_key("scheduler_task", task_id)
    now = _required_text(payload, "now", 64)
    success = bool(payload.get("success"))
    changed = session.execute(
        "UPDATE storage_scheduled_tasks SET last_run=?, last_result=?, last_status=?, "
        "run_count=run_count+1, fail_count=fail_count+?, updated_at=? "
        "WHERE id=? AND user_id=?",
        (
            now,
            str(payload.get("result") or "")[:10000],
            "ok" if success else "failed",
            0 if success else 1,
            now,
            task_id,
            user_id,
        ),
    )
    return {"changed": bool(changed)}


def _scheduler_claim_due(session: Session, payload: Mapping[str, Any]) -> Any:
    """Atomically claim one due execution across scheduler replicas."""
    task_id = _scheduler_task_id(payload)
    user_id = _scheduler_owner(payload)
    lane = _required_text(payload, "lane", 16)
    if lane not in {"run", "poll"}:
        raise StorageError("database_protocol_error", "Invalid scheduler claim lane")
    now_text = _required_text(payload, "now", 64)
    try:
        now_value = datetime.fromisoformat(now_text)
    except ValueError as exc:
        raise StorageError(
            "database_protocol_error", "Invalid scheduler claim timestamp"
        ) from exc
    minimum_interval = _integer(
        payload, "minimum_interval_seconds", default=DUE_CLAIM_INTERVAL_SECONDS,
        minimum=1, maximum=3600
    )
    last_column = "last_poll_at" if lane == "poll" else "last_run"
    session.lock_key("scheduler_task", task_id)
    row = session.fetch_one(
        f"SELECT enabled, {last_column} AS last_claim "
        "FROM storage_scheduled_tasks WHERE id=? AND user_id=?",
        (task_id, user_id),
    )
    if row is None or not bool(row["enabled"]):
        return {"claimed": False}
    last_claim = str(row["last_claim"] or "")
    if last_claim:
        try:
            elapsed = (now_value - datetime.fromisoformat(last_claim)).total_seconds()
        except ValueError as exc:
            raise StorageError(
                "database_integrity", "Stored scheduler claim timestamp is invalid"
            ) from exc
        if elapsed < minimum_interval:
            return {"claimed": False}
    changed = session.execute(
        f"UPDATE storage_scheduled_tasks SET {last_column}=?, updated_at=? "
        "WHERE id=? AND user_id=? AND enabled=1",
        (now_text, now_text, task_id, user_id),
    )
    return {"claimed": bool(changed)}


def _scheduler_poll_append(session: Session, payload: Mapping[str, Any]) -> Any:
    task_id = _scheduler_task_id(payload)
    user_id = _scheduler_owner(payload)
    if session.fetch_one(
        "SELECT 1 AS present FROM storage_scheduled_tasks "
        "WHERE id=? AND user_id=?",
        (task_id, user_id),
    ) is None:
        raise StorageError("database_not_found", "Scheduled task not found")
    session.lock_key("scheduler_poll_log", "global")
    row = session.fetch_one(
        "SELECT COALESCE(MAX(id),0) AS max_id FROM storage_proactive_poll_log"
    )
    ident = int(row["max_id"]) + 1
    session.execute(
        "INSERT INTO storage_proactive_poll_log "
        "(id, task_id, poll_time, decision, reason, status_snapshot, model, tokens_used, execution_task_id, tier, predicate_matched, llm_agreed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ident,
            task_id,
            str(payload.get("poll_time") or ""),
            str(payload.get("decision") or "skip"),
            str(payload.get("reason") or "")[:500],
            str(payload.get("status_snapshot") or "")[:5000],
            str(payload.get("model") or "")[:120],
            int(payload.get("tokens_used") or 0),
            str(payload.get("execution_task_id") or ""),
            str(payload.get("tier") or "llm"),
            int(payload.get("predicate_matched", -1)),
            int(payload.get("llm_agreed", -1)),
        ),
    )
    return {"inserted": True, "id": ident}


def _scheduler_poll_log(session: Session, payload: Mapping[str, Any]) -> Any:
    task_id = _scheduler_task_id(payload)
    user_id = _scheduler_owner(payload)
    limit = _integer(payload, "limit", default=30, minimum=1, maximum=200)
    rows = session.fetch_all(
        "SELECT l.id, l.task_id, l.poll_time, l.decision, l.reason, "
        "l.status_snapshot, l.model, l.tokens_used, l.execution_task_id, "
        "l.tier, l.predicate_matched, l.llm_agreed "
        "FROM storage_proactive_poll_log AS l "
        "JOIN storage_scheduled_tasks AS t ON t.id=l.task_id "
        "WHERE l.task_id=? AND t.user_id=? ORDER BY l.poll_time DESC LIMIT ?",
        (task_id, user_id, limit),
    )
    return [dict(row) for row in rows]
