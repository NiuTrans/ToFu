"""Optimizer proposal/action and log-aggregate operation handlers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session


logger = get_logger(__name__)


from lib.storage_sidecar.operations_pkg._common import (
    _integer,
    _number,
    _required_text,
)
from lib.storage_sidecar.operations_pkg._runs import (
    _optional_text,
)

_OPT_PROPOSAL_COLUMNS = (
    "user_id, id, created_at, title, rationale, action_type, action_args, severity, "
    "confidence, evidence, status, status_reason"
)


_OPT_ACTION_COLUMNS = (
    "user_id, id, proposal_id, applied_at, expires_at, pre_metric, outcome_metric, "
    "outcome_recorded_at, reverted_at, revert_reason"
)


def _optimizer_proposal_create(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    proposal_id = _required_text(payload, "proposal_id", 128)
    session.execute(
        "INSERT INTO optimizer_proposals("
        + _OPT_PROPOSAL_COLUMNS
        + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            proposal_id,
            _required_text(payload, "created_at", 64),
            _optional_text(payload, "title", maximum=500, scope="optimizer"),
            _optional_text(payload, "rationale", maximum=4000, scope="optimizer"),
            _required_text(payload, "action_type", 256),
            _required_text(payload, "action_args", 2_000_000),
            _optional_text(
                payload, "severity", default="low", maximum=64, scope="optimizer"
            ),
            _number(payload, "confidence", minimum=0, maximum=1),
            _required_text(payload, "evidence", 2_000_000),
            _optional_text(
                payload,
                "status",
                default="pending_review",
                maximum=64,
                scope="optimizer",
            ),
            _optional_text(payload, "status_reason", maximum=500, scope="optimizer"),
        ),
    )
    return {"proposal_id": proposal_id}


def _optimizer_proposal_update(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    count = session.execute(
        "UPDATE optimizer_proposals SET status = ?, status_reason = ? "
        "WHERE user_id = ? AND id = ?",
        (
            _required_text(payload, "status", 64),
            _optional_text(payload, "reason", maximum=500, scope="optimizer"),
            user_id,
            _required_text(payload, "proposal_id", 128),
        ),
    )
    return {"changed": bool(count)}


def _optimizer_proposal_get(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    row = session.fetch_one(
        "SELECT " + _OPT_PROPOSAL_COLUMNS + " FROM optimizer_proposals "
        "WHERE user_id = ? AND id = ?",
        (user_id, _required_text(payload, "proposal_id", 128)),
    )
    return row


def _optimizer_proposal_list(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    status = _optional_text(payload, "status", maximum=64, scope="optimizer")
    limit = _integer(payload, "limit", default=50, minimum=1, maximum=500)
    return session.fetch_all(
        "SELECT " + _OPT_PROPOSAL_COLUMNS + " FROM optimizer_proposals "
        "WHERE user_id = ? AND (? = ? OR status = ?) "
        "ORDER BY created_at DESC LIMIT ?",
        (user_id, status, "", status, limit),
    )


def _optimizer_action_record(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    log_id = _required_text(payload, "log_id", 128)
    proposal_id = _required_text(payload, "proposal_id", 128)
    if (
        session.fetch_one(
            "SELECT id FROM optimizer_proposals WHERE user_id = ? AND id = ?",
            (user_id, proposal_id),
        )
        is None
    ):
        raise StorageError("database_integrity", "Optimizer proposal does not exist")
    session.execute(
        "INSERT INTO optimizer_action_log("
        "user_id, id, proposal_id, applied_at, expires_at, pre_metric) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            user_id,
            log_id,
            proposal_id,
            _required_text(payload, "applied_at", 64),
            _required_text(payload, "expires_at", 64),
            _required_text(payload, "pre_metric", 2_000_000),
        ),
    )
    return {"log_id": log_id}


def _optimizer_action_outcome(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    count = session.execute(
        "UPDATE optimizer_action_log SET outcome_metric = ?, "
        "outcome_recorded_at = ? WHERE user_id = ? AND id = ?",
        (
            _required_text(payload, "outcome_metric", 2_000_000),
            _required_text(payload, "recorded_at", 64),
            user_id,
            _required_text(payload, "log_id", 128),
        ),
    )
    return {"changed": bool(count)}


def _optimizer_action_revert(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    count = session.execute(
        "UPDATE optimizer_action_log SET reverted_at = ?, revert_reason = ? "
        "WHERE user_id = ? AND id = ?",
        (
            _required_text(payload, "reverted_at", 64),
            _optional_text(payload, "reason", maximum=500, scope="optimizer"),
            user_id,
            _required_text(payload, "log_id", 128),
        ),
    )
    return {"changed": bool(count)}


def _optimizer_action_list(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    include_reverted = payload.get("include_reverted", False)
    if not isinstance(include_reverted, bool):
        raise StorageError(
            "database_protocol_error", "Invalid include_reverted in storage request"
        )
    limit = _integer(payload, "limit", default=50, minimum=1, maximum=500)
    return session.fetch_all(
        "SELECT a."
        + _OPT_ACTION_COLUMNS.replace(", ", ", a.")
        + ", p.title AS p_title, p.action_type AS p_action_type, "
        "p.action_args AS p_action_args, p.status AS p_status "
        "FROM optimizer_action_log a JOIN optimizer_proposals p "
        "ON p.user_id = a.user_id AND p.id = a.proposal_id "
        "WHERE a.user_id = ? AND (? = 1 OR a.reverted_at = ?) "
        "ORDER BY a.applied_at DESC LIMIT ?",
        (user_id, int(include_reverted), "", limit),
    )


def _optimizer_action_expired(session: Session, payload: Mapping[str, Any]) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    now_iso = _required_text(payload, "now_iso", 64)
    return session.fetch_all(
        "SELECT a."
        + _OPT_ACTION_COLUMNS.replace(", ", ", a.")
        + ", p.action_type AS p_action_type, p.action_args AS p_action_args, "
        "p.status AS p_status FROM optimizer_action_log a "
        "JOIN optimizer_proposals p "
        "ON p.user_id = a.user_id AND p.id = a.proposal_id "
        "WHERE a.user_id = ? AND a.reverted_at = ? AND p.status = ? "
        "AND a.expires_at != ? AND a.expires_at <= ?",
        (user_id, "", "applied", "", now_iso),
    )


def _optimizer_action_for_proposal(
    session: Session,
    payload: Mapping[str, Any],
) -> Any:
    user_id = _integer(payload, "user_id", minimum=1)
    return session.fetch_one(
        "SELECT " + _OPT_ACTION_COLUMNS + " FROM optimizer_action_log "
        "WHERE user_id = ? AND proposal_id = ? "
        "ORDER BY applied_at DESC LIMIT 1",
        (user_id, _required_text(payload, "proposal_id", 128)),
    )


def _log_aggregate_flush(session: Session, payload: Mapping[str, Any]) -> Any:
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) > 500:
        raise StorageError("database_protocol_error", "Invalid log aggregate batch")
    for item in rows:
        if not isinstance(item, Mapping):
            raise StorageError("database_protocol_error", "Invalid log aggregate row")
        session.execute(
            "INSERT INTO log_aggregates("
            "fingerprint, level, logger, template, sample, count, "
            "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(fingerprint) DO UPDATE SET "
            "count = log_aggregates.count + excluded.count, "
            "last_seen = excluded.last_seen, sample = excluded.sample",
            (
                _required_text(item, "fingerprint", 64),
                _required_text(item, "level", 32),
                _optional_text(item, "logger", maximum=256, scope="log aggregate"),
                _optional_text(item, "template", maximum=200, scope="log aggregate"),
                _optional_text(item, "sample", maximum=2000, scope="log aggregate"),
                _integer(item, "count", minimum=1, maximum=1_000_000_000),
                _integer(item, "first_seen", minimum=0),
                _integer(item, "last_seen", minimum=0),
            ),
        )
    swept = 0
    cutoff = payload.get("cutoff_ms")
    if cutoff is not None:
        if not isinstance(cutoff, int) or isinstance(cutoff, bool) or cutoff < 0:
            raise StorageError(
                "database_protocol_error", "Invalid log aggregate cutoff"
            )
        # One hourly sweep must never turn an observability table into an
        # unbounded writer transaction.  The last_seen index makes candidate
        # discovery compact; the subquery keeps deletion portable to both
        # SQLite and PostgreSQL while capping WAL/lock time per invocation.
        swept = session.execute(
            "DELETE FROM log_aggregates WHERE fingerprint IN ("
            "SELECT fingerprint FROM log_aggregates "
            "WHERE last_seen < ? ORDER BY last_seen LIMIT 500)",
            (cutoff,),
        )
    return {"flushed": len(rows), "swept": swept}


def _log_aggregate_query(session: Session, payload: Mapping[str, Any]) -> Any:
    level = _optional_text(payload, "level", maximum=32, scope="log aggregate")
    sort = _optional_text(
        payload, "sort", default="count", maximum=32, scope="log aggregate"
    )
    orders = {
        "count": "count DESC, last_seen DESC",
        "last_seen": "last_seen DESC",
        "level": "level ASC, count DESC",
    }
    if sort not in orders:
        raise StorageError("database_protocol_error", "Invalid log aggregate sort")
    limit = _integer(payload, "limit", default=100, minimum=1, maximum=500)
    q = _optional_text(payload, "q", maximum=200, scope="log aggregate")
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    where = "WHERE (? = ? OR level = ?) AND (? = ? OR template LIKE ? ESCAPE '\\') "
    params = (level, "", level, q, "", pattern)
    rows = session.fetch_all(
        "SELECT fingerprint, level, logger, template, sample, count, "
        "first_seen, last_seen FROM log_aggregates "
        + where
        + "ORDER BY "
        + orders[sort]
        + " LIMIT ?",
        params + (limit,),
    )
    totals = session.fetch_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(count), 0) AS events "
        "FROM log_aggregates " + where,
        params,
    )
    return {
        "items": rows,
        "total_rows": int(totals["n"] if totals else 0),
        "total_events": int(totals["events"] if totals else 0),
    }
