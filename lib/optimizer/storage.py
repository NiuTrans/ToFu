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
from lib.identity import require_user_id
from lib.log import get_logger

logger = get_logger(__name__)


# ── helpers ──

def _storage(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


def _owned_payload(payload: dict, *, owner_user_id: int) -> dict:
    return {
        'user_id': require_user_id(
            owner_user_id, context='optimizer storage operation'),
        **payload,
    }


def _command(operation: str, payload: dict, *, owner_user_id: int) -> dict:
    return _storage(write=True).command(
        operation,
        _owned_payload(payload, owner_user_id=owner_user_id),
        f'{operation}:{uuid.uuid4().hex}',
    )


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
    owner_user_id: int,
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
    }, owner_user_id=owner_user_id)
    logger.info('[Optimizer.storage] created proposal %s action=%s status=%s',
                prop_id, action_type, status)
    return prop_id


def update_proposal_status(
    proposal_id: str,
    status: str,
    reason: str = '',
    *,
    owner_user_id: int,
) -> None:
    _command('optimizer.proposal.update', {
        'proposal_id': proposal_id, 'status': status, 'reason': reason[:500],
    }, owner_user_id=owner_user_id)
    logger.info('[Optimizer.storage] proposal %s → status=%s reason=%.120s',
                proposal_id, status, reason)


def get_proposal(proposal_id: str, *, owner_user_id: int) -> dict | None:
    return _storage().query(
        'optimizer.proposal.get',
        _owned_payload(
            {'proposal_id': proposal_id}, owner_user_id=owner_user_id),
    )


def list_proposals(
    *,
    owner_user_id: int,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    return _storage().query(
        'optimizer.proposal.list',
        _owned_payload(
            {'status': status or '', 'limit': max(1, min(500, int(limit)))},
            owner_user_id=owner_user_id,
        ),
    )


# ── action log ──

def record_applied(
    *,
    owner_user_id: int,
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
    }, owner_user_id=owner_user_id)
    logger.info('[Optimizer.storage] applied proposal=%s ttl_days=%d expires=%s',
                proposal_id, ttl_days, expires_dt.isoformat())
    return log_id


def record_outcome_metric(
    log_id: str,
    outcome_metric: dict,
    *,
    owner_user_id: int,
) -> None:
    _command('optimizer.action.outcome', {
        'log_id': log_id, 'outcome_metric': _as_json(outcome_metric),
        'recorded_at': datetime.now().isoformat(),
    }, owner_user_id=owner_user_id)
    logger.info('[Optimizer.storage] recorded outcome_metric for action_log=%s', log_id)


def mark_reverted(
    log_id: str,
    reason: str,
    *,
    owner_user_id: int,
) -> None:
    _command('optimizer.action.revert', {
        'log_id': log_id, 'reverted_at': datetime.now().isoformat(),
        'reason': reason[:500],
    }, owner_user_id=owner_user_id)
    logger.info('[Optimizer.storage] action_log=%s reverted: %.120s', log_id, reason)


def list_applied_actions(
    *,
    owner_user_id: int,
    include_reverted: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Return applied action rows joined with their proposal metadata."""
    return _storage().query(
        'optimizer.action.list',
        _owned_payload({
            'include_reverted': bool(include_reverted),
            'limit': max(1, min(500, int(limit))),
        }, owner_user_id=owner_user_id),
    )


def list_expired_applied_actions(*, owner_user_id: int) -> list[dict]:
    """Return rows whose expires_at is in the past AND reverted_at is empty
    AND proposal.status is still 'applied'."""
    return _storage().query(
        'optimizer.action.expired',
        _owned_payload(
            {'now_iso': datetime.now().isoformat()},
            owner_user_id=owner_user_id,
        ),
    )


def get_action_log_for_proposal(
    proposal_id: str,
    *,
    owner_user_id: int,
) -> dict | None:
    return _storage().query(
        'optimizer.action.for_proposal',
        _owned_payload(
            {'proposal_id': proposal_id}, owner_user_id=owner_user_id),
    )
