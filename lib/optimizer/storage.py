"""lib/optimizer/storage.py — CRUD for optimizer_proposals / optimizer_action_log.

Both tables live in the SYSTEM domain (see ``lib/database/_schema_*.py``).
All writes use ``db_execute_with_retry`` to survive transient PG contention.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from lib.database import DOMAIN_SYSTEM, db_execute_with_retry, get_thread_db
from lib.ids import short_id
from lib.log import get_logger

logger = get_logger(__name__)


# ── helpers ──

def _db():
    return get_thread_db(DOMAIN_SYSTEM)


def _as_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception as e:
        logger.warning('[Optimizer.storage] JSON encode failed, falling back to str: %s', e)
        return str(value)


# ── proposals ──

def create_proposal(
    *,
    title: str,
    rationale: str,
    action_type: str,
    action_args: dict,
    severity: str = 'low',
    confidence: float = 0.5,
    evidence: list | dict | None = None,
    status: str = 'pending_review',
    status_reason: str = '',
) -> str:
    """Insert a new proposal row and return its id."""
    prop_id = short_id('opt_', 12)
    now = datetime.now().isoformat()
    db = _db()
    db_execute_with_retry(db, """
        INSERT INTO optimizer_proposals
        (id, created_at, title, rationale, action_type, action_args,
         severity, confidence, evidence, status, status_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        prop_id, now, title[:500], rationale[:4000], action_type,
        _as_json(action_args or {}),
        severity, float(confidence),
        _as_json(evidence or []),
        status, status_reason[:500],
    ])
    logger.info('[Optimizer.storage] created proposal %s action=%s status=%s',
                prop_id, action_type, status)
    return prop_id


def update_proposal_status(proposal_id: str, status: str, reason: str = '') -> None:
    db = _db()
    db_execute_with_retry(db,
        'UPDATE optimizer_proposals SET status=?, status_reason=? WHERE id=?',
        [status, reason[:500], proposal_id])
    logger.info('[Optimizer.storage] proposal %s → status=%s reason=%.120s',
                proposal_id, status, reason)


def get_proposal(proposal_id: str) -> dict | None:
    db = _db()
    row = db.execute('SELECT * FROM optimizer_proposals WHERE id=?', [proposal_id]).fetchone()
    return dict(row) if row else None


def list_proposals(*, status: str | None = None, limit: int = 50) -> list[dict]:
    db = _db()
    if status:
        rows = db.execute(
            'SELECT * FROM optimizer_proposals WHERE status=? '
            'ORDER BY created_at DESC LIMIT ?',
            [status, int(limit)]).fetchall()
    else:
        rows = db.execute(
            'SELECT * FROM optimizer_proposals ORDER BY created_at DESC LIMIT ?',
            [int(limit)]).fetchall()
    return [dict(r) for r in rows]


# ── action log ──

def record_applied(
    *,
    proposal_id: str,
    ttl_days: int,
    pre_metric: dict | None = None,
) -> str:
    """Record that an action was applied — returns action_log id."""
    log_id = short_id('act_', 12)
    now_dt = datetime.now()
    expires_dt = now_dt + timedelta(days=max(1, int(ttl_days)))
    db = _db()
    db_execute_with_retry(db, """
        INSERT INTO optimizer_action_log
        (id, proposal_id, applied_at, expires_at, pre_metric)
        VALUES (?, ?, ?, ?, ?)
    """, [
        log_id, proposal_id, now_dt.isoformat(), expires_dt.isoformat(),
        _as_json(pre_metric or {}),
    ])
    logger.info('[Optimizer.storage] applied proposal=%s ttl_days=%d expires=%s',
                proposal_id, ttl_days, expires_dt.isoformat())
    return log_id


def record_outcome_metric(log_id: str, outcome_metric: dict) -> None:
    db = _db()
    db_execute_with_retry(db, """
        UPDATE optimizer_action_log
        SET outcome_metric=?, outcome_recorded_at=?
        WHERE id=?
    """, [_as_json(outcome_metric), datetime.now().isoformat(), log_id])
    logger.info('[Optimizer.storage] recorded outcome_metric for action_log=%s', log_id)


def mark_reverted(log_id: str, reason: str) -> None:
    db = _db()
    db_execute_with_retry(db, """
        UPDATE optimizer_action_log
        SET reverted_at=?, revert_reason=?
        WHERE id=?
    """, [datetime.now().isoformat(), reason[:500], log_id])
    logger.info('[Optimizer.storage] action_log=%s reverted: %.120s', log_id, reason)


def list_applied_actions(*, include_reverted: bool = False, limit: int = 50) -> list[dict]:
    """Return applied action rows joined with their proposal metadata."""
    db = _db()
    sql = """
        SELECT a.*, p.title AS p_title, p.action_type AS p_action_type,
               p.action_args AS p_action_args, p.status AS p_status
        FROM optimizer_action_log a
        JOIN optimizer_proposals p ON p.id = a.proposal_id
    """
    if not include_reverted:
        sql += " WHERE a.reverted_at = '' "
    sql += ' ORDER BY a.applied_at DESC LIMIT ?'
    rows = db.execute(sql, [int(limit)]).fetchall()
    return [dict(r) for r in rows]


def list_expired_applied_actions() -> list[dict]:
    """Return rows whose expires_at is in the past AND reverted_at is empty
    AND proposal.status is still 'applied'."""
    db = _db()
    now_iso = datetime.now().isoformat()
    rows = db.execute("""
        SELECT a.*, p.action_type AS p_action_type,
               p.action_args AS p_action_args, p.status AS p_status
        FROM optimizer_action_log a
        JOIN optimizer_proposals p ON p.id = a.proposal_id
        WHERE a.reverted_at = '' AND p.status = 'applied'
          AND a.expires_at != '' AND a.expires_at <= ?
    """, [now_iso]).fetchall()
    return [dict(r) for r in rows]


def get_action_log_for_proposal(proposal_id: str) -> dict | None:
    db = _db()
    row = db.execute(
        'SELECT * FROM optimizer_action_log WHERE proposal_id=? '
        'ORDER BY applied_at DESC LIMIT 1',
        [proposal_id]).fetchone()
    return dict(row) if row else None
