"""Semantic Sidecar storage for optimizer proposals and action audit rows.

The application sees the established dict-shaped repository API. Database
connections, SQL, retries, and complete transaction boundaries live only in
the Storage Sidecar.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
import uuid

from lib.ids import short_id
from lib.log import get_logger

logger = get_logger(__name__)


# ── helpers ──

def _storage(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


def _command(operation: str, payload: dict) -> dict:
    return _storage(write=True).command(
        operation, payload, f'{operation}:{uuid.uuid4().hex}')


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
    _command('optimizer.proposal.create', {
        'proposal_id': prop_id, 'created_at': now, 'title': title[:500],
        'rationale': rationale[:4000], 'action_type': action_type,
        'action_args': _as_json(action_args or {}), 'severity': severity,
        'confidence': float(confidence), 'evidence': _as_json(evidence or []),
        'status': status, 'status_reason': status_reason[:500],
    })
    logger.info('[Optimizer.storage] created proposal %s action=%s status=%s',
                prop_id, action_type, status)
    return prop_id


def update_proposal_status(proposal_id: str, status: str, reason: str = '') -> None:
    _command('optimizer.proposal.update', {
        'proposal_id': proposal_id, 'status': status, 'reason': reason[:500],
    })
    logger.info('[Optimizer.storage] proposal %s → status=%s reason=%.120s',
                proposal_id, status, reason)


def get_proposal(proposal_id: str) -> dict | None:
    return _storage().query(
        'optimizer.proposal.get', {'proposal_id': proposal_id})


def list_proposals(*, status: str | None = None, limit: int = 50) -> list[dict]:
    return _storage().query('optimizer.proposal.list', {
        'status': status or '', 'limit': max(1, min(500, int(limit))),
    })


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
    _command('optimizer.action.record', {
        'log_id': log_id, 'proposal_id': proposal_id,
        'applied_at': now_dt.isoformat(), 'expires_at': expires_dt.isoformat(),
        'pre_metric': _as_json(pre_metric or {}),
    })
    logger.info('[Optimizer.storage] applied proposal=%s ttl_days=%d expires=%s',
                proposal_id, ttl_days, expires_dt.isoformat())
    return log_id


def record_outcome_metric(log_id: str, outcome_metric: dict) -> None:
    _command('optimizer.action.outcome', {
        'log_id': log_id, 'outcome_metric': _as_json(outcome_metric),
        'recorded_at': datetime.now().isoformat(),
    })
    logger.info('[Optimizer.storage] recorded outcome_metric for action_log=%s', log_id)


def mark_reverted(log_id: str, reason: str) -> None:
    _command('optimizer.action.revert', {
        'log_id': log_id, 'reverted_at': datetime.now().isoformat(),
        'reason': reason[:500],
    })
    logger.info('[Optimizer.storage] action_log=%s reverted: %.120s', log_id, reason)


def list_applied_actions(*, include_reverted: bool = False, limit: int = 50) -> list[dict]:
    """Return applied action rows joined with their proposal metadata."""
    return _storage().query('optimizer.action.list', {
        'include_reverted': bool(include_reverted),
        'limit': max(1, min(500, int(limit))),
    })


def list_expired_applied_actions() -> list[dict]:
    """Return rows whose expires_at is in the past AND reverted_at is empty
    AND proposal.status is still 'applied'."""
    return _storage().query(
        'optimizer.action.expired', {'now_iso': datetime.now().isoformat()})


def get_action_log_for_proposal(proposal_id: str) -> dict | None:
    return _storage().query(
        'optimizer.action.for_proposal', {'proposal_id': proposal_id})
